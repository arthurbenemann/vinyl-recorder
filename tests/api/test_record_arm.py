"""API tests for armed auto-record (`/api/record/{arm,disarm}`).

The real trigger path needs a live upstream + ffmpeg; here we swap in a
fake upstream that records acquire/release/subscribe calls and hands us
the registered sink, so we can drive the whole arm lifecycle — including
the fire and the self-disarm timeout — with synthetic PCM and no audio
stack. Detector math itself is pinned in tests/unit/test_arm_detector.py.
"""
import struct
import time

from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


class _FakeUpstream:
    """Just enough surface for the arm endpoints."""

    def __init__(self, configured=True, live=True):
        self.configured = configured
        self.live = live
        self.fmt = {"sample_rate": 500, "channels": 1}  # 1000 B/s at s16
        self.sample_format = "s16le"
        self.holds: list[str] = []
        self.released: list[str] = []
        self.sink = None
        self.on_close = None
        self.unsubscribed: list[str] = []

    def acquire(self, reason):
        self.holds.append(reason)
        return reason

    def release(self, token):
        self.released.append(token)

    def subscribe(self, name, sink, on_close=None):
        if not self.live:
            raise RuntimeError("upstream not connected")
        self.sink = sink
        self.on_close = on_close

    def unsubscribe(self, name):
        self.unsubscribed.append(name)
        self.sink = None


def _chunk(amplitude: int, seconds: float, bytes_per_second=1000) -> bytes:
    n = int(seconds * bytes_per_second) // 2
    return struct.pack(f"<{n}h", *([amplitude] * n))


def _install(monkeypatch, fake):
    from routes import recordings as recs_mod
    monkeypatch.setattr(recs_mod, "upstream", fake)
    # Disk checks aside — they have their own tests.
    monkeypatch.setattr(recs_mod, "disk_space_error", lambda *a, **kw: None)
    return recs_mod


def _disarm_quietly(recs_mod):
    """Test cleanup — module-level arm state must not leak across tests."""
    try:
        recs_mod._disarm_impl("user")
    except Exception:
        pass


ARM_BODY = {"stream_url": "http://x/stream", "duration": 0,
            "auto_stop_on_silence": True, "silence_seconds": 10}


def test_arm_requires_configured_upstream(monkeypatch):
    _install(monkeypatch, _FakeUpstream(configured=False))
    r = _client().post("/api/record/arm", json=ARM_BODY)
    assert r.status_code == 409


def test_arm_fails_when_upstream_wont_start(monkeypatch):
    fake = _FakeUpstream(live=False)
    _install(monkeypatch, fake)
    r = _client().post("/api/record/arm", json=ARM_BODY)
    assert r.status_code == 503
    # The acquired hold must not leak.
    assert fake.released == fake.holds


def test_arm_disarm_lifecycle(monkeypatch):
    fake = _FakeUpstream()
    recs_mod = _install(monkeypatch, fake)
    client = _client()
    try:
        r = client.post("/api/record/arm", json=ARM_BODY)
        assert r.status_code == 200 and r.json() == {"armed": True}
        assert recs_mod.arm_is_armed()
        assert fake.sink is not None
        assert fake.holds == ["arm"]

        # Second arm (other tab) is an idempotent no-op — no extra hold.
        r = client.post("/api/record/arm", json=ARM_BODY)
        assert r.status_code == 200 and r.json() == {"armed": True}
        assert fake.holds == ["arm"]

        r = client.post("/api/record/disarm")
        assert r.status_code == 200 and r.json() == {"armed": False}
        assert not recs_mod.arm_is_armed()
        assert fake.unsubscribed == ["arm"]
        assert fake.released == ["arm"]

        # Disarm when not armed → no-op, not an error.
        r = client.post("/api/record/disarm")
        assert r.status_code == 200 and r.json() == {"armed": False}
        assert fake.released == ["arm"]
    finally:
        _disarm_quietly(recs_mod)


def test_arm_fires_start_after_silence_then_signal(monkeypatch):
    fake = _FakeUpstream()
    recs_mod = _install(monkeypatch, fake)
    started: list[dict] = []
    monkeypatch.setattr(recs_mod, "_start_recording_impl",
                        lambda req: started.append({"album": req.album}))
    client = _client()
    try:
        assert client.post("/api/record/arm", json=ARM_BODY).status_code == 200
        # Quiet-confirm: 3 s of silence in 100 ms chunks (the detector's
        # default window is 2.5 s — see services/arm.py)…
        for _ in range(30):
            fake.sink(_chunk(0, 0.1))
        # …then sustained signal. The fire happens on a spawned thread;
        # poll for it.
        for _ in range(20):
            fake.sink(_chunk(20000, 0.1))
        deadline = time.monotonic() + 5.0
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started, "armed trigger never started a recording"
        # Still armed afterwards — the arm survives the recordings it
        # starts (hands-free multi-side capture).
        assert recs_mod.arm_is_armed()
    finally:
        _disarm_quietly(recs_mod)


def test_arm_self_disarms_after_deadline(monkeypatch):
    fake = _FakeUpstream()
    recs_mod = _install(monkeypatch, fake)
    # Deadline ~0: the first sink chunk notices it has passed.
    monkeypatch.setattr(recs_mod, "ARM_AUTO_DISARM_HOURS", 0.0)
    client = _client()
    try:
        assert client.post("/api/record/arm", json=ARM_BODY).status_code == 200
        fake.sink(_chunk(0, 0.1))
        deadline = time.monotonic() + 5.0
        while recs_mod.arm_is_armed() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not recs_mod.arm_is_armed()
        assert fake.released == ["arm"]
    finally:
        _disarm_quietly(recs_mod)


def test_upstream_close_disarms(monkeypatch):
    """The subscriber's on_close fires when the upstream tears down — the
    arm must follow it down instead of pretending to be in standby."""
    fake = _FakeUpstream()
    recs_mod = _install(monkeypatch, fake)
    client = _client()
    try:
        assert client.post("/api/record/arm", json=ARM_BODY).status_code == 200
        fake.on_close()
        deadline = time.monotonic() + 5.0
        while recs_mod.arm_is_armed() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not recs_mod.arm_is_armed()
    finally:
        _disarm_quietly(recs_mod)
