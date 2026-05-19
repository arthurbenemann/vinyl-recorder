"""Album-folder layer: every album in `in-progress/` is a directory holding
N side FLACs (untagged, original filenames) plus an `album.json` manifest.
This module owns manifest read/write, side-list reconciliation (drop-in
files auto-append, removed files get pruned), and the wave-editor's
per-side peaks cache. Routes call into it instead of touching the
filesystem directly.

The manifest schema (v2):

    {
      "schema_version": 2,
      "tags":           {artist, album, year, genre, label,
                         catalog_number, country, composer, conductor,
                         musicbrainz_albumid, discogs_release_id},
      "sides":          [filename, ...]  # ORDER matters
      "cover":          "cover.jpg" | null,
      "plan":           {tracks, normalize, target_peak_db,
                         measured_peak_db, bit_depth, sample_rate,
                         output_format} | null,
      "music_relpath":  "Artist/Album (Year)" | null
    }

Every helper in this file works on `album_id` (the dir basename — an
opaque hex slug) rather than a path. Validation guards stop traversal
attempts at the boundary.
"""
import json
import re
import secrets
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from services.ffmpeg import flac_duration_seconds, flac_format, read_tags, safe_path_component
from state import IN_PROGRESS_DIR, MUSIC_DIR, RAW_DIR

# Album dir basenames are restricted to lowercase hex / dashes / underscores
# so they can be used verbatim in URL paths without encoding. The default
# `secrets.token_hex(4)` slug always satisfies this; users dropping a folder
# in by hand can pick any name that does too.
ALBUM_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
SCHEMA_VERSION = 2

# Canonical manifest tag keys (matches the schema docstring above). The
# write path filters `manifest["tags"]` against this set so vestigial
# fields on TagEdit — notably `tracks`, which the apply flow forwards as
# the release tracklist for the editor's UI — never land in album.json
# alongside the wave-editor's own `plan.tracks`. Without the filter, an
# Apply-tags-then-edit-cuts sequence would produce TWO track listings in
# the manifest (`tags.tracks` strings AND `plan.tracks` cut objects).
_TAG_KEYS: frozenset[str] = frozenset((
    "artist", "album", "year", "genre", "label",
    "catalog_number", "country", "composer", "conductor",
    "musicbrainz_albumid", "discogs_release_id",
))


def _stub_manifest() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "tags":           {},
        "sides":          [],
        "cover":          None,
        "plan":           None,
        # Monotonic counter bumped on every plan-update POST. Lets two
        # tabs editing the same album detect a stale write via the
        # `expected_version` field on PlanUpdateRequest. Existing albums
        # without the field read as 0 (see `read_manifest`'s setdefault
        # backfill).
        "plan_version":   0,
        "music_relpath":  None,
        "sources_purged": False,
    }


def new_album_id() -> str:
    """Generate a fresh slug. Caller is responsible for collision retry —
    `combine_album` does this by re-rolling if the dir already exists."""
    return secrets.token_hex(4)


def is_valid_album_id(album_id: str) -> bool:
    return bool(album_id) and bool(ALBUM_ID_PATTERN.fullmatch(album_id))


def album_dir(album_id: str) -> Path:
    """Return `in-progress/{album_id}/`, validating the slug. Does NOT check
    existence — callers that need an existing album should also stat."""
    if not is_valid_album_id(album_id):
        raise ValueError(f"invalid album id: {album_id!r}")
    return IN_PROGRESS_DIR / album_id


def music_dir_for(tags: dict) -> tuple[Path, str]:
    """Compute the Jellyfin-shaped output dir for an album's manifest tags.
    Returns `(absolute_dir, relpath_under_MUSIC_DIR)`. Falls back to "Unknown
    Artist" / "Unknown Album" when tags are missing; the year is omitted from
    the album folder name when DATE is empty."""
    artist = (tags.get("artist") or "").strip() or "Unknown Artist"
    album_ = (tags.get("album")  or "").strip() or "Unknown Album"
    year   = (tags.get("year")   or "").strip()
    album_dirname = (
        f"{safe_path_component(album_)} ({year})"
        if year else safe_path_component(album_)
    )
    relpath = f"{safe_path_component(artist)}/{album_dirname}"
    return MUSIC_DIR / relpath, relpath


def manifest_path(album_id: str) -> Path:
    return album_dir(album_id) / "album.json"


def read_manifest(album_id: str) -> dict:
    """Read the manifest, returning a stub when the file is missing or
    malformed. Always returns a dict with the v2 keys present so callers
    don't need to defend against shape drift."""
    p = manifest_path(album_id)
    stub = _stub_manifest()
    if not p.exists():
        return stub
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return stub
    if not isinstance(data, dict):
        return stub
    # Backfill missing keys from the stub so downstream code can do
    # `manifest["tags"]` without a KeyError.
    for k, v in stub.items():
        data.setdefault(k, v)
    return data


def write_manifest(album_id: str, manifest: dict) -> None:
    p = manifest_path(album_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tags = manifest.get("tags")
    if isinstance(tags, dict):
        filtered = {k: v for k, v in tags.items() if k in _TAG_KEYS}
        if filtered != tags:
            manifest = {**manifest, "tags": filtered}
    p.write_text(json.dumps(manifest, indent=2))


def list_album_ids() -> list[str]:
    """Direct-children-only scan of in-progress/. Hidden dirs (.cache,
    .anything) are skipped so cache trees don't masquerade as albums."""
    if not IN_PROGRESS_DIR.is_dir():
        return []
    out = []
    for p in IN_PROGRESS_DIR.iterdir():
        if p.is_dir() and not p.name.startswith(".") and is_valid_album_id(p.name):
            out.append(p.name)
    return out


def reconcile_sides(album_id: str) -> dict:
    """Walk the album dir and reconcile `sides[]` against the FLACs actually
    on disk:

    - Append any FLAC not already in `sides[]` (sorted lex for determinism).
    - Strip entries from `sides[]` that no longer exist.
    - Persist if anything changed.

    Returns the (possibly mutated) manifest. Single source of truth for
    side-list mutation — never edit `manifest["sides"]` outside this
    function (or `reorder_sides` below)."""
    d = album_dir(album_id)
    manifest = read_manifest(album_id)
    on_disk = sorted(p.name for p in d.glob("*.flac"))
    existing = list(manifest.get("sides") or [])
    kept = [s for s in existing if s in on_disk]
    appended = [s for s in on_disk if s not in kept]
    new_sides = kept + appended
    if new_sides != existing:
        manifest["sides"] = new_sides
        write_manifest(album_id, manifest)
    return manifest


def reorder_sides(album_id: str, new_order: list[str]) -> dict:
    """Persist a user-driven permutation of sides[]. The new list must be a
    permutation of the existing on-disk set (same elements, different
    order); otherwise we raise. Per-side peaks dats are mtime-validated, so
    no explicit cache invalidation is needed."""
    manifest = reconcile_sides(album_id)
    current = set(manifest["sides"])
    proposed = list(new_order)
    if set(proposed) != current or len(proposed) != len(current):
        raise ValueError("reorder must be a permutation of the current sides")
    manifest["sides"] = proposed
    write_manifest(album_id, manifest)
    return manifest


def peaks_cache_dir(album_id: str) -> Path:
    """Per-side peaks dats live under `.cache/peaks/`. Sibling of any other
    cache files so it's swept by `delete_album` / `demote_album` for free."""
    return album_dir(album_id) / ".cache" / "peaks"


def peaks_cache_path_for_side(album_id: str, side_filename: str) -> Path:
    """Per-side `.peaks.dat` path. We key on the side's stem so a re-record
    that produces the same filename naturally invalidates via mtime, and a
    drop-in side with a fresh name gets a fresh dat without collision."""
    stem = Path(side_filename).stem
    return peaks_cache_dir(album_id) / f"{stem}.dat"


def ensure_side_peaks_cache(
    album_id: str,
    side_filename: str,
    job_id: Optional[str] = None,  # accepted for symmetry; render_peaks is sub-second
) -> Path:
    """Build or refresh the `.peaks.dat` for a single side. audiowaveform
    runs directly against the side FLAC — no album-level concat. Mtime-
    validated against the source FLAC so a re-recorded side rebuilds its
    dat on next request and nothing else."""
    from services.peaks import is_fresh, render_peaks
    d = album_dir(album_id)
    src = d / side_filename
    if not src.exists():
        raise FileNotFoundError(
            f"album {album_id}: side {side_filename!r} missing on disk"
        )
    out = peaks_cache_path_for_side(album_id, side_filename)
    if is_fresh(out, src):
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    render_peaks(src, out)
    return out


def album_concat_playlist(album_id: str) -> tuple[Path, list[Path]]:
    """Write a transient ffmpeg concat-demuxer playlist for this album's
    sides and return `(playlist_path, side_paths)`. Caller is responsible
    for unlinking the playlist after the ffmpeg invocation finishes (use
    `try/finally`).

    Used by `/measure` and `/split` to feed every side through ffmpeg in
    album order without precomputing a concat.flac on disk. Quoting follows
    ffmpeg's concat demuxer rules: single quotes around each path, internal
    single quotes escaped as `'\\''`."""
    manifest = reconcile_sides(album_id)
    sides = manifest.get("sides") or []
    if not sides:
        raise FileNotFoundError(f"album {album_id} has no sides")
    d = album_dir(album_id)
    side_paths = [d / s for s in sides]
    missing = [s.name for s in side_paths if not s.exists()]
    if missing:
        raise FileNotFoundError(
            f"album {album_id}: sides referenced in album.json missing on disk: {missing}"
        )
    cache_dir = album_dir(album_id) / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    playlist = cache_dir / f".concat_{secrets.token_hex(4)}.txt"
    quote = chr(39)
    escaped_quote = quote + chr(92) + quote + quote
    playlist.write_text("".join(
        f"file {quote}{str(p).replace(quote, escaped_quote)}{quote}\n"
        for p in side_paths
    ))
    return playlist, side_paths


def _summarize_album(album_id: str, manifest: dict) -> dict:
    """Build the UI-shaped row for `/api/albums`. Stats come from the first
    side (cheap) — total duration is summed across sides without re-encoding.

    `sides` is exposed as `[{filename, duration_seconds}, ...]` so the
    wave editor can build the album timeline locally and address per-side
    peaks/audio endpoints by index without a second round-trip."""
    d = album_dir(album_id)
    sides = manifest.get("sides") or []
    side_entries: list[dict] = []
    side_paths: list[Path] = []
    side_fmts: list[dict] = []
    for s in sides:
        p = d / s
        if not p.exists():
            continue
        side_paths.append(p)
        sf = flac_format(p)
        side_fmts.append(sf)
        side_entries.append({
            "filename":         s,
            "duration_seconds": flac_duration_seconds(p),
            "bit_depth":        sf.get("bit_depth"),
            "sample_rate_khz":  sf.get("sample_rate_khz"),
        })
    total_dur = sum((e["duration_seconds"] or 0.0) for e in side_entries) or None
    fmt: dict = side_fmts[0] if side_fmts else {}
    size_bytes = sum(p.stat().st_size for p in side_paths)
    plan = manifest.get("plan")
    kept_tracks = [t for t in (plan or {}).get("tracks", []) if not t.get("skip")]
    tags = manifest.get("tags") or {}
    music_relpath = manifest.get("music_relpath")
    # `split` is the "tracks have been emitted to music/" signal — drives
    # the UI's In-progress vs Music partition. A plan can exist as a pure
    # draft (saved by the editor mid-edit) without an emit having happened
    # yet; that still belongs in the In-progress section. Once split runs
    # successfully, music_relpath gets set and the row jumps to Music.
    return {
        "album_id":         album_id,
        "mtime":            d.stat().st_mtime,
        "size_mb":          round(size_bytes / 1e6, 1),
        "duration_seconds": total_dur,
        "bit_depth":        fmt.get("bit_depth"),
        "sample_rate_khz":  fmt.get("sample_rate_khz"),
        "artist":           tags.get("artist", ""),
        "album":            tags.get("album", ""),
        "year":             tags.get("year", ""),
        "genre":            tags.get("genre", ""),
        "label":            tags.get("label", ""),
        "catalog_number":   tags.get("catalog_number", ""),
        "country":          tags.get("country", ""),
        "composer":         tags.get("composer", ""),
        "conductor":        tags.get("conductor", ""),
        "musicbrainz_albumid": tags.get("musicbrainz_albumid", ""),
        "discogs_release_id":  tags.get("discogs_release_id"),
        "side_count":       len(sides),
        "sides":            side_entries,
        "split":            bool(music_relpath),
        "has_draft":        plan is not None and not music_relpath,
        "music_relpath":    music_relpath,
        "track_count":      len(kept_tracks),
        "sources_purged":   bool(manifest.get("sources_purged")),
        "external":         bool(manifest.get("external")),
        "tag_warning":      manifest.get("tag_warning") or None,
    }


def list_albums() -> list[dict]:
    """Walk in-progress/, reconcile each album's sides[], return UI rows."""
    out = []
    for album_id in list_album_ids():
        try:
            manifest = reconcile_sides(album_id)
        except (OSError, ValueError):
            continue
        out.append(_summarize_album(album_id, manifest))
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def cleanup_music_for(manifest: dict) -> None:
    """Wipe `music/{music_relpath}/` (if any) named in this album's manifest,
    and prune the now-empty parent artist dir. Called by both the album-
    delete path and the recordings bulk-delete fall-through."""
    relpath = manifest.get("music_relpath")
    if not relpath:
        return
    music_album = MUSIC_DIR / relpath
    if music_album.is_dir():
        for child in music_album.iterdir():
            try: child.unlink()
            except Exception: pass
        try: music_album.rmdir()
        except Exception: pass
    parent = music_album.parent
    try:
        if parent.is_dir() and parent != MUSIC_DIR and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass


def delete_album(album_id: str) -> dict:
    """Remove the in-progress dir AND the music subtree (if split). Returns
    the manifest so callers can log what was deleted."""
    manifest = read_manifest(album_id)
    cleanup_music_for(manifest)
    d = album_dir(album_id)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    return manifest


def purge_sources(album_id: str) -> dict:
    """Free the bulk of disk used by a split album by deleting the side
    FLACs and the `.cache/` tree, while keeping `album.json` (and any
    `cover.jpg`) so the album row stays visible in the Music section.

    Refuses to run if the album hasn't been split — without `music_relpath`
    there are no emitted tracks to fall back on, and dropping the sides
    would be a silent destructive operation.

    Returns `{"bytes_freed": int, "files_removed": int}` so the caller can
    report the savings."""
    manifest = reconcile_sides(album_id)
    if not manifest.get("music_relpath"):
        raise ValueError(
            "album has not been split — refusing to delete originals"
        )
    d = album_dir(album_id)
    bytes_freed = 0
    files_removed = 0
    for fname in list(manifest.get("sides") or []):
        p = d / fname
        if not p.exists():
            continue
        try:
            bytes_freed += p.stat().st_size
            p.unlink()
            files_removed += 1
        except OSError:
            pass
    cache = d / ".cache"
    if cache.is_dir():
        for sub in cache.rglob("*"):
            if sub.is_file():
                try:
                    bytes_freed += sub.stat().st_size
                    files_removed += 1
                except OSError:
                    pass
        shutil.rmtree(cache, ignore_errors=True)
    manifest["sides"] = []
    manifest["sources_purged"] = True
    write_manifest(album_id, manifest)
    return {"bytes_freed": bytes_freed, "files_removed": files_removed}


def demote_album(album_id: str) -> dict:
    """Move every side back to `raw/` (uniquify on collision), then delete
    the album dir. `music/` is preserved if present — the UI confirm dialog
    explains this. Returns the moved-side filenames."""
    manifest = reconcile_sides(album_id)
    d = album_dir(album_id)
    moved: list[str] = []
    for fname in list(manifest.get("sides") or []):
        src = d / fname
        if not src.exists():
            continue
        dst = RAW_DIR / fname
        if dst.exists():
            stem, ext = dst.stem, dst.suffix
            i = 2
            while True:
                cand = RAW_DIR / f"{stem} ({i}){ext}"
                if not cand.exists():
                    dst = cand
                    break
                i += 1
        src.rename(dst)
        moved.append(dst.name)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    return {"moved": moved, "music_preserved": bool(manifest.get("music_relpath"))}


# Serializes `create_album` so two concurrent combine requests with
# overlapping source filenames can't race each other into a half-built
# album dir. Combine is rare and metadata-only (no ffmpeg), so holding a
# process-wide lock for the duration of the rename loop is essentially
# free, and it pairs with the partial-failure cleanup below to keep the
# in-progress/ tree free of orphan empty dirs.
_CREATE_ALBUM_LOCK = threading.Lock()


def create_album(filenames: list[str], tags: dict) -> tuple[str, dict]:
    """Create a new in-progress album: mint a slug, mkdir the album dir,
    move the named raw/ sides into it (preserving filenames; uniquify on
    collision), write album.json. Returns `(album_id, manifest)`.

    Raises FileNotFoundError if any source is missing from raw/. If a
    failure happens partway through moving sources (e.g. a concurrent
    combine already grabbed the file), the freshly-mkdir'd album dir is
    cleaned up before the exception propagates so no orphan empty dir is
    left behind."""
    with _CREATE_ALBUM_LOCK:
        sources: list[Path] = []
        for fn in filenames:
            if "/" in fn or "\\" in fn or ".." in fn:
                raise ValueError(f"invalid filename: {fn!r}")
            p = RAW_DIR / fn
            if not p.exists():
                raise FileNotFoundError(f"side not found in raw/: {fn}")
            sources.append(p)

        # Tiny chance of collision; re-roll a few times before giving up.
        for _ in range(8):
            album_id = new_album_id()
            d = album_dir(album_id)
            if not d.exists():
                d.mkdir(parents=True)
                break
        else:
            raise RuntimeError("could not allocate unique album_id")

        try:
            moved_sides: list[str] = []
            for src in sources:
                dst = d / src.name
                if dst.exists():
                    stem, ext = dst.stem, dst.suffix
                    i = 2
                    while True:
                        cand = d / f"{stem} ({i}){ext}"
                        if not cand.exists():
                            dst = cand
                            break
                        i += 1
                src.rename(dst)
                moved_sides.append(dst.name)

            manifest = _stub_manifest()
            manifest["tags"] = {
                k: v for k, v in (tags or {}).items() if v not in ("", None)
            }
            manifest["sides"] = moved_sides
            write_manifest(album_id, manifest)
        except Exception:
            # Roll back the partially-built album dir so a failed combine
            # (typically `src.rename` raising FileNotFoundError when a
            # concurrent request already moved the source) doesn't leave
            # an empty dir lingering in in-progress/.
            shutil.rmtree(d, ignore_errors=True)
            raise
        return album_id, manifest


def write_cover(album_id: str, data: bytes) -> Path:
    """Save raw image bytes to `cover.jpg` in the album dir. Caller decides
    if/when to also update the manifest's `cover` field — `read_manifest`
    handles either."""
    d = album_dir(album_id)
    cover = d / "cover.jpg"
    cover.write_bytes(data)
    manifest = read_manifest(album_id)
    if manifest.get("cover") != "cover.jpg":
        manifest["cover"] = "cover.jpg"
        write_manifest(album_id, manifest)
    return cover


def cover_path(album_id: str) -> Optional[Path]:
    """Return the cover image path for an album if one exists, else None."""
    d = album_dir(album_id)
    manifest = read_manifest(album_id)
    rel = manifest.get("cover")
    if not rel:
        return None
    if "/" in rel or "\\" in rel or ".." in rel:
        return None
    p = d / rel
    return p if p.exists() else None


# Audio extensions used by `import_external_music` to recognise an album
# folder. Kept in sync with `_AUDIO_EXTS` over in split_orchestrator — same
# set the split path emits — without a cross-module import (this layer must
# not depend on the orchestrator, which imports albums_fs).
_IMPORT_AUDIO_EXTS = (".flac", ".wav", ".mp3", ".ogg", ".m4a")

# `Artist/Album (Year)` is the Jellyfin shape `music_dir_for` writes. We
# accept either form on import — most user libraries have a year suffix,
# but not all.
_YEAR_SUFFIX_RE = re.compile(r"^(.*?)\s*\(([0-9]{4})\)\s*$")


def _parse_relpath_tags(relpath: str) -> dict:
    """Parse `Artist/Album (Year)` (or `Artist/Album`) into `{artist, album,
    year}`. Returns whatever fields can be confidently inferred from the
    path; missing parts come back absent rather than blank.

    Two-level paths only — anything else (single segment, nested deeper)
    yields an empty dict so the caller falls back to FLAC tags."""
    parts = [p for p in relpath.replace("\\", "/").split("/") if p]
    if len(parts) != 2:
        return {}
    artist, album_seg = parts
    out = {"artist": artist}
    m = _YEAR_SUFFIX_RE.match(album_seg)
    if m:
        out["album"] = m.group(1).strip()
        out["year"]  = m.group(2)
    else:
        out["album"] = album_seg
    return out


def _tag_warning(flac_tags: dict, path_tags: dict) -> Optional[str]:
    """Human-readable note when the FLAC's embedded Vorbis tags disagree
    with what the directory layout claims. Returned verbatim to the UI as
    a small "ⓘ" pill next to the row's locked indicator. None when there
    is nothing to flag."""
    if not flac_tags or not path_tags:
        return None
    diffs = []
    for k in ("artist", "album", "year"):
        a = (flac_tags.get(k) or "").strip()
        b = (path_tags.get(k) or "").strip()
        if a and b and a != b:
            diffs.append(f"{k}: tag={a!r}, folder={b!r}")
    return "; ".join(diffs) or None


def _read_album_tags_from_flac(audio_files: list[Path]) -> dict:
    """Walk `audio_files` until one yields a usable Vorbis tag set; only
    FLACs respond to `metaflac --export-tags-to`. The first hit wins —
    every track in an album is expected to share artist/album/year/genre
    tags, so probing further isn't worth the subprocess cost."""
    for f in audio_files:
        if f.suffix.lower() != ".flac":
            continue
        raw = read_tags(f)
        if not raw:
            continue
        # `read_tags` returns the on-disk Vorbis case ("ARTIST", "DATE"); the
        # manifest uses lowercase keys ("artist", "year"). Map them here.
        out: dict = {}
        if raw.get("ARTIST"):         out["artist"] = raw["ARTIST"]
        if raw.get("ALBUM"):          out["album"]  = raw["ALBUM"]
        if raw.get("DATE"):           out["year"]   = raw["DATE"]
        if raw.get("GENRE"):          out["genre"]  = raw["GENRE"]
        if raw.get("LABEL"):          out["label"]  = raw["LABEL"]
        if raw.get("CATALOGNUMBER"):  out["catalog_number"] = raw["CATALOGNUMBER"]
        if raw.get("RELEASECOUNTRY"): out["country"] = raw["RELEASECOUNTRY"]
        if raw.get("COMPOSER"):       out["composer"] = raw["COMPOSER"]
        if raw.get("CONDUCTOR"):      out["conductor"] = raw["CONDUCTOR"]
        if out:
            return out
    return {}


def _existing_music_relpaths() -> set[str]:
    """The set of `music_relpath` strings already claimed by an
    in-progress/{album_id}/album.json. Used by the importer to skip dirs
    that are already represented in the listing — both the normal split-
    emitted case AND a prior import."""
    out: set[str] = set()
    for aid in list_album_ids():
        m = read_manifest(aid)
        rel = m.get("music_relpath")
        if rel:
            out.add(rel)
    return out


def _scan_music_for_orphans() -> list[tuple[str, Path]]:
    """Walk `music/` two levels deep (`<Artist>/<Album>/`) and yield every
    album dir that holds at least one audio file. Returned as
    `(relpath, abspath)` pairs so the caller can keep both forms without
    re-deriving."""
    if not MUSIC_DIR.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for artist_dir in sorted(MUSIC_DIR.iterdir()):
        if not artist_dir.is_dir() or artist_dir.name.startswith("."):
            continue
        for album_dir_ in sorted(artist_dir.iterdir()):
            if not album_dir_.is_dir() or album_dir_.name.startswith("."):
                continue
            has_audio = any(
                p.suffix.lower() in _IMPORT_AUDIO_EXTS and p.is_file()
                for p in album_dir_.iterdir()
            )
            if not has_audio:
                continue
            relpath = f"{artist_dir.name}/{album_dir_.name}"
            out.append((relpath, album_dir_))
    return out


def import_external_music() -> list[str]:
    """Surface manually-added albums in `music/` as locked rows in the UI.

    Walks `music/<Artist>/<Album (Year)>/`, and for each dir without a
    matching in-progress manifest, creates a stub `album.json` carrying:
      - tags from the FLAC's Vorbis tags AND/OR parsed from the folder
        name (FLAC wins on conflicts; `tag_warning` records the diff)
      - `music_relpath`           pointing back at the music dir
      - `sources_purged: True`    so the row paints as "locked"
      - `external: True`          a marker so a future re-scan recognises
                                  this as an auto-imported row (not a
                                  genuine in-progress workspace)
      - `cover: cover.jpg`        if either a sidecar `cover.jpg`/`folder.jpg`
                                  exists or one of the FLACs has embedded art
      - `plan.tracks`             one entry per non-skipped audio file, so
                                  the row's "N tracks" count + size_mb add up
                                  even though there are no sides on disk

    Returns the newly-created album_ids. Idempotent: re-running picks up
    any new orphans and skips dirs that already have a matching manifest."""
    claimed = _existing_music_relpaths()
    created: list[str] = []
    for relpath, abspath in _scan_music_for_orphans():
        if relpath in claimed:
            continue

        audio_files = sorted(
            p for p in abspath.iterdir()
            if p.is_file() and p.suffix.lower() in _IMPORT_AUDIO_EXTS
        )
        flac_tags = _read_album_tags_from_flac(audio_files)
        path_tags = _parse_relpath_tags(relpath)
        merged: dict = {**path_tags, **flac_tags}  # FLAC wins on conflict
        warning = _tag_warning(flac_tags, path_tags)

        # Synthesize a track list from the on-disk files so the row reports
        # something meaningful. Titles come from the filename stem with any
        # leading "NN - " stripped, which matches the orchestrator's emit
        # pattern.
        track_entries: list[dict] = []
        track_strip_re = re.compile(r"^\d+\s*[-_.]\s*")
        for f in audio_files:
            title = track_strip_re.sub("", f.stem) or f.stem
            dur = flac_duration_seconds(f) if f.suffix.lower() == ".flac" else None
            track_entries.append({
                "title": title,
                "duration_seconds": float(dur or 0.0),
                "skip": False,
            })

        # Allocate a fresh album_id. Re-roll on the (vanishingly rare)
        # collision the same way create_album does.
        for _ in range(8):
            aid = new_album_id()
            if not album_dir(aid).exists():
                album_dir(aid).mkdir(parents=True)
                break
        else:
            continue  # pathological — skip and move on

        # Bring across album art so the row's thumbnail isn't blank. Prefer
        # a sidecar image if present (most ripping tools drop one); fall back
        # to extracting from the first FLAC's PICTURE block.
        cover_field: Optional[str] = None
        for cand_name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
            cand = abspath / cand_name
            if cand.is_file():
                try:
                    (album_dir(aid) / "cover.jpg").write_bytes(cand.read_bytes())
                    cover_field = "cover.jpg"
                    break
                except OSError:
                    pass
        if cover_field is None:
            for f in audio_files:
                if f.suffix.lower() != ".flac":
                    continue
                if extract_cover_to_album(aid, f) is not None:
                    cover_field = "cover.jpg"
                    break

        manifest = _stub_manifest()
        manifest["tags"]           = merged
        manifest["sides"]          = []
        manifest["cover"]          = cover_field
        manifest["plan"]           = {"tracks": track_entries}
        manifest["music_relpath"]  = relpath
        manifest["sources_purged"] = True
        manifest["external"]       = True
        if warning:
            manifest["tag_warning"] = warning
        write_manifest(aid, manifest)
        created.append(aid)
    return created


def extract_cover_to_album(album_id: str, src_flac: Path) -> Optional[Path]:
    """Pull embedded cover art out of a FLAC and save it as cover.jpg in
    the album dir. Used by the apply-tags flow when the chosen release has
    a cover available via metaflac's PICTURE block. Returns the new cover
    path or None if the FLAC has no embedded picture."""
    d = album_dir(album_id)
    tmp = d / f".cover_extract_{secrets.token_hex(4)}.jpg"
    rc = subprocess.run(
        ["metaflac", f"--export-picture-to={tmp}", str(src_flac)],
        capture_output=True, check=False,
    )
    if rc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return None
    final = d / "cover.jpg"
    tmp.replace(final)
    manifest = read_manifest(album_id)
    if manifest.get("cover") != "cover.jpg":
        manifest["cover"] = "cover.jpg"
        write_manifest(album_id, manifest)
    return final
