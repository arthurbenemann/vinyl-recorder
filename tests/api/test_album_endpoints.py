"""API tests for the albums router.

Covers the validation + 404 guards and the manifest-only happy paths
(combine, plan update, reorder, demote, delete). The ffmpeg-driven
endpoints (measure / split) live in the e2e suite — testing them here
would require a metaflac/ffmpeg toolchain, which the unit job doesn't
have."""
from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


def _make_raw_side(name: str = "side.flac") -> str:
    """Drop a fake FLAC into RAW_DIR and return the filename. Album
    creation only needs the file to exist — manifest writing doesn't read
    bytes."""
    from state import RAW_DIR
    p = RAW_DIR / name
    p.write_bytes(b"not a real flac")
    return p.name


def _cleanup_album(album_id: str) -> None:
    from services import albums_fs
    d = albums_fs.album_dir(album_id)
    if d.is_dir():
        for f in d.iterdir():
            try: f.unlink()
            except Exception: pass
        try: d.rmdir()
        except Exception: pass


# ── /api/combine validation ──────────────────────────────────────────────
def test_combine_empty_filenames_returns_400():
    r = _client().post("/api/combine", json={"filenames": [], "album": {}})
    assert r.status_code == 400


def test_combine_missing_source_file_returns_404():
    r = _client().post("/api/combine", json={
        "filenames": ["definitely-not-here.flac"],
        "album": {},
    })
    assert r.status_code == 404


def test_combine_traversal_filename_returns_400():
    """Filenames containing slashes / .. must be rejected so an attacker
    can't move arbitrary files into an album dir."""
    r = _client().post("/api/combine", json={
        "filenames": ["../etc/passwd"],
        "album": {},
    })
    assert r.status_code == 400


def test_combine_creates_album_for_valid_side(monkeypatch):
    """Happy path: one raw side → one new album. Patch the route's
    ``flac_duration_seconds`` reference (it was imported by name at module
    load) so we don't depend on metaflac being on PATH."""
    from routes import albums as albums_route

    monkeypatch.setattr(albums_route, "flac_duration_seconds", lambda p: 1234.5)

    name = _make_raw_side("combine_src.flac")
    r = _client().post("/api/combine", json={
        "filenames": [name],
        "album": {"artist": "Foo", "album": "Bar", "year": "2020"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["album_id"]
    assert body["duration_seconds"] == 1234.5
    _cleanup_album(body["album_id"])


# ── /api/promote ─────────────────────────────────────────────────────────
def test_promote_missing_file_returns_404():
    r = _client().post("/api/promote", json={
        "filename": "missing.flac",
        "album": {},
    })
    assert r.status_code == 404


# ── /api/albums/{album_id} DELETE ────────────────────────────────────────
def test_delete_album_unknown_returns_404():
    r = _client().delete("/api/albums/not-a-real-id")
    assert r.status_code == 404


def test_delete_album_invalid_id_returns_404():
    """`is_valid_album_id` rejects ids that aren't the canonical slug
    shape — anything else is also a 404."""
    r = _client().delete("/api/albums/has spaces")
    assert r.status_code == 404


def test_delete_album_removes_directory(monkeypatch):
    from services import ffmpeg as ffmpeg_mod, albums_fs
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 0.0)

    name = _make_raw_side("delete_src.flac")
    body = _client().post("/api/combine", json={
        "filenames": [name],
        "album": {"artist": "Del", "album": "Me"},
    }).json()
    aid = body["album_id"]
    assert albums_fs.album_dir(aid).is_dir()
    r = _client().delete(f"/api/albums/{aid}")
    assert r.status_code == 200
    assert not albums_fs.album_dir(aid).exists()


# ── /api/album/{album_id}/demote ─────────────────────────────────────────
def test_demote_unknown_returns_404():
    r = _client().post("/api/album/not-a-real-id/demote")
    assert r.status_code == 404


def test_demote_moves_sides_back_to_raw(monkeypatch):
    """Demote should put the sides back in raw/ and remove the album dir."""
    from services import albums_fs, ffmpeg as ffmpeg_mod
    from state import RAW_DIR

    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 0.0)

    name = _make_raw_side("demote_src.flac")
    body = _client().post("/api/combine", json={
        "filenames": [name], "album": {},
    }).json()
    aid = body["album_id"]
    # Side now lives under the album dir, NOT in raw/.
    assert (albums_fs.album_dir(aid) / name).exists()
    assert not (RAW_DIR / name).exists()

    r = _client().post(f"/api/album/{aid}/demote")
    assert r.status_code == 200
    assert (RAW_DIR / name).exists()
    assert not albums_fs.album_dir(aid).exists()
    (RAW_DIR / name).unlink(missing_ok=True)


# ── /api/album/{album_id}/purge-sources ──────────────────────────────────
def test_purge_sources_unknown_returns_404():
    r = _client().post("/api/album/not-a-real-id/purge-sources")
    assert r.status_code == 404


def test_purge_sources_unsplit_album_returns_409(monkeypatch):
    """Without music_relpath set there's nothing to fall back on — the
    endpoint refuses with 409 rather than silently deleting the only copy
    of the source audio."""
    from services import ffmpeg as ffmpeg_mod
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 0.0)
    name = _make_raw_side("purge_unsplit_src.flac")
    body = _client().post("/api/combine", json={
        "filenames": [name], "album": {"artist": "X", "album": "Y"},
    }).json()
    aid = body["album_id"]
    try:
        r = _client().post(f"/api/album/{aid}/purge-sources")
        assert r.status_code == 409
    finally:
        _cleanup_album(aid)


def test_purge_sources_split_album_clears_sides(monkeypatch):
    """For a split album, purge-sources removes the side FLAC, sets
    sources_purged on the manifest, and the album row stays in the listing."""
    from services import albums_fs, ffmpeg as ffmpeg_mod
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 0.0)

    name = _make_raw_side("purge_split_src.flac")
    body = _client().post("/api/combine", json={
        "filenames": [name], "album": {"artist": "X", "album": "Y"},
    }).json()
    aid = body["album_id"]
    try:
        # Fake a successful split by patching music_relpath into the manifest.
        m = albums_fs.read_manifest(aid)
        m["music_relpath"] = "X/Y"
        albums_fs.write_manifest(aid, m)

        side_path = albums_fs.album_dir(aid) / name
        assert side_path.exists()

        r = _client().post(f"/api/album/{aid}/purge-sources")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["files_removed"] >= 1
        assert body["bytes_freed"] >= 0

        assert not side_path.exists()
        m = albums_fs.read_manifest(aid)
        assert m["sides"] == []
        assert m["sources_purged"] is True

        # Album row still present in the listing, flagged as locked.
        rows = _client().get("/api/albums").json()["albums"]
        row = next(r for r in rows if r["album_id"] == aid)
        assert row["sources_purged"] is True
        assert row["split"] is True
    finally:
        _cleanup_album(aid)


# ── /api/music/scan ──────────────────────────────────────────────────────
def test_music_scan_imports_orphan_dir(monkeypatch):
    """A manually-dropped folder under music/ shows up as a locked external
    album after a scan call, with the music_relpath the importer parsed
    from the path."""
    from services import albums_fs, ffmpeg as ffmpeg_mod
    from state import MUSIC_DIR
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 0.0)
    # Avoid hitting metaflac in CI — let the importer fall back to path tags.
    monkeypatch.setattr(albums_fs, "read_tags", lambda p: {})
    monkeypatch.setattr(albums_fs, "extract_cover_to_album",
                        lambda aid, src: None)

    relpath = "Test Artist API/Test Album (2023)"
    d = MUSIC_DIR / relpath
    d.mkdir(parents=True, exist_ok=True)
    (d / "01 - Track.flac").write_bytes(b"")
    try:
        r = _client().post("/api/music/scan")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported"] >= 1
        # The orphan now appears in the listing as an external locked album.
        rows = _client().get("/api/albums").json()["albums"]
        match = next(r for r in rows if r.get("music_relpath") == relpath)
        assert match["external"] is True
        assert match["sources_purged"] is True
        assert match["split"] is True
        assert match["artist"] == "Test Artist API"
        assert match["album"]  == "Test Album"
        assert match["year"]   == "2023"
        # Subsequent scan is idempotent.
        r2 = _client().post("/api/music/scan").json()
        assert r2["imported"] == 0
    finally:
        for aid in body.get("album_ids", []):
            _cleanup_album(aid)
        # Best-effort cleanup of the music/ tree we created.
        import shutil
        shutil.rmtree(MUSIC_DIR / "Test Artist API", ignore_errors=True)


# ── /api/album/{album_id}/plan ───────────────────────────────────────────
def test_update_plan_unknown_album_returns_404():
    r = _client().post("/api/album/not-real/plan", json={"tracks": []})
    assert r.status_code == 404


def test_update_plan_persists_to_manifest(monkeypatch):
    from services import albums_fs, ffmpeg as ffmpeg_mod
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 60.0)

    name = _make_raw_side("plan_src.flac")
    aid = _client().post("/api/combine", json={
        "filenames": [name], "album": {},
    }).json()["album_id"]
    try:
        r = _client().post(f"/api/album/{aid}/plan", json={
            "tracks": [
                {"title": "Track A", "duration_seconds": 30.0, "skip": False},
                {"title": "Track B", "duration_seconds": 25.0, "skip": True},
            ],
            "normalize": True,
            "target_peak_db": -1.0,
            "bit_depth": 16,
        })
        assert r.status_code == 200
        plan = r.json()["plan"]
        assert len(plan["tracks"]) == 2
        assert plan["tracks"][1]["skip"] is True
        assert plan["normalize"] is True
        assert plan["target_peak_db"] == -1.0
        assert plan["bit_depth"] == 16
        # Must round-trip: re-read the manifest, see the same plan.
        manifest = albums_fs.read_manifest(aid)
        assert manifest["plan"]["normalize"] is True
        assert manifest["plan"]["bit_depth"] == 16
    finally:
        _cleanup_album(aid)


# ── /api/album/{album_id}/sides/reorder ──────────────────────────────────
def test_reorder_unknown_album_returns_404():
    r = _client().post("/api/album/not-real/sides/reorder", json={"sides": []})
    assert r.status_code == 404


def test_reorder_with_unknown_side_returns_400(monkeypatch):
    from services import ffmpeg as ffmpeg_mod
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 0.0)

    name = _make_raw_side("reorder_src.flac")
    aid = _client().post("/api/combine", json={
        "filenames": [name], "album": {},
    }).json()["album_id"]
    try:
        # Asking for a side that isn't in the album → ValueError → 400.
        r = _client().post(f"/api/album/{aid}/sides/reorder", json={
            "sides": ["never-existed.flac"],
        })
        assert r.status_code == 400
    finally:
        _cleanup_album(aid)


# ── /api/album/{album_id}/peaks/{side_idx} bounds ─────────────────────────
def test_peaks_invalid_album_id_returns_404():
    r = _client().get("/api/album/not-a-real-id/peaks/0")
    assert r.status_code == 404


def test_peaks_side_index_out_of_range_returns_404(monkeypatch):
    from services import ffmpeg as ffmpeg_mod
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 0.0)

    name = _make_raw_side("peaks_src.flac")
    aid = _client().post("/api/combine", json={
        "filenames": [name], "album": {},
    }).json()["album_id"]
    try:
        # The album has 1 side; index 5 is past the end → 404.
        r = _client().get(f"/api/album/{aid}/peaks/5")
        assert r.status_code == 404
    finally:
        _cleanup_album(aid)


# ── /api/album/{album_id}/sides/{side_idx}/audio ─────────────────────────
def test_side_audio_invalid_album_returns_404():
    r = _client().get("/api/album/not-real/sides/0/audio")
    assert r.status_code == 404


def test_side_audio_serves_existing_side(monkeypatch):
    from services import ffmpeg as ffmpeg_mod
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: 0.0)

    name = _make_raw_side("side_audio_src.flac")
    aid = _client().post("/api/combine", json={
        "filenames": [name], "album": {},
    }).json()["album_id"]
    try:
        r = _client().get(f"/api/album/{aid}/sides/0/audio")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/flac"
    finally:
        _cleanup_album(aid)


# ── /api/album/detect-silences validation ────────────────────────────────
def test_detect_silences_unknown_album_returns_404():
    r = _client().post("/api/album/detect-silences", json={
        "album_id": "not-real",
        "noise_db": -40.0,
        "min_silence": 1.5,
    })
    assert r.status_code == 404
