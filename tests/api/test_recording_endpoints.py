"""API tests for the recordings router (raw side ops).

Stays out of the heavy ffmpeg-spawning paths — those are the e2e suite's
job. Here we cover routing, validation, and the no-upstream / not-found
guard branches that don't need a live audio pipeline.
"""
from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


# ── /api/test-stream ─────────────────────────────────────────────────────
def test_test_stream_reports_ffprobe_failure(monkeypatch):
    """ffprobe returns nonzero (e.g. unreachable URL) → 502 with the
    underlying message in ``detail``. Clients branch on HTTP status."""
    from routes import recordings as recs_mod

    class _FakeProc:
        returncode = 1
        stdout = ""
        stderr = "Server returned 404 Not Found\n"

    monkeypatch.setattr(
        recs_mod.subprocess, "run", lambda *a, **kw: _FakeProc(),
    )
    r = _client().post("/api/test-stream", json={"stream_url": "http://x/none"})
    assert r.status_code == 502
    assert "404" in r.json()["detail"]


def test_test_stream_parses_ffprobe_streams_payload(monkeypatch):
    from routes import recordings as recs_mod

    class _FakeProc:
        returncode = 0
        stdout = (
            '{"streams": [{"sample_rate": "44100", "channels": 2, '
            '"codec_name": "mp3", "bits_per_sample": 0}]}'
        )
        stderr = ""

    monkeypatch.setattr(
        recs_mod.subprocess, "run", lambda *a, **kw: _FakeProc(),
    )
    r = _client().post("/api/test-stream", json={"stream_url": "http://x"})
    assert r.status_code == 200
    body = r.json()
    assert body["sample_rate"] == "44100"
    assert body["channels"] == 2
    assert body["codec"] == "mp3"


def test_test_stream_handles_timeout(monkeypatch):
    """ffprobe TimeoutExpired surfaces as 502 + friendly detail."""
    import subprocess as sp

    from routes import recordings as recs_mod

    def boom(*a, **kw):
        raise sp.TimeoutExpired(cmd="ffprobe", timeout=10)

    monkeypatch.setattr(recs_mod.subprocess, "run", boom)
    r = _client().post("/api/test-stream", json={"stream_url": "http://slow/"})
    assert r.status_code == 502
    assert "Timeout" in r.json()["detail"]


def test_test_stream_handles_other_errors(monkeypatch):
    from routes import recordings as recs_mod

    def boom(*a, **kw):
        raise OSError("ffprobe not found")

    monkeypatch.setattr(recs_mod.subprocess, "run", boom)
    r = _client().post("/api/test-stream", json={"stream_url": "http://x"})
    assert r.status_code == 502
    assert "ffprobe" in r.json()["detail"]


def test_test_stream_offloads_subprocess_to_thread(monkeypatch):
    """ffprobe is a blocking call; the handler must route it through
    ``asyncio.to_thread`` so a slow/unreachable URL can't stall the event
    loop. Regression guard for the bug-hunt fix."""
    from routes import recordings as recs_mod

    seen = {"to_thread": False}

    class _FakeProc:
        returncode = 0
        stdout = '{"streams": [{"sample_rate": "48000", "channels": 1}]}'
        stderr = ""

    async def fake_to_thread(func, *args, **kwargs):
        seen["to_thread"] = True
        # Sanity-check that we're actually wrapping subprocess.run.
        assert func is recs_mod.subprocess.run
        return _FakeProc()

    monkeypatch.setattr(recs_mod.asyncio, "to_thread", fake_to_thread)
    r = _client().post("/api/test-stream", json={"stream_url": "http://x"})
    assert r.status_code == 200
    assert seen["to_thread"] is True
    assert r.json()["sample_rate"] == "48000"


# ── /api/stream-proxy guards ─────────────────────────────────────────────
def test_stream_proxy_returns_409_when_upstream_disconnected():
    # No upstream → handler aborts before spawning ffmpeg. Tests the guard
    # without needing a live audio source.
    r = _client().get("/api/stream-proxy")
    assert r.status_code == 409


# ── /api/log/{session_id} ────────────────────────────────────────────────
def test_get_log_for_unknown_session_returns_empty_lines():
    """An unknown session_id is not an error — it's just a session that's
    been finalised. Frontend polls happily expect an empty list."""
    r = _client().get("/api/log/no-such-sid")
    assert r.status_code == 200
    assert r.json() == {"lines": []}


def test_get_log_returns_in_memory_lines():
    from state import sessions, Session
    sid = "log-test-sid"
    sessions.insert(Session(sid=sid, log_lines=["line one", "line two"]))
    try:
        r = _client().get(f"/api/log/{sid}")
        assert r.status_code == 200
        assert r.json()["lines"] == ["line one", "line two"]
    finally:
        sessions.remove(sid)


def test_get_log_appends_ffmpeg_tail_when_present(tmp_path):
    """When a session has a log file on disk, the response splices the
    last 100 lines after a "── ffmpeg ──" separator."""
    from state import sessions, Session

    sid = "log-with-file"
    log_path = tmp_path / "ffmpeg.log"
    log_path.write_text("err1\nerr2\n", encoding="utf-8")
    sessions.insert(Session(sid=sid, log_lines=["session start"],
                            log_path=str(log_path)))
    try:
        body = _client().get(f"/api/log/{sid}").json()
        assert body["lines"][0] == "session start"
        assert "── ffmpeg ──" in body["lines"]
        assert body["lines"][-2:] == ["err1", "err2"]
    finally:
        sessions.remove(sid)


# ── /api/recordings/{filename}/rename ────────────────────────────────────
def test_rename_unknown_returns_404():
    r = _client().post(
        "/api/recordings/missing.flac/rename",
        json={"new_name": "renamed"},
    )
    assert r.status_code == 404


def test_rename_collision_returns_409():
    """Target stem already exists on disk → 409, source untouched."""
    from state import RAW_DIR
    src = RAW_DIR / "rename_src.flac"
    dst = RAW_DIR / "rename_dst.flac"
    src.write_bytes(b"a")
    dst.write_bytes(b"b")
    try:
        r = _client().post(
            "/api/recordings/rename_src.flac/rename",
            json={"new_name": "rename_dst"},
        )
        assert r.status_code == 409
        # Both files should still be there — nothing got moved.
        assert src.exists()
        assert dst.exists()
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def test_rename_success_moves_file_and_returns_new_name():
    from state import RAW_DIR
    src = RAW_DIR / "before.flac"
    src.write_bytes(b"sample")
    try:
        r = _client().post(
            "/api/recordings/before.flac/rename",
            json={"new_name": "After Rename"},
        )
        assert r.status_code == 200
        body = r.json()
        # safe_name turns spaces into underscores.
        assert body["filename"] == "After_Rename.flac"
        assert (RAW_DIR / "After_Rename.flac").exists()
        assert not src.exists()
    finally:
        src.unlink(missing_ok=True)
        (RAW_DIR / "After_Rename.flac").unlink(missing_ok=True)


# ── /api/recordings/{filename} DELETE ────────────────────────────────────
def test_delete_unknown_returns_404():
    r = _client().delete("/api/recordings/no-such.flac")
    assert r.status_code == 404


def test_delete_soft_deletes_to_trash():
    """The DELETE endpoint moves the file to TRASH_DIR (soft-delete) and
    returns a `trash_token` the client can POST back to /restore for the
    Undo UX. The original file vanishes from raw/ — that's still the
    user-visible contract — and the response carries the metadata the
    library JS needs to render the Undo toast."""
    from state import RAW_DIR, TRASH_DIR
    f = RAW_DIR / "to_delete.flac"
    f.write_bytes(b"x")
    try:
        r = _client().delete("/api/recordings/to_delete.flac")
        assert r.status_code == 200
        body = r.json()
        # The token names the trash entry and embeds the original name so
        # the restore endpoint can recover it without server-side state.
        assert isinstance(body.get("trash_token"), str) and body["trash_token"], body
        assert body["original"] == "to_delete.flac"
        assert isinstance(body["ttl_seconds"], int) and body["ttl_seconds"] > 0
        assert not f.exists()
        # The bytes still live in the trash directory under the token.
        trash_path = TRASH_DIR / body["trash_token"]
        assert trash_path.exists()
        assert trash_path.read_bytes() == b"x"
    finally:
        f.unlink(missing_ok=True)
        # Clean up any leftover trash entry from this test.
        for entry in TRASH_DIR.glob("to_delete__trash_*.flac"):
            entry.unlink(missing_ok=True)


def test_restore_recovers_soft_deleted_file():
    """POST /api/recordings/restore with the trash_token returned by
    DELETE puts the file back under its original name in raw/. After
    restore, the trash entry is gone and the file is queryable via
    the regular listing."""
    from state import RAW_DIR, TRASH_DIR
    f = RAW_DIR / "to_restore.flac"
    f.write_bytes(b"hello")
    try:
        del_resp = _client().delete("/api/recordings/to_restore.flac")
        token = del_resp.json()["trash_token"]
        assert not f.exists()
        r = _client().post("/api/recordings/restore", json={"trash_token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"filename": "to_restore.flac"}
        assert f.exists()
        assert f.read_bytes() == b"hello"
        # Trash entry got renamed back, so it should no longer be present.
        assert not (TRASH_DIR / token).exists()
    finally:
        f.unlink(missing_ok=True)
        for entry in TRASH_DIR.glob("to_restore__trash_*.flac"):
            entry.unlink(missing_ok=True)


def test_restore_unknown_token_returns_404():
    """Restore with an unknown / expired / fabricated token surfaces a
    404 rather than 200 — the UI surfaces this as a 'restore failed'
    toast rather than silently doing nothing."""
    r = _client().post(
        "/api/recordings/restore",
        json={"trash_token": "ghost__trash_deadbeef.flac"},
    )
    assert r.status_code == 404


def test_restore_missing_body_returns_422():
    """The endpoint requires a `trash_token` field; missing it surfaces
    as a 422 (matches the FastAPI / pydantic contract for malformed
    requests on every other endpoint)."""
    r = _client().post("/api/recordings/restore", json={})
    assert r.status_code == 422


# ── /api/recordings/bulk-delete ──────────────────────────────────────────
def test_bulk_delete_partitions_present_and_missing():
    from state import RAW_DIR
    a = RAW_DIR / "bulk_a.flac"
    a.write_bytes(b"a")
    try:
        r = _client().post(
            "/api/recordings/bulk-delete",
            json={"filenames": ["bulk_a.flac", "bulk_missing.flac"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] == ["bulk_a.flac"]
        assert body["missing"] == ["bulk_missing.flac"]
        assert not a.exists()
    finally:
        a.unlink(missing_ok=True)


def test_bulk_delete_empty_list_returns_empty_partitions():
    r = _client().post("/api/recordings/bulk-delete", json={"filenames": []})
    assert r.status_code == 200
    assert r.json() == {"deleted": [], "missing": []}


# ── /api/download/{filename} ─────────────────────────────────────────────
def test_download_unknown_returns_404():
    r = _client().get("/api/download/no-such.flac")
    assert r.status_code == 404


def test_download_streams_file_bytes():
    from state import RAW_DIR
    f = RAW_DIR / "downloadme.flac"
    payload = b"\xff\xff\xfa\x52" + b"sample audio bytes"
    f.write_bytes(payload)
    try:
        r = _client().get("/api/download/downloadme.flac")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/flac"
        # FastAPI's FileResponse sets a Content-Disposition with the filename.
        assert "downloadme.flac" in r.headers.get("content-disposition", "")
        assert r.content == payload
    finally:
        f.unlink(missing_ok=True)


# ── /api/record/pause + /resume guards ───────────────────────────────────
def test_pause_unknown_session_returns_404():
    r = _client().post("/api/record/pause/no-such")
    assert r.status_code == 404


def test_resume_unknown_session_returns_404():
    r = _client().post("/api/record/resume/no-such")
    assert r.status_code == 404


def test_pause_and_resume_already_paused_or_running_are_idempotent():
    """A second pause on an already-paused session returns the same shape;
    same for resume on a non-paused session. Idempotency means the UI's
    optimistic state never wedges the server."""
    from state import sessions, Session

    sid = "pause-sentinel"
    # Minimal session shape — pause/resume only touches `paused` /
    # `pause_started` / `start_time` / `sess_state` and appends to the
    # session's log_lines. We never invoke any subprocess paths.
    import time as _t
    sessions.insert(Session(
        sid=sid, proc=None, paused=False,
        sess_state={"paused": False},
        start_time=_t.time(), outfile="/tmp/x.flac",
    ))
    try:
        c = _client()
        # Resume on a non-paused session → {paused: False}.
        r = c.post(f"/api/record/resume/{sid}")
        assert r.status_code == 200
        assert r.json() == {"paused": False}

        # Pause once → {paused: True}.
        r = c.post(f"/api/record/pause/{sid}")
        assert r.status_code == 200
        assert r.json() == {"paused": True}

        # Pause again → still {paused: True}, no spurious double-event.
        r = c.post(f"/api/record/pause/{sid}")
        assert r.status_code == 200
        assert r.json() == {"paused": True}

        # Resume now flips back to False.
        r = c.post(f"/api/record/resume/{sid}")
        assert r.status_code == 200
        assert r.json() == {"paused": False}
    finally:
        sessions.remove(sid)
