"""Unit tests for routes/albums._resolve_threshold.

The mapping converts a dBFS noise-floor value from the wave-editor slider
into an int16 threshold (1..32767) matching the .peaks.dat 16-bit format.
"""
from routes.albums import _resolve_threshold
from state import SilenceDetectRequest


def _req(**kwargs) -> SilenceDetectRequest:
    return SilenceDetectRequest(album_id="abcdef01", **kwargs)


def test_noise_db_maps_to_int16():
    # 0 dBFS -> amp 1.0 -> int16 = 32767. -6 dB -> amp ~0.501 -> int16 ~16424.
    # -36 dB -> amp ~0.01585 -> int16 ~519. -60 dB -> amp ~0.001 -> int16 ~33.
    assert _resolve_threshold(_req(noise_db=0.0))   == 32767
    assert 16400 <= _resolve_threshold(_req(noise_db=-6.0))  <= 16450
    assert 510   <= _resolve_threshold(_req(noise_db=-36.0)) <= 530
    assert 30    <= _resolve_threshold(_req(noise_db=-60.0)) <= 36


def test_default_minus_36_db_maps_to_around_519():
    # The slider default is -36 dB ≈ 1/64 of full scale.
    val = _resolve_threshold(_req())
    assert 510 <= val <= 530


def test_extreme_quiet_clamps_to_one():
    # Very quiet thresholds (e.g. -90 dB) must clamp to 1, not go to 0
    # (threshold 0 would short-circuit the scanner and detect nothing).
    val = _resolve_threshold(_req(noise_db=-90.0))
    assert val == 1


def test_above_zero_db_clamps_to_max():
    # Positive dBFS is impossible for a real signal but the API should not
    # blow up — clamp to the int16 ceiling.
    val = _resolve_threshold(_req(noise_db=6.0))
    assert val == 32767
