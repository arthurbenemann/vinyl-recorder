"""API tests for POST /api/record/silence/{session_id}.

The endpoint mutates the live session's `silence_seconds` field — a
plain Python int the per-session watcher and the sink both read on
their own cadence (~500 ms and per-chunk respectively). We plant a
fake session via the manager helper and exercise the contract; no
ffmpeg subprocess is spawned.

Mirrors `test_duration_edit.py`'s shape because the two endpoints are
intentionally near-identical — the silence cap has no slack guard
(reductions can at most finalize "now if silence is already
accumulating", which is exactly what the user is asking for).
"""
import time

from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


def _plant_session(sid: str, *, silence_seconds: int = 20,
                   silence_threshold_int: int = 327,
                   silence_armed: bool = False,
                   silence_since=None,
                   silence_ms_smoothed: float = 0.0,
                   paused: bool = False):
    """Insert a Session with a deterministic silence state. Fields that
    aren't relevant to the test get defaults; the asserts only touch
    the ones each test cares about."""
    from state import sessions, Session
    now = time.monotonic()
    s = Session(
        sid=sid,
        proc=None,
        outfile=f"/tmp/{sid}.flac",
        duration=0,
        start_time=now - 60.0,
        paused=paused,
        pause_started=now if paused else None,
        sess_state={"paused": paused},
        silence_seconds=silence_seconds,
        silence_threshold_int=silence_threshold_int,
        silence_armed=silence_armed,
        silence_since=silence_since,
        silence_ms_smoothed=silence_ms_smoothed,
    )
    sessions.insert(s)
    return s


# ── 404 / 400 guards ─────────────────────────────────────────────────────
def test_edit_silence_unknown_session_returns_404():
    r = _client().post("/api/record/silence/no-such",
                       json={"silence_seconds": 30})
    assert r.status_code == 404


def test_edit_silence_negative_returns_400():
    """The dropdown can't send negative values, but a hand-crafted POST
    must still be rejected — same defensive shape as edit_duration."""
    sid = "sil-neg"
    _plant_session(sid, silence_seconds=20)
    try:
        r = _client().post(f"/api/record/silence/{sid}",
                           json={"silence_seconds": -1})
        assert r.status_code == 400
        assert "silence_seconds" in r.json()["detail"].lower()
        # Session must be unchanged after a rejection.
        from state import sessions
        assert sessions.get(sid).silence_seconds == 20
    finally:
        from state import sessions
        sessions.remove(sid)


# ── happy path ───────────────────────────────────────────────────────────
def test_extension_is_allowed_without_slack_guard():
    """20 → 60 s: pure extension. No slack guard exists for silence (a
    longer cap can't trigger an unexpected stop), so 200."""
    sid = "sil-extend"
    _plant_session(sid, silence_seconds=20)
    try:
        r = _client().post(f"/api/record/silence/{sid}",
                           json={"silence_seconds": 60})
        assert r.status_code == 200
        assert r.json()["silence_seconds"] == 60
        from state import sessions
        assert sessions.get(sid).silence_seconds == 60
    finally:
        from state import sessions
        sessions.remove(sid)


def test_reduction_is_allowed_without_slack_guard():
    """30 → 10 s mid-recording: allowed even if it'd fire immediately.
    The user actively making this change is signing up for that — and
    the threat model is different from duration (no recording-loss
    risk, just a slightly-early stop)."""
    sid = "sil-reduce"
    _plant_session(sid, silence_seconds=30)
    try:
        r = _client().post(f"/api/record/silence/{sid}",
                           json={"silence_seconds": 10})
        assert r.status_code == 200
        assert r.json()["silence_seconds"] == 10
    finally:
        from state import sessions
        sessions.remove(sid)


def test_disable_via_zero_is_allowed():
    """Setting to 0 (the ∞ option in the dropdown) disables the feature.
    The watcher's silence_seconds<=0 short-circuit will skip the
    autostop check; the sink also short-circuits the audioop.rms call."""
    sid = "sil-disable"
    _plant_session(sid, silence_seconds=20, silence_armed=True,
                   silence_since=time.monotonic())
    try:
        r = _client().post(f"/api/record/silence/{sid}",
                           json={"silence_seconds": 0})
        assert r.status_code == 200
        from state import sessions
        assert sessions.get(sid).silence_seconds == 0
    finally:
        from state import sessions
        sessions.remove(sid)


def test_enable_from_zero_resets_smoothing_state():
    """Enabling auto-stop mid-recording must clear stale smoothed RMS /
    armed / since values. The sink was skipping audioop.rms while the
    feature was off, so those fields carry whatever value start_recording
    initialized them to (zero, but conceptually stale — they don't
    reflect the current audio). A fresh arming cycle starts now."""
    sid = "sil-enable"
    # Stale state from a previous enable/disable cycle.
    _plant_session(sid, silence_seconds=0,
                   silence_armed=True,
                   silence_since=time.monotonic() - 100.0,
                   silence_ms_smoothed=999_999.0)
    try:
        r = _client().post(f"/api/record/silence/{sid}",
                           json={"silence_seconds": 20})
        assert r.status_code == 200
        from state import sessions
        s = sessions.get(sid)
        assert s.silence_seconds == 20
        # Stale fields cleared so the detector arms freshly.
        assert s.silence_armed is False
        assert s.silence_since is None
        assert s.silence_ms_smoothed == 0.0
    finally:
        from state import sessions
        sessions.remove(sid)


def test_positive_to_positive_does_not_reset_smoothing():
    """20 → 30 s while the feature was already on: the smoothing state
    is current and meaningful, and resetting would unfairly drain a
    silence-accumulation that the user wants to keep ticking. Only the
    0→positive transition resets."""
    sid = "sil-keep-state"
    armed_since = time.monotonic() - 5.0
    _plant_session(sid, silence_seconds=20,
                   silence_armed=True,
                   silence_since=armed_since,
                   silence_ms_smoothed=12345.0)
    try:
        r = _client().post(f"/api/record/silence/{sid}",
                           json={"silence_seconds": 30})
        assert r.status_code == 200
        from state import sessions
        s = sessions.get(sid)
        assert s.silence_seconds == 30
        # Smoothing state preserved across the edit.
        assert s.silence_armed is True
        assert s.silence_since == armed_since
        assert s.silence_ms_smoothed == 12345.0
    finally:
        from state import sessions
        sessions.remove(sid)


def test_noop_edit_returns_200_unchanged():
    """Setting silence_seconds to its current value is a no-op. Two
    tabs racing the same dropdown change shouldn't trip an error."""
    sid = "sil-noop"
    _plant_session(sid, silence_seconds=20)
    try:
        r = _client().post(f"/api/record/silence/{sid}",
                           json={"silence_seconds": 20})
        assert r.status_code == 200
        assert r.json()["silence_seconds"] == 20
    finally:
        from state import sessions
        sessions.remove(sid)


def test_edit_silence_emits_ws_event():
    """The dropdown sync across tabs depends on this — the POST response
    is direct to the originating tab, but the visible re-anchor in OTHER
    tabs comes from the bus.publish(record:silence) event. We assert the
    handler called publish exactly once with the right payload shape."""
    sid = "sil-ws"
    _plant_session(sid, silence_seconds=20)
    captured = []
    try:
        from services import eventbus as eb_mod
        orig = eb_mod.bus.publish
        eb_mod.bus.publish = lambda evt: captured.append(evt)
        try:
            r = _client().post(f"/api/record/silence/{sid}",
                               json={"silence_seconds": 60})
            assert r.status_code == 200
        finally:
            eb_mod.bus.publish = orig
        # Among any events published during the edit (a log line goes
        # through publish too), exactly one record:silence event must
        # carry the new cap.
        silences = [e for e in captured
                    if e.get("type") == "record" and e.get("event") == "silence"]
        assert len(silences) == 1
        assert silences[0]["session_id"] == sid
        assert silences[0]["silence_seconds"] == 60
    finally:
        from state import sessions
        sessions.remove(sid)
