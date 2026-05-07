"""Unit tests for the in-process job registry that drives the progress UI."""
import time

from services import jobs as jobs_mod
from services.jobs import finish_job, get_job, start_job, update_job


def setup_function(_):
    # Each test starts from an empty registry.
    with jobs_mod._lock:
        jobs_mod._jobs.clear()


# ── lifecycle ─────────────────────────────────────────────────────────────
def test_lifecycle_progress_then_finish():
    start_job("j1", label="combine")
    assert get_job("j1") == {
        "label": "combine", "phase": "", "progress": 0.0,
        "done": False, "error": None, "ts": get_job("j1")["ts"],
    }
    update_job("j1", 0.5, phase="encoding tracks")
    j = get_job("j1")
    assert j["progress"] == 0.5
    assert j["phase"] == "encoding tracks"
    assert j["done"] is False

    finish_job("j1")
    j = get_job("j1")
    assert j["done"] is True
    assert j["progress"] == 1.0  # finish snaps to 100% on clean exit
    assert j["error"] is None


def test_finish_with_error_keeps_partial_progress():
    # If ffmpeg errors out partway, the bar shouldn't snap to 100% — that
    # would suggest success. Keep the last reported progress.
    start_job("j-err")
    update_job("j-err", 0.42)
    finish_job("j-err", error="ffmpeg exited 1")
    j = get_job("j-err")
    assert j["done"] is True
    assert j["error"] == "ffmpeg exited 1"
    assert j["progress"] == 0.42


def test_update_clamps_to_unit_range():
    # Defensive: ffmpeg's `-progress` channel can occasionally emit weird
    # numbers (out_time past the duration on a rounding edge). Caller
    # shouldn't have to worry about clamping.
    start_job("j-clip")
    update_job("j-clip", 1.7)
    assert get_job("j-clip")["progress"] == 1.0
    update_job("j-clip", -0.3)
    assert get_job("j-clip")["progress"] == 0.0


def test_update_after_finish_is_a_noop():
    # A late `update_job` (e.g. ffmpeg's last progress event arriving after
    # finalize) must not unfinish or rewind a completed job — that would
    # cause the polling client to flicker the bar back to "in progress".
    start_job("j-late")
    finish_job("j-late")
    update_job("j-late", 0.2, phase="late!")
    j = get_job("j-late")
    assert j["done"] is True
    assert j["progress"] == 1.0
    assert j["phase"] == ""  # not overwritten


# ── isolation ─────────────────────────────────────────────────────────────
def test_jobs_are_independent():
    # Two concurrent operations (e.g. measure in tab A, split in tab B)
    # must not stomp on each other's progress.
    start_job("a", label="measure"); start_job("b", label="split")
    update_job("a", 0.3, phase="analysing")
    update_job("b", 0.7, phase="encoding tracks")
    assert get_job("a")["progress"] == 0.3
    assert get_job("b")["progress"] == 0.7
    finish_job("a")
    assert get_job("a")["done"] is True
    assert get_job("b")["done"] is False


# ── unknown ids ───────────────────────────────────────────────────────────
def test_get_unknown_returns_none():
    assert get_job("never-existed") is None


def test_update_unknown_is_silent():
    # Defensive: a route that calls update_job before start_job (or after
    # gc) must not raise — the polling client gets 404 from /api/jobs/{id},
    # which is fine.
    update_job("never-existed", 0.5)
    finish_job("never-existed")
    assert get_job("never-existed") is None


def test_empty_id_is_no_op():
    # Routes that omit job_id (older callers, or paths the progress UI
    # doesn't care about) pass an empty string. The registry stays empty.
    start_job("", label="anonymous")
    update_job("", 0.5)
    finish_job("")
    with jobs_mod._lock:
        assert jobs_mod._jobs == {}


# ── auto-GC ───────────────────────────────────────────────────────────────
def test_finished_jobs_gc_on_next_start(monkeypatch):
    # The registry doesn't run a background thread; it sweeps stale entries
    # at the next `start_job`. Verify a > 5 min old finished job is dropped.
    start_job("old")
    finish_job("old")
    # Fast-forward the timestamp on the finished job past the GC threshold.
    with jobs_mod._lock:
        jobs_mod._jobs["old"]["ts"] = time.time() - jobs_mod._GC_AGE_SEC - 1

    start_job("new")  # triggers _gc_locked
    assert get_job("old") is None
    assert get_job("new") is not None


def test_gc_does_not_drop_in_flight_jobs():
    # Long-running operations (e.g. a 10 min split) must not be GC'd while
    # they're still active, even if their start is older than the cutoff.
    start_job("long")
    with jobs_mod._lock:
        jobs_mod._jobs["long"]["ts"] = time.time() - jobs_mod._GC_AGE_SEC - 1

    start_job("trigger-gc")
    j = get_job("long")
    assert j is not None
    assert j["done"] is False
