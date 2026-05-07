"""Album endpoints — list, combine, delete, demote, sides reorder, waveform,
silence detect, measure, split, per-track download.

Albums are folders under `in-progress/<album_id>/` (see services/albums_fs.py).
Every endpoint here keys on `album_id` rather than a filename. Editor-facing
ffmpeg endpoints (waveform, silences, measure, split) all run against a
single `.cache/concat.flac` rendered on demand from the manifest's `sides`
list, so the underlying audio is never duplicated."""
import asyncio
import re
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from services import albums_fs
from services.ffmpeg import (
    LOW_SPACE_GB, disk_space_error, flac_duration_seconds, parse_astats,
    parse_silencedetect, run_ffmpeg_with_progress, safe_path_component,
)
from services.jobs import finish_job, start_job
from state import (
    CombineRequest, MUSIC_DIR, MeasureRequest, PlanUpdateRequest,
    PromoteRequest, ReorderSidesRequest, SilenceDetectRequest, SplitRequest,
)

router = APIRouter()


def _require_album(album_id: str) -> dict:
    """Validate the album_id, fetch its (reconciled) manifest, and 404 if
    the album dir doesn't exist. Centralizes the validation so each route
    stays a thin wrapper."""
    if not albums_fs.is_valid_album_id(album_id):
        raise HTTPException(404, "album not found")
    if not albums_fs.album_dir(album_id).is_dir():
        raise HTTPException(404, "album not found")
    return albums_fs.reconcile_sides(album_id)


def _music_dir_for(tags: dict) -> tuple[Path, str]:
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


# ── Listing / lifecycle ──────────────────────────────────────────────────

@router.get("/api/albums")
async def get_albums():
    return {"albums": albums_fs.list_albums()}


@router.post("/api/combine")
async def combine_album(req: CombineRequest):
    """Promote N raw sides into a new in-progress album. Metadata-only —
    no ffmpeg encode. Sides are MOVED out of `raw/` into the album dir
    (preserving original filenames; uniquify on collision); tags from the
    request body land in `album.json` only. Returns the new `album_id` plus
    a duration sum so the UI can show "✓ Combined N sides · MMm SSs"."""
    if not req.filenames:
        raise HTTPException(400, "need at least one side to combine")
    tags = {k: v for k, v in (req.album.dict() if req.album else {}).items()
            if v not in ("", None)}
    try:
        album_id, manifest = albums_fs.create_album(req.filenames, tags)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    d = albums_fs.album_dir(album_id)
    total_dur = sum((flac_duration_seconds(d / s) or 0.0)
                    for s in manifest["sides"])
    size_mb = round(
        sum((d / s).stat().st_size for s in manifest["sides"]) / 1e6, 1,
    )
    return {
        "ok":               True,
        "album_id":         album_id,
        "duration_seconds": total_dur,
        "size_mb":          size_mb,
    }


@router.post("/api/promote")
async def promote_album(req: PromoteRequest):
    """Single-side promote — exactly the N=1 case of combine. Kept as a
    separate endpoint so the row-level "promote" button has a one-shot
    handler."""
    return await combine_album(CombineRequest(
        filenames=[req.filename],
        album=req.album,
    ))


@router.delete("/api/albums/{album_id}")
async def delete_album(album_id: str):
    """Remove the in-progress dir AND the music subtree (if split)."""
    if not albums_fs.is_valid_album_id(album_id):
        raise HTTPException(404)
    if not albums_fs.album_dir(album_id).is_dir():
        raise HTTPException(404)
    albums_fs.delete_album(album_id)
    return {"ok": True}


@router.post("/api/album/{album_id}/demote")
async def demote_album(album_id: str):
    """Move every side back to `raw/` and remove the album dir. The music
    subtree is preserved if the album was already split — the UI confirm
    dialog warns about this so the user is never surprised."""
    _require_album(album_id)
    return {"ok": True, **albums_fs.demote_album(album_id)}


@router.post("/api/album/{album_id}/plan")
async def update_plan(album_id: str, req: PlanUpdateRequest):
    """Persist editor draft state to `album.json.plan` without running the
    split. The wave-editor calls this on a debounced timer as the user
    edits cuts/titles/skip flags so their work survives a tab close, a
    page reload, or moving to a different browser. The shape lines up
    with what `/api/album/split` already writes after a successful run —
    `music_relpath` (the "split has been emitted" signal) is left
    untouched here. Only the editor's intent gets written."""
    _require_album(album_id)
    manifest = albums_fs.read_manifest(album_id)
    plan = dict(manifest.get("plan") or {})
    plan["tracks"] = [
        {"title": t.title, "duration_seconds": t.duration_seconds, "skip": t.skip}
        for t in req.tracks
    ]
    if req.normalize        is not None: plan["normalize"]        = req.normalize
    if req.target_peak_db   is not None: plan["target_peak_db"]   = req.target_peak_db
    if req.measured_peak_db is not None: plan["measured_peak_db"] = req.measured_peak_db
    if req.bit_depth        is not None: plan["bit_depth"]        = req.bit_depth
    manifest["plan"] = plan
    albums_fs.write_manifest(album_id, manifest)
    return {"ok": True, "plan": plan}


@router.post("/api/album/{album_id}/sides/reorder")
async def reorder_sides(album_id: str, req: ReorderSidesRequest):
    """Persist a permutation of `sides[]`. Forces the editor's concat cache
    to regenerate on next render."""
    _require_album(album_id)
    try:
        manifest = albums_fs.reorder_sides(album_id, req.sides)
    except ValueError as e:
        raise HTTPException(400, str(e))
    albums_fs.invalidate_concat_cache(album_id)
    return {"ok": True, "sides": manifest["sides"]}


# ── Wave-editor source: concat cache ─────────────────────────────────────

async def _ensure_cache(album_id: str, job_id: Optional[str] = None) -> Path:
    """asyncio wrapper around the cache builder so the route doesn't block
    the event loop while ffmpeg is concatenating sides."""
    try:
        return await asyncio.to_thread(
            albums_fs.ensure_concat_cache, album_id, job_id,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@router.get("/api/album/{album_id}/waveform")
async def album_waveform(album_id: str, w: int = 2400, h: int = 120,
                         start: float = 0.0, end: float = 0.0,
                         job_id: str = ""):
    """Render a PNG waveform of the album's concat cache. The full-album
    view is cached on disk (PNG keyed by cache mtime); zoomed views
    render to a temp file and stream back."""
    _require_album(album_id)
    src = await _ensure_cache(album_id, job_id or None)
    w = max(400, min(8000, int(w)))
    h = max(40,  min(400,  int(h)))
    total = flac_duration_seconds(src) or 0.0

    full_view = (end <= 0.0) or (start <= 0.0 and end >= total - 0.5)
    if full_view:
        cache_dir = albums_fs.album_dir(album_id) / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        png = cache_dir / f"waveform.{w}x{h}.png"
        if not png.exists() or png.stat().st_mtime < src.stat().st_mtime:
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-i", str(src),
                   "-filter_complex",
                   f"showwavespic=s={w}x{h}:colors=0x6db3ff:scale=lin:split_channels=0",
                   "-frames:v", "1",
                   str(png)]
            start_job(job_id, "waveform")
            rc, stderr = await asyncio.to_thread(
                run_ffmpeg_with_progress, cmd, total, job_id, (0.0, 1.0), "rendering",
            )
            if rc != 0:
                err = (stderr or b"").decode(errors="replace")[:300]
                finish_job(job_id, error=err)
                raise HTTPException(500, f"waveform render failed: {err}")
            finish_job(job_id)
        else:
            start_job(job_id, "waveform")
            finish_job(job_id)
        return FileResponse(str(png), media_type="image/png")

    # Zoomed view — render to a temp file, no cache.
    s = max(0.0, float(start))
    e = min(total, float(end)) if total else float(end)
    if e <= s + 0.05:
        raise HTTPException(400, "zoom range too small")
    tmp = Path(f"/tmp/wf_{uuid.uuid4().hex[:8]}.png")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-ss", f"{s:.3f}", "-to", f"{e:.3f}",
           "-i", str(src),
           "-filter_complex",
           f"showwavespic=s={w}x{h}:colors=0x6db3ff:scale=lin:split_channels=0",
           "-frames:v", "1",
           str(tmp)]
    r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True)
    if r.returncode != 0:
        err = (r.stderr or b"").decode(errors="replace")[:300]
        raise HTTPException(500, f"waveform render failed: {err}")
    data = tmp.read_bytes()
    try: tmp.unlink()
    except Exception: pass
    return StreamingResponse(iter([data]), media_type="image/png")


@router.post("/api/album/detect-silences")
async def detect_silences(req: SilenceDetectRequest):
    _require_album(req.album_id)
    src = await _ensure_cache(req.album_id, req.job_id)
    total_dur = flac_duration_seconds(src) or 0.0
    cmd = ["ffmpeg", "-hide_banner", "-i", str(src),
           "-af", f"silencedetect=noise={req.noise_db}dB:d={req.min_silence}",
           "-f", "null", "-"]
    start_job(req.job_id, "detect silences")
    rc, stderr = await asyncio.to_thread(
        run_ffmpeg_with_progress, cmd, total_dur, req.job_id, (0.0, 1.0), "scanning",
    )
    if rc != 0:
        err = (stderr or b"").decode(errors="replace")[:300]
        finish_job(req.job_id, error=err)
        raise HTTPException(500, f"ffmpeg failed: {err}")
    finish_job(req.job_id)
    silences = parse_silencedetect((stderr or b"").decode(errors="replace"))
    return {"silences": silences}


def _astats_filter_for_ranges(ranges: Optional[list[list[float]]]) -> str:
    """Build an ffmpeg -af expression that runs `astats` over the union of the
    given [start, end] ranges (seconds). When `ranges` is empty/None, astats
    sees the whole input. Multiple ranges are concatenated so peak / RMS_trough
    are aggregated across them in one pass."""
    measure = "astats=measure_overall=Peak_level+RMS_trough:measure_perchannel=Peak_level+RMS_trough"
    if not ranges:
        return measure
    parts = []
    for i, (s, e) in enumerate(ranges):
        if e <= s:
            continue
        parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[r{i}]")
    if not parts:
        return measure
    refs = "".join(f"[r{i}]" for i, (s, e) in enumerate(ranges) if e > s)
    n = refs.count("[")
    if n == 1:
        return ";".join(parts) + f";{refs}{measure}"
    return ";".join(parts) + f";{refs}concat=n={n}:v=0:a=1,{measure}"


@router.post("/api/album/measure")
async def measure_album(req: MeasureRequest):
    _require_album(req.album_id)
    src = await _ensure_cache(req.album_id, req.job_id)
    af = _astats_filter_for_ranges(req.included_ranges)
    flag = "-filter_complex" if (req.included_ranges) else "-af"
    cmd = ["ffmpeg", "-hide_banner", "-i", str(src),
           flag, af, "-f", "null", "-"]
    if req.included_ranges:
        total_dur = sum(max(0.0, e - s) for s, e in req.included_ranges)
    else:
        total_dur = flac_duration_seconds(src) or 0.0
    start_job(req.job_id, "measure")
    rc, stderr = await asyncio.to_thread(
        run_ffmpeg_with_progress, cmd, total_dur, req.job_id, (0.0, 1.0), "analysing",
    )
    if rc != 0:
        err = (stderr or b"").decode(errors="replace")[:300]
        finish_job(req.job_id, error=err)
        raise HTTPException(500, f"astats failed: {err}")
    finish_job(req.job_id)
    stats = parse_astats((stderr or b"").decode(errors="replace"))
    peak = stats.get("peak_db")
    noise = stats.get("noise_floor_db")
    dr = (peak - noise) if (peak is not None and noise is not None) else None
    bits = (dr / 6.02) if dr is not None else None
    return {
        "peak_db":          peak,
        "noise_floor_db":   noise,
        "dynamic_range_db": dr,
        "effective_bits":   bits,
    }


# ── Split / track endpoints ──────────────────────────────────────────────

@router.post("/api/album/split")
async def split_album(req: SplitRequest):
    """Cut the concat cache into per-track FLACs in `music/{Artist}/{Album} (Year)/`,
    embed manifest tags + cover.jpg in each track, and persist the plan +
    `music_relpath` back into `album.json`. Idempotent on re-run — clears
    any prior music dir first (including the case where artist/album tags
    changed and the music_relpath moved)."""
    manifest = _require_album(req.album_id)
    if not req.tracks:
        raise HTTPException(400, "no tracks given")

    src = await _ensure_cache(req.album_id, req.job_id)
    src_gb = src.stat().st_size / 1e9
    err = disk_space_error(max(LOW_SPACE_GB, src_gb + 0.5), "split")
    if err:
        raise HTTPException(507, err)

    total = flac_duration_seconds(src) or 0.0
    if total <= 0:
        raise HTTPException(500, "could not read source duration")

    src_fmt: dict = {}
    try:
        # Reuse flac_format imported above? It's not in the import list yet —
        # use the same metaflac probe inline to keep the dependency surface
        # tight. The bit_depth/sample_rate values are only consulted for the
        # apply_aformat optimization below.
        out = subprocess.check_output(
            ["metaflac", "--show-bps", "--show-sample-rate", str(src)],
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

    tags = manifest.get("tags") or {}
    music_dir, relpath = _music_dir_for(tags)
    prior_relpath = manifest.get("music_relpath")
    if prior_relpath and prior_relpath != relpath:
        prior_dir = MUSIC_DIR / prior_relpath
        if prior_dir.is_dir():
            for old in prior_dir.glob("*.flac"):
                try: old.unlink()
                except Exception: pass
            try: prior_dir.rmdir()
            except Exception: pass
            try:
                if prior_dir.parent != MUSIC_DIR and not any(prior_dir.parent.iterdir()):
                    prior_dir.parent.rmdir()
            except Exception:
                pass

    music_dir.mkdir(parents=True, exist_ok=True)
    for old in music_dir.glob("*.flac"):
        try: old.unlink()
        except Exception: pass

    cover_file = albums_fs.cover_path(req.album_id)

    # Pre-walk the tracks to compute the slice range for the progress bar
    # so the bar advances at a constant rate across the whole split, not
    # per-track. Walk twice with the same cursor logic.
    _cur = 0.0
    _kept = 0.0
    for _i, _t in enumerate(req.tracks, start=1):
        _s = _cur
        _e = total if _i == len(req.tracks) else min(total, _s + max(0.0, _t.duration_seconds))
        _cur = _e
        if _e > _s and not _t.skip:
            _kept += _e - _s
    out_dur_total = _kept or 1.0
    start_job(req.job_id, "split")

    created: list[dict] = []
    cursor = 0.0
    out_total = sum(1 for t in req.tracks if not t.skip)
    pad = max(2, len(str(out_total)))
    out_idx = 0
    progress_acc = 0.0

    for i, t in enumerate(req.tracks, start=1):
        start_ = cursor
        end_ = min(total, start_ + max(0.0, t.duration_seconds))
        if i == len(req.tracks):
            end_ = total
        cursor = end_
        if end_ <= start_:
            continue
        if t.skip:
            continue   # advance cursor only — region is dropped from output
        out_idx += 1
        track_name = f"{str(out_idx).zfill(pad)} - {safe_path_component(t.title) or 'Track'}.flac"
        out = music_dir / track_name
        af = []
        if apply_gain:
            af.append(f"volume={gain_db:.4f}dB")
        if sample_fmt:
            af.append(f"aformat=sample_fmts={sample_fmt}")
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-ss", f"{start_:.3f}", "-to", f"{end_:.3f}",
               "-i", str(src)]
        if af:
            cmd += ["-af", ",".join(af)]
        cmd += ["-c:a", "flac", "-compression_level", "5",
                "-map_metadata", "-1", str(out)]
        track_dur = end_ - start_
        slice_a = progress_acc / out_dur_total
        slice_b = (progress_acc + track_dur) / out_dur_total
        progress_acc += track_dur
        rc, stderr = await asyncio.to_thread(
            run_ffmpeg_with_progress, cmd, track_dur, req.job_id,
            (slice_a, slice_b), f"track {out_idx}/{out_total}",
        )
        if rc != 0:
            err = (stderr or b"").decode(errors="replace")[:300]
            finish_job(req.job_id, error=err)
            raise HTTPException(500, f"ffmpeg failed on track {i}: {err}")
        # Tags + cover are committed HERE — only at the music/ emit step.
        tag_args = ["metaflac", "--remove-all-tags",
                    f"--set-tag=ARTIST={tags.get('artist', '')}",
                    f"--set-tag=ALBUM={tags.get('album', '')}",
                    f"--set-tag=DATE={tags.get('year', '')}",
                    f"--set-tag=GENRE={tags.get('genre', '')}",
                    f"--set-tag=LABEL={tags.get('label', '')}",
                    f"--set-tag=CATALOGNUMBER={tags.get('catalog_number', '')}",
                    f"--set-tag=RELEASECOUNTRY={tags.get('country', '')}",
                    f"--set-tag=TITLE={t.title}",
                    f"--set-tag=TRACKNUMBER={out_idx}",
                    f"--set-tag=TRACKTOTAL={out_total}",
                    str(out)]
        if tags.get("musicbrainz_albumid"):
            tag_args.insert(-1, f"--set-tag=MUSICBRAINZ_ALBUMID={tags['musicbrainz_albumid']}")
        if tags.get("discogs_release_id"):
            tag_args.insert(-1, f"--set-tag=DISCOGS_RELEASE_ID={tags['discogs_release_id']}")
        subprocess.run(tag_args, check=False, stderr=subprocess.DEVNULL)
        if cover_file:
            subprocess.run(
                ["metaflac", f"--import-picture-from={cover_file}", str(out)],
                check=False, stderr=subprocess.DEVNULL,
            )
        created.append({
            "filename":         track_name,
            "duration_seconds": end_ - start_,
            "size_mb":          round(out.stat().st_size / 1e6, 1),
        })

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
    }
    manifest = albums_fs.read_manifest(req.album_id)
    manifest["plan"] = plan
    manifest["music_relpath"] = relpath
    albums_fs.write_manifest(req.album_id, manifest)

    finish_job(req.job_id)
    return {"ok": True, "music_relpath": relpath, "tracks": created}


@router.get("/api/album/{album_id}/tracks")
async def album_tracks(album_id: str):
    """List the tracks an album was split into. Titles, durations and skip
    flags come from `album.json`'s `plan`; the per-track FLACs live under
    `music/{music_relpath}/`. Used by the wave editor's re-edit reload
    path and by the Music section's "expand to show tracks" affordance."""
    manifest = _require_album(album_id)
    plan = manifest.get("plan")
    if not plan:
        return {"tracks": [], "music_relpath": None, "plan": None}
    music_relpath = manifest.get("music_relpath") or ""
    music_dir = MUSIC_DIR / music_relpath if music_relpath else None
    kept = [t for t in plan.get("tracks", []) if not t.get("skip")]
    out_total = len(kept)
    pad = max(2, len(str(out_total)))
    tracks = []
    for i, t in enumerate(kept, start=1):
        title = t.get("title", "") or "Track"
        track_name = f"{str(i).zfill(pad)} - {safe_path_component(title) or 'Track'}.flac"
        f = music_dir / track_name if music_dir else None
        size_mb = duration = None
        if f and f.exists():
            size_mb = round(f.stat().st_size / 1e6, 1)
            duration = flac_duration_seconds(f)
        tracks.append({
            "filename":         track_name,
            "title":            title,
            "track_number":     i,
            "duration_seconds": duration if duration is not None else float(t.get("duration_seconds") or 0.0),
            "size_mb":          size_mb,
        })
    return {
        "tracks":         tracks,
        "music_relpath":  music_relpath or None,
        "plan":           plan,
    }


_TRACKNAME_RE = re.compile(r"^[^/\\]+\.flac$")


@router.get("/api/album/{album_id}/track/{trackname}")
async def download_track(album_id: str, trackname: str):
    manifest = _require_album(album_id)
    if not _TRACKNAME_RE.fullmatch(trackname) or ".." in trackname:
        raise HTTPException(400)
    relpath = manifest.get("music_relpath") or ""
    if not relpath:
        raise HTTPException(404)
    p = MUSIC_DIR / relpath / trackname
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="audio/flac", filename=trackname)
