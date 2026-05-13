"""Unit tests for the auto-stop-on-silence helpers in routes.recordings.

The watcher logic itself runs in a background thread fed by ffmpeg; here we
exercise just the two pure functions it relies on:

  * `_silence_threshold_int(db, bps)` — dB → integer RMS / peak cutoff.
    The math is the same in both interpretations (full_scale × 10**(db/20));
    only the integer is reused, with the live sink comparing it against a
    smoothed RMS rather than a per-chunk peak. See `test_silence_rms.py`
    for the EMA-smoothed detector itself.
  * `_silence_should_autostop(session, now)` — whether to finalize.

A full lifecycle (start_recording → silent stream → finalize) is covered
by the e2e suite via the existing `test_auto_stop` harness shape; the
ffmpeg + UpstreamSession plumbing belongs there, not in a unit test.

This module also covers `_infer_auto_stop_on_silence` in `state.py`,
the pure helper that decides `DEFAULT_AUTO_STOP_ON_SILENCE` from the
three env vars (AUTO_STOP_ON_SILENCE / SILENCE_THRESHOLD_DB /
SILENCE_SECONDS).
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
    a cutoff of at least 1, otherwise audioop.rms would compare ≥0 and
    *every* chunk would register as silent → instant auto-stop the moment
    the watcher arms. The floor is what keeps the feature safe."""
    from routes.recordings import _silence_threshold_int
    assert _silence_threshold_int(-300.0, 2) == 1
    assert _silence_threshold_int(-300.0, 3) == 1


def test_threshold_positive_db_clamps_to_zero():
    """Positive dB makes no physical sense for an RMS threshold (above
    full scale). The helper clamps to 0 dBFS — anything looser would
    never trigger silence anyway. This protects against a hand-crafted
    POST that tries to slip silence_threshold_db=+99 in."""
    from routes.recordings import _silence_threshold_int
    assert _silence_threshold_int(99.0, 2) == 0x7FFF


# ── _silence_should_autostop + _silence_progress_payload ────────────────
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


# ── _silence_progress_payload (UI countdown bar) ─────────────────────────
# The payload mirrors _silence_should_autostop's gates so the bar drains
# back to empty the moment any of (paused / not armed / silence_since
# cleared) becomes true. Pure helper → unit tests can drive every branch
# without spinning a real watcher thread.
def test_progress_payload_returns_none_when_feature_disabled():
    """silence_seconds == 0 means the user opted out — no event at all,
    so a feature-off recording doesn't emit watcher-tick traffic."""
    from routes.recordings import _silence_progress_payload
    s = _FakeSession(silence_seconds=0, silence_armed=True,
                     silence_since=1.0)
    assert _silence_progress_payload(s, now=1000.0) is None


def test_progress_payload_zero_when_not_armed():
    """Lead-in silence (sink hasn't seen audio above threshold yet) must
    render as an empty bar — the user mustn't see a progress bar fill
    before the needle even touches a groove."""
    from routes.recordings import _silence_progress_payload
    s = _FakeSession(silence_seconds=20, silence_armed=False,
                     silence_since=None)
    payload = _silence_progress_payload(s, now=10.0)
    assert payload == {"armed": False, "elapsed_seconds": 0.0,
                       "cap_seconds": 20, "progress": 0.0}


def test_progress_payload_zero_when_silence_since_none():
    """Detector is armed but most-recent chunk was above threshold —
    silence_since cleared, bar drains. This is the music-is-back case;
    the UI mustn't keep the bar half-filled across an audio gap."""
    from routes.recordings import _silence_progress_payload
    s = _FakeSession(silence_seconds=20, silence_armed=True,
                     silence_since=None)
    payload = _silence_progress_payload(s, now=10.0)
    assert payload["armed"] is True
    assert payload["elapsed_seconds"] == 0.0
    assert payload["progress"] == 0.0
    assert payload["cap_seconds"] == 20


def test_progress_payload_zero_while_paused():
    """While paused, silence accumulation freezes — same contract as
    _silence_should_autostop. The bar must NOT keep filling for a
    recording the user has deliberately frozen."""
    from routes.recordings import _silence_progress_payload
    now = 1_000.0
    s = _FakeSession(silence_seconds=20, silence_armed=True,
                     silence_since=now - 5.0, paused=True)
    payload = _silence_progress_payload(s, now)
    assert payload["elapsed_seconds"] == 0.0
    assert payload["progress"] == 0.0


def test_progress_payload_half_full_partway_through_silence():
    """Armed, not paused, silence_since set 10 s ago with a 20 s cap →
    bar should read 50% full. This is the canonical "silence is
    accumulating" path the UI renders during a typical side-out."""
    from routes.recordings import _silence_progress_payload
    now = 1_000.0
    s = _FakeSession(silence_seconds=20, silence_armed=True,
                     silence_since=now - 10.0)
    payload = _silence_progress_payload(s, now)
    assert payload["armed"] is True
    assert payload["elapsed_seconds"] == 10.0
    assert payload["cap_seconds"] == 20
    assert payload["progress"] == 0.5


def test_progress_payload_clamps_to_one_when_past_cap():
    """The watcher races between "publish progress" and "_finalize_session"
    — a tick may see silence_since older than the cap by a few ms. Clamp
    to 1.0 so the bar visually settles at 100% rather than overshooting
    (which the CSS would render as "wider than the track")."""
    from routes.recordings import _silence_progress_payload
    now = 1_000.0
    s = _FakeSession(silence_seconds=20, silence_armed=True,
                     silence_since=now - 60.0)
    payload = _silence_progress_payload(s, now)
    assert payload["progress"] == 1.0
    # Raw elapsed is still surfaced so the UI's "auto-stop in Ns" label
    # reads sensibly even at the clamp boundary.
    assert payload["elapsed_seconds"] == 60.0


# ── _infer_auto_stop_on_silence ──────────────────────────────────────────
# Pure function so we can drive the truth table without reloading the
# state module. Re-importing state would swap the `sessions` singleton
# and break route-level tests that hold the old reference.
#
# Contract:
#   * AUTO_STOP_ON_SILENCE truthy   → on  (explicit)
#   * AUTO_STOP_ON_SILENCE falsy    → off (explicit; wins over inference)
#   * unset/unrecognized, EITHER silence var set → on  (inferred)
#   * unset/unrecognized, BOTH silence vars unset → off (safe default)
def test_inference_off_when_all_vars_unset():
    """Safe default: existing deployments untouched."""
    from state import _infer_auto_stop_on_silence as f
    assert f("", "", "") is False


def test_inference_on_when_only_threshold_set():
    """Setting just SILENCE_THRESHOLD_DB implies consent — the user
    wouldn't bother tuning the threshold if they didn't want the
    feature on."""
    from state import _infer_auto_stop_on_silence as f
    assert f("", "-42", "") is True


def test_inference_on_when_only_seconds_set():
    """Symmetric: setting just SILENCE_SECONDS turns the feature on."""
    from state import _infer_auto_stop_on_silence as f
    assert f("", "", "30") is True


def test_inference_on_when_both_silence_vars_set():
    from state import _infer_auto_stop_on_silence as f
    assert f("", "-55", "15") is True


def test_explicit_true_keeps_feature_on():
    """AUTO_STOP_ON_SILENCE=true alone (no silence vars) → on."""
    from state import _infer_auto_stop_on_silence as f
    for truthy in ("true", "True", "TRUE", "1", "yes", "on"):
        assert f(truthy, "", "") is True, f"failed for {truthy!r}"


def test_explicit_false_overrides_inference():
    """The opt-out path: user wants the silence values customised but
    the feature off. Explicit AUTO_STOP_ON_SILENCE=false wins over the
    implicit inference, otherwise there'd be no way to pre-set values
    without also enabling the feature."""
    from state import _infer_auto_stop_on_silence as f
    assert f("false", "-42", "30") is False
    for falsy in ("0", "no", "off", "FALSE"):
        assert f(falsy, "-42", "30") is False, f"failed for {falsy!r}"


def test_unrecognized_auto_stop_value_falls_through_to_inference():
    """Defensive: a typo / unknown value in AUTO_STOP_ON_SILENCE (e.g.
    "maybe", "yep") shouldn't silently disable the feature when the
    user has clearly tuned the silence vars. Unrecognized = "unset" so
    the inference still fires."""
    from state import _infer_auto_stop_on_silence as f
    assert f("maybe", "", "25") is True
    # Unrecognized with no silence vars tuned → still off (safe default).
    assert f("yep", "", "") is False


def test_whitespace_around_env_values_does_not_break_inference():
    """Real-world .env files often have stray whitespace. The inference
    must tolerate that — empty-after-strip means "unset"."""
    from state import _infer_auto_stop_on_silence as f
    # All vars effectively empty.
    assert f("   ", "  ", " ") is False
    # SILENCE_SECONDS set with surrounding whitespace.
    assert f(" ", " ", "  20  ") is True
