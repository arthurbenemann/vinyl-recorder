"""WebSocket route smoke tests.

Confirms the connect/replay/disconnect lifecycle without exercising the
full event-bus fan-out (that's covered in tests/unit/test_eventbus.py).
"""
import json

from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


def test_ws_connect_replays_hello_packet():
    """A fresh /api/ws connection should immediately receive a `hello`
    packet carrying the log ring buffer, current upstream state, and
    recording-state snapshot. The frontend uses this to catch up after a
    page refresh."""
    with _client().websocket_connect("/api/ws") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "hello"
        # All replay sub-fields present (even if empty).
        assert "log" in msg and isinstance(msg["log"], list)
        assert "upstream" in msg
        assert "record" in msg
        # No active recording in tests.
        assert msg["record"]["recording"] is False
        assert msg["record"]["sessions"] == []


def test_ws_record_snapshot_reflects_active_session():
    """When a session is registered in `state.active`, the WS hello packet
    must include it in `record.sessions` — same shape as /api/status. We
    inject a sentinel session directly so we don't need ffmpeg."""
    import time
    from state import active

    sid = "ws-sentinel"
    active[sid] = {
        "proc":    None,
        "paused":  False,
        "start_time": time.time() - 3,  # ~3 s elapsed
        "outfile": "/tmp/sample.flac",
        "meta":    {"artist": "A", "album": "B"},
        "duration": 0,
    }
    try:
        with _client().websocket_connect("/api/ws") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["record"]["recording"] is True
            sessions = msg["record"]["sessions"]
            assert any(s["id"] == sid for s in sessions)
            our = next(s for s in sessions if s["id"] == sid)
            assert our["outfile"] == "sample.flac"
            assert our["paused"] is False
            assert our["elapsed"] >= 0
    finally:
        active.pop(sid, None)


def test_ws_paused_session_freezes_elapsed():
    """A paused session reports elapsed = pause_started - start_time, not
    wall-clock; the UI then doesn't tick the timer while paused."""
    import time
    from state import active

    sid = "ws-paused-sentinel"
    now = time.time()
    active[sid] = {
        "proc":          None,
        "paused":        True,
        "start_time":    now - 10,
        "pause_started": now - 4,  # 6 s elapsed at pause time
        "outfile":       "/tmp/p.flac",
        "meta":          {},
        "duration":      0,
    }
    try:
        with _client().websocket_connect("/api/ws") as ws:
            msg = json.loads(ws.receive_text())
            our = next(s for s in msg["record"]["sessions"] if s["id"] == sid)
            assert our["paused"] is True
            assert our["elapsed"] == 6
    finally:
        active.pop(sid, None)
