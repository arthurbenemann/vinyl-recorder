"""API tests for POST /api/record/duration/{session_id}.

Endpoint mutates the live session's `duration` field — a plain Python
int the per-session watcher reads on its 500 ms tick. We plant a fake
session via the manager helper and exercise the validation + slack
rules; no ffmpeg subprocess is spawned.
"""
import time

from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


def _plant_session(sid: str, *, duration: int, elapsed: float, paused: bool = False):
    """Insert a Session whose start_time is `elapsed` seconds in the
    past on the monotonic clock, so the route's `_elapsed_seconds`
    returns ~elapsed. When paused, also pin pause_started at "now - 0"
    so paused budget = elapsed."""
    from state import sessions, Session
    now = time.monotonic()
    s = Session(
        sid=sid,
        proc=None,
        outfile=f"/tmp/{sid}.flac",
        duration=duration,
        start_time=now - elapsed,
        paused=paused,
        pause_started=now if paused else None,
        sess_state={"paused": paused},
    )
    sessions.insert(s)
    return s


# ── 404 / 400 guards ─────────────────────────────────────────────────────
def test_edit_duration_unknown_session_returns_404():
    r = _client().post("/api/record/duration/no-such",
                       json={"duration": 1800})
    assert r.status_code == 404


def test_edit_duration_negative_returns_400():
    sid = "dur-neg"
    _plant_session(sid, duration=1800, elapsed=60.0)
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": -10})
        assert r.status_code == 400
        assert "duration" in r.json()["detail"].lower()
    finally:
        from state import sessions
        sessions.remove(sid)


# ── Extension paths (always allowed) ─────────────────────────────────────
def test_extension_from_bounded_to_longer_is_allowed():
    """Going 30 → 60 min mid-recording with 5 min elapsed: pure
    extension, no slack guard. Server accepts and returns the new cap."""
    sid = "dur-extend"
    _plant_session(sid, duration=1800, elapsed=300.0)  # 5 min in
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 3600})
        assert r.status_code == 200
        assert r.json()["duration"] == 3600
        # Verify the session was actually mutated.
        from state import sessions
        assert sessions.get(sid).duration == 3600
    finally:
        from state import sessions
        sessions.remove(sid)


def test_extension_to_unlimited_is_always_allowed():
    """Switching to ∞ unlimited (duration=0) is an extension by
    definition — the cap goes away entirely. Always 200."""
    sid = "dur-inf"
    _plant_session(sid, duration=1800, elapsed=1700.0)  # 28 min in
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 0})
        assert r.status_code == 200
        assert r.json()["duration"] == 0
    finally:
        from state import sessions
        sessions.remove(sid)


def test_noop_edit_returns_200_unchanged():
    """Setting the duration to the value it already has is a no-op —
    keeps the dropdown's optimistic write idempotent so a stray UI
    re-render that re-POSTs doesn't trip a "slack too tight" 409."""
    sid = "dur-noop"
    _plant_session(sid, duration=1800, elapsed=1700.0)  # close to cap
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 1800})
        assert r.status_code == 200
        assert r.json()["duration"] == 1800
    finally:
        from state import sessions
        sessions.remove(sid)


# ── Reduction paths (slack-gated) ────────────────────────────────────────
def test_reduction_with_ample_slack_is_allowed():
    """60 → 30 min at 5 min elapsed: new cap (30 min) is 25 min from
    now, well above the 5-min slack floor. Server accepts."""
    sid = "dur-reduce-ok"
    _plant_session(sid, duration=3600, elapsed=300.0)  # 5 min in
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 1800})
        assert r.status_code == 200
        assert r.json()["duration"] == 1800
    finally:
        from state import sessions
        sessions.remove(sid)


def test_reduction_with_insufficient_slack_returns_409():
    """60 → 30 min at 26 min elapsed: new cap leaves only 4 min of
    headroom — below the 5 min floor. Server rejects with 409 and the
    `detail` carries the actual slack so the UI can render it."""
    sid = "dur-reduce-tight"
    _plant_session(sid, duration=3600, elapsed=26 * 60)
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 30 * 60})
        assert r.status_code == 409
        assert "headroom" in r.json()["detail"].lower()
        # Session must be unchanged after a rejection.
        from state import sessions
        assert sessions.get(sid).duration == 3600
    finally:
        from state import sessions
        sessions.remove(sid)


def test_reduction_just_inside_5min_slack_is_allowed():
    """Boundary: slack slightly > 300 s. The guard is `< 300`, so
    301 s should pass. Pinning the inequality direction so a future
    refactor doesn't silently flip it to `<= 300`. (We avoid testing
    exactly 300 s because the clock advances between plant and call,
    making the literal boundary flaky.)"""
    sid = "dur-reduce-boundary"
    # elapsed = 1499 s, new_cap = 1800 s → slack ≈ 301 s (well above 300).
    _plant_session(sid, duration=3600, elapsed=1499.0)
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 1800})
        assert r.status_code == 200
    finally:
        from state import sessions
        sessions.remove(sid)


def test_unlimited_to_bounded_uses_same_slack_rule():
    """Stepping DOWN from unlimited to a bounded value counts as a
    reduction — the new cap could fire on the next tick, exactly the
    case the slack guard exists for. With 5 min elapsed and a 4-min
    new cap, the server rejects."""
    sid = "dur-inf-to-tight"
    _plant_session(sid, duration=0, elapsed=300.0)  # unlimited, 5 min in
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 240})  # 4 min cap
        assert r.status_code == 409
    finally:
        from state import sessions
        sessions.remove(sid)


def test_unlimited_to_bounded_with_ample_slack_is_allowed():
    """Symmetric: same direction, but with enough headroom."""
    sid = "dur-inf-to-loose"
    _plant_session(sid, duration=0, elapsed=300.0)
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 3600})  # 60-min cap
        assert r.status_code == 200
    finally:
        from state import sessions
        sessions.remove(sid)


# ── Pause-awareness ──────────────────────────────────────────────────────
def test_paused_session_uses_frozen_elapsed_for_slack_check():
    """A paused session has its elapsed budget frozen at `pause_started`.
    The slack check must use that frozen value, not wallclock — otherwise
    a long-paused recording could reject a reasonable cap edit."""
    sid = "dur-paused"
    # Started 30 min ago, paused at 5 min in. Frozen elapsed = 5 min.
    # Reducing to a 30-min cap leaves 25 min of slack — should pass even
    # though the user has been paused for 25 wallclock minutes.
    from state import sessions, Session
    now = time.monotonic()
    s = Session(
        sid=sid, proc=None, outfile="/tmp/x.flac",
        duration=3600,
        start_time=now - 30 * 60,
        paused=True,
        pause_started=now - 25 * 60,  # paused for 25 min wall, but frozen at 5 min elapsed
        sess_state={"paused": True},
    )
    sessions.insert(s)
    try:
        r = _client().post(f"/api/record/duration/{sid}",
                           json={"duration": 1800})
        assert r.status_code == 200, r.json()
    finally:
        sessions.remove(sid)
