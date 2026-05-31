"""Unit tests for the small helpers in services/peaks.py.

The decoded-bucket math (`silence_runs_from_dat`, `peak_db_from_dat`)
and `read_header` have happy-path coverage in tests/unit/test_peaks.py
(skipped when audiowaveform isn't on PATH). Here we cover the edge
cases that don't need the binary: cache freshness, malformed headers,
and `render_peaks` failure modes via subprocess mocks.
"""
import struct

import pytest

from services import peaks as peaks_mod


def _valid_16bit_dat_bytes(sample_rate=96000, spp=256, length=1) -> bytes:
    """Minimal valid v1 16-bit audiowaveform dat (flags=0x0)."""
    header = struct.pack("<iIiII", 1, 0, sample_rate, spp, length)
    body = bytes(length * 4)  # length buckets × 2 int16 values × 2 bytes
    return header + body


# ── is_fresh ─────────────────────────────────────────────────────────────
def test_is_fresh_when_dat_newer_than_src(tmp_path):
    src = tmp_path / "side.flac"
    dat = tmp_path / "side.peaks.dat"
    src.write_bytes(b"x")
    dat.write_bytes(_valid_16bit_dat_bytes())
    import os
    os.utime(src, (1_000_000, 1_000_000))
    os.utime(dat, (1_000_100, 1_000_100))
    assert peaks_mod.is_fresh(dat, src) is True


def test_is_fresh_when_dat_older_than_src(tmp_path):
    src = tmp_path / "side.flac"
    dat = tmp_path / "side.peaks.dat"
    src.write_bytes(b"x")
    dat.write_bytes(_valid_16bit_dat_bytes())
    import os
    os.utime(src, (1_000_100, 1_000_100))
    os.utime(dat, (1_000_000, 1_000_000))
    assert peaks_mod.is_fresh(dat, src) is False


def test_is_fresh_returns_false_when_either_missing(tmp_path):
    """Source vanished or dat never built → False, not an exception."""
    assert peaks_mod.is_fresh(tmp_path / "nope.dat", tmp_path / "nope.flac") is False


def test_is_fresh_returns_false_for_8bit_dat(tmp_path):
    """A stale 8-bit dat (flags=0x1) must be re-rendered to upgrade it to
    16-bit, so is_fresh() returns False even when the mtime is fresh."""
    src = tmp_path / "side.flac"
    dat = tmp_path / "side.peaks.dat"
    src.write_bytes(b"x")
    # Write an 8-bit header (flags=0x1).
    dat.write_bytes(struct.pack("<iIiII", 1, 0x1, 96000, 256, 1) + b"\x00" * 2)
    import os
    os.utime(src, (1_000_000, 1_000_000))
    os.utime(dat, (1_000_100, 1_000_100))
    assert peaks_mod.is_fresh(dat, src) is False


# ── read_header ──────────────────────────────────────────────────────────
def _v1_header(sample_rate=96000, spp=256, length=10, flags=0x0) -> bytes:
    """Build a valid v1 (mono, 20-byte) audiowaveform header. flags=0 → 16-bit."""
    return struct.pack("<iIiII", 1, flags, sample_rate, spp, length)


def _v2_header(channels=2, sample_rate=96000, spp=256, length=10, flags=0x0) -> bytes:
    return struct.pack("<iIiII", 2, flags, sample_rate, spp, length) + struct.pack("<i", channels)


def test_read_header_parses_v1(tmp_path):
    dat = tmp_path / "v1.dat"
    # Header + 4*length body bytes (min,max int16 per bucket) so file looks legit.
    dat.write_bytes(_v1_header() + b"\x00" * 40)
    h = peaks_mod.read_header(dat)
    assert h["version"] == 1
    assert h["channels"] == 1
    assert h["header_size"] == 20
    assert h["bits"] == 16


def test_read_header_parses_v2(tmp_path):
    dat = tmp_path / "v2.dat"
    dat.write_bytes(_v2_header(channels=2) + b"\x00" * 80)
    h = peaks_mod.read_header(dat)
    assert h["version"] == 2
    assert h["channels"] == 2
    assert h["header_size"] == 24


def test_read_header_short_file_raises(tmp_path):
    dat = tmp_path / "short.dat"
    dat.write_bytes(b"\x00" * 10)
    with pytest.raises(ValueError, match="short"):
        peaks_mod.read_header(dat)


def test_read_header_short_v2_header_raises(tmp_path):
    """v2 advertises 24 bytes of header. A file with only 20 bytes total
    must not silently misread the channel count from off-the-end."""
    dat = tmp_path / "short_v2.dat"
    # Pack 20 bytes claiming version=2 but truncated.
    head = struct.pack("<iIiII", 2, 0x0, 96000, 256, 10)
    dat.write_bytes(head)  # only 20 bytes — short for v2
    with pytest.raises(ValueError):
        peaks_mod.read_header(dat)


def test_read_header_unsupported_version_raises(tmp_path):
    dat = tmp_path / "vX.dat"
    dat.write_bytes(struct.pack("<iIiII", 9, 0, 0, 0, 0))
    with pytest.raises(ValueError, match="unsupported"):
        peaks_mod.read_header(dat)


# ── read_peaks_raw — wrong bit depth guard ───────────────────────────────
def test_read_peaks_raw_rejects_8bit_files(tmp_path):
    """The wave editor stack requires int16; opening an 8-bit dat must fail
    fast rather than misinterpret one byte per value as two."""
    dat = tmp_path / "8bit.dat"
    # flags bit 0 = 1 → 8-bit per the audiowaveform format spec.
    dat.write_bytes(_v1_header(flags=0x1) + b"\x00" * 20)
    with pytest.raises(ValueError, match="8-bit"):
        peaks_mod.read_peaks_raw(dat)


# ── render_peaks failure paths (mock subprocess) ─────────────────────────
def test_render_peaks_raises_on_nonzero_exit(monkeypatch, tmp_path):
    """audiowaveform error is surfaced as RuntimeError including the
    captured stderr — easier debugging than a generic subprocess error."""
    class _R:
        returncode = 2
        stderr = b"ERROR: bad input file\n"
        stdout = b""

    monkeypatch.setattr(peaks_mod.subprocess, "run", lambda *a, **kw: _R())

    src = tmp_path / "side.flac"
    src.write_bytes(b"x")
    dat = tmp_path / "out.dat"
    with pytest.raises(RuntimeError, match="audiowaveform failed"):
        peaks_mod.render_peaks(src, dat)
    # Tmp file should not be left behind on failure.
    assert not (tmp_path / "out.dat.tmp").exists()


def test_render_peaks_raises_on_empty_output(monkeypatch, tmp_path):
    """rc=0 but the resulting dat is empty / header-only → also a failure
    (this caught the silent-empty-output bug from the audiowaveform-via-
    pipe attempt; the comment in render_peaks explains)."""
    src = tmp_path / "side.flac"
    dat = tmp_path / "out.dat"
    src.write_bytes(b"x")

    class _R:
        returncode = 0
        stderr = b""
        stdout = b""

    def fake_run(*a, **kw):
        # Simulate audiowaveform writing a 5-byte file (well under the 20-byte
        # header). The real binary writes nothing in the pipe-bug scenario.
        (tmp_path / "out.dat.tmp").write_bytes(b"\x00" * 5)
        return _R()

    monkeypatch.setattr(peaks_mod.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="empty output"):
        peaks_mod.render_peaks(src, dat)
    # Cleanup happens on the empty-output branch too.
    assert not (tmp_path / "out.dat.tmp").exists()


# ── silence_runs_from_dat — guard branches ───────────────────────────────
def test_silence_runs_zero_threshold_returns_empty(tmp_path):
    """A threshold of 0 (or negative) doesn't represent any real silence
    band — short-circuit to empty rather than scanning the file."""
    # The dat is never read on this branch, so we don't need a valid file.
    assert peaks_mod.silence_runs_from_dat(tmp_path / "any.dat", 0, 1.5) == []
    assert peaks_mod.silence_runs_from_dat(tmp_path / "any.dat", -5, 1.5) == []


def test_silence_runs_unreadable_dat_returns_empty(tmp_path):
    """Missing / corrupt dat → empty list, not a 500 in the upstream
    detect-silences endpoint."""
    assert peaks_mod.silence_runs_from_dat(tmp_path / "missing.dat", 5, 0.5) == []
