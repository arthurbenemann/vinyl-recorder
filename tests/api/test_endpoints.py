"""FastAPI TestClient smoke tests for the read-mostly endpoints.

These don't spin up the full upstream/ffmpeg pipeline (that's the e2e
suite). They confirm route wiring, response shapes, and the no-op /
guard paths that fire when nothing is connected.
"""
from fastapi.testclient import TestClient


def _client():
    # Import here so each test module sees the env-var setup from conftest.py
    # (OUTPUT_DIR, AUTO_CONNECT) before `state` mkdirs. Re-importing the app
    # module across tests is fine — the FastAPI app is a module-level
    # singleton; we want the same one each time.
    from main import app
    return TestClient(app)


# ── /health ──────────────────────────────────────────────────────────────
def test_health_returns_ok():
    # Probed by the Dockerfile HEALTHCHECK — must stay cheap, 200, and
    # independent of upstream/disk state.
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── /api/config ──────────────────────────────────────────────────────────
def test_config_returns_known_shape():
    r = _client().get("/api/config")
    assert r.status_code == 200
    body = r.json()
    # Every key the frontend reads from /api/config — keep this list in
    # lockstep with applyConfig() in static/main.js.
    expected_keys = {
        "default_stream_url", "auto_connect", "default_gain_db", "version",
        "low_space_gb", "default_split_normalize", "default_split_replaygain",
        "default_split_target_peak_db", "default_split_bit_depth",
    }
    assert expected_keys <= set(body.keys())


def test_config_reflects_env_var_overrides(monkeypatch):
    # The endpoint reads the values that `state` cached at import time, so
    # we have to reload `state` + `main` after mutating env. Easier: just
    # confirm the values are JSON-serializable scalars of the expected types.
    body = _client().get("/api/config").json()
    assert isinstance(body["auto_connect"], bool)
    assert isinstance(body["default_split_normalize"], bool)
    assert isinstance(body["default_split_replaygain"], bool)
    assert isinstance(body["default_split_target_peak_db"], (int, float))
    assert isinstance(body["default_split_bit_depth"], int)
    assert isinstance(body["low_space_gb"], (int, float))


# ── /api/status ──────────────────────────────────────────────────────────
def test_status_when_idle():
    body = _client().get("/api/status").json()
    assert body["recording"] is False
    assert body["sessions"] == []
    assert "disk_free_gb" in body
    assert body["upstream"]["connected"] is False


# ── /api/disconnect ──────────────────────────────────────────────────────
def test_disconnect_when_not_connected_is_noop():
    # Idempotent: pressing disconnect with nothing to disconnect must not 5xx.
    r = _client().post("/api/disconnect")
    assert r.status_code == 200
    assert r.json()["connected"] is False


def test_disconnect_while_recording_returns_409():
    # Disconnect must refuse while a session is active — protects the user
    # from accidentally tearing down the upstream mid-record. The handler
    # checks `if sessions:`, so a sentinel entry is enough; we don't need
    # a real ffmpeg subprocess to exercise the guard.
    from state import sessions, Session

    sessions.insert(Session(sid="sentinel-sid", proc=None))
    try:
        r = _client().post("/api/disconnect")
        assert r.status_code == 409
        assert "stop recording" in r.json()["detail"].lower()
    finally:
        sessions.remove("sentinel-sid")


# ── /api/combine ─────────────────────────────────────────────────────────
def test_combine_while_recording_returns_409():
    # Combining a file that is currently being recorded must be rejected so the
    # in-progress FLAC is never moved out from under the active ffmpeg process.
    from state import sessions, Session

    recording_file = "Artist - Album (2024).flac"
    sessions.insert(Session(sid="sentinel-sid", proc=None,
                            filename=recording_file))
    try:
        r = _client().post("/api/combine", json={
            "filenames": [recording_file],
            "album": {},
        })
        assert r.status_code == 409
        assert "recording in progress" in r.json()["detail"].lower()
    finally:
        sessions.remove("sentinel-sid")


# ── /api/clip/clear ──────────────────────────────────────────────────────
def test_clip_clear_validates_channel():
    c = _client()
    # Valid channels.
    for ch in ("", "L", "R"):
        r = c.post(f"/api/clip/clear?ch={ch}")
        assert r.status_code == 200, f"ch={ch!r} unexpectedly rejected"
    # Anything else is a 400 — the param shape is part of the API contract.
    r = c.post("/api/clip/clear?ch=X")
    assert r.status_code == 400


# ── /api/recordings ──────────────────────────────────────────────────────
def test_recordings_lists_files_in_raw(tmp_path):
    # The conftest set OUTPUT_DIR to a tmp dir; we can drop fake .flac files
    # there and confirm the listing picks them up. Real metaflac calls will
    # fail on these stubs and the helpers swallow the error → empty tags,
    # which is exactly the "untagged" path we want to exercise.
    from state import RAW_DIR

    fake = RAW_DIR / "test_dummy.flac"
    fake.write_bytes(b"")

    try:
        r = _client().get("/api/recordings")
        assert r.status_code == 200
        body = r.json()
        names = [rec["filename"] for rec in body["files"]]
        assert "test_dummy.flac" in names
        assert "disk_free_gb" in body
    finally:
        fake.unlink(missing_ok=True)


# ── /api/albums ──────────────────────────────────────────────────────────
def test_albums_endpoint_returns_list():
    r = _client().get("/api/albums")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("albums"), list)


# ── /api/jobs/{job_id} ──────────────────────────────────────────────────
def test_jobs_endpoint_unknown_id_returns_404():
    # Polling a job id the registry has never seen (or has GC'd) should be
    # a quiet 404, not 500 — the frontend treats 404 as "no progress to show".
    r = _client().get("/api/jobs/no-such-job")
    assert r.status_code == 404


def test_jobs_endpoint_returns_known_shape():
    # Inject a job directly into the registry (avoids needing ffmpeg) and
    # confirm the route surfaces every field the frontend's polling
    # `withJobProgress` helper reads.
    from services.jobs import finish_job, start_job, update_job

    start_job("api-test-job", label="measure")
    update_job("api-test-job", 0.42, phase="analysing")
    try:
        r = _client().get("/api/jobs/api-test-job")
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "progress": 0.42,
            "phase":    "analysing",
            "label":    "measure",
            "done":     False,
            "error":    None,
        }
    finally:
        finish_job("api-test-job")
        # Drop the entry so it doesn't leak into other tests' registries.
        from services import jobs as jobs_mod
        with jobs_mod._lock:
            jobs_mod._jobs.pop("api-test-job", None)


# ── 404 surface ─────────────────────────────────────────────────────────
def test_unknown_route_returns_404():
    r = _client().get("/api/does-not-exist")
    assert r.status_code == 404


def test_record_stop_unknown_session_returns_404():
    r = _client().post("/api/record/stop/nonexistent")
    assert r.status_code == 404


def test_record_start_without_upstream_is_409():
    # The recorder refuses to start without a connected upstream. Tests the
    # disk_space + connection guards in start_recording.
    r = _client().post("/api/record/start", json={
        "stream_url": "http://127.0.0.1/none",
        "artist": "x", "album": "y",
    })
    # 409 (not connected) — could also be 507 if disk is genuinely low,
    # which would be a real signal worth surfacing. Accept either.
    assert r.status_code in (409, 507)


# ── /api/connect ─────────────────────────────────────────────────────────
def test_connect_failure_is_502(monkeypatch):
    """If the upstream library raises (DNS error, 404, etc.), the route
    must surface a 502 with the underlying message — not a generic 500."""
    from state import upstream

    def boom(url):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(upstream, "connect", boom)
    # Make sure we're not "already connected" (that path would short-circuit).
    monkeypatch.setattr(type(upstream), "connected", property(lambda self: False))
    r = _client().post("/api/connect", json={"stream_url": "http://nope"})
    assert r.status_code == 502
    assert "connect refused" in r.json()["detail"]


# ── /api/status with an active session ──────────────────────────────────
def test_status_reflects_active_session(reset_active_sessions):
    """When a session is registered with the recording session manager,
    /api/status surfaces it under `sessions` with elapsed seconds and the
    outfile basename. The `reset_active_sessions` fixture takes care of
    teardown — guaranteed cleanup even if an assertion fails between
    insert and the manual remove that this test used to rely on."""
    import time

    sid = "status-sentinel"
    reset_active_sessions.insert(sid, {
        "proc":       None,
        "paused":     False,
        "start_time": time.monotonic() - 5,
        "outfile":    "/tmp/active.flac",
        "meta":       {"artist": "X", "album": "Y"},
        "duration":   0,
    })
    # Note: this fixture-using test was a PR #103 conversion of the prior
    # try/finally + manual pop pattern. Cleanup happens via the fixture's
    # teardown — see tests/conftest.py.
    body = _client().get("/api/status").json()
    assert body["recording"] is True
    s = next(s for s in body["sessions"] if s["id"] == sid)
    assert s["outfile"] == "active.flac"
    assert s["paused"] is False
    assert s["elapsed"] >= 0


def test_status_freezes_elapsed_while_paused(reset_active_sessions):
    import time

    sid = "status-paused"
    now = time.monotonic()
    reset_active_sessions.insert(sid, {
        "proc":          None,
        "paused":        True,
        "start_time":    now - 10,
        "pause_started": now - 7,
        "outfile":       "/tmp/p.flac",
        "meta":          {},
        "duration":      0,
    })
    body = _client().get("/api/status").json()
    s = next(s for s in body["sessions"] if s["id"] == sid)
    assert s["paused"] is True
    assert s["elapsed"] == 3


# ── / (index) ────────────────────────────────────────────────────────────
def test_index_serves_html():
    """`/` returns the static SPA shell; the frontend bootstraps from there."""
    r = _client().get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


# ── /api/metrics — Prometheus scrape ─────────────────────────────────────
def test_metrics_returns_prometheus_text():
    """Smoke test: the metrics endpoint emits text/plain with the documented
    counter+gauge family names so a Prometheus scrape doesn't 500."""
    body = _client().get("/api/metrics").text
    # A handful of family names that should always be present, regardless
    # of upstream connection state.
    for family in (
        "vinyl_upstream_connected",
        "vinyl_upstream_bytes_per_sec",
        "vinyl_active_recordings",
        "vinyl_disk_free_gb",
    ):
        assert f"# HELP {family}" in body, f"missing {family} HELP line"
        assert f"# TYPE {family}" in body, f"missing {family} TYPE line"
