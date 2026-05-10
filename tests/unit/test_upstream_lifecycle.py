"""Unit tests for the demand-driven UpstreamSession lifecycle.

Covers the new acquire/release/grace/min-uptime flow added to make the Pi
+ server idle to ~0% CPU when no holders are active. The tests fake out
ffmpeg by monkeypatching `_spawn` / `_teardown` to flip flags rather than
actually launching subprocesses — we're testing the state machine, not
the subprocess management (which is exercised end-to-end).
"""
import time
import urllib.error

from services import upstream as up_mod


def _fake_session(events: list[dict] | None = None,
                  grace: float = 0.05, min_uptime: float = 0.0):
    """Build a session with cheap timing knobs so the tests don't sleep
    for real seconds. Replaces `_spawn` / `_teardown` with no-op stand-ins
    that just flip a sentinel `proc` so `live` reads correctly."""
    if events is None:
        events = []
    sess = up_mod.UpstreamSession(
        on_event=events.append,
        preroll_seconds=0,
        idle_grace_seconds=grace,
        min_uptime_seconds=min_uptime,
    )

    class _FakeProc:
        def __init__(self): self._dead = False
        def poll(self): return 0 if self._dead else None
        def terminate(self): self._dead = True
        def kill(self): self._dead = True
        def wait(self, timeout=None): return 0

    spawned: list[float] = []
    torn_down: list[float] = []

    def _fake_spawn():
        with sess._lock:
            sess.proc = _FakeProc()
            sess._spawn_time = time.monotonic()
        spawned.append(time.monotonic())
        sess._on_event({"type": "upstream", "configured": True,
                        "connected": True, "live": True})

    def _fake_teardown(force: bool = False):
        with sess._lock:
            if not force and sess._holders:
                return
            sess.proc = None
        torn_down.append(time.monotonic())
        sess._on_event({"type": "upstream", "configured": sess.configured,
                        "connected": False, "live": False})

    sess._spawn = _fake_spawn  # type: ignore[method-assign]
    sess._teardown = _fake_teardown  # type: ignore[method-assign]
    return sess, events, spawned, torn_down


def _wait_for(predicate, timeout: float = 1.0, step: float = 0.005) -> bool:
    """Poll `predicate` until True or timeout. Avoids fixed sleeps."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


# ── acquire / release / grace ────────────────────────────────────────────
def test_acquire_spawns_ffmpeg_when_idle():
    """With configured but no live ffmpeg, the first acquire spawns."""
    sess, _, spawned, _ = _fake_session()
    sess.configured = True
    sess.url = "http://x/stream"
    assert sess.live is False
    token = sess.acquire("first")
    assert sess.live is True
    assert len(spawned) == 1
    sess.release(token)


def test_acquire_when_not_configured_does_not_spawn():
    """No URL configured yet → acquire just bumps the counter, doesn't
    spawn. The next connect() with holders > 0 triggers the spawn."""
    sess, _, spawned, _ = _fake_session()
    token = sess.acquire("ws:1")
    assert spawned == []
    assert sess.live is False
    sess.release(token)


def test_release_to_zero_starts_grace_timer():
    """Last release schedules teardown; ffmpeg stays up until expiry."""
    sess, _, spawned, torn_down = _fake_session(grace=10.0)
    sess.configured = True
    sess.url = "http://x"
    token = sess.acquire("only")
    assert sess.live is True
    sess.release(token)
    # Grace timer is scheduled but hasn't fired yet — still live.
    assert sess._grace_timer is not None
    assert sess.live is True
    assert torn_down == []
    sess._cancel_grace_locked()  # cleanup


def test_acquire_during_grace_cancels_timer():
    """A new acquire arriving during the grace window cancels the timer
    and ffmpeg keeps running with no teardown event in sight."""
    sess, _, _, torn_down = _fake_session(grace=10.0)
    sess.configured = True
    sess.url = "http://x"
    t1 = sess.acquire("a")
    sess.release(t1)
    assert sess._grace_timer is not None
    t2 = sess.acquire("b")
    assert sess._grace_timer is None
    assert sess.live is True
    assert torn_down == []
    sess.release(t2)
    sess._cancel_grace_locked()


def test_grace_expiry_tears_down():
    """When the grace timer fires with no holders, _teardown runs and
    `live` flips to False."""
    sess, _, _, torn_down = _fake_session(grace=0.02, min_uptime=0.0)
    sess.configured = True
    sess.url = "http://x"
    token = sess.acquire("only")
    sess.release(token)
    assert _wait_for(lambda: len(torn_down) == 1, timeout=1.0)
    assert sess.live is False


def test_min_uptime_blocks_early_teardown():
    """A release that fires a few ms after spawn must wait until the
    min-uptime guard elapses before teardown runs. Pin this explicitly so
    the timing math doesn't regress to "tear down immediately"."""
    grace = 0.0
    min_uptime = 0.15
    sess, _, _, torn_down = _fake_session(grace=grace, min_uptime=min_uptime)
    sess.configured = True
    sess.url = "http://x"
    t = sess.acquire("only")
    spawn_time = sess._spawn_time
    sess.release(t)
    # Release happened at ~0 ms after spawn, so teardown must be deferred
    # until at least min_uptime (within a small jitter).
    assert _wait_for(lambda: len(torn_down) == 1, timeout=1.0)
    elapsed = torn_down[0] - spawn_time
    assert elapsed >= min_uptime - 0.02, (
        f"teardown ran too early: {elapsed:.3f}s < min_uptime {min_uptime}s")


def test_recording_keeps_alive_with_no_ws_holders():
    """Even with zero WS-tab holders, a recording hold prevents teardown.
    Models the "user closes the only browser tab during a recording"
    case — closing tabs must not lose audio."""
    sess, _, _, torn_down = _fake_session(grace=0.02, min_uptime=0.0)
    sess.configured = True
    sess.url = "http://x"
    rec = sess.acquire("record:abc")
    ws_tab = sess.acquire("ws:42")
    # Tab closes, recording stays.
    sess.release(ws_tab)
    # Wait past grace; with the recording hold still active there must be
    # NO teardown.
    time.sleep(0.1)
    assert torn_down == []
    assert sess.live is True
    sess.release(rec)
    assert _wait_for(lambda: len(torn_down) == 1, timeout=1.0)


def test_release_unknown_token_is_noop():
    """An external object passed to release() (or a token released twice)
    must not blow up — common with finally blocks running after an error
    path already released the same token."""
    sess, _, _, _ = _fake_session()
    sess.release(None)  # type: ignore[arg-type]
    sess.configured = True
    sess.url = "http://x"
    t = sess.acquire("only")
    sess.release(t)
    sess.release(t)  # idempotent


def test_disconnect_clears_configured_and_tears_down():
    """disconnect() forces teardown regardless of holders and clears the
    configured flag — the route guard for "stop recording first" runs at
    a higher level (routes/main.py)."""
    sess, _, _, torn_down = _fake_session()
    sess.configured = True
    sess.url = "http://x"
    t = sess.acquire("only")
    sess.disconnect()
    assert sess.configured is False
    assert sess.live is False
    assert torn_down  # forced teardown ran
    # release a stale token after disconnect — must be idempotent.
    sess.release(t)


def test_state_exposes_configured_and_live():
    """Snapshot must surface both flags so the WS replay can drive the
    new UI binding (pill = configured, dot = live/health)."""
    sess, _, _, _ = _fake_session()
    s = sess.state()
    assert s["connected"] is False
    assert s["live"] is False
    assert s["configured"] is False
    sess.configured = True
    sess.url = "http://x"
    t = sess.acquire("only")
    s = sess.state()
    assert s["live"] is True
    assert s["connected"] is True  # backwards-compat alias
    assert s["configured"] is True
    assert s["holders"] == ["only"]
    sess.release(t)


# ── probe (Pi /info first, ffprobe fallback) ─────────────────────────────
class _FakeResp:
    """Minimal urlopen-context-manager double — supports `with`, .read(),
    and a `.status` attribute matching the urllib API surface we touch."""
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def read(self): return self._body


def test_probe_pi_info_success(monkeypatch):
    """Successful /info JSON → returns mapped fmt, ffprobe is NOT called."""
    payload = b'{"sample_rate": 96000, "channels": 2, "bit_depth": 24}'
    seen: list[str] = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url if hasattr(req, "full_url") else str(req))
        return _FakeResp(payload, status=200)

    def fake_run(*a, **kw):
        raise AssertionError("ffprobe should not be called when /info succeeds")

    monkeypatch.setattr(up_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(up_mod.subprocess, "run", fake_run)
    fmt = up_mod._probe_format("http://pi.local:8000/stream")
    assert fmt == {
        "sample_rate": 96000,
        "channels":    2,
        "bit_depth":   24,
        "codec":       "pcm_s24le",
    }
    # The /info call hits the base, not the original URL with /stream.
    assert seen and seen[0].endswith("/info")
    assert "/stream" not in seen[0]


def test_probe_falls_back_to_ffprobe_on_404(monkeypatch):
    """Non-200 from /info → fall back to the ffprobe path."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    fake_ffprobe_called = {"n": 0}

    class _R:
        returncode = 0
        stdout = '{"streams": [{"sample_rate": "44100", "channels": 2, ' \
                 '"codec_name": "mp3", "bits_per_sample": 16}]}'
        stderr = ""

    def fake_run(*a, **kw):
        fake_ffprobe_called["n"] += 1
        return _R()

    monkeypatch.setattr(up_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(up_mod.subprocess, "run", fake_run)
    fmt = up_mod._probe_format("http://x/stream")
    assert fake_ffprobe_called["n"] == 1
    assert fmt["sample_rate"] == 44100
    assert fmt["codec"] == "mp3"


def test_probe_falls_back_to_ffprobe_on_network_error(monkeypatch):
    """A URLError (DNS failure, connection refused, timeout) routes through
    the ffprobe fallback — same behavior as a 404."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    class _R:
        returncode = 0
        stdout = '{"streams": [{"sample_rate": "48000", "channels": 1, ' \
                 '"codec_name": "pcm_s16le", "bits_per_sample": 16}]}'
        stderr = ""

    monkeypatch.setattr(up_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(up_mod.subprocess, "run",
                        lambda *a, **kw: _R())
    fmt = up_mod._probe_format("http://offline/stream")
    assert fmt["sample_rate"] == 48000
    assert fmt["channels"] == 1


def test_probe_falls_back_on_missing_keys(monkeypatch):
    """A /info response that's missing required keys (older Pi without
    the new bit_depth field, broken proxy in front) must not crash —
    fall through to ffprobe rather than 500-ing on connect."""
    def fake_urlopen(req, timeout=None):
        return _FakeResp(b'{"sample_rate": 48000}', status=200)

    class _R:
        returncode = 0
        stdout = '{"streams": [{"sample_rate": "48000", "channels": 2, ' \
                 '"codec_name": "pcm_s24le", "bits_per_sample": 24}]}'
        stderr = ""

    monkeypatch.setattr(up_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(up_mod.subprocess, "run", lambda *a, **kw: _R())
    fmt = up_mod._probe_format("http://pi/stream")
    # Came from ffprobe (the only one with bit_depth=24 here).
    assert fmt["bit_depth"] == 24


# ── connect() integration with new probe + lifecycle ─────────────────────
def test_connect_sets_configured_without_spawning_when_no_holders(monkeypatch):
    """connect() probes + flips configured but does NOT spawn ffmpeg
    until a holder appears. This is the AUTO_CONNECT semantics that lets
    a quiescent server idle to ~0% CPU."""
    monkeypatch.setattr(up_mod, "_probe_format",
                        lambda url: {"sample_rate": 48000, "channels": 2,
                                     "bit_depth": 16, "codec": "pcm_s16le"})
    sess, _, spawned, _ = _fake_session()
    fmt = sess.connect("http://x")
    assert sess.configured is True
    assert sess.live is False
    assert spawned == []
    assert fmt["sample_rate"] == 48000


def test_connect_spawns_immediately_if_holder_already_present(monkeypatch):
    """If a tab connected first and acquired a hold while the URL was
    blank, the subsequent connect() must spawn right away — otherwise the
    user would see the URL set up but no audio flowing."""
    monkeypatch.setattr(up_mod, "_probe_format",
                        lambda url: {"sample_rate": 48000, "channels": 2,
                                     "bit_depth": 16, "codec": "pcm_s16le"})
    sess, _, spawned, _ = _fake_session()
    pre_token = sess.acquire("ws:before-connect")
    sess.connect("http://x")
    assert spawned, "expected spawn on connect with holders > 0"
    assert sess.live is True
    sess.release(pre_token)
