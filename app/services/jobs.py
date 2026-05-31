"""In-process registry of long-running ffmpeg jobs (combine / split / measure
/ silence / waveform). Routes call `start_job` before launching ffmpeg and
push `update_job(progress)` as ffmpeg's `-progress pipe:1` output streams in;
the browser polls GET /api/jobs/{id} to draw a progress bar."""
import threading
import time
from typing import Optional

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# Drop completed jobs older than this so the dict doesn't grow forever.
_GC_AGE_SEC = 300.0


def _gc_locked() -> None:
    cutoff = time.time() - _GC_AGE_SEC
    stale = [k for k, v in _jobs.items() if v["done"] and v["ts"] < cutoff]
    for k in stale:
        del _jobs[k]


def start_job(job_id: str, label: str = "") -> None:
    if not job_id:
        return
    with _lock:
        _gc_locked()
        _jobs[job_id] = {
            "label":    label,
            "phase":    "",
            "progress": 0.0,
            "done":     False,
            "error":    None,
            "result":   None,
            "ts":       time.time(),
        }


def update_job(job_id: str, progress: float, phase: Optional[str] = None) -> None:
    if not job_id:
        return
    with _lock:
        j = _jobs.get(job_id)
        if not j or j["done"]:
            return
        j["progress"] = max(0.0, min(1.0, float(progress)))
        if phase is not None:
            j["phase"] = phase
        j["ts"] = time.time()


def finish_job(job_id: str, error: Optional[str] = None,
               result: Optional[dict] = None) -> None:
    if not job_id:
        return
    with _lock:
        j = _jobs.get(job_id)
        if not j:
            return
        j["done"]     = True
        j["error"]    = error
        j["result"]   = result
        j["progress"] = j["progress"] if error else 1.0
        j["ts"]       = time.time()


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None
