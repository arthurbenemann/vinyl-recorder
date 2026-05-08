"""Extra unit tests for services/albums_fs.py.

Focus: validation, cover-art helpers, and the demote/create collision-
uniquify branch — the parts not exercised by the existing fs/peaks suite
or the route-level tests."""
import json

import pytest

from services import albums_fs


# ── album_dir / is_valid_album_id ────────────────────────────────────────
def test_album_dir_invalid_id_raises():
    """Defence-in-depth: every callsite already checks via is_valid_album_id,
    but album_dir must still refuse a bad slug — otherwise a typo
    elsewhere could write outside in-progress/."""
    with pytest.raises(ValueError):
        albums_fs.album_dir("has spaces")
    with pytest.raises(ValueError):
        albums_fs.album_dir("../../etc/passwd")
    with pytest.raises(ValueError):
        albums_fs.album_dir("")


def test_is_valid_album_id_accepts_canonical_slug():
    aid = albums_fs.new_album_id()
    assert albums_fs.is_valid_album_id(aid) is True


def test_is_valid_album_id_rejects_bad_shapes():
    assert albums_fs.is_valid_album_id("") is False
    assert albums_fs.is_valid_album_id("has spaces") is False
    assert albums_fs.is_valid_album_id("../etc") is False


# ── read_manifest stub fallback paths ────────────────────────────────────
def _make_album(album_id: str, contents: str | None = None):
    """Create an in-progress album dir and (optionally) write a manifest
    file containing `contents` verbatim. Returns the manifest path."""
    d = albums_fs.album_dir(album_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "album.json"
    if contents is not None:
        p.write_text(contents)
    return p


def _cleanup_album(album_id: str) -> None:
    d = albums_fs.album_dir(album_id)
    if d.is_dir():
        for f in d.iterdir():
            try: f.unlink()
            except Exception: pass
        try: d.rmdir()
        except Exception: pass


def test_read_manifest_missing_file_returns_stub():
    aid = albums_fs.new_album_id()
    albums_fs.album_dir(aid).mkdir(parents=True, exist_ok=True)
    try:
        m = albums_fs.read_manifest(aid)
        # _stub_manifest fills in the v2 keys downstream code expects.
        assert "tags" in m
        assert m.get("sides") == [] or m.get("sides") is None or isinstance(m["sides"], list)
    finally:
        _cleanup_album(aid)


def test_read_manifest_invalid_json_returns_stub():
    """Corrupt manifest → stub, not a 500 anywhere up the call chain."""
    aid = albums_fs.new_album_id()
    _make_album(aid, "{not valid json")
    try:
        m = albums_fs.read_manifest(aid)
        assert isinstance(m, dict)
        assert "tags" in m
    finally:
        _cleanup_album(aid)


def test_read_manifest_non_object_top_level_returns_stub():
    """A manifest that decodes but isn't a dict (e.g. ``[1, 2]``) is
    treated as malformed — same stub fallback."""
    aid = albums_fs.new_album_id()
    _make_album(aid, "[1, 2, 3]")
    try:
        m = albums_fs.read_manifest(aid)
        assert isinstance(m, dict)
    finally:
        _cleanup_album(aid)


# ── write_cover + cover_path ─────────────────────────────────────────────
def test_write_cover_persists_bytes_and_updates_manifest():
    aid = albums_fs.new_album_id()
    d = albums_fs.album_dir(aid)
    d.mkdir(parents=True, exist_ok=True)
    try:
        p = albums_fs.write_cover(aid, b"\xff\xd8\xff fake jpeg")
        assert p.read_bytes().startswith(b"\xff\xd8\xff")
        manifest = albums_fs.read_manifest(aid)
        assert manifest["cover"] == "cover.jpg"
    finally:
        _cleanup_album(aid)


def test_cover_path_returns_none_when_manifest_lacks_cover():
    aid = albums_fs.new_album_id()
    d = albums_fs.album_dir(aid)
    d.mkdir(parents=True, exist_ok=True)
    try:
        # No cover written → manifest has cover unset → None.
        assert albums_fs.cover_path(aid) is None
    finally:
        _cleanup_album(aid)


def test_cover_path_rejects_traversal_in_manifest():
    """If the manifest's cover field somehow holds a traversal-y path
    (corrupt write, hostile copy), cover_path must NOT serve a file
    outside the album dir."""
    aid = albums_fs.new_album_id()
    d = albums_fs.album_dir(aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "album.json").write_text(json.dumps({"cover": "../../../etc/passwd"}))
    try:
        assert albums_fs.cover_path(aid) is None
    finally:
        _cleanup_album(aid)


def test_cover_path_returns_path_when_present():
    aid = albums_fs.new_album_id()
    d = albums_fs.album_dir(aid)
    d.mkdir(parents=True, exist_ok=True)
    try:
        albums_fs.write_cover(aid, b"jpegbytes")
        p = albums_fs.cover_path(aid)
        assert p is not None
        assert p.name == "cover.jpg"
    finally:
        _cleanup_album(aid)


# ── create_album: rejects traversal-style filenames ──────────────────────
def test_create_album_rejects_traversal_filenames():
    """Filenames with `/`, `\\`, or `..` must short-circuit to ValueError
    before any FS interaction."""
    with pytest.raises(ValueError):
        albums_fs.create_album(["../etc/passwd"], {})
    with pytest.raises(ValueError):
        albums_fs.create_album(["sub/dir/file.flac"], {})


def test_create_album_missing_source_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        albums_fs.create_album(["definitely-not-here.flac"], {})


# ── demote_album: collision uniquifies on the way back to raw/ ────────────
def test_demote_uniquifies_filename_on_collision():
    """If raw/ already has a side with the same name when demoting, the
    function must rename the moved-back side to `name (2).flac` instead
    of clobbering the existing file."""
    from state import RAW_DIR

    src1 = RAW_DIR / "demote_unique.flac"
    src1.write_bytes(b"first")
    aid, _ = albums_fs.create_album(["demote_unique.flac"], {})
    # Now drop a NEW file at raw/demote_unique.flac so the demote target
    # collides.
    (RAW_DIR / "demote_unique.flac").write_bytes(b"new")
    try:
        result = albums_fs.demote_album(aid)
        moved = result["moved"]
        assert moved == ["demote_unique (2).flac"]
        assert (RAW_DIR / "demote_unique.flac").read_bytes() == b"new"
        assert (RAW_DIR / "demote_unique (2).flac").read_bytes() == b"first"
    finally:
        for p in [
            RAW_DIR / "demote_unique.flac",
            RAW_DIR / "demote_unique (2).flac",
        ]:
            p.unlink(missing_ok=True)


# ── reorder_sides validation ─────────────────────────────────────────────
def test_reorder_sides_rejects_non_permutations():
    """Adding or removing a side via reorder is forbidden — only
    permutations of the existing on-disk set are allowed. Otherwise the
    bad-side test in the route would be just a route-level guard."""
    from state import RAW_DIR

    a = RAW_DIR / "reorder_a.flac"
    a.write_bytes(b"a")
    aid, _ = albums_fs.create_album(["reorder_a.flac"], {})
    try:
        with pytest.raises(ValueError):
            albums_fs.reorder_sides(aid, ["different.flac"])
        with pytest.raises(ValueError):
            albums_fs.reorder_sides(aid, [])
    finally:
        _cleanup_album(aid)
