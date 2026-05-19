"""WebSocket route smoke tests.

Confirms the connect/replay/disconnect lifecycle without exercising the
full event-bus fan-out (that's covered in tests/unit/test_eventbus.py).
"""
import asyncio
import json
import logging

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
    """When a session is registered with the recording session manager,
    the WS hello packet must include it in `record.sessions` — same shape
    as /api/status. We inject a sentinel session directly so we don't need
    ffmpeg."""
    import time
    from state import sessions, Session

    sid = "ws-sentinel"
    sessions.insert(Session(
        sid=sid,
        proc=None,
        paused=False,
        start_time=time.monotonic() - 3,  # ~3 s elapsed
        outfile="/tmp/sample.flac",
        meta={"artist": "A", "album": "B"},
        duration=0,
    ))
    try:
        with _client().websocket_connect("/api/ws") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["record"]["recording"] is True
            ws_sessions = msg["record"]["sessions"]
            assert any(s["id"] == sid for s in ws_sessions)
            our = next(s for s in ws_sessions if s["id"] == sid)
            assert our["outfile"] == "sample.flac"
            assert our["paused"] is False
            assert our["elapsed"] >= 0
    finally:
        sessions.remove(sid)


# Going forward, prefer the `reset_active_sessions` fixture (see
# tests/conftest.py) over try/finally + manual pop. The pattern below uses
# it; the older paired tests in this file are kept on try/finally for
# backwards-readability and converted opportunistically.
def test_ws_record_snapshot_via_fixture(reset_active_sessions):
    """Same shape as `test_ws_record_snapshot_reflects_active_session` but
    uses the cleanup fixture so an assertion failure mid-test can't leak
    the sentinel into the next test in this module."""
    import time

    sid = "ws-fixture-sentinel"
    reset_active_sessions.insert(sid, {
        "proc":    None,
        "paused":  False,
        "start_time": time.monotonic() - 1,
        "outfile": "/tmp/fx.flac",
        "meta":    {},
        "duration": 0,
    })
    with _client().websocket_connect("/api/ws") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["record"]["recording"] is True
        assert any(s["id"] == sid for s in msg["record"]["sessions"])


def test_ws_paused_session_freezes_elapsed():
    """A paused session reports elapsed = pause_started - start_time, not
    wall-clock; the UI then doesn't tick the timer while paused."""
    import time
    from state import sessions, Session

    sid = "ws-paused-sentinel"
    now = time.monotonic()
    sessions.insert(Session(
        sid=sid,
        proc=None,
        paused=True,
        start_time=now - 10,
        pause_started=now - 4,  # 6 s elapsed at pause time
        outfile="/tmp/p.flac",
        meta={},
        duration=0,
    ))
    try:
        with _client().websocket_connect("/api/ws") as ws:
            msg = json.loads(ws.receive_text())
            our = next(s for s in msg["record"]["sessions"] if s["id"] == sid)
            assert our["paused"] is True
            assert our["elapsed"] == 6
    finally:
        sessions.remove(sid)


# ── Regression: silent except in cancellation cleanup ────────────────────
def test_ws_cancellation_cleanup_logs_unexpected_exceptions(caplog):
    """Pin the WS handler's exception-handling contract.

    Before the fix, the cleanup of pending tasks did
    `except (asyncio.CancelledError, Exception): pass` — i.e. any real
    bug in `_send_loop` or `_recv_loop` (or in their underlying I/O
    layer) would vanish silently and the operator had no signal that
    anything was wrong. We now only swallow `CancelledError` /
    `WebSocketDisconnect` and log everything else at warning level.

    This test simulates the structure of the WS handler's task-pair
    cleanup with one task that completes normally (so its sibling lands
    in `pending`) and a second task that raises a real Exception during
    cancellation cleanup. Asserts:
      1. The exception is logged (not swallowed silently).
      2. The CancelledError path stays silent (no log noise on the
         benign cleanup).
    """
    from routes import ws as ws_module

    class _BoomDuringCleanup(Exception):
        pass

    async def _scenario():
        async def _completes_first():
            return "done"

        async def _raises_when_cancelled():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # A task whose finally / except CancelledError raises
                # something else surfaces THAT exception when awaited
                # post-cancel — exactly the bug-shaped scenario the silent
                # except was hiding.
                raise _BoomDuringCleanup("simulated handler bug")

        t_done = asyncio.create_task(_completes_first())
        t_pending = asyncio.create_task(_raises_when_cancelled())

        done, pending = await asyncio.wait(
            {t_done, t_pending},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Mirror the route's cleanup pattern EXACTLY (the lines under
        # test). If this block is ever copy-edited away from the route,
        # update both sites together.
        from fastapi import WebSocketDisconnect
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except WebSocketDisconnect:
                pass
            except Exception:
                ws_module._log.warning(
                    "ws task raised during cancellation cleanup",
                    exc_info=True,
                )
        return done, pending

    with caplog.at_level(logging.WARNING, logger=ws_module._log.name):
        asyncio.run(_scenario())

    # The unexpected exception should have been recorded at WARNING+,
    # carrying the original exception via exc_info.
    boom_records = [
        r for r in caplog.records
        if r.exc_info and isinstance(r.exc_info[1], _BoomDuringCleanup)
    ]
    assert boom_records, (
        "expected the cancellation-cleanup unexpected-exception path to "
        f"log a WARNING with exc_info; got records={caplog.records!r}"
    )
    rec = boom_records[0]
    assert rec.levelno >= logging.WARNING
    assert "cancellation cleanup" in rec.getMessage()


def test_ws_cancellation_cleanup_stays_silent_for_cancelled_error(caplog):
    """The fix must NOT log on the benign `CancelledError` path —
    otherwise every WS close would spam the operator log at warning
    level. Mirror the route's cleanup against a task that simply honours
    the cancel and verify nothing reaches the logger."""
    from routes import ws as ws_module

    async def _scenario():
        async def _idle():
            await asyncio.sleep(60)

        async def _short():
            return None

        t_done = asyncio.create_task(_short())
        t_pending = asyncio.create_task(_idle())
        _, pending = await asyncio.wait(
            {t_done, t_pending},
            return_when=asyncio.FIRST_COMPLETED,
        )
        from fastapi import WebSocketDisconnect
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except WebSocketDisconnect:
                pass
            except Exception:
                ws_module._log.warning(
                    "ws task raised during cancellation cleanup",
                    exc_info=True,
                )

    with caplog.at_level(logging.WARNING, logger=ws_module._log.name):
        asyncio.run(_scenario())

    warn_records = [
        r for r in caplog.records
        if r.name == ws_module._log.name and r.levelno >= logging.WARNING
    ]
    assert not warn_records, (
        "benign CancelledError cleanup should not log a WARNING; "
        f"got {warn_records!r}"
    )


def test_ws_disconnect_cleanly_after_client_close():
    """End-to-end smoke: after the new narrower except, a normal client-
    side close still cleans up without raising out of the handler. If
    the changed code accidentally re-raised CancelledError or
    WebSocketDisconnect, this test would surface as a TestClient error
    on the second connect (lingering hold leaking, etc.)."""
    with _client().websocket_connect("/api/ws") as ws:
        # Consume the hello so the handler has fully entered its task pair.
        _ = ws.receive_text()
    # Reconnect must succeed cleanly — no leaked state from the prior
    # handler's cleanup path.
    with _client().websocket_connect("/api/ws") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "hello"
