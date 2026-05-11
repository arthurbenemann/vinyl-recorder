"""Unit tests for the duration-cap helper in routes.recordings.

The full mid-recording-edit lifecycle (live ffmpeg, watcher tick fires
at the cap, FLAC trailer flushes) is exercised by the e2e suite via
test_auto_stop. Here we exercise the pure decision function and its
pause-awareness without spinning a real subprocess.
"""
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class _FakeSession:
    duration: int = 0
    start_time: float = 0.0
    paused: bool = False
    pause_started: Optional[float] = None


# ── _duration_cap_reached ────────────────────────────────────────────────
def test_unlimited_duration_never_caps():
    """duration == 0 is the "no cap" sentinel. The watcher must never
    trigger an auto-stop on a session whose user picked ∞ unlimited."""
    from routes.recordings import _duration_cap_reached
    s = _FakeSession(duration=0, start_time=0.0)
    # An hour into recording, still no cap.
    assert _duration_cap_reached(s, now=3600.0) is False


def test_under_cap_returns_false():
    from routes.recordings import _duration_cap_reached
    s = _FakeSession(duration=1800, start_time=0.0)
    assert _duration_cap_reached(s, now=1500.0) is False


def test_at_cap_returns_true():
    """Exactly at the cap boundary, the watcher should fire on this tick
    (>=, not >) so the auto-stop never slips past the user-configured
    duration by an extra tick."""
    from routes.recordings import _duration_cap_reached
    s = _FakeSession(duration=1800, start_time=0.0)
    assert _duration_cap_reached(s, now=1800.0) is True


def test_well_past_cap_returns_true():
    from routes.recordings import _duration_cap_reached
    s = _FakeSession(duration=1800, start_time=0.0)
    assert _duration_cap_reached(s, now=3600.0) is True


def test_paused_freezes_elapsed_so_cap_does_not_advance():
    """While paused, time spent paused must not consume the cap. The
    elapsed budget is anchored at `pause_started`; subsequent ticks all
    return False even if the cap WOULD have fired had we used `now`."""
    from routes.recordings import _duration_cap_reached
    # Recording started at t=0, user paused at t=100 (well under the
    # 1800s cap), and "now" is t=2000 — 1900 seconds since start_time,
    # but only 100 s of un-paused recording.
    s = _FakeSession(duration=1800, start_time=0.0,
                     paused=True, pause_started=100.0)
    assert _duration_cap_reached(s, now=2000.0) is False


def test_paused_at_cap_still_caps():
    """If the user paused EXACTLY at the cap (or past it), pause_started
    already encodes that — the freeze-at-pause math returns True. Edge
    case but worth covering so pausing right at the boundary doesn't
    leave the session zombied."""
    from routes.recordings import _duration_cap_reached
    s = _FakeSession(duration=1800, start_time=0.0,
                     paused=True, pause_started=1800.0)
    assert _duration_cap_reached(s, now=time.monotonic()) is True


def test_paused_without_pause_started_falls_back_to_now():
    """Defensive: if `paused=True` but `pause_started` somehow wasn't
    set (corrupted state, test fixture), the helper must not crash on
    `None - float`. The fallback to `now` is conservative — it might
    trigger an auto-stop a tick early, but won't hang the session."""
    from routes.recordings import _duration_cap_reached
    s = _FakeSession(duration=10, start_time=0.0,
                     paused=True, pause_started=None)
    assert _duration_cap_reached(s, now=20.0) is True
