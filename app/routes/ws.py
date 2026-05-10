"""WebSocket endpoint that broadcasts shared server state to every tab.

What clients get over `/api/ws`:
  - vu      — peak L/R + clip flags every ~50 ms (from the upstream reader)
  - clip    — discrete events when a CLIP latch is acknowledged or rises
  - log     — user-facing log lines (replayed on connect, then incremental)
  - upstream— configured/live transitions (carries `configured` and `live`;
              `connected` retained as backwards-compat alias for `live`)
  - record  — recording session start/stop/pause/resume + elapsed
  - health  — stream-quality stats every ~500 ms (bytes/sec, gaps, level)
  - status  — periodic snapshot (disk_free, recording, upstream)

The client also speaks back over the same WS for visibility hints —
`{type: "visibility", hidden: bool}` — so the server can keep ffmpeg up
only while at least one tab is actually visible. A visible tab counts
as a lifecycle holder; backgrounded tabs release the hold and let the
upstream slip into idle-with-grace.

Everything is JSON. No auth — same scope as the rest of the app.
"""
import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services.eventbus import bus
from state import active, upstream

router = APIRouter()


def _record_snapshot() -> dict:
    """Live snapshot of all in-flight recording sessions for the WS replay."""
    sessions = []
    for sid, s in active.items():
        if s.get("paused"):
            elapsed = int(s.get("pause_started", time.monotonic()) - s["start_time"])
        else:
            elapsed = int(time.monotonic() - s["start_time"])
        sessions.append({
            "id":       sid,
            "elapsed":  elapsed,
            "paused":   bool(s.get("paused")),
            "outfile":  Path(s["outfile"]).name,
            "meta":     s["meta"],
            "duration": s["duration"],
        })
    return {"recording": len(sessions) > 0, "sessions": sessions}


@router.websocket("/api/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    q = bus.add_subscriber()

    # Lifecycle hold: a visible tab counts as a holder so the upstream
    # ffmpeg stays alive. The hold is acquired on connect (treating "the
    # tab just opened" as visible until told otherwise) and toggled by
    # the client's visibility messages. Always released in `finally` so a
    # crashed handler doesn't leak the hold.
    hold = upstream.acquire(f"ws:{id(ws)}")

    def _hold_release():
        nonlocal hold
        if hold is not None:
            try: upstream.release(hold)
            except Exception: pass
            hold = None

    def _hold_acquire():
        nonlocal hold
        if hold is None:
            try:
                hold = upstream.acquire(f"ws:{id(ws)}")
            except Exception:
                # Probe failure on (re)spawn — leave hold None and let the
                # client retry on the next visibility flip. The ws keeps
                # running so the user still sees state events.
                hold = None

    try:
        # Replay: log ring buffer + current upstream + recording state. New
        # tabs (and refreshed tabs) catch up on the recent history this way.
        await ws.send_text(json.dumps({
            "type":     "hello",
            "log":      bus.recent_log(),
            "upstream": upstream.state(),
            "record":   _record_snapshot(),
        }))

        # Run two concurrent tasks: one drains the bus into the socket,
        # the other reads incoming control frames (visibility hints). The
        # send loop also doubles as the keepalive — `q.get()` with a 15 s
        # timeout sends a `ping` so a wedged proxy / TCP RST drops us.
        async def _send_loop():
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                    if evt is None:
                        # Bus evicted us — queue overflowed with a critical
                        # state change. Closing forces the client to
                        # reconnect and replay `hello`, which is the only
                        # way to be sure they're back in sync.
                        return
                    await ws.send_text(json.dumps(evt))
                except asyncio.TimeoutError:
                    # Periodic ping doubles as a liveness check —
                    # disconnected peers raise on send and break us out.
                    await ws.send_text(json.dumps({"type": "ping",
                                                   "ts": time.time()}))

        async def _recv_loop():
            while True:
                msg = await ws.receive_text()
                try:
                    payload = json.loads(msg)
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "visibility":
                    hidden = bool(payload.get("hidden"))
                    if hidden:
                        _hold_release()
                    else:
                        _hold_acquire()

        send_task = asyncio.create_task(_send_loop())
        recv_task = asyncio.create_task(_recv_loop())
        try:
            done, pending = await asyncio.wait(
                {send_task, recv_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                # Suppress task-cancellation noise from the other coroutine.
                try: await t
                except (asyncio.CancelledError, Exception): pass
            for t in done:
                # Surface unexpected exceptions only if they're not the
                # benign "client closed" / cancellation paths.
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect,
                                                 asyncio.CancelledError)):
                    raise exc
        except WebSocketDisconnect:
            pass
    except WebSocketDisconnect:
        pass
    except Exception:
        # Any send failure (broken pipe, JSON encode error) drops the client.
        pass
    finally:
        _hold_release()
        bus.remove_subscriber(q)
