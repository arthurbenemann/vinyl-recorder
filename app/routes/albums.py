"""Combined-album list, combine, delete, waveform, silence detect, split,
and per-track download."""
import asyncio
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from services.ffmpeg import (
    LOW_SPACE_GB, disk_space_error, find_file, flac_duration_seconds,
    flac_format, list_albums, parse_astats, parse_silencedetect, read_tags,
    run_ffmpeg_with_progress, safe_name, write_tags,
)
from services.jobs import finish_job, start_job
from state import (
    ALBUMS_DIR, CombineRequest, MeasureRequest, PromoteRequest,
    SilenceDetectRequest, SplitRequest, TAGGED_DIR, UNTAGGED_DIR,
)

router = APIRouter()


@router.get("/api/albums")
async def get_albums():
    return {"albums": list_albums()}


@router.post("/api/combine")
async def combine_album(req: CombineRequest):
    """Concatenate the given side recordings (in order) into a single FLAC in
    albums/, copy tags from the request, and embed cover art if any source
    side has one. Sources are left in place — they remain in the library so
    they can be re-combined or split later."""
    if len(req.filenames) < 2:
        raise HTTPException(400, "need at least 2 sides to combine")

    paths: list[Path] = []
    for fn in req.filenames:
        p = find_file(fn)
        if not p or p.parent == ALBUMS_DIR:
            raise HTTPException(404, f"side not found: {fn}")
        paths.append(p)

    # Output is roughly the sum of input sizes (lossless re-encode); demand a
    # small safety margin so writes don't fail near the end.
    total_in_gb = sum(p.stat().st_size for p in paths) / 1e9
    err = disk_space_error(max(LOW_SPACE_GB, total_in_gb + 0.5), "combine")
    if err:
        raise HTTPException(507, err)

    artist = (req.album.artist or "").strip() or "Unknown"
    album  = (req.album.album  or "").strip() or datetime.now().strftime("%Y%m%d_%H%M%S")
    year   = (req.album.year   or "").strip() or datetime.now().strftime("%Y")
    out_name = f"{safe_name(artist)} - {safe_name(album)} ({year}).flac"
    out_path = ALBUMS_DIR / out_name
    if out_path.exists():
        stem = out_path.stem
        i = 2
        while True:
            cand = ALBUMS_DIR / f"{stem} ({i}).flac"
            if not cand.exists():
                out_path = cand
                break
            i += 1

    # ffmpeg's concat demuxer needs a tiny playlist file.
    playlist = ALBUMS_DIR / f".combine_{uuid.uuid4().hex[:8]}.txt"
    # Total output duration ≈ sum of input durations; drives the progress bar.
    total_dur = sum((flac_duration_seconds(p) or 0.0) for p in paths)
    start_job(req.job_id, "combine")
    try:
        playlist.write_text(
            "".join(f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for p in paths)
        )
        # FLAC's `-c copy` concat preserves only the first input's STREAMINFO
        # total_samples, so most players stop after the first side even though
        # all the audio data is in the file. Always re-encode.
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0",
               "-i", str(playlist),
               "-c:a", "flac", "-compression_level", "8",
               "-map_metadata", "-1",
               str(out_path)]
        rc, stderr = await asyncio.to_thread(
            run_ffmpeg_with_progress, cmd, total_dur, req.job_id, (0.0, 1.0), "encoding",
        )
        if rc != 0:
            err = (stderr or b"").decode(errors="replace")[:500]
            finish_job(req.job_id, error=err)
            raise HTTPException(500, f"ffmpeg failed: {err}")
    finally:
        try: playlist.unlink()
        except Exception: pass
    finish_job(req.job_id)

    # Tag the result.
    tag_fields = {k: v for k, v in req.album.dict().items() if v is not None}
    write_tags(out_path, tag_fields)
    subprocess.run(
        ["metaflac",
         "--remove-tag=SIDECOUNT", f"--set-tag=SIDECOUNT={len(paths)}",
         str(out_path)],
        check=False, stderr=subprocess.DEVNULL,
    )

    # Inherit cover art from the first side that has one.
    for src in paths:
        try:
            pic = subprocess.run(
                ["metaflac", "--list", "--block-type=PICTURE", str(src)],
                capture_output=True, text=True, check=False,
            )
            if pic.stdout and "type: 3 (Cover (front))" in pic.stdout:
                tmp = Path(f"/tmp/cover_{uuid.uuid4().hex[:8]}.jpg")
                ex = subprocess.run(
                    ["metaflac", f"--export-picture-to={tmp}", str(src)],
                    capture_output=True, check=False,
                )
                if ex.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    subprocess.run(["metaflac", f"--import-picture-from={tmp}", str(out_path)],
                                   check=False, stderr=subprocess.DEVNULL)
                    try: tmp.unlink()
                    except Exception: pass
                    break
        except Exception:
            continue

    stat = out_path.stat()
    return {
        "ok": True,
        "filename": out_path.name,
        "size_mb": round(stat.st_size / 1e6, 1),
        "duration_seconds": flac_duration_seconds(out_path),
    }


@router.post("/api/promote")
async def promote_album(req: PromoteRequest):
    """Promote a single side recording (tagged or untagged) to album status by
    moving the FLAC into albums/, applying the supplied tags, and marking it as
    a one-side album. The source is removed from the library — promote is a
    one-way operation, mirroring the way combined sides leave the side list.
    Cover art embedded in the source is preserved by the move."""
    src = find_file(req.filename)
    if not src or src.parent not in (TAGGED_DIR, UNTAGGED_DIR):
        raise HTTPException(404, f"recording not found: {req.filename}")

    artist = (req.album.artist or "").strip() or "Unknown"
    album  = (req.album.album  or "").strip() or datetime.now().strftime("%Y%m%d_%H%M%S")
    year   = (req.album.year   or "").strip() or datetime.now().strftime("%Y")
    out_name = f"{safe_name(artist)} - {safe_name(album)} ({year}).flac"
    out_path = ALBUMS_DIR / out_name
    if out_path.exists():
        stem = out_path.stem
        i = 2
        while True:
            cand = ALBUMS_DIR / f"{stem} ({i}).flac"
            if not cand.exists():
                out_path = cand
                break
            i += 1

    # Move (rename) keeps the FLAC bitstream and embedded cover art untouched
    # and is free on the same filesystem — no extra disk space needed and no
    # re-encode. ALBUMS_DIR is always under OUTPUT_DIR alongside the library.
    src.rename(out_path)

    tag_fields = {k: v for k, v in req.album.dict().items() if v is not None}
    write_tags(out_path, tag_fields)
    subprocess.run(
        ["metaflac",
         "--remove-tag=SIDECOUNT", "--set-tag=SIDECOUNT=1",
         str(out_path)],
        check=False, stderr=subprocess.DEVNULL,
    )

    stat = out_path.stat()
    return {
        "ok": True,
        "filename": out_path.name,
        "size_mb": round(stat.st_size / 1e6, 1),
        "duration_seconds": flac_duration_seconds(out_path),
    }


@router.delete("/api/albums/{filename}")
async def delete_album(filename: str):
    p = find_file(filename)
    if not p or p.parent != ALBUMS_DIR:
        raise HTTPException(404)
    p.unlink()
    sub = ALBUMS_DIR / p.stem
    if sub.is_dir():
        try:
            for f in sub.iterdir():
                f.unlink()
            sub.rmdir()
        except Exception:
            pass
    cache_dir = ALBUMS_DIR / ".cache"
    if cache_dir.is_dir():
        for f in cache_dir.glob(f"{p.stem}.*"):
            try: f.unlink()
            except Exception: pass
    return {"ok": True}


@router.get("/api/album/{filename}/waveform")
async def album_waveform(filename: str, w: int = 2400, h: int = 120,
                         start: float = 0.0, end: float = 0.0,
                         job_id: str = ""):
    """Render a PNG waveform of the album using ffmpeg's showwavespic. When
    start/end (seconds) are given, only that segment is rendered, enabling
    arbitrary zoom. The full-album view is cached on disk; zoomed views are
    rendered on demand (typically <1s for a minute-long segment)."""
    src = find_file(filename)
    if not src or src.parent != ALBUMS_DIR:
        raise HTTPException(404)
    w = max(400, min(8000, int(w)))
    h = max(40,  min(400,  int(h)))
    total = flac_duration_seconds(src) or 0.0

    # Treat 0/0 (or end<=start, or covering the whole file) as "full album",
    # which we cache aggressively because the editor reopens it often.
    full_view = (end <= 0.0) or (start <= 0.0 and end >= total - 0.5)
    if full_view:
        cache_dir = ALBUMS_DIR / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        png = cache_dir / f"{src.stem}.{w}x{h}.png"
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
            # Cache hit — still mark the job done so the client tears the bar
            # down promptly instead of waiting for a poll-404.
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
    src = find_file(req.filename)
    if not src or src.parent != ALBUMS_DIR:
        raise HTTPException(404, "album not found")
    total_dur = flac_duration_seconds(src) or 0.0
    # `-nostats` would otherwise hide the silencedetect=… messages we parse.
    # run_ffmpeg_with_progress injects -progress -nostats which is fine: the
    # silencedetect filter prints to stderr (parsed below), separate from the
    # progress channel on stdout.
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
        raise HTTPException(500, f"silencedetect failed: {err}")
    finish_job(req.job_id)
    return {
        "silences":       parse_silencedetect((stderr or b"").decode(errors="replace")),
        "total_duration": total_dur,
        "noise_db":       req.noise_db,
        "min_silence":    req.min_silence,
    }


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
    """Measure peak level and noise floor (RMS trough) using ffmpeg's `astats`
    filter. When `included_ranges` is given, only those segments are measured —
    this lets the caller exclude skipped regions (e.g. side flips with
    needle-drop pops) so the readout reflects the audio that will end up in
    the final tracks."""
    src = find_file(req.filename)
    if not src or src.parent != ALBUMS_DIR:
        raise HTTPException(404, "album not found")
    af = _astats_filter_for_ranges(req.included_ranges)
    flag = "-filter_complex" if (req.included_ranges) else "-af"
    cmd = ["ffmpeg", "-hide_banner", "-i", str(src),
           flag, af, "-f", "null", "-"]
    # When ranges are given astats sees the concat'd union, so progress is
    # driven by the sum of those ranges, not the album duration.
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
        "peak_db":           peak,
        "noise_floor_db":    noise,
        "dynamic_range_db":  dr,
        "effective_bits":    bits,
    }


@router.post("/api/album/split")
async def split_album(req: SplitRequest):
    src = find_file(req.filename)
    if not src or src.parent != ALBUMS_DIR:
        raise HTTPException(404, "album not found")
    if not req.tracks:
        raise HTTPException(400, "no tracks given")

    # Tracks are produced via `-c copy`, so total output size is at most the
    # source size; require a small margin so we don't fail mid-split.
    src_gb = src.stat().st_size / 1e9
    err = disk_space_error(max(LOW_SPACE_GB, src_gb + 0.5), "split")
    if err:
        raise HTTPException(507, err)

    total = flac_duration_seconds(src) or 0.0
    if total <= 0:
        raise HTTPException(500, "could not determine source duration")

    if req.bit_depth not in (0, 16, 24):
        raise HTTPException(400, "bit_depth must be 0, 16, or 24")
    if req.normalize and req.measured_peak_db is None:
        raise HTTPException(400, "normalize requires measured_peak_db")

    # Compute one gain value reused for every track — preserves relative
    # loudness across all sides of the album.
    gain_db: float = 0.0
    if req.normalize:
        gain_db = float(req.target_peak_db) - float(req.measured_peak_db)
    # Skip filters individually when they'd be no-ops: sub-0.01 dB gain is
    # inaudible, matching bit depth needs no aformat. Re-encoding still happens
    # unconditionally — `-c copy` would inherit the source STREAMINFO duration
    # and break the editor's "load existing split" path.
    src_bits = flac_format(src).get("bit_depth")
    apply_gain = req.normalize and abs(gain_db) >= 0.01
    apply_aformat = req.bit_depth in (16, 24) and req.bit_depth != src_bits
    sample_fmt = {16: "s16", 24: "s32"}.get(req.bit_depth) if apply_aformat else None

    album_tags = read_tags(src)
    out_dir = ALBUMS_DIR / src.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.flac"):
        try: old.unlink()
        except Exception: pass

    cover_tmp: Optional[Path] = Path(f"/tmp/cover_{uuid.uuid4().hex[:8]}.jpg")
    ex = subprocess.run(
        ["metaflac", f"--export-picture-to={cover_tmp}", str(src)],
        capture_output=True, check=False,
    )
    if ex.returncode != 0 or not cover_tmp.exists() or cover_tmp.stat().st_size == 0:
        cover_tmp = None

    # Map each non-skipped track onto a slice of [0, 1] proportional to its
    # duration so the bar advances at a constant rate across the whole split,
    # not per-track. Walk the tracks once with the same cursor logic the main
    # loop uses, so the totals line up exactly.
    _cur = float(req.offset_seconds or 0.0)
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
    cursor = float(req.offset_seconds or 0.0)
    out_total = sum(1 for t in req.tracks if not t.skip)
    pad = max(2, len(str(out_total)))
    out_idx = 0
    progress_acc = 0.0
    try:
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
            track_name = f"{str(out_idx).zfill(pad)} - {safe_name(t.title) or 'Track'}.flac"
            out = out_dir / track_name
            # Always re-encode: ffmpeg's stream copy preserves the source's
            # STREAMINFO, so every split track would advertise the full album
            # duration via metaflac, breaking `flac_duration_seconds` and the
            # editor's "load existing split" path. Skip per-filter when it'd
            # be a no-op so the default normalize/bit-depth knobs don't add
            # cost when they don't change anything.
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
            tag_args = ["metaflac", "--remove-all-tags",
                        f"--set-tag=ARTIST={album_tags.get('ARTIST', '')}",
                        f"--set-tag=ALBUM={album_tags.get('ALBUM', '')}",
                        f"--set-tag=DATE={album_tags.get('DATE', '')}",
                        f"--set-tag=GENRE={album_tags.get('GENRE', '')}",
                        f"--set-tag=LABEL={album_tags.get('LABEL', '')}",
                        f"--set-tag=CATALOGNUMBER={album_tags.get('CATALOGNUMBER', '')}",
                        f"--set-tag=RELEASECOUNTRY={album_tags.get('RELEASECOUNTRY', '')}",
                        f"--set-tag=TITLE={t.title}",
                        f"--set-tag=TRACKNUMBER={out_idx}",
                        f"--set-tag=TRACKTOTAL={out_total}",
                        str(out)]
            subprocess.run(tag_args, check=False, stderr=subprocess.DEVNULL)
            if cover_tmp:
                subprocess.run(["metaflac", f"--import-picture-from={cover_tmp}", str(out)],
                               check=False, stderr=subprocess.DEVNULL)
            created.append({
                "filename":         track_name,
                "duration_seconds": end_ - start_,
                "size_mb":          round(out.stat().st_size / 1e6, 1),
            })
    finally:
        if cover_tmp:
            try: cover_tmp.unlink()
            except Exception: pass

    finish_job(req.job_id)
    return {"ok": True, "subdir": src.stem, "tracks": created}


@router.get("/api/album/{filename}/tracks")
async def album_tracks(filename: str):
    src = find_file(filename)
    if not src or src.parent != ALBUMS_DIR:
        raise HTTPException(404)
    sub = ALBUMS_DIR / src.stem
    if not sub.is_dir():
        return {"tracks": []}
    tracks = []
    for f in sorted(sub.glob("*.flac")):
        tags = read_tags(f)
        tracks.append({
            "filename":         f.name,
            "title":            tags.get("TITLE", f.stem),
            "track_number":     int(tags.get("TRACKNUMBER", "0") or 0),
            "duration_seconds": flac_duration_seconds(f),
            "size_mb":          round(f.stat().st_size / 1e6, 1),
        })
    return {"tracks": tracks, "subdir": src.stem}


@router.get("/api/album/{filename}/track/{trackname}")
async def download_track(filename: str, trackname: str):
    src = find_file(filename)
    if not src or src.parent != ALBUMS_DIR:
        raise HTTPException(404)
    if "/" in trackname or "\\" in trackname or ".." in trackname:
        raise HTTPException(400)
    p = ALBUMS_DIR / src.stem / trackname
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="audio/flac", filename=trackname)
