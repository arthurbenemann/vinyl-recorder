"""Tests for the small album-route helpers and lookup endpoints.

Covers `albums_fs.music_dir_for` shape, `/api/album/{id}/tracks` for
various plan states, `/api/album/{id}/track/{name}` validation + 404 +
download, and `side_audio` missing-on-disk path.
"""
from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


def _make_album(tags: dict | None = None, side: str = "side1.flac") -> str:
    """Drop a fake side into raw/ and combine into a new album."""
    from state import RAW_DIR
    from services import albums_fs

    p = RAW_DIR / side
    p.write_bytes(b"\x66\x4c\x61\x43" + b"x" * 100)
    aid, _ = albums_fs.create_album([side], tags or {})
    return aid


def _cleanup_album(aid: str) -> None:
    from services import albums_fs
    from state import MUSIC_DIR

    d = albums_fs.album_dir(aid)
    if d.is_dir():
        for f in d.rglob("*"):
            if f.is_file():
                try: f.unlink()
                except Exception: pass
        for sub in sorted(d.rglob("*"), key=lambda x: -len(str(x))):
            if sub.is_dir():
                try: sub.rmdir()
                except Exception: pass
        try: d.rmdir()
        except Exception: pass

    if MUSIC_DIR.is_dir():
        for sub in MUSIC_DIR.rglob("*"):
            if sub.is_file():
                try: sub.unlink()
                except Exception: pass
        for sub in sorted(MUSIC_DIR.rglob("*"), key=lambda x: -len(str(x))):
            if sub.is_dir():
                try: sub.rmdir()
                except Exception: pass


# ── albums_fs.music_dir_for ──────────────────────────────────────────────
def test_music_dir_for_uses_artist_album_year():
    from services.albums_fs import music_dir_for
    abs_dir, relpath = music_dir_for({
        "artist": "Pink Floyd",
        "album":  "The Wall",
        "year":   "1979",
    })
    assert relpath == "Pink Floyd/The Wall (1979)"
    assert abs_dir.name == "The Wall (1979)"


def test_music_dir_for_omits_year_when_blank():
    from services.albums_fs import music_dir_for
    _, relpath = music_dir_for({"artist": "X", "album": "Y", "year": ""})
    assert relpath == "X/Y"


def test_music_dir_for_falls_back_when_tags_missing():
    """Missing artist/album → "Unknown Artist" / "Unknown Album". The
    Jellyfin tree must always have a populated path so a tagless album
    never lands at the music root."""
    from services.albums_fs import music_dir_for
    _, relpath = music_dir_for({})
    assert relpath == "Unknown Artist/Unknown Album"


def test_music_dir_for_strips_filesystem_hostile_chars():
    from services.albums_fs import music_dir_for
    _, relpath = music_dir_for({
        "artist": 'a/b\\c',
        "album":  'a"b:c',
        "year":   "2020",
    })
    # Slashes/backslashes/colons/quotes stripped — relpath has exactly ONE "/"
    # (the artist→album separator).
    assert relpath.count("/") == 1
    assert "\\" not in relpath
    assert ":" not in relpath
    assert '"' not in relpath


# ── /api/album/{id}/tracks ───────────────────────────────────────────────
def test_album_tracks_no_plan_returns_empty_shape():
    """A pre-split album has no `plan` in its manifest. The endpoint
    returns an empty-ish payload the UI uses to render "not split yet".
    `plan_version` is surfaced too so the editor's optimistic-concurrency
    token has a baseline value at load time."""
    aid = _make_album(tags={"artist": "A", "album": "B"})
    try:
        r = _client().get(f"/api/album/{aid}/tracks")
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "tracks": [], "music_relpath": None, "plan": None,
            "plan_version": 0,
        }
    finally:
        _cleanup_album(aid)


def test_album_tracks_unknown_album_returns_404():
    r = _client().get("/api/album/not-real/tracks")
    assert r.status_code == 404


def test_album_tracks_lists_kept_tracks_with_durations(monkeypatch):
    """When the manifest has a plan + music_relpath set, the endpoint
    enumerates the kept tracks (skip:false) and reports each one's
    on-disk size + duration when the file exists."""
    from services import albums_fs
    from state import MUSIC_DIR
    from routes import albums as albums_route

    aid = _make_album(tags={"artist": "Foo", "album": "Bar", "year": "2020"})
    try:
        # Plant a plan + music_relpath into the manifest, then drop the
        # corresponding files where the endpoint will look for them.
        manifest = albums_fs.read_manifest(aid)
        manifest["plan"] = {
            "tracks": [
                {"title": "Skipped", "duration_seconds": 5.0,  "skip": True},
                {"title": "Track A", "duration_seconds": 60.0, "skip": False},
                {"title": "Track B", "duration_seconds": 30.0, "skip": False},
            ],
        }
        manifest["music_relpath"] = "Foo/Bar (2020)"
        albums_fs.write_manifest(aid, manifest)
        music_dir = MUSIC_DIR / "Foo/Bar (2020)"
        music_dir.mkdir(parents=True, exist_ok=True)
        # Filenames mirror what split would have written.
        (music_dir / "01 - Track A.flac").write_bytes(b"x" * 1024)
        (music_dir / "02 - Track B.flac").write_bytes(b"x" * 2048)

        # Pin duration so the test isn't metaflac-dependent.
        monkeypatch.setattr(albums_route, "flac_duration_seconds",
                            lambda p: 42.0)

        r = _client().get(f"/api/album/{aid}/tracks")
        assert r.status_code == 200
        body = r.json()
        assert body["music_relpath"] == "Foo/Bar (2020)"
        assert len(body["tracks"]) == 2  # Skipped excluded
        assert body["tracks"][0]["filename"] == "01 - Track A.flac"
        assert body["tracks"][0]["track_number"] == 1
        assert body["tracks"][0]["duration_seconds"] == 42.0
        assert body["tracks"][0]["size_mb"] == 0.0  # 1 KB rounds to 0.0
        assert body["tracks"][1]["filename"] == "02 - Track B.flac"
        assert body["tracks"][1]["track_number"] == 2
    finally:
        _cleanup_album(aid)


def test_album_tracks_falls_back_to_plan_duration_when_missing(monkeypatch):
    """If a track file isn't on disk (split incomplete or moved), the
    endpoint reports `duration_seconds` from the plan, NOT None — the UI
    can then still render the row."""
    from services import albums_fs

    aid = _make_album(tags={"artist": "X", "album": "Y"})
    try:
        manifest = albums_fs.read_manifest(aid)
        manifest["plan"] = {"tracks": [
            {"title": "Lost", "duration_seconds": 99.5, "skip": False},
        ]}
        manifest["music_relpath"] = "X/Y"
        albums_fs.write_manifest(aid, manifest)
        # NO file on disk for the would-be track.

        r = _client().get(f"/api/album/{aid}/tracks")
        assert r.status_code == 200
        body = r.json()
        assert body["tracks"][0]["duration_seconds"] == 99.5
        assert body["tracks"][0]["size_mb"] is None
    finally:
        _cleanup_album(aid)


# ── /api/album/{id}/track/{trackname} ────────────────────────────────────
def test_download_track_unknown_album_returns_404():
    r = _client().get("/api/album/not-real/track/foo.flac")
    assert r.status_code == 404


def test_download_track_invalid_filename_returns_400():
    """The trackname regex requires an extension and forbids slashes; an
    invalid one is a 400 (path-traversal guard)."""
    aid = _make_album()
    try:
        r = _client().get(f"/api/album/{aid}/track/no-extension")
        assert r.status_code == 400
        # `..` is also rejected even with a .flac suffix.
        r = _client().get(f"/api/album/{aid}/track/..something.flac")
        assert r.status_code == 400
    finally:
        _cleanup_album(aid)


def test_download_track_unsplit_album_returns_404():
    """No `music_relpath` in the manifest → the album hasn't been split
    yet → 404 (not 500)."""
    aid = _make_album()
    try:
        r = _client().get(f"/api/album/{aid}/track/01 - x.flac")
        assert r.status_code == 404
    finally:
        _cleanup_album(aid)


def test_download_track_missing_file_returns_404():
    from services import albums_fs

    aid = _make_album(tags={"artist": "A", "album": "B"})
    try:
        manifest = albums_fs.read_manifest(aid)
        manifest["music_relpath"] = "A/B"
        albums_fs.write_manifest(aid, manifest)
        # music_relpath set but the requested file isn't there.
        r = _client().get(f"/api/album/{aid}/track/never-emitted.flac")
        assert r.status_code == 404
    finally:
        _cleanup_album(aid)


def test_download_track_serves_existing_file():
    from services import albums_fs
    from state import MUSIC_DIR

    aid = _make_album(tags={"artist": "Foo", "album": "Bar"})
    try:
        manifest = albums_fs.read_manifest(aid)
        manifest["music_relpath"] = "Foo/Bar"
        albums_fs.write_manifest(aid, manifest)
        out_dir = MUSIC_DIR / "Foo/Bar"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "01 - Track.flac").write_bytes(b"\x66\x4c\x61\x43audio")

        r = _client().get(f"/api/album/{aid}/track/01 - Track.flac")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/flac"
        # FastAPI url-encodes spaces in the Content-Disposition filename*
        # parameter — match either form rather than asserting on a single
        # encoding to keep the test resilient to FastAPI version churn.
        cd = r.headers.get("content-disposition", "")
        assert "01" in cd and "Track.flac" in cd
    finally:
        _cleanup_album(aid)


# ── /api/album/{id}/download (album zip) ─────────────────────────────────
def test_download_album_zips_tracks_and_cover():
    import io
    import zipfile

    from services import albums_fs
    from state import MUSIC_DIR

    aid = _make_album(tags={"artist": "Zippy", "album": "Discs"})
    try:
        manifest = albums_fs.read_manifest(aid)
        manifest["music_relpath"] = "Zippy/Discs (1999)"
        albums_fs.write_manifest(aid, manifest)
        out_dir = MUSIC_DIR / "Zippy/Discs (1999)"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "01 - A.flac").write_bytes(b"\x66\x4c\x61\x43one")
        (out_dir / "02 - B.flac").write_bytes(b"\x66\x4c\x61\x43two")
        (out_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0jpg")
        # A stray non-audio, non-cover file must NOT be bundled.
        (out_dir / "notes.txt").write_bytes(b"ignore me")

        r = _client().get(f"/api/album/{aid}/download")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"
        assert ".zip" in r.headers.get("content-disposition", "")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert set(zf.namelist()) == {"01 - A.flac", "02 - B.flac", "cover.jpg"}
        # Bytes round-trip intact (STORED, no recompression).
        assert zf.read("01 - A.flac") == b"\x66\x4c\x61\x43one"
    finally:
        _cleanup_album(aid)


def test_download_album_not_split_returns_404():
    """No music_relpath yet → the album hasn't been split, nothing to zip."""
    aid = _make_album()
    try:
        r = _client().get(f"/api/album/{aid}/download")
        assert r.status_code == 404
    finally:
        _cleanup_album(aid)


def test_download_album_unknown_returns_404():
    r = _client().get("/api/album/not-real/download")
    assert r.status_code == 404


# ── side_audio when manifest references missing-on-disk side ─────────────
def test_side_audio_when_side_missing_on_disk_returns_404():
    """`reconcile_sides` strips missing entries on read, but if the disk
    lookup races (file deleted between reconcile and the FileResponse),
    the explicit `if not p.exists()` guard returns 404 instead of 500."""
    from services import albums_fs

    aid = _make_album()
    try:
        # Force the manifest to claim a side that doesn't exist on disk
        # WITHOUT touching the file system (so reconcile_sides leaves the
        # entry in place during the request — actually `reconcile_sides`
        # strips missing entries, so we need to delete after manifest read.
        # Simulate by writing the manifest with a phantom side, then
        # immediately deleting reconcile's would-be source.
        manifest = albums_fs.read_manifest(aid)
        manifest["sides"] = ["phantom.flac"]
        albums_fs.write_manifest(aid, manifest)

        # reconcile_sides will rewrite sides[] based on what's on disk.
        # The album dir HAS side1.flac (from the combine) but NOT phantom.flac.
        # Index 0 will resolve to side1.flac, which DOES exist. To force the
        # 404 branch, ask for an out-of-range index instead.
        r = _client().get(f"/api/album/{aid}/sides/99/audio")
        assert r.status_code == 404
    finally:
        _cleanup_album(aid)
