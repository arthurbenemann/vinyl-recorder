"""Unit tests for UpstreamSession's PCM-byte peak detection + CLIP latch.

`_update_peaks` runs in the upstream reader thread for every audio frame.
It's the only thing keeping VU meters honest if a slow subscriber stalls,
and the CLIP detector reports off it. Bugs here go unnoticed until users
see flatline VUs — worth a unit test.
"""
import struct

from services.upstream import CLIP_THRESHOLD, UpstreamSession


def _s24le(value: int) -> bytes:
    """Encode a 24-bit signed integer as little-endian bytes."""
    if value < 0:
        value += 1 << 24
    return bytes([value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF])


def _s16le(value: int) -> bytes:
    return struct.pack("<h", value)


def _make_session() -> tuple[UpstreamSession, list[dict]]:
    events: list[dict] = []
    sess = UpstreamSession(on_event=events.append)
    return sess, events


# ── 24-bit / stereo (Pi default) ──────────────────────────────────────────
def test_24bit_silence_reports_zero_peak():
    sess, _ = _make_session()
    chunk = b"\x00" * (3 * 2 * 100)  # 100 stereo sample-pairs of zero
    sess._update_peaks(chunk, bps=3, channels=2)
    assert sess.peak_l == 0.0
    assert sess.peak_r == 0.0
    assert sess.clipped_l is False
    assert sess.clipped_r is False


def test_24bit_full_scale_left_only_triggers_clip_on_left():
    sess, events = _make_session()
    full_scale = 0x7FFFFF  # 24-bit positive max
    sample_pair = _s24le(full_scale) + _s24le(0)
    chunk = sample_pair * 50
    sess._update_peaks(chunk, bps=3, channels=2)
    # L hits 1.0 (well above 0.99 threshold), R stays at 0.
    assert sess.peak_l == 1.0
    assert sess.peak_r == 0.0
    assert sess.clipped_l is True
    assert sess.clipped_r is False
    # And a CLIP-on-L log event was emitted.
    clip_logs = [e for e in events if e.get("type") == "log" and "CLIP on L" in e.get("msg", "")]
    assert len(clip_logs) == 1


def test_24bit_negative_full_scale_also_triggers_clip():
    # Symmetry: the absolute-value path must catch -max as well as +max.
    # -0x800000 has slightly higher magnitude than +0x7FFFFF (asymmetric
    # 24-bit range), so the normalized peak nudges just past 1.0 — that's
    # fine for clip detection but the test must not assert exact equality.
    sess, _ = _make_session()
    neg_full = -0x800000  # 24-bit negative max
    chunk = (_s24le(0) + _s24le(neg_full)) * 50
    sess._update_peaks(chunk, bps=3, channels=2)
    assert sess.peak_r >= 1.0
    assert sess.clipped_r is True


def test_24bit_below_clip_threshold_does_not_latch():
    sess, _ = _make_session()
    # 0.95 of full scale — meter shows it but CLIP must not latch.
    val = int(0.95 * 0x7FFFFF)
    chunk = (_s24le(val) + _s24le(val)) * 50
    sess._update_peaks(chunk, bps=3, channels=2)
    assert 0.94 < sess.peak_l < 0.96
    assert sess.clipped_l is False
    assert sess.clipped_r is False


def test_24bit_clip_is_sticky_until_cleared():
    sess, _ = _make_session()
    # Frame 1: clip on L.
    chunk = (_s24le(0x7FFFFF) + _s24le(0)) * 10
    sess._update_peaks(chunk, bps=3, channels=2)
    assert sess.clipped_l is True
    # Frame 2: silence. Peak drops, latch holds.
    sess._update_peaks(b"\x00" * 60, bps=3, channels=2)
    assert sess.peak_l == 0.0
    assert sess.clipped_l is True
    # User acknowledges → latch clears.
    sess.clear_clip("L")
    assert sess.clipped_l is False


# ── 16-bit fallback ───────────────────────────────────────────────────────
def test_16bit_full_scale_triggers_clip():
    sess, _ = _make_session()
    full_scale = 0x7FFF
    chunk = (_s16le(full_scale) + _s16le(full_scale)) * 50
    sess._update_peaks(chunk, bps=2, channels=2)
    assert sess.peak_l == 1.0
    assert sess.peak_r == 1.0
    assert sess.clipped_l is True
    assert sess.clipped_r is True


def test_clip_threshold_constant_matches_expected():
    # Guards against an accidental loosening of the latch threshold —
    # the UI badge messaging assumes "near-full-scale only".
    assert CLIP_THRESHOLD == 0.99
