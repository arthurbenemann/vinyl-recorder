"""API tests for the tagging router (search / release / cover / apply)."""
from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


_VALID_MBID = "3c1c2dab-fcc1-4d1c-9d6f-9ef00bf1f9d7"


# ── /api/release/{mbid} ──────────────────────────────────────────────────
def test_release_invalid_mbid_returns_400():
    r = _client().get("/api/release/not-an-mbid")
    assert r.status_code == 400


def test_release_upstream_failure_returns_502(monkeypatch):
    """MusicBrainz error → 502 (not 500). The upstream-error code lets the
    frontend distinguish "MB is down" from "we have a bug"."""
    from routes import tagging as tg

    def boom(mbid):
        raise RuntimeError("MB unreachable")

    monkeypatch.setattr(tg, "release_full", boom)
    r = _client().get(f"/api/release/{_VALID_MBID}")
    assert r.status_code == 502


def test_release_returns_canonical_shape(monkeypatch):
    """Mock the MB and Discogs callouts so we hit the response-builder
    branches (artist/year/label/format/track durations) deterministically."""
    from routes import tagging as tg

    mb_release = {
        "id":             _VALID_MBID,
        "title":          "Sample MB Album",
        "date":           "1973-04-15",
        "artist-credit":  [{"name": "Sample Artist"}],
        "label-info":     [{"label": {"name": "MB Label"}, "catalog-number": "MB-1"}],
        "country":        "US",
        "media": [{
            "format": "Vinyl",
            "tracks": [
                {"title": "Track A", "length": 120000},
                {"title": "Track B", "length": None},
            ],
        }],
        "relations": [],  # no Discogs link → discogs_id stays None
    }
    monkeypatch.setattr(tg, "release_full", lambda mbid: mb_release)
    monkeypatch.setattr(tg, "extract_discogs_id", lambda mb: None)

    r = _client().get(f"/api/release/{_VALID_MBID}")
    assert r.status_code == 200
    body = r.json()
    assert body["mbid"]   == _VALID_MBID
    assert body["title"]  == "Sample MB Album"
    assert body["artist"] == "Sample Artist"
    assert body["year"]   == "1973"
    assert body["label"]  == "MB Label"
    assert body["catalog_number"] == "MB-1"
    assert body["format"] == "Vinyl"
    assert body["tracks"] == ["Track A", "Track B"]
    # 120000 ms → 120 s; second track has no length → None.
    assert body["track_details"][0]["duration_seconds"] == 120.0
    assert body["track_details"][1]["duration_seconds"] is None
    # No Discogs match → cover proxies through /api/cover/<mbid>.
    assert body["cover_url"].endswith(f"/api/cover/{_VALID_MBID}")
    assert body["discogs_url"] is None


def test_release_includes_composer_and_conductor_from_mb(monkeypatch):
    """Release-level artist-rels on MB surface as the conductor field; the
    composer field stays empty until Discogs enrichment fills it in."""
    from routes import tagging as tg

    mb_release = {
        "id":            _VALID_MBID,
        "title":         "Symphony",
        "date":          "1962",
        "artist-credit": [{"name": "Berlin Phil"}],
        "media":         [{"format": "Vinyl", "tracks": []}],
        "relations": [
            {"type": "conductor", "artist": {"name": "Herbert von Karajan"}},
        ],
    }
    monkeypatch.setattr(tg, "release_full", lambda mbid: mb_release)
    monkeypatch.setattr(tg, "extract_discogs_id", lambda mb: None)

    body = _client().get(f"/api/release/{_VALID_MBID}").json()
    assert body["conductor"] == "Herbert von Karajan"
    # Composer comes from Discogs only — no Discogs link → empty string.
    assert body["composer"] == ""


# ── /api/release/discogs/{id} validation ─────────────────────────────────
def test_release_discogs_zero_id_returns_400():
    r = _client().get("/api/release/discogs/0")
    assert r.status_code == 400


def test_release_discogs_includes_composer_and_conductor(monkeypatch):
    """Discogs extraartists drive both fields. Prefix-matching catches
    "Composed By" credits, and bare "Conductor" maps directly."""
    from services import discogs as ds_mod

    fake = {
        "title":   "Concerto",
        "year":    1965,
        "artists": [{"name": "Soloist"}],
        "labels":  [{"name": "DG", "catno": "ABC-1"}],
        "country": "DE",
        "formats": [{"name": "Vinyl", "descriptions": ["LP"]}],
        "genres":  ["Classical"],
        "styles":  [],
        "tracklist": [],
        "images":  [],
        "uri":     "https://www.discogs.com/release/42",
        "extraartists": [
            {"role": "Composed By", "name": "Beethoven"},
            {"role": "Conductor",   "name": "Karajan"},
        ],
    }
    monkeypatch.setattr(ds_mod, "release", lambda rid: fake)

    body = _client().get("/api/release/discogs/42").json()
    assert body["composer"]  == "Beethoven"
    assert body["conductor"] == "Karajan"


def test_release_discogs_joins_genres_and_styles_with_semicolons(monkeypatch):
    """Genres + styles are merged into one ';'-separated string so the split
    flow can write each as its own GENRE tag. ';' (not ',') is the separator
    because a Discogs genre can itself contain commas."""
    from services import discogs as ds_mod

    fake = {
        "title": "X", "year": 1990, "artists": [{"name": "A"}],
        "labels": [], "country": "", "formats": [],
        "genres": ["Electronic", "Folk, World, & Country"],
        "styles": ["Techno", "House"],
        "tracklist": [], "images": [], "extraartists": [],
        "uri": "https://www.discogs.com/release/7",
    }
    monkeypatch.setattr(ds_mod, "release", lambda rid: fake)
    body = _client().get("/api/release/discogs/7").json()
    # ';'-joined, dedup-ordered, comma-containing genre kept whole.
    assert body["genre"] == "Electronic; Folk, World, & Country; Techno; House"


# ── /api/cover/{mbid} ────────────────────────────────────────────────────
def test_cover_invalid_mbid_returns_400():
    r = _client().get("/api/cover/not-an-mbid")
    assert r.status_code == 400


def test_cover_no_art_anywhere_returns_404(monkeypatch):
    """When CAA has no front and the MB release has no Discogs link, the
    handler must return 404 (the frontend falls back to a placeholder)."""
    from routes import tagging as tg
    monkeypatch.setattr(tg, "caa_front", lambda mbid: None)
    monkeypatch.setattr(tg, "release_full", lambda mbid: {"relations": []})
    monkeypatch.setattr(tg, "extract_discogs_id", lambda mb: None)
    r = _client().get(f"/api/cover/{_VALID_MBID}")
    assert r.status_code == 404


def test_cover_returns_caa_bytes(monkeypatch):
    from routes import tagging as tg
    monkeypatch.setattr(tg, "caa_front", lambda mbid: b"\xff\xd8\xff\xe0fakeJPG")
    r = _client().get(f"/api/cover/{_VALID_MBID}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8\xff\xe0")


# ── /api/file-cover/{album_id} ───────────────────────────────────────────
def test_file_cover_unknown_album_returns_404():
    r = _client().get("/api/file-cover/not-real")
    assert r.status_code == 404


def test_file_cover_invalid_id_returns_404():
    r = _client().get("/api/file-cover/bad id with spaces")
    assert r.status_code == 404


# ── /api/apply validation ────────────────────────────────────────────────
def test_apply_requires_exactly_one_target():
    """`album_id`, `filename`, and `filenames` are mutually exclusive — the
    400 message guides the caller toward the right shape."""
    c = _client()
    # None of them set.
    r = c.post("/api/apply", json={"fields": {}})
    assert r.status_code == 400
    # Two of them set.
    r = c.post("/api/apply", json={
        "album_id": "x", "filename": "y.flac", "fields": {},
    })
    assert r.status_code == 400


def test_apply_invalid_mbid_returns_400():
    r = _client().post("/api/apply", json={
        "filename": "missing.flac",
        "fields": {},
        "mbid": "not-an-mbid",
    })
    assert r.status_code == 400


def test_apply_unknown_album_id_returns_404():
    r = _client().post("/api/apply", json={
        "album_id": "not-real",
        "fields": {"artist": "X"},
    })
    assert r.status_code == 404


def test_apply_strips_tracks_from_manifest_tags(monkeypatch):
    """The release tracklist arrives on `fields.tracks` (vestigial TagEdit
    field used by the editor's UI), but it must NOT land in `tags` on
    album.json. If it did, a later wave-editor save would produce TWO
    track listings in the manifest — `tags.tracks` (strings) alongside
    `plan.tracks` (cut objects)."""
    import json

    from routes import tagging as tg
    from state import IN_PROGRESS_DIR
    monkeypatch.setattr(tg, "caa_front", lambda mbid: None)

    # Seed an existing in-progress album so we exercise the album_id branch.
    album_id = "ttagsclean1"
    d = IN_PROGRESS_DIR / album_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "side1.flac").write_bytes(b"")
    (d / "album.json").write_text(json.dumps({
        "schema_version": 2,
        "tags": {"artist": "Old"},
        "sides": ["side1.flac"],
        "cover": None,
        "plan": None,
        "music_relpath": None,
    }))
    try:
        r = _client().post("/api/apply", json={
            "album_id": album_id,
            "fields": {
                "artist": "X", "album": "Y",
                "tracks": ["Side A track 1", "Side A track 2"],
            },
        })
        assert r.status_code == 200
        manifest = json.loads((d / "album.json").read_text())
        assert manifest["tags"]["artist"] == "X"
        assert "tracks" not in manifest["tags"], (
            f"tags polluted with tracklist: {manifest['tags']!r}"
        )
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_apply_persists_mb_ids_media_releasetype(monkeypatch):
    """Applying an MB pick persists the stable MB IDs + media + releasetype
    into album.json (so the split writes them onto every track). They flow
    in from release_full at apply-time, not from the user's edit fields."""
    import json

    from routes import tagging as tg
    from state import IN_PROGRESS_DIR

    fake_mb = {
        "release-group": {"id": "rg-xyz", "primary-type": "Album"},
        "artist-credit": [{"artist": {"id": "art-xyz", "name": "A"}}],
        "media": [{"format": "Vinyl"}],
        "relations": [],  # no Discogs link → extract_discogs_id → None
    }
    monkeypatch.setattr(tg, "release_full", lambda mbid: fake_mb)
    monkeypatch.setattr(tg, "caa_front", lambda mbid: None)

    album_id = "mbidsalbum1"
    d = IN_PROGRESS_DIR / album_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "side1.flac").write_bytes(b"")
    (d / "album.json").write_text(json.dumps({
        "schema_version": 2, "tags": {"artist": "A"},
        "sides": ["side1.flac"], "cover": None, "plan": None,
        "music_relpath": None,
    }))
    try:
        r = _client().post("/api/apply", json={
            "album_id": album_id,
            "mbid": _VALID_MBID,
            "fields": {"artist": "A", "album": "B"},
        })
        assert r.status_code == 200
        tags = json.loads((d / "album.json").read_text())["tags"]
        assert tags["musicbrainz_albumid"] == _VALID_MBID
        assert tags["musicbrainz_releasegroupid"] == "rg-xyz"
        assert tags["musicbrainz_artistid"] == "art-xyz"
        assert tags["musicbrainz_albumartistid"] == "art-xyz"
        assert tags["media"] == "Vinyl"
        assert tags["releasetype"] == "Album"
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_apply_missing_raw_side_returns_404(monkeypatch):
    """The promote-style apply (single filename → new album) must surface
    a missing source as 404, not as a generic 500."""
    # caa_front / discogs.release default to no-op so cover fetch doesn't
    # try to hit the network. The route then calls albums_fs.create_album
    # which raises FileNotFoundError → 404.
    from routes import tagging as tg
    monkeypatch.setattr(tg, "caa_front", lambda mbid: None)

    r = _client().post("/api/apply", json={
        "filename": "no-such-side.flac",
        "fields": {"artist": "Foo"},
    })
    assert r.status_code == 404


# ── /api/collection/refresh upstream failure ────────────────────────────
def test_collection_refresh_upstream_error_returns_502(monkeypatch):
    """Discogs collection fetch error must surface as 502 (not 500) so the
    UI can distinguish it from a real bug. We need DISCOGS_USERNAME to be
    set for the call to even reach the network step, so we patch the
    routes-module reference."""
    from routes import tagging as tg
    from services import discogs as ds_mod

    monkeypatch.setattr(tg, "DISCOGS_USERNAME", "someone")

    def boom(*a, **kw):
        raise RuntimeError("Discogs 503")

    monkeypatch.setattr(ds_mod, "collection_releases", boom)
    r = _client().post("/api/collection/refresh")
    assert r.status_code == 502
