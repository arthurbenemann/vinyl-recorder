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
                         catalog_number, country, musicbrainz_albumid,
                         discogs_release_id},
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
from pathlib import Path
from typing import Optional

from services.ffmpeg import flac_duration_seconds, flac_format, safe_path_component
from state import IN_PROGRESS_DIR, MUSIC_DIR, RAW_DIR

# Album dir basenames are restricted to lowercase hex / dashes / underscores
# so they can be used verbatim in URL paths without encoding. The default
# `secrets.token_hex(4)` slug always satisfies this; users dropping a folder
# in by hand can pick any name that does too.
ALBUM_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
SCHEMA_VERSION = 2


def _stub_manifest() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "tags":           {},
        "sides":          [],
        "cover":          None,
        "plan":           None,
        "music_relpath":  None,
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
    for s in sides:
        p = d / s
        if not p.exists():
            continue
        side_paths.append(p)
        side_entries.append({
            "filename":         s,
            "duration_seconds": flac_duration_seconds(p),
        })
    total_dur = sum((e["duration_seconds"] or 0.0) for e in side_entries) or None
    fmt: dict = {}
    size_bytes = 0
    for p in side_paths:
        size_bytes += p.stat().st_size
        if not fmt:
            fmt = flac_format(p)
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
        "musicbrainz_albumid": tags.get("musicbrainz_albumid", ""),
        "discogs_release_id":  tags.get("discogs_release_id"),
        "side_count":       len(sides),
        "sides":            side_entries,
        "split":            bool(music_relpath),
        "has_draft":        plan is not None and not music_relpath,
        "music_relpath":    music_relpath,
        "track_count":      len(kept_tracks),
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


def create_album(filenames: list[str], tags: dict) -> tuple[str, dict]:
    """Create a new in-progress album: mint a slug, mkdir the album dir,
    move the named raw/ sides into it (preserving filenames; uniquify on
    collision), write album.json. Returns `(album_id, manifest)`.

    Raises FileNotFoundError if any source is missing from raw/."""
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
    manifest["tags"] = {k: v for k, v in (tags or {}).items() if v not in ("", None)}
    manifest["sides"] = moved_sides
    write_manifest(album_id, manifest)
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
