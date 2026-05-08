"""Unit tests for `services.albums_fs` — the album-folder + manifest layer.

These tests do NOT shell out to ffmpeg/metaflac. They exercise:
  - manifest read/write (incl. malformed JSON fallback)
  - reconcile_sides drop-in / removal logic
  - reorder_sides validation
  - cache freshness (mtime-based) — NOT the ffmpeg run itself
  - new_album_id format
  - delete / demote helpers (filesystem state)

A real-ffmpeg "concat the cache" check lives in the e2e harness.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from services import albums_fs


# ── new_album_id ─────────────────────────────────────────────────────────
def test_new_album_id_is_eight_lower_hex():
    for _ in range(20):
        slug = albums_fs.new_album_id()
        assert len(slug) == 8
        assert slug == slug.lower()
        assert all(c in "0123456789abcdef" for c in slug)
        assert albums_fs.is_valid_album_id(slug)


def test_is_valid_album_id_rejects_traversal_and_empty():
    assert not albums_fs.is_valid_album_id("")
    assert not albums_fs.is_valid_album_id("..")
    assert not albums_fs.is_valid_album_id("foo/bar")
    assert not albums_fs.is_valid_album_id("foo bar")  # whitespace not allowed
    # A user-picked drop-in slug can use lower-case alnum + dashes/underscores.
    assert albums_fs.is_valid_album_id("rubber-soul-1965")


# ── read_manifest / write_manifest ───────────────────────────────────────
def test_read_manifest_returns_stub_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    (tmp_path / album_id).mkdir()
    m = albums_fs.read_manifest(album_id)
    # Stub keys are populated so callers can use `m["tags"]` without KeyError.
    assert m == {
        "schema_version": 2,
        "tags":           {},
        "sides":          [],
        "cover":          None,
        "plan":           None,
        "music_relpath":  None,
    }


def test_read_manifest_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    (tmp_path / album_id).mkdir()
    plan = {
        "tags": {"artist": "X", "album": "Y"},
        "sides": ["a.flac"],
        "plan": {"tracks": [], "normalize": False, "target_peak_db": -1.0,
                 "measured_peak_db": None, "bit_depth": 0},
    }
    albums_fs.write_manifest(album_id, plan)
    rt = albums_fs.read_manifest(album_id)
    assert rt["tags"] == {"artist": "X", "album": "Y"}
    assert rt["sides"] == ["a.flac"]
    # Stub keys backfilled when absent in the on-disk file.
    assert rt["schema_version"] == 2
    assert rt["cover"] is None


def test_read_manifest_falls_back_on_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    d = tmp_path / album_id
    d.mkdir()
    (d / "album.json").write_text("{not valid json")
    m = albums_fs.read_manifest(album_id)
    assert m["tags"] == {}
    assert m["sides"] == []


# ── reconcile_sides ──────────────────────────────────────────────────────
def _seed_album(tmp: Path, album_id: str, sides_in_manifest: list, sides_on_disk: list):
    d = tmp / album_id
    d.mkdir(parents=True, exist_ok=True)
    for s in sides_on_disk:
        (d / s).write_bytes(b"")
    if sides_in_manifest is not None:
        (d / "album.json").write_text(
            '{"schema_version":2,"tags":{},"sides":'
            + str(sides_in_manifest).replace("'", '"')
            + ',"cover":null,"plan":null,"music_relpath":null}'
        )
    return d


def test_reconcile_sides_appends_dropped_in_flacs(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    _seed_album(tmp_path, album_id,
                sides_in_manifest=["a.flac"],
                sides_on_disk=["a.flac", "b.flac"])
    m = albums_fs.reconcile_sides(album_id)
    # Append-at-end so existing order is preserved; b.flac appears after a.
    assert m["sides"] == ["a.flac", "b.flac"]
    # Persisted to disk.
    assert albums_fs.read_manifest(album_id)["sides"] == ["a.flac", "b.flac"]


def test_reconcile_sides_strips_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    _seed_album(tmp_path, album_id,
                sides_in_manifest=["a.flac", "ghost.flac", "b.flac"],
                sides_on_disk=["a.flac", "b.flac"])
    m = albums_fs.reconcile_sides(album_id)
    assert m["sides"] == ["a.flac", "b.flac"]


def test_reconcile_sides_no_op_when_synced(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    d = _seed_album(tmp_path, album_id,
                    sides_in_manifest=["a.flac", "b.flac"],
                    sides_on_disk=["a.flac", "b.flac"])
    before = (d / "album.json").stat().st_mtime_ns
    albums_fs.reconcile_sides(album_id)
    after = (d / "album.json").stat().st_mtime_ns
    # File is left untouched when nothing changed — avoids needless mtime
    # bumps that would invalidate the editor's cache.
    assert before == after


# ── reorder_sides ────────────────────────────────────────────────────────
def test_reorder_sides_persists_permutation(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    _seed_album(tmp_path, album_id,
                sides_in_manifest=["a.flac", "b.flac"],
                sides_on_disk=["a.flac", "b.flac"])
    albums_fs.reorder_sides(album_id, ["b.flac", "a.flac"])
    assert albums_fs.read_manifest(album_id)["sides"] == ["b.flac", "a.flac"]


def test_reorder_sides_rejects_non_permutation(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    _seed_album(tmp_path, album_id,
                sides_in_manifest=["a.flac", "b.flac"],
                sides_on_disk=["a.flac", "b.flac"])
    with pytest.raises(ValueError):
        albums_fs.reorder_sides(album_id, ["a.flac", "c.flac"])
    with pytest.raises(ValueError):
        albums_fs.reorder_sides(album_id, ["a.flac"])  # missing b.flac


# ── concat cache freshness ───────────────────────────────────────────────
def test_cache_is_fresh_helper(tmp_path):
    cache = tmp_path / "cache.flac"
    side_a = tmp_path / "a.flac"
    side_a.write_bytes(b"a")
    cache.write_bytes(b"c")
    # cache is older than side → not fresh
    import os, time
    older = time.time() - 100
    os.utime(cache, (older, older))
    assert albums_fs._cache_is_fresh(cache, [side_a]) is False
    # touch cache to a newer mtime
    newer = time.time() + 100
    os.utime(cache, (newer, newer))
    assert albums_fs._cache_is_fresh(cache, [side_a]) is True
    # missing side → not fresh
    side_a.unlink()
    assert albums_fs._cache_is_fresh(cache, [side_a]) is False


def test_ensure_concat_cache_raises_on_missing_side(tmp_path, monkeypatch):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    _seed_album(tmp_path, album_id,
                sides_in_manifest=["nope.flac"],
                sides_on_disk=[])
    with pytest.raises(FileNotFoundError):
        albums_fs.ensure_concat_cache(album_id)


def test_ensure_concat_cache_skips_ffmpeg_when_fresh(tmp_path, monkeypatch):
    """If the cache file already exists and is newer than every side, the
    builder must skip the ffmpeg subprocess entirely."""
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    d = _seed_album(tmp_path, album_id,
                    sides_in_manifest=["a.flac"],
                    sides_on_disk=["a.flac"])
    cache = d / ".cache" / "concat.flac"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"already-fresh")
    import os, time
    newer = time.time() + 100
    os.utime(cache, (newer, newer))
    with patch.object(albums_fs, "run_ffmpeg_with_progress") as m:
        albums_fs.ensure_concat_cache(album_id)
        m.assert_not_called()


# ── delete / demote ──────────────────────────────────────────────────────
def test_demote_album_moves_sides_back_and_removes_dir(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    inp = tmp_path / "in-progress"
    raw.mkdir(); inp.mkdir()
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(albums_fs, "RAW_DIR", raw)
    album_id = "abcd0123"
    _seed_album(inp, album_id,
                sides_in_manifest=["a.flac", "b.flac"],
                sides_on_disk=["a.flac", "b.flac"])
    res = albums_fs.demote_album(album_id)
    assert sorted(res["moved"]) == ["a.flac", "b.flac"]
    assert (raw / "a.flac").exists()
    assert (raw / "b.flac").exists()
    assert not (inp / album_id).exists()


def test_demote_album_uniquifies_on_collision(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    inp = tmp_path / "in-progress"
    raw.mkdir(); inp.mkdir()
    (raw / "a.flac").write_bytes(b"existing")
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(albums_fs, "RAW_DIR", raw)
    album_id = "abcd0123"
    _seed_album(inp, album_id,
                sides_in_manifest=["a.flac"],
                sides_on_disk=["a.flac"])
    res = albums_fs.demote_album(album_id)
    # Existing raw/a.flac is untouched; the moved side comes back as a (2).flac.
    assert res["moved"] == ["a (2).flac"]
    assert (raw / "a.flac").read_bytes() == b"existing"
    assert (raw / "a (2).flac").exists()


def test_delete_album_removes_dir_and_music_subtree(tmp_path, monkeypatch):
    inp = tmp_path / "in-progress"
    music = tmp_path / "music"
    inp.mkdir(); music.mkdir()
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(albums_fs, "MUSIC_DIR", music)
    album_id = "abcd0123"
    d = inp / album_id
    d.mkdir()
    (d / "album.json").write_text(
        '{"schema_version":2,"tags":{},"sides":[],"cover":null,"plan":{},'
        '"music_relpath":"X/Y (1999)"}'
    )
    target = music / "X" / "Y (1999)"
    target.mkdir(parents=True)
    (target / "01 - track.flac").write_bytes(b"")
    albums_fs.delete_album(album_id)
    assert not d.exists()
    assert not target.exists()
    # Empty parent artist dir is pruned.
    assert not (music / "X").exists()


# ── create_album ─────────────────────────────────────────────────────────
def test_create_album_moves_sides_and_writes_manifest(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    inp = tmp_path / "in-progress"
    raw.mkdir(); inp.mkdir()
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(albums_fs, "RAW_DIR", raw)
    (raw / "side1.flac").write_bytes(b"a")
    (raw / "side2.flac").write_bytes(b"b")
    album_id, manifest = albums_fs.create_album(
        ["side1.flac", "side2.flac"],
        {"artist": "X", "album": "Y", "year": "1999", "genre": ""},
    )
    assert albums_fs.is_valid_album_id(album_id)
    d = inp / album_id
    assert (d / "side1.flac").exists()
    assert (d / "side2.flac").exists()
    # Sides moved out of raw/ — not copied.
    assert not (raw / "side1.flac").exists()
    # Manifest reflects ordered sides + non-empty tags only.
    assert manifest["sides"] == ["side1.flac", "side2.flac"]
    assert manifest["tags"] == {"artist": "X", "album": "Y", "year": "1999"}


def test_create_album_404s_on_missing_side(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    inp = tmp_path / "in-progress"
    raw.mkdir(); inp.mkdir()
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(albums_fs, "RAW_DIR", raw)
    with pytest.raises(FileNotFoundError):
        albums_fs.create_album(["ghost.flac"], {})


def test_create_album_retries_on_slug_collision(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    inp = tmp_path / "in-progress"
    raw.mkdir(); inp.mkdir()
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(albums_fs, "RAW_DIR", raw)
    (raw / "side1.flac").write_bytes(b"a")
    # Pre-create a directory at "aaaaaaaa" so the first slug collides; the
    # second attempt with "bbbbbbbb" should win.
    (inp / "aaaaaaaa").mkdir()
    slugs = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(albums_fs, "new_album_id", lambda: next(slugs))
    album_id, _ = albums_fs.create_album(["side1.flac"], {})
    assert album_id == "bbbbbbbb"


# ── concat-cache invalidation ────────────────────────────────────────────
def test_ensure_concat_cache_rebuilds_when_side_mtime_advances(tmp_path, monkeypatch):
    """If a side's mtime is newer than the cache (file edited, dropped in,
    or `reorder_sides` left the cache stale), the next `ensure_concat_cache`
    must call back into ffmpeg. This is the contract the editor relies on
    when peeking at the waveform after a side change."""
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    d = _seed_album(tmp_path, album_id,
                    sides_in_manifest=["a.flac"],
                    sides_on_disk=["a.flac"])
    cache = d / ".cache" / "concat.flac"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"stale")
    import os, time
    # Cache predates the side: stale.
    older = time.time() - 100
    os.utime(cache, (older, older))
    side_newer = time.time() + 50
    os.utime(d / "a.flac", (side_newer, side_newer))
    # Mock the ffmpeg run so the test stays hermetic. The body must:
    # 1. be invoked (cache stale)
    # 2. cause the cache file to exist after, so subsequent calls don't
    #    rebuild — emulate by writing a fresh file from the side.
    def fake_run(cmd, total_dur, job_id, phase_range, label):
        out_path = Path(cmd[-1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"rebuilt")
        return (0, b"")
    with patch.object(albums_fs, "run_ffmpeg_with_progress", side_effect=fake_run) as m:
        albums_fs.ensure_concat_cache(album_id)
        assert m.call_count == 1


def test_invalidate_concat_cache_drops_the_file(tmp_path, monkeypatch):
    """`invalidate_concat_cache` is what the sides-reorder endpoint calls
    after persisting a permutation, so the next editor render rebuilds
    against the new side order."""
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    album_id = "abcd0123"
    d = _seed_album(tmp_path, album_id,
                    sides_in_manifest=["a.flac"],
                    sides_on_disk=["a.flac"])
    cache = d / ".cache" / "concat.flac"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"about to be killed")
    albums_fs.invalidate_concat_cache(album_id)
    assert not cache.exists()
    # Idempotent: calling again on an absent cache is a no-op, not a raise.
    albums_fs.invalidate_concat_cache(album_id)


# ── demote-of-split preserves the music subtree ──────────────────────────
def test_demote_album_preserves_emitted_music_subtree(tmp_path, monkeypatch):
    """When the user demotes an album that's already been split, the per-
    track FLACs in `music/{music_relpath}/` are a finished export and stay
    on disk. The album dir + sides go back to raw/. (UX promise the merged
    PR explicitly committed to in the demote dialog text.)"""
    raw = tmp_path / "raw"
    inp = tmp_path / "in-progress"
    music = tmp_path / "music"
    raw.mkdir(); inp.mkdir(); music.mkdir()
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(albums_fs, "RAW_DIR", raw)
    monkeypatch.setattr(albums_fs, "MUSIC_DIR", music)
    album_id = "abcd0123"
    # Seed a "split" album: manifest has music_relpath, music subtree exists.
    d = inp / album_id
    d.mkdir()
    (d / "side1.flac").write_bytes(b"")
    (d / "album.json").write_text(
        '{"schema_version":2,"tags":{"artist":"X","album":"Y","year":"1999"},'
        '"sides":["side1.flac"],"cover":null,'
        '"plan":{"tracks":[],"normalize":false,"target_peak_db":-1.0,'
        '"measured_peak_db":null,"bit_depth":0},'
        '"music_relpath":"X/Y (1999)"}'
    )
    music_album = music / "X" / "Y (1999)"
    music_album.mkdir(parents=True)
    (music_album / "01 - track.flac").write_bytes(b"keep me")

    res = albums_fs.demote_album(album_id)
    assert res["music_preserved"] is True
    assert res["moved"] == ["side1.flac"]
    assert (raw / "side1.flac").exists()
    assert not d.exists()
    # The music subtree stays put — including parent artist dir.
    assert music_album.is_dir()
    assert (music_album / "01 - track.flac").read_bytes() == b"keep me"
    assert (music / "X").is_dir()


# ── has_draft flag in /api/albums summary ────────────────────────────────
@pytest.mark.parametrize("plan, music_relpath, expected_split, expected_has_draft", [
    (None, None, False, False),                        # never opened
    ({"tracks": []}, None, False, True),               # editor saved a draft
    ({"tracks": []}, "X/Y", True, False),              # split has run
    (None, "X/Y", True, False),                        # exotic: relpath but
                                                       # plan cleared (still
                                                       # treated as "split")
])
def test_has_draft_flag_matrix(tmp_path, monkeypatch,
                                plan, music_relpath, expected_split, expected_has_draft):
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", tmp_path)
    monkeypatch.setattr(albums_fs, "MUSIC_DIR", tmp_path / "music")
    album_id = "abcd0123"
    d = tmp_path / album_id
    d.mkdir()
    (d / "a.flac").write_bytes(b"")
    import json as _json
    (d / "album.json").write_text(_json.dumps({
        "schema_version": 2, "tags": {}, "sides": ["a.flac"],
        "cover": None, "plan": plan, "music_relpath": music_relpath,
    }))
    rows = albums_fs.list_albums()
    assert len(rows) == 1
    row = rows[0]
    assert row["split"] is expected_split
    assert row["has_draft"] is expected_has_draft
