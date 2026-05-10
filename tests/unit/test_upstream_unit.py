"""Unit tests for the pure-logic parts of services/upstream.py.

The connect/disconnect/_read_loop/_health_loop paths are e2e-tested
against real ffmpeg + audio. Here we cover the pieces that don't need
a subprocess: the subscriber queue's drop-oldest dynamics, the PCM peak
math (s16le + s24le, mono + stereo), CLIP latching and edge events,
and probe_stream's branches.
"""
import json
import threading
import time

from services import upstream as up_mod


# ── _Subscriber queue dynamics ───────────────────────────────────────────
def test_subscriber_writes_drained_to_sink():
    """Happy path: chunks pushed via `write` arrive at the sink in order."""
    received: list[bytes] = []
    done = threading.Event()

    def sink(b: bytes) -> None:
        received.append(b)
        if len(received) == 3:
            done.set()

    sub = up_mod._Subscriber("t", sink)
    sub.write(b"a")
    sub.write(b"b")
    sub.write(b"c")
    assert done.wait(timeout=1.0)
    assert received == [b"a", b"b", b"c"]
    sub.close()


def test_subscriber_write_after_close_is_dropped():
    """Once close() flips `alive=False`, further writes are silent no-ops
    so a reader thread racing against unsubscribe doesn't OOM the queue
    of a dying subscriber."""
    received: list[bytes] = []
    sub = up_mod._Subscriber("t", lambda b: received.append(b))
    sub.close()
    # Give the worker time to drain the sentinel.
    time.sleep(0.05)
    sub.write(b"x")
    time.sleep(0.05)
    assert b"x" not in received


def test_subscriber_drops_oldest_when_queue_full():
    """A wedged sink fills the bound (64) — the next put must EVICT the
    oldest queued chunk so the upstream reader thread never blocks. We
    block the sink on an event so we can pile chunks into the queue
    deterministically, then release and confirm only the freshest
    survived."""
    gate = threading.Event()
    received: list[bytes] = []

    def slow_sink(b: bytes) -> None:
        gate.wait()
        received.append(b)

    sub = up_mod._Subscriber("t", slow_sink)
    # Worker will pull the first chunk and block on `gate` inside slow_sink.
    sub.write(b"first")
    time.sleep(0.02)  # let the worker pick the first chunk

    # Now flood the queue past _QUEUE_MAX. Each extra `write` evicts the
    # oldest queued (NOT the in-flight first).
    for i in range(up_mod._Subscriber._QUEUE_MAX + 50):
        sub.write(f"q-{i}".encode())

    gate.set()
    # Wait for the worker to drain everything still queued.
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if len(received) >= up_mod._Subscriber._QUEUE_MAX + 1:
            break
        time.sleep(0.01)
    sub.close()

    # We expect: the first (in-flight) chunk + at most _QUEUE_MAX queued
    # entries. The earliest chunks were dropped; the very last write
    # ("q-{N-1}") must always be present.
    assert received[0] == b"first"
    assert b"q-" + str(up_mod._Subscriber._QUEUE_MAX + 49).encode() in received
    # Some early ones were dropped — `q-0` is gone.
    assert b"q-0" not in received


def test_subscriber_close_is_idempotent():
    """close() is called from both the explicit unsubscribe path AND the
    sink-failure path inside _drain. Calling twice must not raise."""
    sub = up_mod._Subscriber("t", lambda b: None)
    sub.close()
    sub.close()  # no exception


def test_subscriber_calls_on_close_callback():
    flag = {"called": False}

    def on_close():
        flag["called"] = True

    sub = up_mod._Subscriber("t", lambda b: None, on_close=on_close)
    sub.close()
    time.sleep(0.05)
    assert flag["called"] is True


def test_subscriber_swallows_on_close_exception():
    """A failing on_close (e.g. proc.stdin.close on an already-dead ffmpeg)
    must not propagate — close() is called from the read loop's broken-
    pipe path and a raise there would wedge the upstream session."""
    def bad_on_close():
        raise RuntimeError("pipe already closed")

    sub = up_mod._Subscriber("t", lambda b: None, on_close=bad_on_close)
    sub.close()  # no exception expected


# ── probe_stream branches ────────────────────────────────────────────────
def test_probe_stream_returns_normalized_format(monkeypatch):
    class _R:
        returncode = 0
        stdout = json.dumps({
            "streams": [{
                "sample_rate": "96000",
                "channels": 2,
                "codec_name": "pcm_s24le",
                "bits_per_sample": 24,
            }],
        })
        stderr = ""

    monkeypatch.setattr(up_mod.subprocess, "run", lambda *a, **kw: _R())
    fmt = up_mod.probe_stream("http://x")
    assert fmt == {
        "sample_rate": 96000,
        "channels":    2,
        "bit_depth":   24,
        "codec":       "pcm_s24le",
    }


def test_probe_stream_defaults_when_fields_missing(monkeypatch):
    """An ffprobe payload missing sample_rate / channels / bits_per_sample
    falls back to safe defaults (44.1 kHz, 2ch, 16-bit) so callers never
    crash on a partial probe."""
    class _R:
        returncode = 0
        stdout = json.dumps({"streams": [{"codec_name": "mp3"}]})
        stderr = ""

    monkeypatch.setattr(up_mod.subprocess, "run", lambda *a, **kw: _R())
    fmt = up_mod.probe_stream("http://x")
    assert fmt["sample_rate"] == 44100
    assert fmt["channels"] == 2
    assert fmt["bit_depth"] == 16
    assert fmt["codec"] == "mp3"


def test_probe_stream_raises_on_ffprobe_failure(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "Server returned 404"

    monkeypatch.setattr(up_mod.subprocess, "run", lambda *a, **kw: _R())
    import pytest
    with pytest.raises(RuntimeError, match="404"):
        up_mod.probe_stream("http://x")


def test_probe_stream_raises_when_no_streams(monkeypatch):
    class _R:
        returncode = 0
        stdout = '{"streams": []}'
        stderr = ""

    monkeypatch.setattr(up_mod.subprocess, "run", lambda *a, **kw: _R())
    import pytest
    with pytest.raises(RuntimeError, match="no streams"):
        up_mod.probe_stream("http://x")


# ── _update_peaks PCM math ───────────────────────────────────────────────
def _new_session():
    """Construct an UpstreamSession that records every emitted event."""
    events: list[dict] = []
    sess = up_mod.UpstreamSession(on_event=events.append, preroll_seconds=0)
    return sess, events


def _set_format(sess, sample_format: str, channels: int = 2):
    """Pin the fmt + sample_format that _update_peaks reads. Mirrors what
    `connect` would do without spawning ffmpeg."""
    sess.sample_format = sample_format
    sess.fmt = {"sample_rate": 48000, "channels": channels, "codec": "raw",
                "bit_depth": 24 if sample_format == "s24le" else 16}


def _s16_pair(left: int, right: int) -> bytes:
    """Pack a single L/R int16 sample pair as little-endian bytes."""
    import struct
    return struct.pack("<hh", left, right)


def _s24_pair(left: int, right: int) -> bytes:
    """Pack a single L/R int24 sample pair as little-endian bytes."""
    def _enc(v: int) -> bytes:
        if v < 0:
            v += 1 << 24
        return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF])
    return _enc(left) + _enc(right)


def test_update_peaks_s16_stereo_picks_max_per_channel():
    sess, events = _new_session()
    _set_format(sess, "s16le", channels=2)
    # L peaks at half-scale; R peaks at full-scale on the second sample.
    chunk = _s16_pair(16384, 100) + _s16_pair(0, 32767)
    sess._update_peaks(chunk, bps=2, channels=2)
    # peak_l ≈ 16384/32767, peak_r = 1.0 (full scale).
    assert 0.49 < sess.peak_l < 0.51
    assert sess.peak_r >= 0.99
    # CLIP fires on R only.
    assert sess.clipped_l is False
    assert sess.clipped_r is True
    # A 'vu' event was published with both peaks.
    vu = next(e for e in events if e["type"] == "vu")
    assert vu["peak_l"] == sess.peak_l
    assert vu["peak_r"] == sess.peak_r
    # And exactly one CLIP-rose log on R.
    rose = [e for e in events if e["type"] == "log" and "CLIP on R" in e["msg"]]
    assert len(rose) == 1


def test_update_peaks_negative_samples_use_absolute_value():
    sess, events = _new_session()
    _set_format(sess, "s16le", channels=2)
    # A near-min-int16 negative on L (peak at full-scale once abs'd).
    chunk = _s16_pair(-32767, 0)
    sess._update_peaks(chunk, bps=2, channels=2)
    assert sess.peak_l >= 0.99
    assert sess.clipped_l is True


def test_update_peaks_s24_stereo():
    sess, events = _new_session()
    _set_format(sess, "s24le", channels=2)
    # Half-scale on L, full-scale on R.
    chunk = _s24_pair(0x400000, 0) + _s24_pair(0, 0x7FFFFF)
    sess._update_peaks(chunk, bps=3, channels=2)
    assert 0.49 < sess.peak_l < 0.51
    assert sess.peak_r >= 0.99
    assert sess.clipped_r is True


def test_update_peaks_mono_mirrors_to_both_meters():
    """A mono source feeds the same value to both L and R so the UI's
    stereo VU still draws sensibly."""
    sess, events = _new_session()
    _set_format(sess, "s16le", channels=1)
    chunk = _s16_pair(20000, 0)[:2]  # one channel only — 2 bytes per sample
    sess._update_peaks(chunk, bps=2, channels=1)
    assert sess.peak_l > 0
    assert sess.peak_l == sess.peak_r


def test_update_peaks_clip_latch_is_sticky_until_cleared():
    """Once a CLIP latch is set, a subsequent quieter frame must NOT
    un-latch it — that's the job of the user via /api/clip/clear."""
    sess, events = _new_session()
    _set_format(sess, "s16le", channels=2)
    sess._update_peaks(_s16_pair(32767, 32767), bps=2, channels=2)
    assert sess.clipped_l is True and sess.clipped_r is True
    # Second frame is silent; latches stay set.
    sess._update_peaks(_s16_pair(0, 0), bps=2, channels=2)
    assert sess.clipped_l is True
    assert sess.clipped_r is True


def test_clip_rose_log_only_emitted_once():
    """The CLIP-rose log lines fire on the latch transition only — a
    second clip frame while still latched must NOT spam the log ring."""
    sess, events = _new_session()
    _set_format(sess, "s16le", channels=2)
    sess._update_peaks(_s16_pair(32767, 32767), bps=2, channels=2)
    sess._update_peaks(_s16_pair(32767, 32767), bps=2, channels=2)
    rose_l = [e for e in events if e["type"] == "log" and "CLIP on L" in e["msg"]]
    rose_r = [e for e in events if e["type"] == "log" and "CLIP on R" in e["msg"]]
    assert len(rose_l) == 1
    assert len(rose_r) == 1


# ── clear_clip ───────────────────────────────────────────────────────────
def test_clear_clip_specific_channel():
    sess, events = _new_session()
    sess.clipped_l = True
    sess.clipped_r = True
    sess.clear_clip("L")
    assert sess.clipped_l is False
    assert sess.clipped_r is True
    # A clip event was emitted with cleared=True.
    cleared = [e for e in events if e.get("type") == "clip" and e.get("cleared")]
    assert cleared
    assert cleared[-1]["clipped_l"] is False
    assert cleared[-1]["clipped_r"] is True


def test_clear_clip_both_when_no_channel_specified():
    sess, _ = _new_session()
    sess.clipped_l = sess.clipped_r = True
    sess.clear_clip()
    assert sess.clipped_l is False
    assert sess.clipped_r is False


# ── state() shape when idle ─────────────────────────────────────────────
def test_state_when_disconnected_returns_known_keys():
    sess, _ = _new_session()
    s = sess.state()
    assert s["connected"] is False
    # A handful of keys the UI / WS handler always reads.
    for k in ("url", "format", "peak_l", "peak_r", "clipped_l",
              "clipped_r", "subscribers", "health"):
        assert k in s


# ── unsubscribe is a no-op for unknown names ─────────────────────────────
def test_unsubscribe_unknown_name_is_silent():
    sess, _ = _new_session()
    sess.unsubscribe("never-existed")  # no exception


# ── disconnect when not connected is a no-op ─────────────────────────────
def test_disconnect_when_not_connected_is_silent():
    sess, _ = _new_session()
    sess.disconnect()  # no exception, no event fired


# ── Regression: stream-proxy teardown order ──────────────────────────────
def test_proxy_teardown_kills_before_unsubscribe(monkeypatch):
    """`_teardown_proxy` MUST kill ffmpeg first, then unsubscribe. Killing
    second can deadlock against the subscriber's worker thread holding the
    BufferedWriter `_write_lock` while blocked in `stdin.write`. Pin the
    order with a sentinel so a future refactor that swaps these calls
    fails the test."""
    from routes import recordings

    calls: list[str] = []

    class _FakeProc:
        def __init__(self):
            self._dead = False
        def poll(self):
            return 0 if self._dead else None
        def kill(self):
            calls.append("kill")
            self._dead = True

    def fake_unsubscribe(name):
        calls.append(f"unsubscribe:{name}")

    def fake_reap(p):
        calls.append("reap")

    monkeypatch.setattr(recordings.upstream, "unsubscribe", fake_unsubscribe)
    monkeypatch.setattr(recordings, "_reap", fake_reap)

    p = _FakeProc()
    recordings._teardown_proxy(p, "proxy-abc")
    assert calls == ["kill", "unsubscribe:proxy-abc", "reap"], (
        f"teardown order wrong — kill must precede unsubscribe; got {calls}"
    )


def test_proxy_teardown_skips_kill_if_already_dead(monkeypatch):
    """If ffmpeg has already exited (e.g. EOF on its stdout flushed the
    generator), don't bother sending another signal — `proc.poll()` reports
    a returncode and we skip straight to unsubscribe + reap."""
    from routes import recordings

    calls: list[str] = []

    class _FakeProc:
        def poll(self):
            return 0
        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(recordings.upstream, "unsubscribe",
                        lambda n: calls.append(f"unsub:{n}"))
    monkeypatch.setattr(recordings, "_reap", lambda p: calls.append("reap"))

    recordings._teardown_proxy(_FakeProc(), "proxy-x")
    assert "kill" not in calls
    assert calls == ["unsub:proxy-x", "reap"]


# ── Regression: concurrent finalize race (user-stop + watcher) ──────────
def test_finalize_session_is_idempotent_under_concurrent_calls(monkeypatch):
    """Pin the user-stop / watcher race fix.

    Both the `/api/record/stop/{sid}` request handler and the per-session
    watcher's `_watch_session` thread can wake up on the same SIGINT-driven
    ffmpeg exit and race into `_finalize_session`. Pre-fix, the loser
    either returned `{"elapsed": 0, ...}` to its caller (so the e2e
    `test_record_3s_from_loop` assertion `2 <= elapsed <= 6` failed) or
    raised KeyError on a duplicate session remove. Post-fix, the second
    call returns the same payload as the first and the session is removed
    exactly once."""
    import os
    import time as _time
    from routes import recordings as rec
    from state import sessions, Session

    class _DeadProc:
        returncode = 0
        def poll(self): return 0
        def send_signal(self, sig): pass
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    class _FH:
        def close(self): pass

    monkeypatch.setattr(rec.upstream, "unsubscribe", lambda name: None)
    monkeypatch.setattr(rec.bus, "log", lambda *a, **kw: None)
    monkeypatch.setattr(rec.bus, "publish", lambda *a, **kw: None)

    sid = "race-sid"
    outfile = "/tmp/__race_test_finalize.flac"
    with open(outfile, "wb") as f:
        f.write(b"x" * 1024)

    sessions.insert(Session(
        sid=sid,
        proc=_DeadProc(),
        outfile=outfile,
        log_fh=_FH(),
        start_time=_time.monotonic() - 3.0,
        duration=0,
        meta={"artist": "x", "album": "y", "year": "2026"},
        filename="x.flac",
        sess_state={"paused": False},
        finalize_lock=threading.Lock(),
        finalized=False,
    ))

    try:
        results: list[dict] = []
        errors: list[BaseException] = []

        def runner(reason: str) -> None:
            try:
                results.append(rec._finalize_session(sid, reason))
            except BaseException as e:  # pragma: no cover — must NOT happen
                errors.append(e)

        t1 = threading.Thread(target=runner, args=("user",))
        t2 = threading.Thread(target=runner, args=("auto",))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        assert not errors, f"finalize race raised: {errors!r}"
        assert len(results) == 2
        # Both callers see identical payload — the second got the cached one.
        assert results[0] == results[1]
        assert results[0]["filename"] == os.path.basename(outfile)
        assert results[0]["elapsed"] >= 2
        # Session removed from the manager exactly once.
        assert sessions.get(sid) is None
    finally:
        sessions.remove(sid)
        try: os.unlink(outfile)
        except OSError: pass
