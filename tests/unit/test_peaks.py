"""Unit tests for services/peaks.py.

Cover the parts that don't need an audiowaveform binary or real audio:
header parsing, peak-from-.dat math, and the silence-run scanner. All
fixtures synthesise a v2 binary peak file in-memory.
"""
import struct
from pathlib import Path

import pytest

from services.peaks import (
    peak_db_from_dat, read_header, read_peaks_raw, silence_runs_from_dat,
)


def _write_dat(path: Path, sample_rate: int, samples_per_pixel: int,
               buckets, channels: int = 1, flags_bits16: bool = True,
               version: int = 1) -> Path:
    """Write a synthetic audiowaveform binary peak file (16-bit by default).

    Defaults to **v1 mono** because that's what audiowaveform produces by
    default (it downmixes to mono unless --split-channels is passed). For
    v1: 20-byte header, no `channels` field, body = 4*length int16 bytes.
    For v2: 24-byte header with channels, body = 4*length*channels int16.
    Each entry in `buckets` is either (min, max) for mono or
    [(minC0, maxC0), (minC1, maxC1)…] for multi-channel.
    """
    flags = 0x0 if flags_bits16 else 0x1  # bit 0: 1=8-bit, 0=16-bit
    if version == 1:
        header = struct.pack("<iIiII", 1, flags, sample_rate, samples_per_pixel, len(buckets))
    elif version == 2:
        header = struct.pack(
            "<iIiIIi",
            2, flags, sample_rate, samples_per_pixel, len(buckets), channels,
        )
    else:
        raise ValueError(f"unsupported version: {version}")
    body = bytearray()
    for entry in buckets:
        if version == 1 or channels == 1:
            mn, mx = entry
            body.extend(struct.pack("<hh", mn, mx))
        else:
            for mn, mx in entry:
                body.extend(struct.pack("<hh", mn, mx))
    path.write_bytes(header + bytes(body))
    return path


# ── Header / payload parsing ─────────────────────────────────────────────

def test_read_header_v1_mono(tmp_path):
    # The default audiowaveform output: v1 with a 20-byte header and an
    # implicit channels=1 (no explicit field on the wire).
    dat = _write_dat(tmp_path / "mono.peaks.dat", 96000, 256, [(-10, 12), (-2, 5)])
    head = read_header(dat)
    assert head == {
        "version": 1, "flags": 0, "sample_rate": 96000,
        "samples_per_pixel": 256, "length": 2, "channels": 1, "bits": 16,
        "header_size": 20,
    }


def test_read_header_v2_stereo(tmp_path):
    # v2 path covers the case where audiowaveform was invoked with
    # --split-channels (we don't, but the parser must still handle it).
    dat = _write_dat(tmp_path / "stereo.peaks.dat", 96000, 256,
                     [[(-10, 12), (-8, 10)], [(-2, 5), (-1, 4)]],
                     channels=2, version=2)
    head = read_header(dat)
    assert head["version"] == 2
    assert head["channels"] == 2
    assert head["length"] == 2
    assert head["header_size"] == 24


def test_read_header_rejects_unsupported_version(tmp_path):
    # Any version that isn't 1 or 2 is malformed; refuse rather than
    # mis-render. The wave editor surfaces this as "waveform unavailable".
    bad = tmp_path / "bad.dat"
    bad.write_bytes(struct.pack("<iIiII", 99, 0, 48000, 256, 0))
    with pytest.raises(ValueError, match="version"):
        read_header(bad)


def test_read_peaks_raw_v1_payload(tmp_path):
    # Round-trip a known v1 sequence to confirm the bytes survive verbatim
    # AND the body offset is 20 (not 24) — the bug that caused the editor's
    # "Invalid typed array length" was reading 4 body bytes as an int32
    # `channels` field at offset 20.
    dat = _write_dat(tmp_path / "p.dat", 48000, 128, [(-50, 70), (5, 120), (-127, -5)])
    head, body = read_peaks_raw(dat)
    assert head["version"] == 1
    assert head["header_size"] == 20
    assert head["length"] == 3
    # 3 buckets × 2 int16 values × 2 bytes = 12 bytes
    assert len(body) == 12
    vals = struct.unpack("<6h", body)
    assert vals == (-50, 70, 5, 120, -127, -5)


def test_read_peaks_raw_rejects_8bit(tmp_path):
    # 8-bit data has flags bit 0 = 1. Refuse it rather than misinterpret.
    dat = _write_dat(tmp_path / "q.dat", 48000, 256, [(0, 0)], flags_bits16=False)
    with pytest.raises(ValueError, match="8-bit"):
        read_peaks_raw(dat)


# ── Peak math ────────────────────────────────────────────────────────────

def test_peak_db_uses_max_abs_envelope(tmp_path):
    # Bucket 0 peaks at v=8000 (amp 8000/32768 ≈ 0.244 → ≈ -12.2 dBFS);
    # bucket 1 peaks at v=16000 (amp ≈ 0.488 → ≈ -6.2 dBFS). Album peak
    # comes from the louder bucket.
    p = _write_dat(tmp_path / "a.dat", 96000, 256, [(-4000, 8000), (-16000, 5000)])
    db = peak_db_from_dat(p)
    assert db is not None
    assert -6.3 < db < -6.1


def test_peak_db_picks_negative_extreme(tmp_path):
    # |v|=20000 from min, max=500 is small. The envelope must use |min|.
    p = _write_dat(tmp_path / "n.dat", 48000, 256, [(-20000, 500)])
    db = peak_db_from_dat(p)
    # amp = 20000/32768 ≈ 0.6104, 20*log10 ≈ -4.29 dB.
    assert -4.35 < db < -4.25


def test_peak_db_returns_none_for_silent(tmp_path):
    # All-zero buckets -> amp = 0 -> log10(0) blows up. Must short-circuit
    # and return None so the editor renders an empty/zero state instead.
    silent = _write_dat(tmp_path / "s.dat", 48000, 256, [(0, 0)] * 4)
    assert peak_db_from_dat(silent) is None


def test_peak_db_at_full_scale_caps_at_zero_dbfs(tmp_path):
    # v=32767 sits at the edge of int16; amp = 32767/32768 ≈ 1.0 so the
    # readout is 0 dBFS.
    p = _write_dat(tmp_path / "m.dat", 48000, 256, [(-32767, 32767)])
    db = peak_db_from_dat(p)
    assert db is not None and -0.01 <= db <= 0.0


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
        (-500, 500),
        (-2, 2), (-1, 1), (-2, 1),
        (-500, 500),
    ])
    runs = silence_runs_from_dat(dat, threshold=8, min_duration_s=2.0)
    assert len(runs) == 1
    assert runs[0]["start"] == pytest.approx(1.0)
    assert runs[0]["end"]   == pytest.approx(4.0)
    assert runs[0]["duration"] == pytest.approx(3.0)


def test_silence_skips_runs_below_min_duration(tmp_path):
    # 1-second quiet window doesn't meet a 2-second minimum. The auto-cut
    # placement relies on min_silence to filter inter-track gaps from
    # mid-song breath — never emit short hits.
    dat = _write_dat(tmp_path / "q.dat", 256, 256, [
        (-500, 500), (-1, 1), (-500, 500)
    ])
    runs = silence_runs_from_dat(dat, threshold=8, min_duration_s=2.0)
    assert runs == []


def test_silence_emits_trailing_run(tmp_path):
    # Quiet tail extending to EOF still counts. Without this the lead-out
    # at the end of a side wouldn't get a cut and would drag into the
    # next track's lead-in.
    dat = _write_dat(tmp_path / "tail.dat", 256, 256, [
        (-500, 500), (-1, 1), (-1, 1), (-1, 1)
    ])
    runs = silence_runs_from_dat(dat, threshold=8, min_duration_s=2.0)
    assert len(runs) == 1
    assert runs[0]["start"] == pytest.approx(1.0)
    assert runs[0]["end"]   == pytest.approx(4.0)


def test_silence_threshold_zero_returns_empty(tmp_path):
    # The slider's lower bound is 1; clamp threshold to >=1 in the
    # API layer. The scanner short-circuits on 0 to avoid emitting the
    # entire album as one giant silent run.
    dat = _write_dat(tmp_path / "s.dat", 256, 256, [(0, 0), (0, 0)])
    assert silence_runs_from_dat(dat, threshold=0, min_duration_s=1.0) == []


def test_silence_loud_album_returns_no_runs(tmp_path):
    # Threshold below every bucket's envelope -> no runs detected.
    dat = _write_dat(tmp_path / "loud.dat", 256, 256, [(-1000, 1000)] * 5)
    assert silence_runs_from_dat(dat, threshold=10, min_duration_s=1.0) == []


def test_silence_v2_stereo_combines_channels(tmp_path):
    # v2 stereo: each bucket holds (min,max) per channel contiguously. The
    # scanner's envelope is the loudest extreme across both channels — a
    # quiet left + loud right must NOT be flagged as silence even if left
    # alone would qualify. Without the channel-combine fix the scanner
    # would treat each channel's bytes as a separate bucket and over-detect.
    dat = _write_dat(
        tmp_path / "stereo.peaks.dat", 256, 256,
        [
            [(-500, 500), (-480, 480)],   # bucket 0: loud both
            [(-1, 1),     (-1, 1)],       # bucket 1: quiet both
            [(-1, 1),     (-1, 1)],       # bucket 2: quiet both
            [(-500, 500), (-1, 1)],       # bucket 3: loud L (despite quiet R) → not silence
            [(-1, 1),     (-1, 1)],       # bucket 4: quiet both
            [(-500, 500), (-500, 500)],   # bucket 5: loud
        ],
        channels=2, version=2,
    )
    runs = silence_runs_from_dat(dat, threshold=8, min_duration_s=1.5)
    # Only buckets 1+2 (2 s) qualify; bucket 4 alone (1 s) is too short.
    assert len(runs) == 1
    assert runs[0]["start"] == pytest.approx(1.0)
    assert runs[0]["end"]   == pytest.approx(3.0)


# ── Integration: real audiowaveform binary ───────────────────────────────
# Skipped when audiowaveform isn't on PATH so a fresh dev install (no apt
# / brew needed) still passes the unit suite. The Docker image always
# carries audiowaveform so the CI e2e job exercises this path implicitly;
# this test is a fast local guard against regressions in the
# audiowaveform-output → parser handshake.

import shutil
_AW_AVAILABLE = (shutil.which("audiowaveform") is not None
                 and shutil.which("ffmpeg") is not None)


@pytest.mark.skipif(not _AW_AVAILABLE,
                    reason="audiowaveform/ffmpeg not on PATH")
def test_audiowaveform_v1_output_round_trips(tmp_path):
    # Generate a real FLAC, run audiowaveform exactly as render_peaks does,
    # then verify the parser reads coherent values. Catches the v1/v2
    # regression that produced "Invalid typed array length" in the editor.
    import subprocess
    from services.peaks import render_peaks
    flac = tmp_path / "sine.flac"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=f=440:duration=2",
         "-ar", "48000", "-ac", "2", "-c:a", "flac", "-y", str(flac)],
        check=True, capture_output=True,
    )
    dat = tmp_path / "sine.peaks.dat"
    render_peaks(flac, dat)
    head = read_header(dat)
    # audiowaveform downmixes to mono → v1 by default. If this ever flips
    # to v2, the JS parser's v1 branch becomes dead code and we should
    # re-evaluate the editor's channel-combine path.
    assert head["version"] == 1
    assert head["channels"] == 1
    assert head["bits"] == 16
    assert head["sample_rate"] == 48000
    assert head["samples_per_pixel"] == 256
    assert head["length"] > 0
    # The body has exactly 4*length bytes (min/max int16 per bucket).
    body_bytes = dat.stat().st_size - head["header_size"]
    assert body_bytes == head["length"] * 4
    # ffmpeg's lavfi sine generator outputs around -20 dBFS by default —
    # we don't care about the exact level, just that it parses to a real
    # finite number (not None, not the v1/v2 bug's nonsense value).
    db = peak_db_from_dat(dat)
    assert db is not None and -60 < db < 0.5
