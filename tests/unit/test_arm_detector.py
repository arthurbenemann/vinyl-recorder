"""Unit tests for `app/services/arm.py` — the armed auto-record onset
detector.

All synthetic PCM, no threads: the detector accumulates time from chunk
byte-lengths, so feeding N seconds of loud/quiet chunks is fully
deterministic. 16-bit mono at 1000 bytes/sec keeps the arithmetic legible
(1 chunk of 500 bytes = 0.5 s). Constant-amplitude chunks make the peak
exactly the amplitude.
"""
import struct

from services.arm import ArmDetector


BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = 1000.0  # 500 samples/s mono 16-bit — test-friendly


def _chunk(amplitude: int, seconds: float) -> bytes:
    """Constant-amplitude PCM chunk: audioop.max == |amplitude| exactly."""
    n = int(seconds * BYTES_PER_SECOND) // BYTES_PER_SAMPLE
    return struct.pack(f"<{n}h", *([amplitude] * n))


def _detector(**kw) -> ArmDetector:
    defaults = dict(threshold_int=1000, bytes_per_sample=BYTES_PER_SAMPLE,
                    bytes_per_second=BYTES_PER_SECOND, quiet_seconds=1.0)
    defaults.update(kw)
    return ArmDetector(**defaults)


def _feed(det, amplitude: int, seconds: float, step: float = 0.1) -> bool:
    """Feed `seconds` of constant signal in `step`-sized chunks; True if
    any chunk fired."""
    fired = False
    t = 0.0
    while t < seconds - 1e-9:
        fired = det.update(_chunk(amplitude, step)) or fired
        t += step
    return fired


def test_fires_on_silence_then_signal():
    det = _detector()
    assert not _feed(det, 0, 1.5)        # quiet-confirm
    assert det.ready
    assert det.update(_chunk(20000, 0.05))   # one loud chunk is enough


def test_needle_drop_click_fires():
    """The whole point of peak detection: a single short set-down thump
    fires even though its energy contribution (RMS) would be negligible —
    this is what makes quiet albums recordable hands-free."""
    det = _detector()
    _feed(det, 0, 1.5)
    assert det.ready
    assert det.update(_chunk(5000, 0.01))    # 10 ms click above threshold


def test_runout_clicks_below_threshold_never_fire():
    """Runout-groove clicks (~-29 dBFS, below the -20 default) neither
    fire nor un-ready the detector — the post-side runout can spin for
    minutes without re-triggering, and still counts as quiet."""
    det = _detector()
    # Quiet-confirm with periodic sub-threshold clicks mixed in.
    for _ in range(15):
        assert not det.update(_chunk(0, 0.1))
        assert not det.update(_chunk(500, 0.01))   # click below threshold
    assert det.ready
    assert not det.update(_chunk(500, 0.01))       # still no fire
    assert det.ready


def test_does_not_fire_when_armed_mid_music():
    """Arming while music already plays must NOT instantly fire — the
    trigger is an edge, not a level."""
    det = _detector()
    assert not _feed(det, 20000, 5.0)
    assert not det.ready


def test_signal_resets_quiet_confirm_clock():
    """Quiet must be CONTINUOUS: 0.5 s quiet + above-threshold blip +
    0.5 s quiet is not 1 s of quiet."""
    det = _detector()
    _feed(det, 0, 0.5)
    det.update(_chunk(20000, 0.1))       # blip resets the clock
    _feed(det, 0, 0.5)
    assert not det.ready
    _feed(det, 0, 0.6)                   # now a full second since the blip
    assert det.ready


def test_fire_resets_to_not_ready():
    det = _detector()
    _feed(det, 0, 1.5)
    assert _feed(det, 20000, 0.3)
    assert not det.ready
    # Sustained signal after the fire can't immediately re-fire.
    assert not _feed(det, 20000, 5.0)


def test_refires_after_quiet_gap():
    """Side A ends (runout), record flipped, needle drops → re-fire.
    This is the hands-free multi-side loop. With no smoothing the
    detector re-readies after just `quiet_seconds` of sub-threshold
    audio — runout clicks don't disturb it (see the runout test)."""
    det = _detector()
    _feed(det, 0, 1.5)
    assert _feed(det, 20000, 0.3)        # side A trigger
    _feed(det, 0, 1.5)                   # runout / flipping the record
    assert det.ready
    assert det.update(_chunk(15000, 0.05))   # side B set-down


def test_reset_clears_ready():
    det = _detector()
    _feed(det, 0, 1.5)
    assert det.ready
    det.reset()
    assert not det.ready
    # And quiet-confirm starts over.
    _feed(det, 0, 0.5)
    assert not det.ready


def test_empty_chunk_is_inert():
    det = _detector()
    assert not det.update(b"")
    assert not det.ready
