"""Unit tests for routes/albums._resolve_threshold_int8.

The mapping is what the slider UI on the wave-editor relies on: each of
the 127 notches sets the silence-detection threshold in the same int8
units the .peaks.dat is stored in. Legacy clients that still POST the
old `noise_db` field must keep working — those go through the dB → int8
fallback path.
"""
from routes.albums import _resolve_threshold_int8
from state import SilenceDetectRequest


def _req(**kwargs) -> SilenceDetectRequest:
    return SilenceDetectRequest(album_id="abcdef01", **kwargs)


def test_explicit_threshold_int8_passed_through():
    # Slider sets threshold_int8 directly; the dB field is ignored.
    assert _resolve_threshold_int8(_req(threshold_int8=8, noise_db=-99)) == 8
    assert _resolve_threshold_int8(_req(threshold_int8=64)) == 64


def test_explicit_threshold_int8_clamps_to_useful_range():
    # < 1 would make the scanner short-circuit (everything silent); > 127
    # has no representable amplitude bucket above it.
    assert _resolve_threshold_int8(_req(threshold_int8=0))   == 1
    assert _resolve_threshold_int8(_req(threshold_int8=-5))  == 1
    assert _resolve_threshold_int8(_req(threshold_int8=200)) == 127


def test_legacy_noise_db_mapped_to_int8():
    # 0 dBFS -> amp 1.0 -> int8 = 127. -6 dB -> amp ~0.5 -> int8 ~64.
    # -42 dB -> amp ~0.0079 -> int8 ~1 (the practical floor).
    assert _resolve_threshold_int8(_req(noise_db=0.0))   == 127
    assert _resolve_threshold_int8(_req(noise_db=-6.0))  == 64
    assert _resolve_threshold_int8(_req(noise_db=-42.0)) == 1


def test_legacy_default_minus_40_db_maps_to_low_int8():
    # The previous endpoint defaulted to -40 dB; the int8 equivalent is
    # within ±1 of the slider's default value of 8 (≈ -24 dB).
    val = _resolve_threshold_int8(_req())
    assert 1 <= val <= 2  # -40 dB ≈ amp 0.01 ≈ int8 1.27 -> rounds to 1


def test_int8_takes_priority_over_db():
    # Both fields set: the explicit int8 wins. Mismatched values would be
    # the new client signalling intent the old field can't express.
    req = _req(threshold_int8=20, noise_db=-99)
    assert _resolve_threshold_int8(req) == 20
