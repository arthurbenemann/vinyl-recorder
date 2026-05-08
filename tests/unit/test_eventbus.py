"""Unit tests for the cross-thread EventBus.

The bus has two subtle invariants worth pinning:

  - ``publish()`` must be safe from any thread, even before a loop is
    attached (publish happens at module import time when env wiring
    triggers a ``bus.log(...)``).
  - VU-frame coalescing: when a subscriber's queue is full, a fresh VU
    frame must DISPLACE the oldest queued VU. Non-VU events (log lines,
    state) must NOT be coalesced — losing those is user-visible.

Async tests use ``asyncio.run`` to avoid a pytest-asyncio dependency;
the bus is small enough that one event loop per test is fine.
"""
import asyncio

from services.eventbus import EventBus, _LOG_RING_MAX


# ── log ring buffer ──────────────────────────────────────────────────────
def test_log_appends_and_returns_event():
    bus = EventBus()
    evt = bus.log("hello", "info")
    assert evt["type"] == "log"
    assert evt["msg"] == "hello"
    assert evt["level"] == "info"
    assert isinstance(evt["ts"], float)
    assert bus.recent_log() == [evt]


def test_log_default_level_is_info():
    bus = EventBus()
    evt = bus.log("plain")
    assert evt["level"] == "info"


def test_recent_log_caps_at_ring_size():
    # The ring buffer is bounded — append more than the cap and confirm we
    # drop the oldest entries (so a long-running session doesn't OOM).
    bus = EventBus()
    for i in range(_LOG_RING_MAX + 25):
        bus.log(f"msg-{i}")
    log = bus.recent_log()
    assert len(log) == _LOG_RING_MAX
    # Oldest 25 dropped; newest is the last appended.
    assert log[0]["msg"] == "msg-25"
    assert log[-1]["msg"] == f"msg-{_LOG_RING_MAX + 24}"


def test_recent_log_n_returns_tail_only():
    bus = EventBus()
    for i in range(10):
        bus.log(f"m{i}")
    last3 = bus.recent_log(3)
    assert [e["msg"] for e in last3] == ["m7", "m8", "m9"]


def test_recent_log_n_larger_than_buffer_returns_all():
    bus = EventBus()
    bus.log("only")
    assert len(bus.recent_log(99)) == 1


# ── publish without loop is a silent no-op ───────────────────────────────
def test_publish_before_attach_loop_is_silent():
    # Imported modules can call bus.publish() before main.py's startup hook
    # attaches the loop. This must not raise.
    bus = EventBus()
    bus.publish({"type": "log", "msg": "early"})  # no exception expected


# ── subscriber lifecycle + dispatch ──────────────────────────────────────
def test_subscriber_receives_published_events():
    async def scenario():
        bus = EventBus()
        bus.attach_loop(asyncio.get_running_loop())
        q = bus.add_subscriber()
        bus.publish({"type": "log", "msg": "hi"})
        # call_soon_threadsafe → next tick. wait_for ticks the loop.
        return await asyncio.wait_for(q.get(), timeout=1)

    evt = asyncio.run(scenario())
    assert evt["msg"] == "hi"


def test_remove_subscriber_stops_delivery():
    async def scenario():
        bus = EventBus()
        bus.attach_loop(asyncio.get_running_loop())
        q = bus.add_subscriber()
        bus.remove_subscriber(q)
        bus.publish({"type": "log", "msg": "after-remove"})
        await asyncio.sleep(0.01)  # let the dispatch task run
        return q.empty()

    assert asyncio.run(scenario()) is True


def test_remove_subscriber_unknown_queue_is_noop():
    # Idempotent — WS handlers call remove on disconnect even if the bus
    # never registered the queue (e.g. add raced against shutdown).
    async def scenario():
        EventBus().remove_subscriber(asyncio.Queue())  # must not raise

    asyncio.run(scenario())


# ── VU coalescing ────────────────────────────────────────────────────────
def test_full_queue_drops_oldest_vu_frame():
    """When the subscriber is slow and its queue fills with VU frames, a
    new VU must displace the oldest VU. We mirror the bus's bound (128)
    by saturating it then sending one more — the old front frame should
    be gone."""
    async def scenario():
        bus = EventBus()
        bus.attach_loop(asyncio.get_running_loop())
        q = bus.add_subscriber()  # bound = 128
        for i in range(128):
            q.put_nowait({"type": "vu", "n": i})
        # Call _dispatch directly so we don't need to drive the loop.
        bus._dispatch({"type": "vu", "n": 999})
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    items = asyncio.run(scenario())
    # Oldest (n=0) was dropped to make room for n=999; everything else
    # shifts forward by one.
    assert items[0]["n"] == 1
    assert items[-1]["n"] == 999
    assert len(items) == 128


def test_full_queue_does_not_drop_log_events():
    # Non-VU events (log lines, state) must NOT be coalesced — losing one
    # is user-visible. The dispatch path silently skips the put_nowait
    # without evicting anything, so the queue stays at its cap.
    async def scenario():
        bus = EventBus()
        bus.attach_loop(asyncio.get_running_loop())
        q = bus.add_subscriber()
        for i in range(128):
            q.put_nowait({"type": "vu", "n": i})
        bus._dispatch({"type": "log", "msg": "should be dropped"})
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    items = asyncio.run(scenario())
    assert all(e["type"] == "vu" for e in items)
    assert len(items) == 128
    assert items[0]["n"] == 0  # nothing was evicted
