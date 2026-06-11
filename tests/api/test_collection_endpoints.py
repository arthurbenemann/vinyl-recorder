"""API tests for the new Discogs-collection endpoints + the extended
/api/search response shape."""
from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


# ── /api/config carries the new flags ────────────────────────────────────
def test_config_exposes_new_flags():
    r = _client().get("/api/config")
    assert r.status_code == 200
    body = r.json()
    # Pre-roll knob.
    assert "pre_roll_seconds" in body
    assert isinstance(body["pre_roll_seconds"], int)
    # Discogs-collection enable flag — boolean only, never the username.
    assert "discogs_collection_enabled" in body
    assert isinstance(body["discogs_collection_enabled"], bool)
    # Server must NEVER return the raw username/token to the frontend.
    assert "discogs_username" not in body
    assert "discogs_token" not in body


# ── /api/search shape ────────────────────────────────────────────────────
def test_search_returns_collection_candidates_field():
    """Even with no Discogs username configured, the response shape stays
    backwards-compatible with an empty `collection_candidates` list. The
    frontend always reads the field; missing it would break rendering."""
    # Empty body short-circuits before any external call.
    r = _client().post("/api/search", json={"artist": "", "album": ""})
    assert r.status_code == 200
    body = r.json()
    assert "candidates" in body
    assert "collection_candidates" in body
    assert body["candidates"] == []
    assert body["collection_candidates"] == []


def test_search_accepts_generic_q(monkeypatch):
    """The UI's search bars now send a free-text `q` instead of an
    artist+album pair. /api/search should route that through MB's generic
    Lucene query and the collection's free-text fuzzy match, returning
    the same response shape."""
    # The route imports `search_releases` by name from services.musicbrainz,
    # so we have to monkeypatch the name on the route module (the binding
    # the route actually calls), not on the source module.
    import routes.tagging as tagging_mod
    captured: dict = {}

    def fake_search(*args, q="", **kwargs):
        captured["q"] = q
        captured["artist"] = args[0] if args else kwargs.get("artist", "")
        captured["album"]  = args[1] if len(args) > 1 else kwargs.get("album", "")
        return [{"mbid": "abc", "title": "Kind of Blue", "artist": "Miles Davis",
                 "year": "1959", "label": "", "catalog_number": "", "country": "",
                 "format": "", "score": 100}]

    monkeypatch.setattr(tagging_mod, "search_releases", fake_search)
    r = _client().post("/api/search", json={"q": "Kind of Blue"})
    assert r.status_code == 200
    body = r.json()
    assert captured["q"] == "Kind of Blue"
    # Structured fields stay empty when `q` is the source.
    assert captured["artist"] == "" and captured["album"] == ""
    assert body["candidates"][0]["title"] == "Kind of Blue"


def test_search_empty_q_and_empty_struct_short_circuits():
    """Empty body (no q, no artist, no album) returns the empty-shape
    response without making any network calls."""
    r = _client().post("/api/search", json={"q": "", "artist": "", "album": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"] == [] and body["collection_candidates"] == []


# ── /api/collection (list endpoint backing the live filter) ──────────────
def test_collection_list_without_username_returns_empty():
    """No DISCOGS_USERNAME → empty list. The frontend uses this to decide
    whether to even show the filter input."""
    r = _client().get("/api/collection")
    assert r.status_code == 200
    assert r.json() == {"releases": []}


# ── /api/collection/refresh guard ────────────────────────────────────────
def test_collection_refresh_without_username_returns_409():
    """Refusing the call (rather than silently returning 0) makes the user
    aware that DISCOGS_USERNAME isn't set — easier to debug than mystery
    empty results."""
    r = _client().post("/api/collection/refresh")
    assert r.status_code == 409


# ── /api/release/discogs/{id} ────────────────────────────────────────────
def test_release_discogs_invalid_id_returns_400():
    r = _client().get("/api/release/discogs/0")
    assert r.status_code == 400


def test_release_discogs_payload_shape(monkeypatch):
    """Mirrors /api/release/{mbid} so the frontend can consume it the same
    way. We monkeypatch discogs.release to skip the network call."""
    from services import discogs as ds_mod

    canned = {
        "id":      9999,
        "title":   "Sample Album",
        "year":    1972,
        "artists": [{"name": "Sample Artist"}],
        "labels":  [{"name": "Acme", "catno": "ACM-1"}],
        "country": "US",
        "formats": [{"name": "Vinyl", "descriptions": ["LP"]}],
        "genres":  ["Rock"],
        "styles":  ["Psychedelic Rock"],
        "tracklist": [
            {"type_": "track", "title": "Side A Track 1", "duration": "3:45"},
            {"type_": "heading", "title": "Side B"},
            {"type_": "track", "title": "Side B Track 1", "duration": ""},
        ],
        "images": [{"type": "primary", "uri": "https://img.example/cover.jpg"}],
        "uri":    "https://www.discogs.com/release/9999",
    }
    monkeypatch.setattr(ds_mod, "release", lambda rid: canned)

    r = _client().get("/api/release/discogs/9999")
    assert r.status_code == 200
    body = r.json()
    assert body["title"]          == "Sample Album"
    assert body["artist"]         == "Sample Artist"
    assert body["year"]           == "1972"
    assert body["label"]          == "Acme"
    assert body["catalog_number"] == "ACM-1"
    assert body["country"]        == "US"
    assert "Vinyl" in body["format"]
    # Genres + styles concatenated.
    assert "Rock" in body["genre"]
    # Heading rows stripped — only `type_=="track"` (or unset) entries kept.
    assert body["tracks"] == ["Side A Track 1", "Side B Track 1"]
    # First track's "3:45" → 225 s; second has no duration → None.
    assert body["track_details"][0]["duration_seconds"] == 225
    assert body["track_details"][1]["duration_seconds"] is None
    # Cover URL passes through (external URL, not a /api/cover proxy).
    assert body["cover_url"].endswith("cover.jpg")
    assert body["discogs_id"] == 9999


def test_release_discogs_walks_sub_tracks(monkeypatch):
    """Classical and other multi-part releases use a hierarchical tracklist:
    a parent row with type_="index" carries the work title and the actual
    playable parts live in `sub_tracks`. Without recursion the response
    came back with an empty tracklist (e.g. Tchaikovsky 5 from senhorb's
    collection), so the wave-editor showed "no tracklist on this candidate"
    even though Discogs has all four movements with durations."""
    from services import discogs as ds_mod

    canned = {
        "id": 14417946,
        "title": "Sinfonie Nr. 5 e-moll, op. 64",
        "year": 1976,
        "artists": [{"name": "Tchaikovsky"}],
        "tracklist": [
            {
                "position": "",
                "type_":    "index",
                "title":    "Sinfonie Nr. 5 e-moll, op. 64",
                "sub_tracks": [
                    {"position": "A1", "type_": "track",
                     "title": "Andante - Allegro Con Anima", "duration": "15:07"},
                    {"position": "A2", "type_": "track",
                     "title": "Andante Cantabile", "duration": "15:18"},
                    {"position": "B1", "type_": "track",
                     "title": "Valse (Allegro Moderato)", "duration": "6:27"},
                    {"position": "B2", "type_": "track",
                     "title": "Finale (Andante Maestoso - Allegro Vivace)",
                     "duration": "12:58"},
                ],
            },
        ],
        "images": [],
        "uri":    "https://www.discogs.com/release/14417946",
    }
    monkeypatch.setattr(ds_mod, "release", lambda rid: canned)

    r = _client().get("/api/release/discogs/14417946")
    assert r.status_code == 200
    body = r.json()
    assert body["tracks"] == [
        "Andante - Allegro Con Anima", "Andante Cantabile",
        "Valse (Allegro Moderato)",
        "Finale (Andante Maestoso - Allegro Vivace)",
    ]
    assert [t["duration_seconds"] for t in body["track_details"]] == [
        15 * 60 + 7, 15 * 60 + 18, 6 * 60 + 27, 12 * 60 + 58,
    ]


def test_release_discogs_upstream_failure_returns_502(monkeypatch):
    from services import discogs as ds_mod
    monkeypatch.setattr(ds_mod, "release", lambda rid: None)
    r = _client().get("/api/release/discogs/12345")
    assert r.status_code == 502


# ── /api/collection/status (Collection checklist section) ───────────────
def test_collection_status_without_username_disabled_shape():
    """No DISCOGS_USERNAME → enabled:false with the full (empty) shape, as
    a 200 — the frontend uses `enabled` to keep the section hidden."""
    r = _client().get("/api/collection/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "total": 0, "recorded": 0,
                        "releases": []}


def test_collection_status_annotates_recorded(monkeypatch):
    """Exact discogs_release_id matches (including string-typed manifest
    ids) come back recorded:true with the album slug; everything else is
    recorded:false."""
    # DISCOGS_USERNAME is imported by name into the route module — patch
    # the route-module binding (same trap as test_search_accepts_generic_q).
    import routes.tagging as tagging_mod
    from services import albums_fs as afs_mod
    from services import discogs as ds_mod

    monkeypatch.setattr(tagging_mod, "DISCOGS_USERNAME", "testuser")
    owned = [
        {"discogs_release_id": 111, "title": "Kind of Blue",
         "artist": "Miles Davis", "year": "1959", "label": "Columbia",
         "catno": "CL 1355", "format": "Vinyl, LP", "cover_url": ""},
        {"discogs_release_id": 222, "title": "Aja", "artist": "Steely Dan",
         "year": "1977", "label": "ABC", "catno": "AB-1006",
         "format": "Vinyl, LP", "cover_url": ""},
    ]
    monkeypatch.setattr(ds_mod, "collection_releases", lambda *a, **k: owned)
    monkeypatch.setattr(afs_mod, "list_album_tag_summaries", lambda: [
        {"album_id": "aa11", "artist": "Miles Davis", "album": "Kind of Blue",
         "discogs_release_id": "111"},  # string id — must still match
        {"album_id": "zz99", "artist": "Neil Young", "album": "Harvest",
         "discogs_release_id": None},   # tagged without a Discogs id
    ])

    r = _client().get("/api/collection/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["total"] == 2 and body["recorded"] == 1
    by_id = {x["discogs_release_id"]: x for x in body["releases"]}
    assert by_id[111]["recorded"] is True and by_id[111]["album_id"] == "aa11"
    assert by_id[222]["recorded"] is False and by_id[222]["album_id"] is None


def test_collection_status_fetch_failure_degrades_to_empty(monkeypatch):
    """A Discogs failure with a cold cache yields enabled:true + empty
    releases (the UI shows its inline 'unavailable' row) rather than a 5xx
    — same non-fatal posture as /api/collection."""
    import routes.tagging as tagging_mod
    from services import discogs as ds_mod

    monkeypatch.setattr(tagging_mod, "DISCOGS_USERNAME", "testuser")

    def boom(*a, **k):
        raise RuntimeError("discogs down")

    monkeypatch.setattr(ds_mod, "collection_releases", boom)
    r = _client().get("/api/collection/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["total"] == 0 and body["recorded"] == 0
    assert body["releases"] == []
