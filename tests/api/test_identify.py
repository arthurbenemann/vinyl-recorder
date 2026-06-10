"""API tests for `POST /api/identify` (AcoustID identify-by-audio).

The acoustid service is stubbed — fingerprint math and lookup mapping are
pinned in tests/unit/test_acoustid.py. Here we cover the route contract:
feature gating, target resolution (raw filename / album_id / traversal),
and error mapping.
"""
from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


def _enable(monkeypatch, *, candidates=None, fail=None):
    """Flip the feature on and stub the service pipeline."""
    from services import acoustid as ac

    monkeypatch.setattr(ac, "ACOUSTID_API_KEY", "k3y")

    def fake_fingerprint(path):
        if fail == "fingerprint":
            raise ac.AcoustidError("fpcalc not found — the chromaprint "
                                   "package is missing from this image")
        return 100.0, "FP"

    def fake_lookup(fp, duration):
        if fail == "lookup":
            raise ac.AcoustidError("AcoustID error: invalid API key")
        return candidates or []

    monkeypatch.setattr(ac, "fingerprint", fake_fingerprint)
    monkeypatch.setattr(ac, "lookup", fake_lookup)
    return ac


def test_identify_disabled_returns_503(monkeypatch):
    from services import acoustid as ac
    monkeypatch.setattr(ac, "ACOUSTID_API_KEY", "")
    r = _client().post("/api/identify", json={"filename": "x.flac"})
    assert r.status_code == 503
    assert "ACOUSTID_API_KEY" in r.json()["detail"]


def test_identify_requires_a_target(monkeypatch):
    _enable(monkeypatch)
    r = _client().post("/api/identify", json={})
    assert r.status_code == 400


def test_identify_unknown_file_404(monkeypatch):
    _enable(monkeypatch)
    r = _client().post("/api/identify", json={"filename": "nope.flac"})
    assert r.status_code == 404


def test_identify_rejects_traversal(monkeypatch):
    _enable(monkeypatch)
    r = _client().post("/api/identify",
                       json={"filename": "../../../etc/passwd"})
    assert r.status_code == 404


def test_identify_happy_path_filename(monkeypatch):
    from state import RAW_DIR
    cand = [{"mbid": "abc", "title": "T", "artist": "A", "year": "1971",
             "score": 95, "track_count": 8}]
    _enable(monkeypatch, candidates=cand)
    p = RAW_DIR / "identify_me.flac"
    p.write_bytes(b"\x66\x4c\x61\x43" + b"x" * 64)
    try:
        r = _client().post("/api/identify",
                           json={"filename": "identify_me.flac"})
        assert r.status_code == 200
        assert r.json() == {"candidates": cand}
    finally:
        p.unlink(missing_ok=True)


def test_identify_happy_path_album_first_side(monkeypatch):
    from services import albums_fs
    from state import RAW_DIR
    cand = [{"mbid": "abc", "title": "T", "artist": "A", "year": "",
             "score": 80, "track_count": 0}]
    ac = _enable(monkeypatch, candidates=cand)
    seen = {}
    real_fp = ac.fingerprint

    def spy_fingerprint(path):
        seen["path"] = path
        return real_fp(path)

    monkeypatch.setattr(ac, "fingerprint", spy_fingerprint)
    (RAW_DIR / "sideA.flac").write_bytes(b"\x66\x4c\x61\x43" + b"a" * 64)
    aid, _ = albums_fs.create_album(["sideA.flac"], {})
    try:
        r = _client().post("/api/identify", json={"album_id": aid})
        assert r.status_code == 200
        assert r.json() == {"candidates": cand}
        assert seen["path"].name == "sideA.flac"
    finally:
        d = albums_fs.album_dir(aid)
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
        cache = d / ".cache"
        if cache.is_dir():
            for f in cache.glob("*"):
                f.unlink(missing_ok=True)
            cache.rmdir()
        d.rmdir()


def test_identify_unknown_album_404(monkeypatch):
    _enable(monkeypatch)
    r = _client().post("/api/identify", json={"album_id": "ffffffff"})
    assert r.status_code == 404
    # Invalid slug shape is also a 404, not a 500.
    r = _client().post("/api/identify", json={"album_id": "../../x"})
    assert r.status_code == 404


def test_identify_fingerprint_failure_maps_to_502(monkeypatch):
    from state import RAW_DIR
    _enable(monkeypatch, fail="fingerprint")
    p = RAW_DIR / "broken.flac"
    p.write_bytes(b"\x66\x4c\x61\x43")
    try:
        r = _client().post("/api/identify", json={"filename": "broken.flac"})
        assert r.status_code == 502
        assert "fpcalc" in r.json()["detail"]
    finally:
        p.unlink(missing_ok=True)


def test_identify_lookup_failure_maps_to_502(monkeypatch):
    from state import RAW_DIR
    _enable(monkeypatch, fail="lookup")
    p = RAW_DIR / "lk.flac"
    p.write_bytes(b"\x66\x4c\x61\x43")
    try:
        r = _client().post("/api/identify", json={"filename": "lk.flac"})
        assert r.status_code == 502
        assert "AcoustID" in r.json()["detail"]
    finally:
        p.unlink(missing_ok=True)
