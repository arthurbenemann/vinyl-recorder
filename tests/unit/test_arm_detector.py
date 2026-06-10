"""Unit tests for `app/services/arm.py` — the armed auto-record onset
detector.

All synthetic PCM, no threads: the detector accumulates time from chunk
byte-lengths, so feeding N seconds of loud/quiet chunks is fully
deterministic. 16-bit mono at 1000 bytes/sec keeps the arithmetic legible
(1 chunk of 500 bytes = 0.5 s).
"""
import struct

from services.arm import ArmDetector


BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = 1000.0  # 500 samples/s mono 16-bit — test-friendly


def _chunk(amplitude: int, seconds: float) -> bytes:
    """Constant-amplitude PCM chunk: RMS == |amplitude| exactly."""
    n = int(seconds * BYTES_PER_SECOND) // BYTES_PER_SAMPLE
    return struct.pack(f"<{n}h", *([amplitude] * n))


def _detector(**kw) -> ArmDetector:
    defaults = dict(threshold_int=1000, bytes_per_sample=BYTES_PER_SAMPLE,
                    bytes_per_second=BYTES_PER_SECOND,
                    quiet_seconds=1.0, tau_seconds=0.5)
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
    assert not _feed(det, 0, 2.0)        # quiet-confirm
    assert det.ready
    assert _feed(det, 20000, 1.0)        # needle drop → music


def test_does_not_fire_when_armed_mid_music():
    """Arming while music already plays must NOT instantly fire — the
    trigger is an edge, not a level."""
    det = _detector()
    assert not _feed(det, 20000, 5.0)
    assert not det.ready


def test_signal_resets_quiet_confirm_clock():
    """Quiet must be CONTINUOUS: 0.5 s quiet + blip + 0.5 s quiet is not
    1 s of quiet."""
    det = _detector(quiet_seconds=1.0, tau_seconds=0.0)  # no smoothing
    _feed(det, 0, 0.5)
    _feed(det, 20000, 0.1)               # blip resets the clock
    _feed(det, 0, 0.5)
    assert not det.ready
    _feed(det, 0, 0.6)                   # now a full second since the blip
    assert det.ready


def test_smoothing_rejects_single_click():
    """A few-ms pop (needle-drop click) barely moves the 0.5 s mean — it
    must not fire even when the detector is ready."""
    det = _detector()
    _feed(det, 0, 2.0)
    assert det.ready
    # One 10 ms click at moderate amplitude, then silence again.
    assert not det.update(_chunk(3000, 0.01))
    assert not _feed(det, 0, 0.2)
    assert det.ready                     # still armed, still ready


def test_fire_resets_to_not_ready():
    det = _detector()
    _feed(det, 0, 2.0)
    assert _feed(det, 20000, 1.0)
    assert not det.ready
    # Sustained signal after the fire can't immediately re-fire.
    assert not _feed(det, 20000, 5.0)


def test_refires_after_quiet_gap():
    """Side A ends (silence), record flipped, needle drops → re-fire.
    This is the hands-free multi-side loop. The gap must outlast the EMA
    decay (loud → below threshold takes ~ln(20000/1000) · tau·2 ≈ 3 s)
    plus the 1 s quiet-confirm — flipping a record takes well over that."""
    det = _detector()
    _feed(det, 0, 2.0)
    assert _feed(det, 20000, 1.0)        # side A trigger
    _feed(det, 0, 8.0)                   # runout / flipping the record
    assert det.ready
    assert _feed(det, 20000, 1.0)        # side B trigger


def test_reset_clears_ready():
    det = _detector()
    _feed(det, 0, 2.0)
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
