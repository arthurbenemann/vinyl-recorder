"""Unit tests for `app/services/acoustid.py`.

fpcalc and the AcoustID HTTP call are both stubbed — these tests pin the
subprocess argv / JSON parsing, the lookup request shape, and the
flatten/dedup/rank of the response payload into tag-panel candidates.
"""
import json
import subprocess
import urllib.request

import pytest

from services import acoustid as ac


# ── fingerprint ──────────────────────────────────────────────────────────

class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_fingerprint_argv_and_parse(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc(stdout=json.dumps(
            {"duration": 1273.4, "fingerprint": "AQAAfp_abc"}))

    monkeypatch.setattr(ac.subprocess, "run", fake_run)
    p = tmp_path / "side.flac"
    duration, fp = ac.fingerprint(p)
    assert duration == 1273.4 and fp == "AQAAfp_abc"
    assert seen["cmd"][0] == "fpcalc"
    assert "-json" in seen["cmd"]
    # -length caps the fingerprinted window, not the reported duration.
    assert seen["cmd"][seen["cmd"].index("-length") + 1] == \
        str(ac.FINGERPRINT_SECONDS)
    assert seen["cmd"][-1] == str(p)


def test_fingerprint_missing_binary(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("fpcalc")

    monkeypatch.setattr(ac.subprocess, "run", fake_run)
    with pytest.raises(ac.AcoustidError, match="fpcalc not found"):
        ac.fingerprint(tmp_path / "x.flac")


def test_fingerprint_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(ac.subprocess, "run",
                        lambda *a, **kw: _Proc(rc=1, stderr="bad file"))
    with pytest.raises(ac.AcoustidError, match="bad file"):
        ac.fingerprint(tmp_path / "x.flac")


def test_fingerprint_timeout(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(ac.subprocess, "run", fake_run)
    with pytest.raises(ac.AcoustidError, match="timed out"):
        ac.fingerprint(tmp_path / "x.flac")


def test_fingerprint_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr(ac.subprocess, "run",
                        lambda *a, **kw: _Proc(stdout="not json"))
    with pytest.raises(ac.AcoustidError, match="parse"):
        ac.fingerprint(tmp_path / "x.flac")


# ── lookup ───────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_lookup_posts_form_encoded(monkeypatch):
    monkeypatch.setattr(ac, "ACOUSTID_API_KEY", "k3y")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        seen["ctype"] = req.get_header("Content-type")
        return _FakeResponse({"status": "ok", "results": []})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ac.lookup("FPDATA", 1273.9) == []
    assert seen["url"].endswith("/v2/lookup")
    assert seen["ctype"] == "application/x-www-form-urlencoded"
    body = dict(p.split("=", 1) for p in seen["body"].split("&"))
    assert body["client"] == "k3y"
    assert body["fingerprint"] == "FPDATA"
    assert body["duration"] == "1273"
    assert "recordings" in body["meta"]


def test_lookup_error_status_raises(monkeypatch):
    monkeypatch.setattr(ac, "ACOUSTID_API_KEY", "k3y")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResponse(
            {"status": "error", "error": {"message": "invalid API key"}}))
    with pytest.raises(ac.AcoustidError, match="invalid API key"):
        ac.lookup("FP", 100)


def test_lookup_network_error_wrapped(monkeypatch):
    monkeypatch.setattr(ac, "ACOUSTID_API_KEY", "k3y")

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ac.AcoustidError, match="lookup failed"):
        ac.lookup("FP", 100)


# ── map_candidates ───────────────────────────────────────────────────────

def _payload(results):
    return {"status": "ok", "results": results}


def test_map_candidates_shape_and_rank():
    data = _payload([{
        "score": 0.97,
        "recordings": [{
            "artists": [{"name": "Miles Davis"}],
            "releases": [
                {"id": "aaa", "title": "Kind of Blue",
                 "date": {"year": 1959}, "track_count": 5},
            ],
        }],
    }, {
        "score": 0.41,
        "recordings": [{
            "artists": [{"name": "Someone Else"}],
            "releases": [{"id": "bbb", "title": "Other Album"}],
        }],
    }])
    out = ac.map_candidates(data)
    assert [c["mbid"] for c in out] == ["aaa", "bbb"]   # score-ranked
    top = out[0]
    assert top == {"mbid": "aaa", "title": "Kind of Blue",
                   "artist": "Miles Davis", "year": "1959",
                   "score": 97, "track_count": 5}


def test_map_candidates_dedups_and_backfills():
    """The same release matched via two recordings collapses to one
    candidate with the best score and merged metadata."""
    data = _payload([{
        "score": 0.5,
        "recordings": [{
            "artists": [],                       # no artist here…
            "releases": [{"id": "aaa", "title": "Album"}],
        }],
    }, {
        "score": 0.9,
        "recordings": [{
            "artists": [{"name": "Artist"}],     # …but here
            "releases": [{"id": "aaa", "title": "Album",
                          "date": {"year": 1971}, "track_count": 9}],
        }],
    }])
    out = ac.map_candidates(data)
    assert len(out) == 1
    assert out[0]["score"] == 90
    assert out[0]["artist"] == "Artist"
    assert out[0]["year"] == "1971"
    assert out[0]["track_count"] == 9


def test_map_candidates_caps_and_skips_idless():
    rels = [{"id": f"id{i}", "title": f"T{i}"} for i in range(20)]
    rels.append({"title": "no id — skipped"})
    data = _payload([{"score": 0.8,
                      "recordings": [{"artists": [], "releases": rels}]}])
    out = ac.map_candidates(data)
    assert len(out) == 8                          # _MAX_CANDIDATES
    assert all(c["mbid"] for c in out)


def test_map_candidates_empty_payload():
    assert ac.map_candidates(_payload([])) == []
    assert ac.map_candidates({"status": "ok"}) == []
