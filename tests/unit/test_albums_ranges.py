"""Unit tests for routes/albums.py:_astats_filter_for_ranges.

The helper builds the -filter_complex expression for the album measure
endpoint. Wave-editor uses it to exclude skipped regions from the album
peak/RMS readout, so the user-visible normalization gain is computed off
audio that will actually end up on disk.
"""
from routes.albums import _astats_filter_for_ranges


_MEASURE = (
    "astats=measure_overall=Peak_level+RMS_trough"
    ":measure_perchannel=Peak_level+RMS_trough"
)


def test_no_ranges_returns_bare_astats():
    # When the caller doesn't restrict ranges, we feed the whole track to
    # astats with no atrim — measure the entire input.
    assert _astats_filter_for_ranges(None) == _MEASURE
    assert _astats_filter_for_ranges([]) == _MEASURE


def test_single_range_uses_atrim_without_concat():
    # Single segment: atrim then astats, no concat needed.
    out = _astats_filter_for_ranges([[1.5, 10.0]])
    assert "atrim=start=1.500:end=10.000" in out
    assert "asetpts=PTS-STARTPTS" in out
    assert "concat=" not in out
    assert out.endswith(_MEASURE)


def test_multiple_ranges_chain_concat():
    out = _astats_filter_for_ranges([[0.0, 5.0], [10.0, 20.0], [30.0, 35.0]])
    # Each range produces its own atrim with a unique label.
    assert "[r0]" in out
    assert "[r1]" in out
    assert "[r2]" in out
    # And they're concatenated before astats sees them.
    assert "concat=n=3:v=0:a=1" in out
    assert out.endswith(_MEASURE)


def test_zero_or_negative_length_ranges_dropped():
    # A range with end <= start is a UI bug; skip it silently rather than
    # emitting a malformed atrim that would fail the whole measure.
    out = _astats_filter_for_ranges([[10.0, 10.0], [20.0, 30.0]])
    assert "[r0]" not in out
    assert "[r1]" in out
    # Down to one valid range — concat is unnecessary.
    assert "concat=" not in out


def test_all_ranges_invalid_falls_back_to_whole_input():
    # If everything is filtered out, we must still measure something rather
    # than emit a no-op pipeline that astats will choke on.
    assert _astats_filter_for_ranges([[5.0, 5.0]]) == _MEASURE
