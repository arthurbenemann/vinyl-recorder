"""WebSocket endpoint that broadcasts shared server state to every tab.

What clients get over `/api/ws`:
  - vu      — peak L/R + clip flags every ~50 ms (from the upstream reader)
  - clip    — discrete events when a CLIP latch is acknowledged or rises
  - log     — user-facing log lines (replayed on connect, then incremental)
  - upstream— connect/disconnect transitions
  - record  — recording session start/stop/pause/resume + elapsed
  - health  — stream-quality stats every ~500 ms (bytes/sec, gaps, level)
  - status  — periodic snapshot (disk_free, recording, upstream)

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
    try:
        # Replay: log ring buffer + current upstream + recording state. New
        # tabs (and refreshed tabs) catch up on the recent history this way.
        await ws.send_text(json.dumps({
            "type":     "hello",
            "log":      bus.recent_log(),
            "upstream": upstream.state(),
            "record":   _record_snapshot(),
        }))
        # Then forward every event published to the bus.
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=15.0)
                if evt is None:
                    # Bus evicted us — queue overflowed with a critical
                    # state change. Closing forces the client to reconnect
                    # and replay `hello`, which is the only way to be sure
                    # they're back in sync.
                    break
                await ws.send_text(json.dumps(evt))
            except asyncio.TimeoutError:
                # Periodic ping doubles as a liveness check — disconnected
                # peers raise on send and break us out of the loop.
                await ws.send_text(json.dumps({"type": "ping",
                                               "ts": time.time()}))
    except WebSocketDisconnect:
        pass
    except Exception:
        # Any send failure (broken pipe, JSON encode error) drops the client.
        pass
    finally:
        bus.remove_subscriber(q)
