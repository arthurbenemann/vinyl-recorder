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
