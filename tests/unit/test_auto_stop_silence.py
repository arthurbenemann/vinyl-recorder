"""Unit tests for the auto-stop-on-silence helpers in routes.recordings.

The watcher logic itself runs in a background thread fed by ffmpeg; here we
exercise just the two pure functions it relies on:

  * `_silence_threshold_int(db, bps)` — dB → integer peak-sample cutoff.
  * `_silence_should_autostop(session, now)` — whether to finalize.

A full lifecycle (start_recording → silent stream → finalize) is covered
by the e2e suite via the existing `test_auto_stop` harness shape; the
ffmpeg + UpstreamSession plumbing belongs there, not in a unit test.
"""
import time
from dataclasses import dataclass
from typing import Optional


# ── _silence_threshold_int ───────────────────────────────────────────────
def test_threshold_zero_db_maps_to_full_scale_16bit():
    from routes.recordings import _silence_threshold_int
    assert _silence_threshold_int(0.0, 2) == 0x7FFF


def test_threshold_zero_db_maps_to_full_scale_24bit():
    from routes.recordings import _silence_threshold_int
    assert _silence_threshold_int(0.0, 3) == 0x7FFFFF


def test_threshold_minus_6db_is_half_scale():
    """-6 dBFS ≈ amp 0.501. 16-bit: ~0.501 × 32767 = 16422."""
    from routes.recordings import _silence_threshold_int
    v = _silence_threshold_int(-6.0, 2)
    # Allow ±1% slack on the discrete-int answer.
    assert 16250 <= v <= 16600


def test_threshold_minus_50db_clamped_at_one_floor():
    """-50 dBFS ≈ amp 0.00316. 16-bit: 103, well above the floor of 1."""
    from routes.recordings import _silence_threshold_int
    v16 = _silence_threshold_int(-50.0, 2)
    v24 = _silence_threshold_int(-50.0, 3)
    # 24-bit gives a much larger absolute cutoff than 16-bit for the same dB.
    assert v16 < v24
    # Both are positive — the threshold must never collapse to "everything
    # is silent" (a 0 cutoff would compare ≥0, which is always true).
    assert v16 >= 1 and v24 >= 1


def test_threshold_very_negative_db_floors_at_one():
    """An absurdly low dB value (-300 dB → amp ~0) must still produce
    a cutoff of at least 1, otherwise audioop.max would compare ≥0 and
    *every* chunk would register as silent → instant auto-stop the moment
    the watcher arms. The floor is what keeps the feature safe."""
    from routes.recordings import _silence_threshold_int
    assert _silence_threshold_int(-300.0, 2) == 1
    assert _silence_threshold_int(-300.0, 3) == 1


def test_threshold_positive_db_clamps_to_zero():
    """Positive dB makes no physical sense for a peak threshold (above
    full scale). The helper clamps to 0 dBFS — anything looser would
    never trigger silence anyway. This protects against a hand-crafted
    POST that tries to slip silence_threshold_db=+99 in."""
    from routes.recordings import _silence_threshold_int
    assert _silence_threshold_int(99.0, 2) == 0x7FFF


# ── _silence_should_autostop ─────────────────────────────────────────────
@dataclass
class _FakeSession:
    silence_seconds: int = 0
    silence_armed: bool = False
    silence_since: Optional[float] = None
    paused: bool = False


def test_should_autostop_disabled_when_seconds_zero():
    """silence_seconds == 0 means feature off — the watcher must never
    trigger regardless of armed/since values that may linger on a
    repurposed Session struct."""
    from routes.recordings import _silence_should_autostop
    s = _FakeSession(silence_seconds=0, silence_armed=True, silence_since=0.0)
    assert _silence_should_autostop(s, now=10_000.0) is False


def test_should_autostop_not_armed_yet():
    """Lead-in silence (sink hasn't seen a single above-threshold chunk
    yet) MUST not trigger. The whole arm-after-first-audio policy
    depends on this branch."""
    from routes.recordings import _silence_should_autostop
    now = time.monotonic()
    s = _FakeSession(silence_seconds=5, silence_armed=False,
                     silence_since=now - 1000)
    assert _silence_should_autostop(s, now) is False


def test_should_autostop_no_silent_run_yet():
    """silence_since == None means the last chunk was above threshold.
    Even after arming, we can't trigger until silence_since gets set."""
    from routes.recordings import _silence_should_autostop
    s = _FakeSession(silence_seconds=5, silence_armed=True, silence_since=None)
    assert _silence_should_autostop(s, now=time.monotonic()) is False


def test_should_autostop_paused_blocks_trigger():
    """While paused, the sink isn't writing — but old silence_since may
    still be set. The watcher must respect the pause flag and refuse to
    auto-stop a recording the user has deliberately frozen."""
    from routes.recordings import _silence_should_autostop
    now = time.monotonic()
    s = _FakeSession(silence_seconds=2, silence_armed=True,
                     silence_since=now - 60, paused=True)
    assert _silence_should_autostop(s, now) is False


def test_should_autostop_under_threshold_duration():
    """Silent for less than silence_seconds — not yet."""
    from routes.recordings import _silence_should_autostop
    now = time.monotonic()
    s = _FakeSession(silence_seconds=10, silence_armed=True,
                     silence_since=now - 3)
    assert _silence_should_autostop(s, now) is False


def test_should_autostop_fires_at_threshold():
    """Silent for ≥ silence_seconds, armed, not paused — the moment all
    four conditions line up, the watcher returns True. This is the
    canonical "side ended, stop the recording" trigger."""
    from routes.recordings import _silence_should_autostop
    now = 1_000.0
    s = _FakeSession(silence_seconds=5, silence_armed=True,
                     silence_since=now - 5.0)
    assert _silence_should_autostop(s, now) is True


def test_should_autostop_well_past_threshold():
    """A long-overdue trigger (e.g. watcher resumes after a stalled
    thread) still fires."""
    from routes.recordings import _silence_should_autostop
    now = 1_000.0
    s = _FakeSession(silence_seconds=5, silence_armed=True,
                     silence_since=now - 60.0)
    assert _silence_should_autostop(s, now) is True
