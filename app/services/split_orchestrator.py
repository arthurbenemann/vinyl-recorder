"""Album-split orchestration: turns a wave-editor plan into per-track FLACs
under `music/{Artist}/{Album} (Year)/`.

The split flow used to live inline in `routes/albums.py`. It pulls in
ffmpeg + metaflac, the album-folder layer (`services.albums_fs`), the
jobs registry, and disk-space accounting — i.e. it's all domain logic
with no HTTP shape of its own. Extracting it here keeps the route file a
thin handler and makes the pipeline testable without FastAPI.

Public surface:

  - `split_album(req, manifest)`: the full orchestrator. Returns a result
    dict matching the route's response body (`{music_relpath, tracks}`)
    on success, or raises one of the domain exceptions below.
  - `wipe_prior_music_dir`, `kept_duration_total`, `write_track_tags`:
    individually testable helpers that the orchestrator composes.

Domain exceptions (mapped to HTTP status by the route):

  - `SplitNotFoundError`     → 404
  - `SplitValidationError`   → 400
  - `SplitDiskSpaceError`    → 507
  - `SplitProcessingError`   → 500

Everything else (route-level guards like is_valid_album_id, request
parsing) stays in the route — the orchestrator's preconditions are
documented in `split_album`'s docstring.
"""
import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from services import albums_fs
from services.ffmpeg import (
    LOW_SPACE_GB, disk_space_error, flac_duration_seconds,
    run_ffmpeg_with_progress, safe_path_component,
)
from services.jobs import finish_job, start_job
from state import ALLOWED_OUTPUT_FORMATS, ALLOWED_SPLIT_SAMPLE_RATES, MUSIC_DIR


# Container/codec settings per output_format. `ext` is the file extension
# (with leading dot); `ffmpeg_args` is the codec arg list appended to the
# encode command. `lossless` controls whether bit-depth selection (via
# aformat) gets applied — lossy codecs ignore it and pick their own
# internal precision.
_FORMAT_SETTINGS: dict[str, dict] = {
    "flac":     {"ext": ".flac", "ffmpeg_args": ["-c:a", "flac", "-compression_level", "5"], "lossless": True,  "supports_metaflac": True},
    "wav":      {"ext": ".wav",  "ffmpeg_args": ["-c:a", "pcm_s16le"],                       "lossless": True,  "supports_metaflac": False},
    "mp3":      {"ext": ".mp3",  "ffmpeg_args": ["-c:a", "libmp3lame", "-q:a", "0"],         "lossless": False, "supports_metaflac": False},
    "ogg":      {"ext": ".ogg",  "ffmpeg_args": ["-c:a", "libvorbis",  "-q:a", "8"],         "lossless": False, "supports_metaflac": False},
    "m4a-aac":  {"ext": ".m4a",  "ffmpeg_args": ["-c:a", "aac",        "-b:a", "256k"],      "lossless": False, "supports_metaflac": False},
    "m4a-alac": {"ext": ".m4a",  "ffmpeg_args": ["-c:a", "alac"],                            "lossless": True,  "supports_metaflac": False},
}

# Every audio extension the split pipeline can produce. Used to wipe
# prior music dirs and to recognise track filenames in `download_track`
# / `album_tracks` regardless of which format the album was emitted as.
_AUDIO_EXTS: tuple[str, ...] = (".flac", ".wav", ".mp3", ".ogg", ".m4a")


_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+(.+)$", re.IGNORECASE)


def sort_name(name: str) -> str:
    """Move a leading English article to the end for library sorting:
    "The Beatles" -> "Beatles, The". Music servers (Jellyfin, Navidrome, …)
    alphabetize by ARTISTSORT / ALBUMARTISTSORT when present, so this is
    what files a "The …" artist under the right letter instead of all
    bunched under "T". Returns the name unchanged when there's no leading
    article (or nothing follows it). Article case is preserved as typed."""
    if not name:
        return ""
    m = _LEADING_ARTICLE_RE.match(name.strip())
    if not m:
        return name.strip()
    return f"{m.group(2).strip()}, {m.group(1)}"


def _wav_codec_for_bits(bits: Optional[int]) -> str:
    """16-bit signed LE for WAV unless the user asked for 24-bit explicitly."""
    return "pcm_s24le" if bits == 24 else "pcm_s16le"


def split_genres(genre: str) -> list[str]:
    """Split a genre string into individual values on the `;` separator
    (plus newlines), trimmed, blanks dropped.

    Music servers (Jellyfin, Navidrome, …) want each genre as its own value,
    not one delimited blob — a single "Electronic; Techno; House" tag shows
    up as one nonsense genre and breaks genre browsing. The tagging flow
    joins MusicBrainz/Discogs genres + styles with `;`, so this splits them
    back apart at write time into repeated GENRE Vorbis comments.

    Only `;` (and newlines) split — NOT commas — because a single Discogs
    genre legitimately contains commas ("Folk, World, & Country") and must
    survive intact as one value."""
    if not genre:
        return []
    parts = re.split(r"[;\n]", genre)
    return [p.strip() for p in parts if p.strip()]


def _is_compilation(tags: dict) -> bool:
    """Whether to stamp the COMPILATION flag that music servers (Jellyfin,
    Navidrome, …) read to file an album under a single "Various Artists"
    heading instead of fragmenting it into one album per track artist.

    Heuristic: the album's ARTIST is literally "Various Artists" (the
    convention MusicBrainz/Discogs use for compilations). Case-insensitive
    so a hand-typed "various artists" still triggers it."""
    return (tags.get("artist") or "").strip().lower() == "various artists"


def _media_type_for(ext: str) -> str:
    """Map an audio file extension to the HTTP `Content-Type` used by
    `download_track`. Falls back to octet-stream so an unknown extension
    still downloads instead of confusing the browser into trying to render."""
    return {
        ".flac": "audio/flac", ".wav":  "audio/wav",  ".mp3":  "audio/mpeg",
        ".ogg":  "audio/ogg",  ".m4a":  "audio/mp4",
    }.get(ext.lower(), "application/octet-stream")


def _ffmpeg_metadata_args(title: str, out_idx: int, out_total: int,
                          tags: dict, cover_file: Optional[Path]) -> list[str]:
    """Build the `-metadata key=value` flag set used for non-FLAC encodes.
    metaflac handles FLAC tag writing after encode (existing flow); for
    every other container we have to bake the tags in at encode time.

    Cover-art embedding for AAC/MP3/OGG would need a second `-i` input
    plus `-c:v copy -disposition:v attached_pic`. ffmpeg's behaviour
    varies by container (mp3 ID3v2.3 / m4a covr atom / vorbis
    METADATA_BLOCK_PICTURE base64). For now we leave cover embedding to
    the FLAC-only path; the cover.jpg sits in the album dir and
    Jellyfin's scanner picks it up as folder art for the non-FLAC tracks
    too."""
    args: list[str] = []
    pairs = [
        ("artist",       tags.get("artist", "")),
        # album_artist groups the album in every music server; without it a
        # multi-artist or "feat." track scatters the album across artists.
        # Defaults to the album ARTIST (correct for single-artist LPs).
        ("album_artist", tags.get("artist", "")),
        ("album",        tags.get("album", "")),
        ("date",         tags.get("year", "")),
        ("genre",        tags.get("genre", "")),
        ("publisher",    tags.get("label", "")),
        ("title",        title),
        ("track",        f"{out_idx}/{out_total}"),
    ]
    if tags.get("composer"):  pairs.append(("composer",  tags["composer"]))
    if tags.get("conductor"): pairs.append(("conductor", tags["conductor"]))
    if _is_compilation(tags):  pairs.append(("compilation", "1"))
    for k, v in pairs:
        if v != "":
            args += ["-metadata", f"{k}={v}"]
    return args


# ── Domain exceptions ────────────────────────────────────────────────────

class SplitError(Exception):
    """Base — never raised directly."""


class SplitNotFoundError(SplitError):
    """Album / sides missing on disk. → HTTP 404."""


class SplitValidationError(SplitError):
    """Plan rejected (no tracks / unsupported sample rate). → HTTP 400."""


class SplitDiskSpaceError(SplitError):
    """Not enough free space to run the split. → HTTP 507."""


class SplitProcessingError(SplitError):
    """ffmpeg / metaflac failure mid-encode. → HTTP 500."""


# ── Pure helpers ─────────────────────────────────────────────────────────

def wipe_prior_music_dir(prior_relpath: Optional[str], new_relpath: str) -> None:
    """If artist/album tags have changed since the last split, the music_relpath
    moved — remove the OLD output dir so we don't leave orphaned tracks under
    the previous Artist/Album path. No-op when relpath is unchanged."""
    if not prior_relpath or prior_relpath == new_relpath:
        return
    prior_dir = MUSIC_DIR / prior_relpath
    if prior_dir.is_dir():
        for ext in _AUDIO_EXTS:
            for old in prior_dir.glob(f"*{ext}"):
                try: old.unlink()
                except Exception: pass
        # Our own folder-art sidecar; remove so the moved dir can be pruned.
        (prior_dir / "cover.jpg").unlink(missing_ok=True)
        try: prior_dir.rmdir()
        except Exception: pass
        try:
            if prior_dir.parent != MUSIC_DIR and not any(prior_dir.parent.iterdir()):
                prior_dir.parent.rmdir()
        except Exception:
            pass


def add_replay_gain(track_paths: list[Path]) -> None:
    """Compute and write ReplayGain 2.0 tags over a set of FLAC tracks in a
    single metaflac pass.

    One `metaflac --add-replay-gain` invocation over ALL of an album's
    tracks writes both the per-track gain (REPLAYGAIN_TRACK_GAIN/_PEAK) and
    a shared album gain (REPLAYGAIN_ALBUM_GAIN/_PEAK) computed across the
    whole set — album gain preserves the LP's intra-side dynamics while
    letting players normalise the library. The audio is never touched, so
    this is fully reversible (`metaflac --remove-replay-gain`).

    metaflac requires the files to share sample rate + channel count; every
    track emitted from one split does, so that precondition holds. Failure
    is non-fatal (the tracks already exist and play fine) — we swallow it
    the same way `write_track_tags` does, rather than abort a finished
    split over a missing-loudness-tag. FLAC only; the caller gates on
    output_format."""
    if not track_paths:
        return
    subprocess.run(
        ["metaflac", "--add-replay-gain", *[str(p) for p in track_paths]],
        check=False, stderr=subprocess.DEVNULL,
    )


def kept_duration_total(tracks: list, total: float) -> float:
    """Total seconds of audio that will actually land in `music/` (skip
    tracks excluded). Drives the constant-rate progress bar across the
    full split — without this the bar would jump per track."""
    cur = 0.0
    kept = 0.0
    for i, t in enumerate(tracks, start=1):
        s = cur
        e = total if i == len(tracks) else min(total, s + max(0.0, t.duration_seconds))
        cur = e
        if e > s and not t.skip:
            kept += e - s
    return kept


def write_track_tags(out: Path, title: str, out_idx: int, out_total: int,
                     tags: dict, cover_file: Optional[Path]) -> None:
    """Replace the FLAC's tag set with the manifest's tags + per-track
    title/track-number, then embed cover.jpg if present. Single metaflac
    invocation for the tags; the picture import has to be its own call.

    Distinct from `services.ffmpeg.write_tags` (which writes the side-level
    tag set used during apply-tags). This one is the per-track flavour: it
    additionally sets ALBUMARTIST / TITLE / TRACKNUMBER / TRACKTOTAL, the
    COMPILATION flag on Various-Artists albums, plus the optional
    MUSICBRAINZ_* IDs, DISCOGS_RELEASE_ID, MEDIA, and RELEASETYPE, and
    embeds a cover."""
    tag_args = ["metaflac", "--remove-all-tags",
                f"--set-tag=ARTIST={tags.get('artist', '')}",
                # ALBUMARTIST is what every music server groups an album by.
                # Defaults to ARTIST (right for single-artist LPs); a
                # "Various Artists" ARTIST additionally trips COMPILATION
                # below so comps file under one heading instead of splitting.
                f"--set-tag=ALBUMARTIST={tags.get('artist', '')}",
                f"--set-tag=ALBUM={tags.get('album', '')}",
                f"--set-tag=DATE={tags.get('year', '')}",
                f"--set-tag=LABEL={tags.get('label', '')}",
                f"--set-tag=CATALOGNUMBER={tags.get('catalog_number', '')}",
                f"--set-tag=RELEASECOUNTRY={tags.get('country', '')}",
                f"--set-tag=TITLE={title}",
                f"--set-tag=TRACKNUMBER={out_idx}",
                f"--set-tag=TRACKTOTAL={out_total}"]
    # One GENRE Vorbis comment per value (servers browse by individual
    # genre, not a delimited blob). Blank genre → no GENRE tag at all
    # rather than an empty one.
    for g in split_genres(tags.get("genre", "")):
        tag_args.append(f"--set-tag=GENRE={g}")
    # Compilation flag — only on Various-Artists albums (see _is_compilation).
    if _is_compilation(tags):
        tag_args.append("--set-tag=COMPILATION=1")
    # Sort names — only when a leading article actually moves (otherwise the
    # sort form equals the display form and the tag is pure litter). Files
    # "The Beatles" under B in servers that sort by *SORT tags. Album-artist
    # defaults to artist, so both sort tags share the value.
    artist_sort = sort_name(tags.get("artist", ""))
    if artist_sort and artist_sort != (tags.get("artist") or "").strip():
        tag_args.append(f"--set-tag=ARTISTSORT={artist_sort}")
        tag_args.append(f"--set-tag=ALBUMARTISTSORT={artist_sort}")
    # Optional classical-style tags — only emit when present so we don't
    # leave empty COMPOSER=/CONDUCTOR= entries on every track.
    if tags.get("composer"):
        tag_args.append(f"--set-tag=COMPOSER={tags['composer']}")
    if tags.get("conductor"):
        tag_args.append(f"--set-tag=CONDUCTOR={tags['conductor']}")
    if tags.get("musicbrainz_albumid"):
        tag_args.append(f"--set-tag=MUSICBRAINZ_ALBUMID={tags['musicbrainz_albumid']}")
    if tags.get("discogs_release_id"):
        tag_args.append(f"--set-tag=DISCOGS_RELEASE_ID={tags['discogs_release_id']}")
    # Stable MB identifiers + release facts (filled at apply-time from the
    # chosen release). Servers use these for reliable matching/grouping and
    # artist/album art. Each only when present.
    for key, tagname in (
        ("musicbrainz_releasegroupid", "MUSICBRAINZ_RELEASEGROUPID"),
        ("musicbrainz_artistid",       "MUSICBRAINZ_ARTISTID"),
        ("musicbrainz_albumartistid",  "MUSICBRAINZ_ALBUMARTISTID"),
        ("media",                      "MEDIA"),
        ("releasetype",                "RELEASETYPE"),
    ):
        if tags.get(key):
            tag_args.append(f"--set-tag={tagname}={tags[key]}")
    tag_args.append(str(out))
    subprocess.run(tag_args, check=False, stderr=subprocess.DEVNULL)
    if cover_file:
        subprocess.run(
            ["metaflac", f"--import-picture-from={cover_file}", str(out)],
            check=False, stderr=subprocess.DEVNULL,
        )


# ── Orchestrator ─────────────────────────────────────────────────────────

async def _emit_track(*, req, t, i: int, out_idx: int, out_total: int, pad: int,
                      music_dir: Path, playlist: Path,
                      start_: float, end_: float, apply_gain: bool, gain_db: float,
                      sample_fmt: Optional[str], target_rate: Optional[int],
                      tags: dict, cover_file: Optional[Path],
                      slice_range: tuple[float, float]) -> dict:
    """Encode one track into music/ in the requested container, write tags,
    embed cover. FLAC keeps the existing flow (encode -> metaflac post-pass);
    every other format embeds tags inline with `-metadata` flags during the
    ffmpeg encode. Raises SplitProcessingError on ffmpeg failure (the outer
    handler unlinks the playlist via the existing try/finally before
    propagation)."""
    settings = _FORMAT_SETTINGS[req.output_format]
    track_name = f"{str(out_idx).zfill(pad)} - {safe_path_component(t.title) or 'Track'}{settings['ext']}"
    out = music_dir / track_name
    af = []
    if apply_gain:
        af.append(f"volume={gain_db:.4f}dB")
    if target_rate:
        # SoX resampler at 28-bit precision — well above 24-bit FLAC headroom
        # so quantisation from the resample is inaudible. Placed BEFORE
        # aformat so the bit-depth conversion happens after the rate change.
        af.append("aresample=resampler=soxr:precision=28")
    if sample_fmt and settings["lossless"]:
        # aformat-driven bit-depth selection only makes sense for the lossless
        # codec path. WAV's bit depth is the codec choice itself (handled
        # below); lossy codecs would silently ignore aformat.
        af.append(f"aformat=sample_fmts={sample_fmt}")
    # The concat demuxer presents the full album as one virtual input
    # stream so -ss/-to act in album time, including across side boundaries
    # — same behaviour as the old concat.flac input but no on-disk artifact.
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0",
           "-ss", f"{start_:.3f}", "-to", f"{end_:.3f}",
           "-i", str(playlist)]
    if af:
        cmd += ["-af", ",".join(af)]
    if target_rate:
        cmd += ["-ar", str(target_rate)]
    cmd += list(settings["ffmpeg_args"])
    if req.output_format == "wav":
        # WAV's bit depth IS the codec choice, not an aformat operation.
        # Replace the default pcm_s16le with pcm_s24le when the user asked
        # for 24-bit. (settings appended pcm_s16le above; rewrite that token.)
        idx = cmd.index("pcm_s16le")
        cmd[idx] = _wav_codec_for_bits(req.bit_depth or None)
    if req.output_format == "flac":
        # FLAC: metaflac writes tags after encode (existing flow). Strip any
        # inherited metadata from the input first.
        cmd += ["-map_metadata", "-1"]
    else:
        # Non-FLAC: bake tags + track number in via -metadata flags. metaflac
        # only handles FLAC, so for every other container we have to do the
        # tagging at encode time.
        cmd += _ffmpeg_metadata_args(t.title, out_idx, out_total, tags, cover_file)
    cmd.append(str(out))
    track_dur = end_ - start_
    rc, stderr = await asyncio.to_thread(
        run_ffmpeg_with_progress, cmd, track_dur, req.job_id,
        slice_range, f"track {out_idx}/{out_total}",
    )
    if rc != 0:
        err = (stderr or b"").decode(errors="replace")[:300]
        finish_job(req.job_id, error=err)
        raise SplitProcessingError(f"ffmpeg failed on track {i}: {err}")
    if req.output_format == "flac":
        write_track_tags(out, t.title, out_idx, out_total, tags, cover_file)
    return {
        "filename":         track_name,
        "duration_seconds": end_ - start_,
        "size_mb":          round(out.stat().st_size / 1e6, 1),
    }


def _persist_split_plan(req, relpath: str) -> None:
    """Write the resolved plan + the new music_relpath back into album.json
    so the wave-editor can re-load and re-edit later.

    Bumps `plan_version` for consistency with the plan-update route's
    optimistic-concurrency contract — a successful split is a plan write
    too, so any wave-editor tab still holding a stale plan_version will
    correctly detect a conflict on its next debounced save."""
    plan = {
        "tracks": [
            {"title": t.title,
             "duration_seconds": t.duration_seconds,
             "skip": t.skip}
            for t in req.tracks
        ],
        "normalize":        req.normalize,
        "target_peak_db":   req.target_peak_db,
        "measured_peak_db": req.measured_peak_db,
        "bit_depth":        req.bit_depth,
        "sample_rate":      req.sample_rate,
        "output_format":    req.output_format,
        "replaygain":       req.replaygain,
    }
    manifest = albums_fs.read_manifest(req.album_id)
    manifest["plan"] = plan
    manifest["music_relpath"] = relpath
    manifest["plan_version"] = int(manifest.get("plan_version") or 0) + 1
    albums_fs.write_manifest(req.album_id, manifest)


async def split_album(req, manifest: dict) -> dict:
    """Cut the album into per-track FLACs in `music/{Artist}/{Album} (Year)/`,
    embed manifest tags + cover.jpg in each track, and persist the plan +
    `music_relpath` back into `album.json`. Idempotent on re-run — clears
    any prior music dir first (including the case where artist/album tags
    changed and the music_relpath moved).

    Preconditions (caller's responsibility):
      - `req.album_id` is a valid, existing album id (validated by the
        route's `_require_album` helper); `manifest` is its (reconciled)
        manifest.

    Returns `{music_relpath, tracks}` on success.

    Raises one of the `Split*Error` types listed at module top for the
    typed failure cases; the route maps each to its HTTP status."""
    if not req.tracks:
        raise SplitValidationError("no tracks given")
    # Defence in depth — the UI's <select> only offers values from
    # ALLOWED_SPLIT_SAMPLE_RATES, but a hand-crafted POST mustn't be able
    # to slip an arbitrary -ar through to ffmpeg.
    if req.sample_rate not in ALLOWED_SPLIT_SAMPLE_RATES:
        raise SplitValidationError(
            f"unsupported sample_rate {req.sample_rate}; "
            f"allowed: {sorted(ALLOWED_SPLIT_SAMPLE_RATES)}"
        )
    if req.output_format not in ALLOWED_OUTPUT_FORMATS:
        raise SplitValidationError(
            f"unsupported output_format {req.output_format!r}; "
            f"allowed: {sorted(ALLOWED_OUTPUT_FORMATS)}"
        )

    try:
        playlist, side_paths = albums_fs.album_concat_playlist(req.album_id)
    except FileNotFoundError as e:
        raise SplitNotFoundError(str(e))

    src_bytes = sum(p.stat().st_size for p in side_paths)
    err = disk_space_error(max(LOW_SPACE_GB, src_bytes / 1e9 + 0.5), "split")
    if err:
        playlist.unlink(missing_ok=True)
        raise SplitDiskSpaceError(err)

    total = sum((flac_duration_seconds(p) or 0.0) for p in side_paths)
    if total <= 0:
        playlist.unlink(missing_ok=True)
        raise SplitProcessingError("could not read source duration")

    src_fmt: dict = {}
    try:
        # Bit depth comes from the first side; the recorder produces a
        # single-format upstream so all sides share it. Used for the
        # `apply_aformat` optimization below — skip aformat when the user
        # asked for the same bit depth already on disk.
        out = subprocess.check_output(
            ["metaflac", "--show-bps", "--show-sample-rate", str(side_paths[0])],
            stderr=subprocess.DEVNULL, text=True,
        ).split()
        if len(out) >= 1:
            src_fmt["bit_depth"] = int(out[0])
    except Exception:
        pass

    src_bits = src_fmt.get("bit_depth")
    gain_db = 0.0
    if req.normalize and req.measured_peak_db is not None:
        gain_db = req.target_peak_db - req.measured_peak_db
    apply_gain = req.normalize and abs(gain_db) >= 0.01
    apply_aformat = req.bit_depth in (16, 24) and req.bit_depth != src_bits
    sample_fmt = {16: "s16", 24: "s32"}.get(req.bit_depth) if apply_aformat else None
    # 0 = keep source rate (skip resample). When set, route adds `-ar
    # <rate>` to the per-track encode and prepends a SoX-resampler aresample
    # filter for high-quality conversion; alpine's ffmpeg (see Dockerfile)
    # ships with libsoxr enabled.
    target_rate = req.sample_rate if req.sample_rate else None

    tags = manifest.get("tags") or {}
    music_dir, relpath = albums_fs.music_dir_for(tags)
    prior_relpath = manifest.get("music_relpath")
    wipe_prior_music_dir(prior_relpath, relpath)
    music_dir.mkdir(parents=True, exist_ok=True)
    for ext in _AUDIO_EXTS:
        for old in music_dir.glob(f"*{ext}"):
            try: old.unlink()
            except Exception: pass

    cover_file = albums_fs.cover_path(req.album_id)
    # Drop a folder-level cover.jpg next to the tracks. Music servers read
    # folder art directly, and — crucially — non-FLAC outputs get NO embedded
    # art (only the FLAC path embeds via metaflac), so without this a WAV/MP3/
    # AAC album would have no cover at all. The in-progress cover lives under
    # in-progress/<id>/ which the server never scans, so it has to be copied.
    if cover_file:
        try:
            shutil.copyfile(cover_file, music_dir / "cover.jpg")
        except OSError:
            pass
    out_dur_total = kept_duration_total(req.tracks, total) or 1.0
    out_total = sum(1 for t in req.tracks if not t.skip)
    pad = max(2, len(str(out_total)))
    start_job(req.job_id, "split")

    created: list[dict] = []
    cursor = 0.0
    out_idx = 0
    progress_acc = 0.0

    try:
        for i, t in enumerate(req.tracks, start=1):
            start_ = cursor
            end_ = total if i == len(req.tracks) else min(total, start_ + max(0.0, t.duration_seconds))
            cursor = end_
            if end_ <= start_ or t.skip:
                continue
            out_idx += 1
            track_dur = end_ - start_
            slice_a = progress_acc / out_dur_total
            slice_b = (progress_acc + track_dur) / out_dur_total
            progress_acc += track_dur
            entry = await _emit_track(
                req=req, t=t, i=i, out_idx=out_idx, out_total=out_total,
                pad=pad, music_dir=music_dir, playlist=playlist,
                start_=start_, end_=end_, apply_gain=apply_gain,
                gain_db=gain_db, sample_fmt=sample_fmt,
                target_rate=target_rate, tags=tags,
                cover_file=cover_file, slice_range=(slice_a, slice_b),
            )
            created.append(entry)
    finally:
        playlist.unlink(missing_ok=True)

    # ReplayGain is a post-encode tag pass over the finished FLACs — one
    # metaflac call computes per-track + shared album gain. FLAC only
    # (metaflac is the writer); lossy/WAV/ALAC outputs skip it.
    if req.replaygain and req.output_format == "flac" and created:
        track_paths = [music_dir / e["filename"] for e in created]
        await asyncio.to_thread(add_replay_gain, track_paths)

    _persist_split_plan(req, relpath)
    finish_job(req.job_id)
    return {"music_relpath": relpath, "tracks": created}
