"""Filesystem + FLAC tag helpers backed by metaflac/ffmpeg subprocess calls.
Pure functions — no FastAPI deps — so they can be reused from routes and from
the higher-level album/recording listing helpers. Album-folder logic lives
in `services/albums_fs.py`; this file just deals with raw side files and
tag/format helpers shared across the codebase."""
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from services.jobs import update_job
from state import OUTPUT_DIR, RAW_DIR


def run_ffmpeg_with_progress(
    cmd: list[str],
    total_sec: float,
    job_id: Optional[str] = None,
    phase_range: tuple[float, float] = (0.0, 1.0),
    phase_label: Optional[str] = None,
) -> tuple[int, bytes]:
    """Run ffmpeg as a subprocess with `-progress pipe:1` injected so we can
    track its position by parsing `out_time_us` lines, and push the fraction
    into the jobs registry as it advances. Returns (returncode, stderr_bytes)
    — same shape as `subprocess.run(..., capture_output=True)` callers expect,
    minus stdout (which we consume as the progress channel).

    `phase_range = (a, b)` maps this single ffmpeg call onto a slice of an
    aggregate job, so a multi-step op (split: N tracks; combine: 1 pass)
    advances a single overall bar.
    """
    if total_sec is None or total_sec <= 0 or not job_id:
        # No bar to drive — fall back to a plain blocking run that still
        # captures stderr so callers can surface ffmpeg errors.
        r = subprocess.run(cmd, capture_output=True)
        if job_id:
            update_job(job_id, phase_range[1], phase=phase_label)
        return r.returncode, r.stderr or b""

    # Inject `-progress pipe:1 -nostats` right after the ffmpeg binary token so
    # any caller-supplied -loglevel / -hide_banner / -y still take effect.
    inj = ["-progress", "pipe:1", "-nostats"]
    full_cmd = [cmd[0], *inj, *cmd[1:]]

    proc = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_chunks: list[bytes] = []

    def _drain_stderr():
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
        except Exception:
            pass

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    a, b = phase_range
    last_overall = -1.0
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace").strip()
            # ffmpeg emits both keys; out_time_us is always microseconds.
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                frac = max(0.0, min(1.0, (us / 1e6) / total_sec))
                overall = a + (b - a) * frac
                # Throttle updates so the lock isn't hammered.
                if overall - last_overall >= 0.005:
                    update_job(job_id, overall, phase=phase_label)
                    last_overall = overall
            elif line == "progress=end":
                break
    except Exception:
        pass

    proc.wait()
    t.join(timeout=2)
    update_job(job_id, b, phase=phase_label)
    return proc.returncode, b"".join(stderr_chunks)


def safe_name(s: str) -> str:
    return re.sub(r'[^\w\s\-\.]', '', s).strip().replace(' ', '_') or 'untitled'


def safe_path_component(s: str) -> str:
    """Sanitize a string for use as a single Jellyfin-friendly path component:
    keeps spaces, strips filesystem-hostile chars (`<>:"/\\|?*` + control
    chars). Used for `music/{Artist}/{Album} (Year)/` directory names."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', s).strip().rstrip('.')
    return s or 'Unknown'


LOW_SPACE_GB = 2.0  # below this threshold the UI marker turns red and writes are refused.


def disk_free_gb() -> float:
    st = shutil.disk_usage(str(OUTPUT_DIR))
    return round(st.free / 1e9, 1)


def disk_space_error(min_needed_gb: float, op: str) -> Optional[str]:
    """Return a user-facing error message if free space < `min_needed_gb`,
    else None. Used by the routes to short-circuit operations that would
    otherwise fail mid-write with cryptic ffmpeg/metaflac errors."""
    free = disk_free_gb()
    if free >= min_needed_gb:
        return None
    return (
        f"Not enough disk space for {op}: {free} GB free, "
        f"need at least {min_needed_gb:.1f} GB. Delete some recordings to free up space."
    )


def find_side(filename: str) -> Optional[Path]:
    """Locate a side recording in `raw/` by filename. Albums are folders now
    so they don't have a single-file handle — endpoints that need to address
    an album do so by `album_id` via `services/albums_fs.album_dir(...)`."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    p = RAW_DIR / filename
    return p if p.exists() else None


def flac_duration_seconds(path: Path) -> Optional[float]:
    """Return the playback duration of a FLAC in seconds, or None on failure."""
    try:
        out = subprocess.check_output(
            ["metaflac", "--show-total-samples", "--show-sample-rate", str(path)],
            stderr=subprocess.DEVNULL, text=True,
        ).split()
        if len(out) >= 2 and int(out[1]) > 0:
            return int(out[0]) / int(out[1])
    except Exception:
        pass
    return None


def flac_format(path: Path) -> dict:
    """Return {bit_depth, sample_rate_khz, channels} for a FLAC, or empty dict
    on failure. Surfaced in the library + albums listings, and used by the
    split flow to skip aformat when the requested bit depth already matches
    the source."""
    try:
        out = subprocess.check_output(
            ["metaflac", "--show-bps", "--show-sample-rate", "--show-channels", str(path)],
            stderr=subprocess.DEVNULL, text=True,
        ).split()
        if len(out) >= 3:
            return {
                "bit_depth":       int(out[0]),
                "sample_rate_khz": round(int(out[1]) / 1000.0, 1),
                "channels":        int(out[2]),
            }
    except Exception:
        pass
    return {}


def read_tags(path: Path) -> dict:
    try:
        out = subprocess.check_output(
            ["metaflac", "--export-tags-to=-", str(path)],
            stderr=subprocess.DEVNULL, text=True
        )
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    except Exception:
        return {}


TAG_KEY_MAP = {
    "artist":         "ARTIST",
    "album":          "ALBUM",
    "year":           "DATE",
    "genre":          "GENRE",
    "label":          "LABEL",
    "catalog_number": "CATALOGNUMBER",
    "country":        "RELEASECOUNTRY",
}


def write_tags(path: Path, fields: dict):
    """Replace the standard tag set on a FLAC file. Keys: artist/album/year/
    genre/label/tracks. metaflac honors --remove-tag and --set-tag flags
    in argv order, so a single invocation handles "remove existing + set
    new" — half the subprocess overhead of the previous two-pass approach."""
    args = ["metaflac"]
    for k in list(TAG_KEY_MAP.values()) + ["TRACKLIST"]:
        args.append(f"--remove-tag={k}")
    for k, v in fields.items():
        if k == "tracks":
            tl = " / ".join(t.strip() for t in (v or []) if t and t.strip())
            if tl:
                args.append(f"--set-tag=TRACKLIST={tl}")
        elif k in TAG_KEY_MAP and v:
            args.append(f"--set-tag={TAG_KEY_MAP[k]}={v}")
    args.append(str(path))
    subprocess.run(args, check=False, stderr=subprocess.DEVNULL)


def list_recordings() -> list[dict]:
    """List side recordings in raw/. These are always untagged (any
    tagging happens at the in-progress album level via album.json — sides
    never carry Vorbis tags in the new model)."""
    files = []
    for f in RAW_DIR.glob("*.flac"):
        stat = f.stat()
        fmt = flac_format(f)
        files.append({
            "filename":         f.name,
            "size_mb":          round(stat.st_size / 1e6, 1),
            "mtime":            stat.st_mtime,
            "duration_seconds": flac_duration_seconds(f),
            "bit_depth":        fmt.get("bit_depth"),
            "sample_rate_khz":  fmt.get("sample_rate_khz"),
        })
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files


def parse_silencedetect(stderr: str) -> list[dict]:
    out: list[dict] = []
    cur_start: Optional[float] = None
    for line in stderr.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            cur_start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([\d.]+).*silence_duration:\s*([\d.]+)", line)
        if m and cur_start is not None:
            out.append({"start": cur_start, "end": float(m.group(1)), "duration": float(m.group(2))})
            cur_start = None
    return out


def parse_astats(stderr: str) -> dict:
    """Pick the loudest peak and lowest sustained RMS across all channels from
    ffmpeg's `astats` filter output. astats prints one block per channel plus
    an Overall block; we scan everything and aggregate."""
    peak_db: Optional[float] = None      # max across channels
    rms_trough_db: Optional[float] = None  # min across channels (= noise floor)
    for line in stderr.splitlines():
        m = re.search(r"Peak level dB:\s*(-?\d+(?:\.\d+)?|-?inf)", line)
        if m:
            v = _parse_db(m.group(1))
            if v is not None and (peak_db is None or v > peak_db):
                peak_db = v
            continue
        m = re.search(r"RMS trough dB:\s*(-?\d+(?:\.\d+)?|-?inf)", line)
        if m:
            v = _parse_db(m.group(1))
            if v is not None and (rms_trough_db is None or v < rms_trough_db):
                rms_trough_db = v
    return {"peak_db": peak_db, "noise_floor_db": rms_trough_db}


def _parse_db(s: str) -> Optional[float]:
    s = s.strip().lower()
    if s in ("-inf", "inf", "nan", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None
