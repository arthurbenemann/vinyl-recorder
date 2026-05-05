"""Cross-thread event bus + log ring buffer.

The upstream reader thread, the recording start/stop endpoints, and the
ffmpeg crash watcher all want to push events to whatever WebSocket clients
are connected. This module offers:

  - publish(event)       — thread-safe, non-blocking, callable from anywhere
  - subscribe()          — async generator yielding events; one per client
  - log(msg, level)      — convenience wrapper that records the line in a
                           ring buffer (replayed to new WS connections) and
                           broadcasts a {"type":"log", ...} event
  - recent_log()         — list[dict] for replay on new client connect

Coalescing: VU frames arrive at ~20 Hz from the reader thread regardless of
how many clients are connected. If a client's queue is full (slow tab,
backgrounded), we drop the oldest VU frame rather than blocking. Log lines
and state changes are never coalesced — those are user-facing.
"""
import asyncio
import threading
import time
from collections import deque
from typing import Optional


# Ring buffer holds the last N user-facing log lines. New WS connections
# replay the buffer on connect so a refreshed tab catches the recent
# history. Sized generously — log volume is small (a few lines per minute).
_LOG_RING_MAX = 200


class EventBus:
    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        # The asyncio loop where subscribers live. Set at app startup so
        # `publish()` (called from threads) can dispatch onto it.
        self._loop: Optional[asyncio.AbstractEventLoop] = loop
        self._subs_lock = threading.Lock()
        # Subscribers are asyncio.Queue instances owned by the WS handler.
        # Bounded so a stalled client can't OOM the server.
        self._subs: list[asyncio.Queue] = []
        self._log_lock = threading.Lock()
        self._log: deque[dict] = deque(maxlen=_LOG_RING_MAX)

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── publishing ────────────────────────────────────────────────────────
    def publish(self, event: dict) -> None:
        """Thread-safe, non-blocking. Called from sync/async contexts alike."""
        loop = self._loop
        if loop is None:
            return  # bus not yet wired (e.g. import time)
        # Schedule the dispatch onto the loop. Cheap; doesn't block.
        try:
            loop.call_soon_threadsafe(self._dispatch, event)
        except RuntimeError:
            # Loop closed during shutdown — drop the event silently.
            pass

    def _dispatch(self, event: dict) -> None:
        """Runs on the event loop. Fans out to all subscriber queues."""
        with self._subs_lock:
            subs = list(self._subs)
        is_vu = event.get("type") == "vu"
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                if is_vu:
                    # Drop the oldest VU frame to make room for a fresher one.
                    try: q.get_nowait()
                    except asyncio.QueueEmpty: pass
                    try: q.put_nowait(event)
                    except asyncio.QueueFull: pass
                # For non-VU events, the queue is so backed up that the
                # client is effectively gone — it'll be reaped on the next
                # send failure in the WS handler.

    # ── log helpers ───────────────────────────────────────────────────────
    def log(self, msg: str, level: str = "info") -> dict:
        """Append to the ring buffer + broadcast. Returns the event."""
        evt = {
            "type":  "log",
            "level": level,
            "msg":   msg,
            "ts":    time.time(),
        }
        with self._log_lock:
            self._log.append(evt)
        self.publish(evt)
        return evt

    def recent_log(self, n: Optional[int] = None) -> list[dict]:
        with self._log_lock:
            if n is None or n >= len(self._log):
                return list(self._log)
            return list(self._log)[-n:]

    # ── subscription ──────────────────────────────────────────────────────
    def add_subscriber(self) -> asyncio.Queue:
        # Bound chosen to comfortably hold a couple of seconds of VU at 20 Hz
        # plus headroom for log lines, without letting a stalled client buffer
        # arbitrarily.
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def remove_subscriber(self, q: asyncio.Queue) -> None:
        with self._subs_lock:
            try: self._subs.remove(q)
            except ValueError: pass


# Module-level singleton — there's only ever one upstream + one event bus
# per process.
bus = EventBus()
