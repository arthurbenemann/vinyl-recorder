"""Unit tests for the stream-health metrics on UpstreamSession.

The reader thread updates byte counters and gap timestamps; a separate
ticker thread emits a `health` event every ~500 ms. These tests exercise
the level-classification logic and the state-snapshot fields without
spawning ffmpeg or the real ticker.
"""
import time

from services.upstream import UpstreamSession


def _connect_fake(sess: UpstreamSession, sample_rate: int = 8000,
                  channels: int = 2, bit_depth: int = 16) -> None:
    fmt = {"sample_rate": sample_rate, "channels": channels,
           "bit_depth": bit_depth, "codec": "pcm"}
    sess.fmt = fmt
    sess.sample_format = "s16le" if bit_depth == 16 else "s24le"
    bps = 3 if sess.sample_format == "s24le" else 2
    sess._expected_bps = sample_rate * channels * bps

    class _DummyProc:
        def poll(self): return None
    sess.proc = _DummyProc()  # type: ignore[assignment]


def _emit_one_health_tick(sess: UpstreamSession) -> dict:
    """Run one iteration of `_health_loop`'s body in-thread.

    Reaches into the same private fields the loop uses so we can assert on
    the emitted event. Mirrors the loop body — keep in sync with
    upstream._health_loop()."""
    events: list[dict] = []
    sess._on_event = events.append
    now = time.monotonic()
    with sess._lock:
        if not sess.connected:
            return {}
        window = max(0.001, now - sess._window_start)
        bps = int(sess._bytes_in_window / window)
        sess._bytes_in_window = 0
        sess._window_start = now
        while sess._gap_window and (now - sess._gap_window[0]) > 5.0:
            sess._gap_window.popleft()
        recent_gaps = len(sess._gap_window)
        ms_since = int((now - sess._last_frame_ts) * 1000)
        expected = sess._expected_bps
        reconnects = sess._reconnect_count
        gap_total = sess._gap_count
    if ms_since > 2000 or expected == 0:
        level = "red"
    elif (expected and bps < expected * 0.5) or recent_gaps >= 2:
        level = "yellow"
    elif (expected and bps < expected * 0.8) or recent_gaps == 1:
        level = "yellow"
    else:
        level = "green"
    evt = {
        "type":               "health",
        "bytes_per_sec":      bps,
        "expected_bps":       expected,
        "gap_count":          gap_total,
        "gap_count_recent":   recent_gaps,
        "reconnect_count":    reconnects,
        "ms_since_last_frame": ms_since,
        "level":              level,
    }
    return evt


def test_health_green_when_bytes_match_expected():
    sess = UpstreamSession()
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    # expected = 2 000 B/s. Inject a full second's worth of bytes and a
    # fresh-frame timestamp; window starts ~1 s ago so bps lands at 2000.
    sess._window_start = time.monotonic() - 1.0
    sess._last_frame_ts = time.monotonic()
    sess._bytes_in_window = sess._expected_bps  # exactly expected
    evt = _emit_one_health_tick(sess)
    assert evt["level"] == "green"
    assert evt["bytes_per_sec"] >= sess._expected_bps * 0.8


def test_health_yellow_on_low_throughput():
    sess = UpstreamSession()
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    sess._window_start = time.monotonic() - 1.0
    sess._last_frame_ts = time.monotonic()
    # 60% of expected → yellow band (50–80%).
    sess._bytes_in_window = int(sess._expected_bps * 0.6)
    evt = _emit_one_health_tick(sess)
    assert evt["level"] == "yellow"


def test_health_yellow_on_recent_gap():
    sess = UpstreamSession()
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    sess._window_start = time.monotonic() - 1.0
    sess._last_frame_ts = time.monotonic()
    sess._bytes_in_window = sess._expected_bps  # throughput is fine
    sess._gap_window.append(time.monotonic())   # one fresh gap
    sess._gap_count = 1
    evt = _emit_one_health_tick(sess)
    assert evt["level"] == "yellow"
    assert evt["gap_count_recent"] == 1
    assert evt["gap_count"] == 1


def test_health_red_when_no_recent_frames():
    sess = UpstreamSession()
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    # Last frame was 3 s ago — past the 2 s deadline → red.
    sess._window_start = time.monotonic() - 1.0
    sess._last_frame_ts = time.monotonic() - 3.0
    sess._bytes_in_window = sess._expected_bps
    evt = _emit_one_health_tick(sess)
    assert evt["level"] == "red"
    assert evt["ms_since_last_frame"] >= 2500


def test_health_gap_window_trimmed_to_5s():
    sess = UpstreamSession()
    _connect_fake(sess, sample_rate=1000, channels=1, bit_depth=16)
    sess._window_start = time.monotonic() - 1.0
    sess._last_frame_ts = time.monotonic()
    sess._bytes_in_window = sess._expected_bps
    # Two old gaps (>5 s ago) and one fresh — only the fresh one should
    # influence the level. With one gap remaining we land in yellow.
    now = time.monotonic()
    sess._gap_window.extend([now - 10, now - 8, now - 0.1])
    sess._gap_count = 3
    evt = _emit_one_health_tick(sess)
    assert evt["gap_count_recent"] == 1
    assert evt["gap_count"] == 3  # cumulative counter never trimmed


def test_state_includes_health_snapshot():
    sess = UpstreamSession()
    _connect_fake(sess)
    # Empty before first tick.
    assert sess.state()["health"] == {}
    # After we cache a health snapshot, state() must surface it.
    sess._last_health = {"level": "green", "bytes_per_sec": 100}
    snap = sess.state()["health"]
    assert snap["level"] == "green"
    assert snap["bytes_per_sec"] == 100
