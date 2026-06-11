"""Unit tests for the Discogs collection helpers — pagination/cache logic
and the fuzzy matcher that surfaces owned releases in the candidate panel.

We never hit the live Discogs API in tests; `_http_json_with_token` is
monkeypatched to return canned page payloads.
"""
from services import discogs


# ── Fixtures ─────────────────────────────────────────────────────────────
def _make_item(rid: int, artist: str, title: str, year: int = 1972,
               label: str = "Blue Note", catno: str = "BN-1234",
               fmt_name: str = "Vinyl", fmt_descs=None, cover: str = "") -> dict:
    """Build a minimal Discogs collection-API item shaped like the real
    /users/.../folders/0/releases response."""
    fmt_descs = fmt_descs or ["LP", "Album"]
    return {
        "id": rid,
        "instance_id": rid * 10,
        "basic_information": {
            "id":        rid,
            "title":     title,
            "year":      year,
            "artists":   [{"name": artist}],
            "labels":    [{"name": label, "catno": catno}],
            "formats":   [{"name": fmt_name, "descriptions": fmt_descs}],
            "cover_image": cover,
            "thumb":       cover,
        },
    }


def _reset_cache():
    """Tests share module state; clear the cache so each one starts fresh."""
    with discogs._collection_lock:
        discogs._collection_cache.clear()


# ── _summarize_release ───────────────────────────────────────────────────
def test_summarize_release_extracts_canonical_fields():
    item = _make_item(123, "Miles Davis", "Kind of Blue", year=1959,
                      label="Columbia", catno="CL 1355",
                      fmt_name="Vinyl", fmt_descs=["LP", "Stereo"])
    out = discogs._summarize_release(item)
    assert out["discogs_release_id"] == 123
    assert out["title"]  == "Kind of Blue"
    assert out["artist"] == "Miles Davis"
    assert out["year"]   == "1959"
    assert out["label"]  == "Columbia"
    assert out["catno"]  == "CL 1355"
    assert "Vinyl" in out["format"]
    assert "LP" in out["format"]


def test_summarize_release_handles_missing_fields():
    out = discogs._summarize_release({"id": 7, "basic_information": {"id": 7, "title": "x"}})
    assert out["discogs_release_id"] == 7
    assert out["artist"] == ""
    assert out["labels"] == []
    assert out["formats"] == []


# ── collection_releases (paginated fetch + cache) ────────────────────────
def test_collection_releases_paginates_and_caches(monkeypatch):
    _reset_cache()
    pages = {
        1: {"pagination": {"page": 1, "pages": 2},
            "releases":  [_make_item(1, "A", "Album One"),
                          _make_item(2, "B", "Album Two")]},
        2: {"pagination": {"page": 2, "pages": 2},
            "releases":  [_make_item(3, "C", "Album Three")]},
    }
    calls: list[str] = []

    def fake(url, token, timeout=20):
        calls.append(url)
        # Quick page extraction: the URL ends with `&page=N`.
        n = int(url.rsplit("page=", 1)[-1])
        return pages[n]

    monkeypatch.setattr(discogs, "_http_json_with_token", fake)

    out = discogs.collection_releases("alice", token=None)
    assert [r["title"] for r in out] == ["Album One", "Album Two", "Album Three"]
    # Two pages → two HTTP calls.
    assert len(calls) == 2
    # Cache hit on second invocation — no further calls.
    out2 = discogs.collection_releases("alice")
    assert out2 == out
    assert len(calls) == 2


def test_collection_releases_force_refetches(monkeypatch):
    _reset_cache()
    pages = {1: {"pagination": {"page": 1, "pages": 1},
                 "releases": [_make_item(99, "X", "Y")]}}
    n_calls = {"v": 0}

    def fake(url, token, timeout=20):
        n_calls["v"] += 1
        return pages[1]

    monkeypatch.setattr(discogs, "_http_json_with_token", fake)
    discogs.collection_releases("u")
    discogs.collection_releases("u")
    assert n_calls["v"] == 1   # second was a cache hit
    discogs.collection_releases("u", force=True)
    assert n_calls["v"] == 2   # forced refetch


def test_collection_releases_empty_username_returns_empty():
    assert discogs.collection_releases("") == []


def test_collection_releases_falls_back_to_cache_on_failure(monkeypatch):
    _reset_cache()
    # First call: succeeds, populates cache.
    payload = {"pagination": {"page": 1, "pages": 1},
               "releases": [_make_item(1, "A", "B")]}
    monkeypatch.setattr(discogs, "_http_json_with_token",
                        lambda *a, **k: payload)
    discogs.collection_releases("u")
    # Second forced call: blow up — should serve the cached snapshot.
    def boom(*a, **k): raise RuntimeError("network down")
    monkeypatch.setattr(discogs, "_http_json_with_token", boom)
    out = discogs.collection_releases("u", force=True)
    assert len(out) == 1
    assert out[0]["title"] == "B"


def test_collection_releases_pagination_safety_stop(monkeypatch):
    """If Discogs ever reports infinite pages, we cap at 50 to avoid
    a runaway fetch loop."""
    _reset_cache()

    def fake(url, token, timeout=20):
        return {"pagination": {"page": 1, "pages": 9999},
                "releases": [_make_item(1, "A", "T")]}

    monkeypatch.setattr(discogs, "_http_json_with_token", fake)
    out = discogs.collection_releases("u")
    # 50 pages × 1 release each — exceeded the safety cap.
    assert 0 < len(out) <= 50


# ── Fuzzy matcher ────────────────────────────────────────────────────────
def _summarized(rid, artist, title):
    return discogs._summarize_release(_make_item(rid, artist, title))


def test_match_collection_returns_high_score_for_exact_title():
    releases = [_summarized(1, "Miles Davis", "Kind of Blue"),
                _summarized(2, "John Coltrane", "Blue Train")]
    out = discogs.match_collection("Miles Davis", "Kind of Blue", releases)
    assert out
    assert out[0]["title"] == "Kind of Blue"
    assert out[0]["score"] == 100
    assert out[0]["source"] == "collection"


def test_match_collection_tolerates_minor_typos():
    releases = [_summarized(1, "Miles Davis", "Kind of Blue")]
    out = discogs.match_collection("miles davis", "kind of bleu", releases)
    assert out
    assert out[0]["discogs_release_id"] == 1


def test_match_collection_drops_low_scores():
    releases = [_summarized(1, "Miles Davis", "Kind of Blue")]
    out = discogs.match_collection("Pink Floyd", "Dark Side of the Moon", releases)
    assert out == []


def test_match_collection_empty_query_returns_empty():
    releases = [_summarized(1, "A", "T")]
    assert discogs.match_collection("", "", releases) == []


def test_match_collection_handles_diacritics():
    releases = [_summarized(1, "Sigur Rós", "Ágætis byrjun")]
    out = discogs.match_collection("sigur ros", "agaetis byrjun", releases)
    assert out
    assert out[0]["discogs_release_id"] == 1


def test_match_collection_respects_limit():
    releases = [_summarized(i, "Same Artist", f"Same Album {i}") for i in range(20)]
    out = discogs.match_collection("Same Artist", "Same Album", releases, limit=3)
    assert len(out) == 3


# ── match_collection_q (generic free-text variant) ──────────────────────
def test_match_collection_q_finds_by_title_only():
    """The structured matcher dings releases whose artist doesn't match
    the (empty) artist query. The free-text matcher just tokenises the
    query against `artist + title`, so "kind of blue" finds the Miles
    Davis release even though the artist isn't typed."""
    releases = [
        _summarized(1, "Miles Davis", "Kind of Blue"),
        _summarized(2, "Pink Floyd", "Animals"),
    ]
    out = discogs.match_collection_q("kind of blue", releases)
    assert len(out) == 1
    assert out[0]["discogs_release_id"] == 1
    assert out[0]["score"] == 100  # all 3 tokens present in haystack


def test_match_collection_q_finds_by_artist_only():
    releases = [
        _summarized(1, "Miles Davis", "Kind of Blue"),
        _summarized(2, "Pink Floyd", "Animals"),
    ]
    out = discogs.match_collection_q("pink floyd", releases)
    assert len(out) == 1
    assert out[0]["discogs_release_id"] == 2


def test_match_collection_q_empty_returns_empty():
    releases = [_summarized(1, "x", "y")]
    assert discogs.match_collection_q("", releases) == []
    assert discogs.match_collection_q("   ", releases) == []


def test_match_collection_q_handles_diacritics():
    """Free-text matcher normalises both query and haystack so "sigur ros"
    finds "Sigur Rós · Ágætis byrjun"."""
    releases = [_summarized(1, "Sigur Rós", "Ágætis byrjun")]
    out = discogs.match_collection_q("sigur ros", releases)
    assert len(out) == 1


# ── Token + cache (added in API hardening pass) ──────────────────────────
def test_release_uses_configured_token(monkeypatch):
    """`release()` must include the Discogs Authorization header when
    DISCOGS_TOKEN is set, otherwise the request lands in the slower
    unauthenticated bucket."""
    captured: dict = {}

    def fake_with_token(url, token, timeout=20):
        captured["url"] = url
        captured["token"] = token
        return {"id": 42, "title": "T"}

    monkeypatch.setattr(discogs, "_http_json_with_token", fake_with_token)
    monkeypatch.setattr(discogs, "DISCOGS_TOKEN", "secrettoken123")
    discogs._clear_caches_for_tests()
    out = discogs.release(42)
    assert out and out["id"] == 42
    assert captured["token"] == "secrettoken123"
    assert "/releases/42" in captured["url"]


def test_release_caches_within_ttl(monkeypatch):
    """Repeat calls for the same release id hit the network once."""
    calls = {"n": 0}

    def fake_with_token(url, token, timeout=20):
        calls["n"] += 1
        return {"id": 7}

    monkeypatch.setattr(discogs, "_http_json_with_token", fake_with_token)
    discogs._clear_caches_for_tests()
    a = discogs.release(7)
    b = discogs.release(7)
    assert a == b == {"id": 7}
    assert calls["n"] == 1


def test_release_caches_failure_as_none(monkeypatch):
    """A failed fetch is cached as None so a flaky upstream isn't repeatedly
    hammered for the same id within the TTL."""
    calls = {"n": 0}

    def boom(url, token, timeout=20):
        calls["n"] += 1
        raise RuntimeError("nope")

    monkeypatch.setattr(discogs, "_http_json_with_token", boom)
    discogs._clear_caches_for_tests()
    assert discogs.release(99) is None
    assert discogs.release(99) is None
    assert calls["n"] == 1


# ── annotate_recorded (Collection checklist) ─────────────────────────────
def _rel(rid, artist="A", title="T"):
    return {"discogs_release_id": rid, "artist": artist, "title": title}


def test_annotate_recorded_exact_id_match():
    releases = [_rel(1), _rel(2)]
    albums = [{"album_id": "aa11", "artist": "A", "album": "T",
               "discogs_release_id": 2}]
    out = discogs.annotate_recorded(releases, albums)
    assert [r["recorded"] for r in out] == [False, True]
    assert out[1]["album_id"] == "aa11"
    assert out[0]["album_id"] is None
    # Inputs are not mutated — copies come back.
    assert "recorded" not in releases[0]


def test_annotate_recorded_coerces_string_ids():
    """Manifests may store the id as a string; the release side is int —
    both sides go through int() so '123' still matches 123."""
    out = discogs.annotate_recorded(
        [_rel(123)], [{"album_id": "bb22", "discogs_release_id": "123"}])
    assert out[0]["recorded"] is True
    assert out[0]["album_id"] == "bb22"


def test_annotate_recorded_no_fuzzy_fallback():
    """Exact ID only by design: identical artist/title but no Discogs id on
    the album must NOT tick the release off the checklist."""
    releases = [_rel(5, artist="Miles Davis", title="Kind of Blue")]
    albums = [{"album_id": "cc33", "artist": "Miles Davis",
               "album": "Kind of Blue", "discogs_release_id": None}]
    out = discogs.annotate_recorded(releases, albums)
    assert out[0]["recorded"] is False


def test_annotate_recorded_ignores_garbage_ids():
    albums = [{"album_id": "dd44", "discogs_release_id": "not-a-number"},
              {"album_id": "ee55", "discogs_release_id": 0}]
    out = discogs.annotate_recorded([_rel(7)], albums)
    assert out[0]["recorded"] is False
