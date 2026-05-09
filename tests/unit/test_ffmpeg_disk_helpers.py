"""Unit tests for the disk / filesystem helpers in services/ffmpeg.py.
The numeric parsers (parse_astats, parse_silencedetect, _parse_db) and
path-name sanitizers live in test_ffmpeg_helpers.py."""
from pathlib import Path

from services import ffmpeg as ffmpeg_mod
from services.ffmpeg import (
    LOW_SPACE_GB, disk_free_gb, disk_space_error, find_side, list_recordings,
)


# ── disk_free_gb ─────────────────────────────────────────────────────────
def test_disk_free_gb_returns_positive_float():
    # OUTPUT_DIR is a freshly-created tmp dir from conftest, so this should
    # always succeed and return a non-negative number rounded to 1dp.
    free = disk_free_gb()
    assert isinstance(free, float)
    assert free >= 0
    # Rounded to one decimal place — verify by re-rounding.
    assert round(free, 1) == free


# ── disk_space_error ─────────────────────────────────────────────────────
def test_disk_space_error_returns_none_when_plenty(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "disk_free_gb", lambda: 10.0)
    assert disk_space_error(LOW_SPACE_GB, "recording") is None


def test_disk_space_error_returns_message_when_low(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "disk_free_gb", lambda: 0.5)
    msg = disk_space_error(LOW_SPACE_GB, "recording")
    assert msg is not None
    # The error must mention the operation, the free amount, and the
    # threshold so the user knows exactly why the request was refused.
    assert "recording" in msg
    assert "0.5" in msg
    assert "2.0" in msg
    assert "Delete" in msg  # actionable suggestion


# ── find_side path-traversal guard ───────────────────────────────────────
def test_find_side_rejects_slashes():
    assert find_side("../etc/passwd") is None
    assert find_side("subdir/file.flac") is None
    assert find_side("subdir\\file.flac") is None


def test_find_side_rejects_dotdot_anywhere():
    # The check is a substring scan, so any `..` in the name fails closed
    # — even seemingly-benign ones. That's intentional: easier to reason
    # about than carving out exceptions.
    assert find_side("foo..bar.flac") is None


def test_find_side_returns_none_for_missing():
    # Slash-free name that doesn't exist on disk → None (not an error).
    assert find_side("definitely-not-here.flac") is None


def test_find_side_returns_path_when_present(tmp_path, monkeypatch):
    # Drop a fake file into RAW_DIR via the helper's view of state.
    from state import RAW_DIR
    f = RAW_DIR / "side-found.flac"
    f.write_bytes(b"not a real flac")
    try:
        p = find_side("side-found.flac")
        assert p is not None
        assert p.name == "side-found.flac"
        assert p.exists()
    finally:
        f.unlink(missing_ok=True)


# ── list_recordings ──────────────────────────────────────────────────────
def test_list_recordings_sorts_by_mtime_desc(monkeypatch):
    # We're not booting metaflac so flac_format/flac_duration_seconds will
    # silently return {}/None and the listing still works — that's the
    # "no-tags" path used for fresh raw sides anyway.
    from state import RAW_DIR

    # Stub the metaflac-backed helpers so the test doesn't depend on
    # whether the binary is on PATH.
    monkeypatch.setattr(ffmpeg_mod, "flac_format", lambda p: {})
    monkeypatch.setattr(ffmpeg_mod, "flac_duration_seconds", lambda p: None)

    older = RAW_DIR / "older.flac"
    newer = RAW_DIR / "newer.flac"
    older.write_bytes(b"x" * 1024)
    newer.write_bytes(b"y" * 2048)

    # Pin the order — set explicit mtimes so the test isn't racy on fast FS.
    import os
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (1_000_000 + 60, 1_000_000 + 60))

    try:
        recs = list_recordings()
        names = [r["filename"] for r in recs]
        assert "newer.flac" in names and "older.flac" in names
        # newer first, older second (sorted desc by mtime).
        assert names.index("newer.flac") < names.index("older.flac")
        # Each entry exposes the keys the listing endpoint promises to the
        # frontend.
        sample = next(r for r in recs if r["filename"] == "newer.flac")
        assert set(sample.keys()) >= {
            "filename", "size_mb", "mtime",
            "duration_seconds", "bit_depth", "sample_rate_khz",
        }
        # 2048 bytes → 0.0 MB at 1dp.
        assert sample["size_mb"] == 0.0
    finally:
        older.unlink(missing_ok=True)
        newer.unlink(missing_ok=True)


# ── flac_duration_seconds / flac_format error paths ──────────────────────
def test_flac_duration_seconds_missing_file_returns_none():
    # subprocess.check_output on a non-FLAC raises → helper swallows → None.
    assert ffmpeg_mod.flac_duration_seconds(Path("/no/such/file.flac")) is None


def test_flac_format_missing_file_returns_empty_dict():
    assert ffmpeg_mod.flac_format(Path("/no/such/file.flac")) == {}


def test_read_tags_missing_file_returns_empty_dict():
    # Same swallow-and-return-empty pattern; surfaces as "no tags" in the UI.
    assert ffmpeg_mod.read_tags(Path("/no/such/file.flac")) == {}


# ── write_tags shape (no metaflac dependency) ────────────────────────────
def test_write_tags_emits_one_invocation_for_empty_fields(monkeypatch):
    """When every supplied field is empty, write_tags still issues a
    single metaflac call: the --remove-tag pass that clears any leftover
    values from a prior tagging round. Same call also gets the file path
    appended at the end."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    ffmpeg_mod.write_tags(Path("/tmp/dummy.flac"), {"artist": "", "tracks": []})

    assert len(calls) == 1
    cmd = calls[0]
    assert any(a.startswith("--remove-tag=") for a in cmd)
    assert cmd[-1] == "/tmp/dummy.flac"


def test_write_tags_emits_set_tags_for_known_fields(monkeypatch):
    """Combined "remove existing + set new" in a single metaflac call —
    metaflac honors the flags in argv order, so this is half the
    subprocess overhead of the previous two-pass version."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    ffmpeg_mod.write_tags(
        Path("/tmp/dummy.flac"),
        {"artist": "Foo", "album": "Bar", "year": "1999", "tracks": ["A", "B"]},
    )
    assert len(calls) == 1
    cmd = calls[0]
    # Same call carries both the removes AND the sets.
    assert any(a.startswith("--remove-tag=") for a in cmd)
    assert "--set-tag=ARTIST=Foo" in cmd
    assert "--set-tag=ALBUM=Bar" in cmd
    assert "--set-tag=DATE=1999" in cmd
    assert any(s == "--set-tag=TRACKLIST=A / B" for s in cmd)
    assert cmd[-1] == "/tmp/dummy.flac"
