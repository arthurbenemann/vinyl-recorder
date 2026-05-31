"""Unit tests for albums_fs.ensure_side_peaks_cache.

Stubs out `services.peaks.render_peaks` (the audiowaveform invocation)
and asserts the per-side cache-freshness behaviour: if the side's
`.peaks.dat` is newer than the source FLAC, return immediately;
otherwise call render_peaks once and cache the result.
"""
import json
import os
import struct
from pathlib import Path
from unittest.mock import patch


def _valid_16bit_dat() -> bytes:
    """Minimal valid v1 16-bit audiowaveform dat body (flags=0x0 → 16-bit)."""
    header = struct.pack("<iIiII", 1, 0, 96000, 256, 1)
    body = b"\x00" * 4  # 1 bucket × 2 int16 values
    return header + body


def _seed_album(tmp_path: Path, monkeypatch, sides: list[str]) -> str:
    """Build a minimal in-progress/<id>/ tree with placeholder side FLACs.
    The state-module path constants are patched per-attribute via
    `monkeypatch.setattr` so the cleanup is automatic."""
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


def test_ensure_side_peaks_cache_renders_when_missing(tmp_path, monkeypatch):
    album_id = _seed_album(tmp_path, monkeypatch, ["a.flac", "b.flac"])
    import services.albums_fs as albums_fs

    calls = []

    def fake_render(src, out_dat):
        calls.append((Path(src).name, Path(out_dat).name))
        out_dat.write_bytes(b"\x00" * 32)

    with patch("services.peaks.render_peaks", side_effect=fake_render):
        out_a = albums_fs.ensure_side_peaks_cache(album_id, "a.flac")
        out_b = albums_fs.ensure_side_peaks_cache(album_id, "b.flac")

    assert out_a == albums_fs.peaks_cache_path_for_side(album_id, "a.flac")
    assert out_b == albums_fs.peaks_cache_path_for_side(album_id, "b.flac")
    assert out_a.exists() and out_b.exists()
    assert len(calls) == 2
    # Each side gets its own dat; no cross-contamination.
    assert {c[0] for c in calls} == {"a.flac", "b.flac"}
    assert {c[1] for c in calls} == {"a.dat", "b.dat"}


def test_ensure_side_peaks_cache_skips_when_fresh(tmp_path, monkeypatch):
    album_id = _seed_album(tmp_path, monkeypatch, ["a.flac"])
    import services.albums_fs as albums_fs

    # Pre-populate the dat with an mtime newer than the source side.
    dat = albums_fs.peaks_cache_path_for_side(album_id, "a.flac")
    dat.parent.mkdir(parents=True, exist_ok=True)
    dat.write_bytes(_valid_16bit_dat())
    src = albums_fs.album_dir(album_id) / "a.flac"
    os.utime(dat, (src.stat().st_atime, src.stat().st_mtime + 5))

    def boom(src, out_dat):
        raise AssertionError("render_peaks must not be called when cache is fresh")

    with patch("services.peaks.render_peaks", side_effect=boom):
        out = albums_fs.ensure_side_peaks_cache(album_id, "a.flac")

    assert out == dat


def test_ensure_side_peaks_cache_re_renders_when_side_advances(tmp_path, monkeypatch):
    """A re-recorded side bumps its mtime; only that side's dat rebuilds —
    other sides keep their existing dats."""
    album_id = _seed_album(tmp_path, monkeypatch, ["a.flac", "b.flac"])
    import services.albums_fs as albums_fs

    # Both dats currently fresh.
    for s in ("a.flac", "b.flac"):
        dat = albums_fs.peaks_cache_path_for_side(album_id, s)
        dat.parent.mkdir(parents=True, exist_ok=True)
        dat.write_bytes(_valid_16bit_dat())
        src = albums_fs.album_dir(album_id) / s
        os.utime(dat, (src.stat().st_atime, src.stat().st_mtime + 5))

    # Now bump only side a's source mtime — its dat is now stale.
    src_a = albums_fs.album_dir(album_id) / "a.flac"
    dat_a = albums_fs.peaks_cache_path_for_side(album_id, "a.flac")
    os.utime(src_a, (src_a.stat().st_atime, dat_a.stat().st_mtime + 10))

    calls = []

    def fake_render(src, out_dat):
        calls.append(Path(src).name)
        out_dat.write_bytes(b"\x00" * 32)

    with patch("services.peaks.render_peaks", side_effect=fake_render):
        albums_fs.ensure_side_peaks_cache(album_id, "a.flac")
        albums_fs.ensure_side_peaks_cache(album_id, "b.flac")

    assert calls == ["a.flac"], "only the stale side should re-render"


def test_ensure_side_peaks_cache_raises_on_missing_side(tmp_path, monkeypatch):
    album_id = _seed_album(tmp_path, monkeypatch, ["a.flac"])
    import services.albums_fs as albums_fs

    try:
        albums_fs.ensure_side_peaks_cache(album_id, "nonexistent.flac")
    except FileNotFoundError as e:
        assert "nonexistent.flac" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError for missing side")


def test_album_concat_playlist_writes_each_side_in_order(tmp_path, monkeypatch):
    """The /measure and /split routes feed ffmpeg via this transient
    playlist. Sides must appear in manifest order so album-time -ss/-to
    addressing produces the correct slice."""
    album_id = _seed_album(tmp_path, monkeypatch, ["a.flac", "b.flac", "c.flac"])
    import services.albums_fs as albums_fs

    playlist, side_paths = albums_fs.album_concat_playlist(album_id)
    try:
        body = playlist.read_text()
        lines = [ln for ln in body.splitlines() if ln.strip()]
        assert len(lines) == 3
        assert lines[0].endswith("a.flac'")
        assert lines[1].endswith("b.flac'")
        assert lines[2].endswith("c.flac'")
        assert [p.name for p in side_paths] == ["a.flac", "b.flac", "c.flac"]
    finally:
        playlist.unlink(missing_ok=True)


def test_album_concat_playlist_escapes_single_quotes(tmp_path, monkeypatch):
    """ffmpeg's concat demuxer wraps each path in single quotes; an
    embedded apostrophe in a side filename has to escape as `'\\''`. The
    real-world failure mode is the apostrophe in album titles like
    "It's Only Rock 'n Roll" leaking into a re-recorded side filename."""
    album_id = _seed_album(tmp_path, monkeypatch, ["it's a side.flac"])
    import services.albums_fs as albums_fs

    playlist, _ = albums_fs.album_concat_playlist(album_id)
    try:
        body = playlist.read_text()
        # The path appears as `file '...'` with internal `'` becoming `'\''`.
        assert "it'\\''s a side.flac" in body
    finally:
        playlist.unlink(missing_ok=True)
