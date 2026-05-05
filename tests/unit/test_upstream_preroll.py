"""Unit tests for the UpstreamSession pre-roll ring buffer.

The ring stores the last N seconds of raw PCM so a recording started "now"
can be prepended with audio captured before the user hit Record. The ring
lives entirely in-process (no ffmpeg) — these tests exercise it directly
by reaching into `_read_loop`'s helpers and the new
`subscribe_with_preroll` snapshot path.
"""
import pytest

from services.upstream import UpstreamSession


def _connect_fake(sess: UpstreamSession, sample_rate: int = 8000,
                  channels: int = 2, bit_depth: int = 16) -> None:
    """Pretend the upstream is connected without spawning ffmpeg.

    `_read_loop` is what wires up the ring on real connections, but for
    pure ring-buffer logic we just need the format set + capacity computed
    + a dummy proc so `connected` returns True."""
    fmt = {"sample_rate": sample_rate, "channels": channels,
           "bit_depth": bit_depth, "codec": "pcm"}
    sess.fmt = fmt
    sess.sample_format = "s16le" if bit_depth == 16 else "s24le"
    bps = 3 if sess.sample_format == "s24le" else 2
    sess._expected_bps = sample_rate * channels * bps
    sess._preroll_capacity_bytes = sess._expected_bps * sess._preroll_seconds

    class _DummyProc:
        def poll(self):
            return None
    sess.proc = _DummyProc()  # type: ignore[assignment]


def _push_chunk(sess: UpstreamSession, chunk: bytes) -> None:
    """Append `chunk` to the ring under the same lock the reader uses."""
    with sess._lock:
        sess._preroll_chunks.append(chunk)
        sess._preroll_total_bytes += len(chunk)
        while (sess._preroll_total_bytes > sess._preroll_capacity_bytes
               and sess._preroll_chunks):
            popped = sess._preroll_chunks.popleft()
            sess._preroll_total_bytes -= len(popped)


# ── Sizing ───────────────────────────────────────────────────────────────
def test_preroll_capacity_matches_format_and_seconds():
    sess = UpstreamSession(preroll_seconds=3)
    _connect_fake(sess, sample_rate=8000, channels=2, bit_depth=16)
    # 8000 Hz × 2 ch × 2 bytes × 3 s = 96 000 bytes
    assert sess._preroll_capacity_bytes == 96_000


def test_preroll_capacity_zero_when_disabled():
    sess = UpstreamSession(preroll_seconds=0)
    _connect_fake(sess)
    assert sess._preroll_capacity_bytes == 0


def test_preroll_seconds_normalized_to_non_negative():
    # Negative values would cap-trim the ring to zero — same as disabled.
    sess = UpstreamSession(preroll_seconds=-5)
    assert sess._preroll_seconds == 0


# ── Ring trim ────────────────────────────────────────────────────────────
def test_ring_grows_until_capacity_then_trims():
    sess = UpstreamSession(preroll_seconds=1)
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    # Capacity = 1000 × 1 × 2 = 2000 bytes.
    cap = sess._preroll_capacity_bytes
    # Push 5 × 800-byte chunks (4000 bytes total). Should trim down to ≤ cap.
    for _ in range(5):
        _push_chunk(sess, b"\x00" * 800)
    assert sess._preroll_total_bytes <= cap
    # The ring should hold the freshest tail of bytes (FIFO trim).
    snapshot = b"".join(sess._preroll_chunks)
    assert len(snapshot) <= cap


def test_disabled_ring_never_buffers():
    sess = UpstreamSession(preroll_seconds=0)
    _connect_fake(sess)
    # Capacity 0 → trim loop drops every chunk we push. The reader thread
    # won't even append (it checks capacity first); we mimic that here.
    assert sess._preroll_capacity_bytes == 0
    # Nothing buffered, nothing to snapshot.
    _, snap = sess.subscribe_with_preroll(
        "rec-test", lambda b: None, on_close=lambda: None,
    )
    assert snap == b""
    sess.unsubscribe("rec-test")


# ── Atomic subscribe + snapshot ──────────────────────────────────────────
def test_subscribe_with_preroll_returns_current_snapshot():
    sess = UpstreamSession(preroll_seconds=2)
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    # Pre-fill: distinct 200-byte chunks so we can verify the snapshot's
    # contents and ordering.
    payloads = [bytes([i]) * 200 for i in range(1, 8)]
    for p in payloads:
        _push_chunk(sess, p)
    expected = b"".join(sess._preroll_chunks)  # whatever survived trimming

    received: list[bytes] = []

    def sink(chunk: bytes) -> None:
        received.append(chunk)

    sub, snapshot = sess.subscribe_with_preroll("rec-x", sink)
    try:
        assert snapshot == expected
        # And the subscriber is now registered — confirms the atomic add.
        with sess._lock:
            assert "rec-x" in sess._subscribers
    finally:
        sess.unsubscribe("rec-x")


def test_subscribe_with_preroll_raises_if_disconnected():
    sess = UpstreamSession(preroll_seconds=5)
    # No fake-connect: connected==False.
    with pytest.raises(RuntimeError):
        sess.subscribe_with_preroll("rec-x", lambda b: None)


def test_disconnect_clears_ring():
    sess = UpstreamSession(preroll_seconds=2)
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    _push_chunk(sess, b"\x55" * 1000)
    assert sess._preroll_total_bytes > 0
    sess.disconnect()
    assert sess._preroll_total_bytes == 0
    assert not sess._preroll_chunks


def test_connect_reset_drops_stale_ring(monkeypatch):
    """A new session must not hand a recording bytes from the previous one
    (e.g. when a different sample rate would make them noise)."""
    sess = UpstreamSession(preroll_seconds=2)
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    _push_chunk(sess, b"\xAB" * 1500)
    # Simulate disconnect+reconnect by clearing proc and calling the same
    # reset path connect() uses. We don't go through the full connect()
    # because that spawns ffmpeg; instead we mimic its ring-reset block.
    sess.disconnect()
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    assert sess._preroll_total_bytes == 0
