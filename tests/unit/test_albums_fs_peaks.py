"""Unit tests for albums_fs.ensure_peaks_cache.

Stubs out `services.peaks.render_peaks` (the audiowaveform invocation)
and asserts the cache-freshness behaviour: if `.peaks.dat` is newer than
the concat cache, return immediately; otherwise call render_peaks once
and cache the result.
"""
import importlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _restore_state_modules():
    # state.py and albums_fs.py cache OUTPUT_DIR-derived paths at import
    # time. Each test in this file reloads them against a per-test tmp dir
    # (see _seed_album); restore the conftest-set OUTPUT_DIR afterwards so
    # unrelated tests keep seeing the shared throwaway dir.
    saved = os.environ.get("OUTPUT_DIR")
    yield
    if saved is not None:
        os.environ["OUTPUT_DIR"] = saved
    else:
        os.environ.pop("OUTPUT_DIR", None)
    import state
    import services.albums_fs as albums_fs
    importlib.reload(state)
    importlib.reload(albums_fs)


def _seed_album(tmp_path: Path, monkeypatch, sides: list[str]) -> str:
    """Build a minimal in-progress/<id>/ tree with empty side FLACs and a
    pre-built concat.flac so ensure_peaks_cache doesn't try to run ffmpeg.

    Path constants are patched per-attribute via `monkeypatch.setattr` so
    the cleanup is automatic. An earlier version reloaded the `state`
    module after `monkeypatch.setenv`, which left a stale RAW_DIR pointing
    at the now-deleted tmp_path even after monkeypatch reverted the env
    var, breaking unrelated API tests (`test_recordings_lists_files_in_raw`)
    that ran later in the suite."""
    ip = tmp_path / "in-progress"
    raw = tmp_path / "raw"
    music = tmp_path / "music"
    log_dir = tmp_path / ".logs"
    for _d in (ip, raw, music, log_dir):
        _d.mkdir(parents=True, exist_ok=True)
    import services.albums_fs as albums_fs
    import state
    monkeypatch.setattr(state, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(state, "IN_PROGRESS_DIR", ip)
    monkeypatch.setattr(state, "RAW_DIR", raw)
    monkeypatch.setattr(state, "MUSIC_DIR", music)
    monkeypatch.setattr(state, "LOG_DIR", log_dir)
    monkeypatch.setattr(albums_fs, "IN_PROGRESS_DIR", ip)

    album_id = "ab12cd34"
    d = ip / album_id
    d.mkdir(parents=True)
    for s in sides:
        (d / s).write_bytes(b"FLAC\x00\x00\x00")  # placeholder bytes
    cache = d / ".cache"
    cache.mkdir()
    (cache / "concat.flac").write_bytes(b"placeholder concat")
    manifest = {
        "schema_version": 2,
        "tags": {},
        "sides": list(sides),
        "cover": None,
        "plan": None,
        "music_relpath": None,
    }
    (d / "album.json").write_text(json.dumps(manifest))
    return album_id


def test_ensure_peaks_cache_renders_when_missing(tmp_path, monkeypatch):
    album_id = _seed_album(tmp_path, monkeypatch, ["a.flac"])
    import services.albums_fs as albums_fs

    calls = []

    def fake_render(src, out_dat):
        calls.append((src, out_dat))
        out_dat.write_bytes(b"\x00" * 32)

    # ensure_concat_cache also normally invokes ffmpeg — short-circuit it
    # to return the placeholder we already wrote in _seed_album.
    def fake_concat(album_id, job_id=None):
        return albums_fs.concat_cache_path(album_id)

    with patch("services.peaks.render_peaks", side_effect=fake_render), \
         patch.object(albums_fs, "ensure_concat_cache", side_effect=fake_concat):
        out = albums_fs.ensure_peaks_cache(album_id)

    assert out == albums_fs.peaks_cache_path(album_id)
    assert out.exists()
    assert len(calls) == 1


def test_ensure_peaks_cache_skips_when_fresh(tmp_path, monkeypatch):
    album_id = _seed_album(tmp_path, monkeypatch, ["a.flac"])
    import services.albums_fs as albums_fs

    # Pre-populate the dat with an mtime newer than concat.flac.
    dat = albums_fs.peaks_cache_path(album_id)
    dat.parent.mkdir(parents=True, exist_ok=True)
    dat.write_bytes(b"\x00" * 32)
    import os
    cat = albums_fs.concat_cache_path(album_id)
    os.utime(dat, (cat.stat().st_atime, cat.stat().st_mtime + 5))

    def boom(src, out_dat):
        raise AssertionError("render_peaks must not be called when cache is fresh")

    def fake_concat(album_id, job_id=None):
        return cat

    with patch("services.peaks.render_peaks", side_effect=boom), \
         patch.object(albums_fs, "ensure_concat_cache", side_effect=fake_concat):
        out = albums_fs.ensure_peaks_cache(album_id)

    assert out == dat


def test_ensure_peaks_cache_re_renders_after_concat_invalidated(tmp_path, monkeypatch):
    album_id = _seed_album(tmp_path, monkeypatch, ["a.flac"])
    import services.albums_fs as albums_fs

    # Populate dat with mtime EARLIER than concat (simulating a side
    # reorder that bumped concat's mtime).
    dat = albums_fs.peaks_cache_path(album_id)
    dat.parent.mkdir(parents=True, exist_ok=True)
    dat.write_bytes(b"\x00" * 32)
    import os
    cat = albums_fs.concat_cache_path(album_id)
    os.utime(dat, (cat.stat().st_atime, cat.stat().st_mtime - 5))

    calls = []

    def fake_render(src, out_dat):
        calls.append((src, out_dat))
        out_dat.write_bytes(b"\x00" * 32)

    def fake_concat(album_id, job_id=None):
        return cat

    with patch("services.peaks.render_peaks", side_effect=fake_render), \
         patch.object(albums_fs, "ensure_concat_cache", side_effect=fake_concat):
        albums_fs.ensure_peaks_cache(album_id)

    assert len(calls) == 1, "stale dat must trigger a re-render"
