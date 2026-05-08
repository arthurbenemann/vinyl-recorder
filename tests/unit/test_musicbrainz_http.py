"""Extended unit tests for services/musicbrainz.py.

The HTTP helpers (`_http_json`, `_http_bytes`, `release_full`, `caa_front`)
and the `search_releases` parsing path normally need a live MB endpoint;
we stub urllib.request.urlopen so the parsing branches are still
exercised against canned bodies.
"""
import json

from services import musicbrainz as mb


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, body: bytes, capture: list | None = None):
    """Replace urllib.request.urlopen with a stub that returns `body` and
    optionally records the URL it was called with."""
    def fake_urlopen(req, timeout=None):
        if capture is not None:
            capture.append(req.full_url if hasattr(req, "full_url") else str(req))
        return _FakeResponse(body)

    monkeypatch.setattr(mb.urllib.request, "urlopen", fake_urlopen)


# ── _http_json + _http_bytes wiring ──────────────────────────────────────
def test_http_json_returns_decoded_payload(monkeypatch):
    _patch_urlopen(monkeypatch, b'{"ok": true, "n": 1}')
    assert mb._http_json("http://x") == {"ok": True, "n": 1}


def test_http_bytes_returns_body(monkeypatch):
    _patch_urlopen(monkeypatch, b"\xff\xd8\xff fake jpeg")
    assert mb._http_bytes("http://x") == b"\xff\xd8\xff fake jpeg"


def test_http_bytes_swallows_errors(monkeypatch):
    """Network/IO errors are turned into None — callers fall back to other
    sources without surfacing the exception to the user."""
    def boom(req, timeout=None):
        raise OSError("dns failure")

    monkeypatch.setattr(mb.urllib.request, "urlopen", boom)
    assert mb._http_bytes("http://x") is None


# ── search_releases query construction + parsing ─────────────────────────
def test_search_releases_returns_empty_when_no_terms():
    """No artist + no album → don't even hit the network. Cheap short-
    circuit because the MB UI fires this on every keystroke."""
    assert mb.search_releases("", "") == []


def test_search_releases_quotes_query_terms(monkeypatch):
    """The MB Lucene query needs `field:"value"` formatting; the URL
    encodes the whole query string. We just confirm the URL contains
    both fields and the limit parameter."""
    captured: list[str] = []
    _patch_urlopen(monkeypatch, b'{"releases": []}', capture=captured)

    out = mb.search_releases("Pink Floyd", "Animals", limit=3)
    assert out == []
    url = captured[0]
    # Quoted "Pink Floyd" gets url-encoded — both halves appear.
    assert "Pink" in url and "Animals" in url
    assert "limit=3" in url
    assert "fmt=json" in url


def test_search_releases_parses_release_payload(monkeypatch):
    canned = {
        "releases": [
            {
                "id":             "abc",
                "title":          "Animals",
                "artist-credit":  [{"name": "Pink Floyd"}],
                "date":           "1977-01-23",
                "country":        "UK",
                "media":          [{"format": "Vinyl"}],
                "label-info": [{
                    "label": {"name": "Harvest"},
                    "catalog-number": "SHVL 815",
                }],
                "score": 100,
            },
            {
                "id":    "def",
                "title": "untagged",
            },
        ]
    }
    _patch_urlopen(monkeypatch, json.dumps(canned).encode())
    out = mb.search_releases("Pink Floyd", "Animals", limit=5)
    assert len(out) == 2
    first = out[0]
    assert first["mbid"] == "abc"
    assert first["title"] == "Animals"
    assert first["artist"] == "Pink Floyd"
    # Year derived from the leading 4 chars of `date`.
    assert first["year"] == "1977"
    assert first["country"] == "UK"
    assert first["format"] == "Vinyl"
    assert first["label"] == "Harvest"
    assert first["catalog_number"] == "SHVL 815"
    # Missing fields fall back to "" without raising.
    assert out[1]["mbid"] == "def"
    assert out[1]["title"] == "untagged"
    assert out[1]["artist"] == ""
    assert out[1]["label"] == ""
    assert out[1]["country"] == ""


def test_search_releases_only_artist_provided(monkeypatch):
    captured: list[str] = []
    _patch_urlopen(monkeypatch, b'{"releases": []}', capture=captured)
    mb.search_releases("Solo Artist", "")
    # No `release:"..."` clause when album is empty.
    assert "Solo" in captured[0]
    assert "release%3A" not in captured[0] and "release:" not in captured[0]


# ── release_full + caa_front URL shape ───────────────────────────────────
def test_release_full_hits_release_endpoint(monkeypatch):
    captured: list[str] = []
    _patch_urlopen(monkeypatch, b'{"id": "x"}', capture=captured)
    mb.release_full("3c1c2dab-fcc1-4d1c-9d6f-9ef00bf1f9d7")
    url = captured[0]
    assert "release/3c1c2dab" in url
    # The handler asks for the full set of relations the tag panel needs.
    assert "inc=artist-credits" in url
    assert "labels" in url and "url-rels" in url


def test_caa_front_hits_caa_url(monkeypatch):
    captured: list[str] = []
    _patch_urlopen(monkeypatch, b"jpeg-bytes", capture=captured)
    out = mb.caa_front("3c1c2dab-fcc1-4d1c-9d6f-9ef00bf1f9d7")
    assert out == b"jpeg-bytes"
    assert "coverartarchive.org" in captured[0]
    assert "front-500" in captured[0]
