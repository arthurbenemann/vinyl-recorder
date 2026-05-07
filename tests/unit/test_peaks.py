"""Unit tests for services/peaks.py.

Cover the parts that don't need an audiowaveform binary or real audio:
header parsing, peak-from-.dat math, and the silence-run scanner. All
fixtures synthesise a v2 binary peak file in-memory.
"""
import struct
from pathlib import Path

import pytest

from services.peaks import (
    peak_db_from_dat, read_header, read_peaks_int8, silence_runs_from_dat,
)


def _write_dat(path: Path, sample_rate: int, samples_per_pixel: int,
               buckets: list[tuple[int, int]], channels: int = 1,
               flags_bits8: bool = True) -> Path:
    """Write a synthetic audiowaveform v2 binary peak file. Each entry in
    `buckets` is (min_int8, max_int8). Caller picks values in [-128, 127]."""
    flags = 0x1 if flags_bits8 else 0x0
    header = struct.pack(
        "<iIiIIi",
        2, flags, sample_rate, samples_per_pixel, len(buckets), channels,
    )
    body = bytearray()
    for mn, mx in buckets:
        body.append(mn & 0xFF)
        body.append(mx & 0xFF)
    path.write_bytes(header + bytes(body))
    return path


# ── Header / payload parsing ─────────────────────────────────────────────

def test_read_header_decodes_v2_layout(tmp_path):
    dat = _write_dat(tmp_path / "side.peaks.dat", 96000, 256, [(-10, 12), (-2, 5)])
    head = read_header(dat)
    assert head == {
        "version": 2, "flags": 1, "sample_rate": 96000,
        "samples_per_pixel": 256, "length": 2, "channels": 1, "bits": 8,
    }


def test_read_peaks_int8_returns_payload(tmp_path):
    # Round-trip a known sequence to confirm the bytes survive verbatim.
    dat = _write_dat(tmp_path / "p.dat", 48000, 128, [(-50, 70), (5, 120), (-127, -5)])
    head, body = read_peaks_int8(dat)
    assert head["length"] == 3
    assert list(body) == [206, 70, 5, 120, 129, 251]  # signed -> unsigned bytes


def test_read_peaks_int8_rejects_16bit(tmp_path):
    # 16-bit data would mis-quantise the silence threshold scan. Refuse it
    # rather than silently misinterpret bytes as something they're not.
    dat = _write_dat(tmp_path / "q.dat", 48000, 256, [(0, 0)], flags_bits8=False)
    with pytest.raises(ValueError, match="8-bit"):
        read_peaks_int8(dat)


# ── Peak math ────────────────────────────────────────────────────────────

def test_peak_db_uses_max_abs_envelope(tmp_path):
    # Bucket 0 peaks at v=64 (mid-bin amp ≈ 0.5039 → ≈ -5.95 dBFS); bucket 1
    # peaks at v=100 (≈ -2.10 dBFS). Album peak comes from the louder bucket.
    p = _write_dat(tmp_path / "a.dat", 96000, 256, [(-32, 64), (-100, 50)])
    db = peak_db_from_dat(p)
    assert db is not None
    assert -2.15 < db < -2.05


def test_peak_db_picks_negative_extreme(tmp_path):
    # |v|=90 from min, max=5 is small. The envelope must use |min|.
    p = _write_dat(tmp_path / "n.dat", 48000, 256, [(-90, 5)])
    db = peak_db_from_dat(p)
    # amp = (90*256 + 127.5)/32768 ≈ 0.7068, 20*log10 ≈ -3.02 dB.
    assert -3.10 < db < -2.95


def test_peak_db_returns_none_for_silent(tmp_path):
    # All-zero buckets -> amp = 0 -> log10(0) blows up. Must short-circuit
    # and return None so the editor renders an empty/zero state instead.
    silent = _write_dat(tmp_path / "s.dat", 48000, 256, [(0, 0)] * 4)
    assert peak_db_from_dat(silent) is None


def test_peak_db_at_full_scale_caps_at_zero_dbfs(tmp_path):
    # v=127 sits at the edge of int8; mid-bin amp clamps to 1.0 so the
    # readout is 0 dBFS, not a small positive value.
    p = _write_dat(tmp_path / "m.dat", 48000, 256, [(-127, 127)])
    db = peak_db_from_dat(p)
    assert db is not None and -0.05 <= db <= 0.0


def test_peak_db_returns_none_on_corrupt_file(tmp_path):
    # Truncated / unparseable .dat — return None rather than 500 the
    # request. The editor falls back to "click measure" copy in this case.
    bad = tmp_path / "corrupt.dat"
    bad.write_bytes(b"\x00")
    assert peak_db_from_dat(bad) is None


# ── Silence-run scanner ──────────────────────────────────────────────────

def test_silence_detects_quiet_window(tmp_path):
    # samples_per_pixel/sample_rate = 1 s per bucket. Three quiet buckets
    # (level=2) flanked by loud ones; threshold=8 catches the quiet run.
    dat = _write_dat(tmp_path / "q.dat", 256, 256, [
        (-50, 50),
        (-2, 2), (-1, 1), (-2, 1),
        (-50, 50),
    ])
    runs = silence_runs_from_dat(dat, threshold_int8=8, min_duration_s=2.0)
    assert len(runs) == 1
    assert runs[0]["start"] == pytest.approx(1.0)
    assert runs[0]["end"]   == pytest.approx(4.0)
    assert runs[0]["duration"] == pytest.approx(3.0)


def test_silence_skips_runs_below_min_duration(tmp_path):
    # 1-second quiet window doesn't meet a 2-second minimum. The auto-cut
    # placement relies on min_silence to filter inter-track gaps from
    # mid-song breath — never emit short hits.
    dat = _write_dat(tmp_path / "q.dat", 256, 256, [
        (-50, 50), (-1, 1), (-50, 50)
    ])
    runs = silence_runs_from_dat(dat, threshold_int8=8, min_duration_s=2.0)
    assert runs == []


def test_silence_emits_trailing_run(tmp_path):
    # Quiet tail extending to EOF still counts. Without this the lead-out
    # at the end of a side wouldn't get a cut and would drag into the
    # next track's lead-in.
    dat = _write_dat(tmp_path / "tail.dat", 256, 256, [
        (-50, 50), (-1, 1), (-1, 1), (-1, 1)
    ])
    runs = silence_runs_from_dat(dat, threshold_int8=8, min_duration_s=2.0)
    assert len(runs) == 1
    assert runs[0]["start"] == pytest.approx(1.0)
    assert runs[0]["end"]   == pytest.approx(4.0)


def test_silence_threshold_zero_returns_empty(tmp_path):
    # The slider's lower bound is 1; clamp threshold_int8 to >=1 in the
    # API layer. The scanner short-circuits on 0 to avoid emitting the
    # entire album as one giant silent run.
    dat = _write_dat(tmp_path / "s.dat", 256, 256, [(0, 0), (0, 0)])
    assert silence_runs_from_dat(dat, threshold_int8=0, min_duration_s=1.0) == []


def test_silence_loud_album_returns_no_runs(tmp_path):
    # Threshold below every bucket's envelope -> no runs detected.
    dat = _write_dat(tmp_path / "loud.dat", 256, 256, [(-100, 100)] * 5)
    assert silence_runs_from_dat(dat, threshold_int8=10, min_duration_s=1.0) == []
