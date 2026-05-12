"""End-to-end behaviour test for the smoothed RMS silence detector.

The live recording sink in `routes.recordings._sink` was rebuilt around an
EMA-smoothed RMS (rather than per-chunk peak) so vinyl runout-groove
clicks can't keep re-arming the auto-stop. This module drives the same
math the sink runs — `_update_smoothed_ms` + the
arming / silence_since transitions — against synthetic signals that mimic:

  * a vinyl endrun  — low noise floor (-55 dBFS) + periodic ~-29 dBFS
    clicks every 1.8 s (33⅓ RPM rotation period)
  * music           — full-energy sustained signal (~-15 dBFS RMS)
  * absolute silence — zero samples

and verifies the detector reaches the right decision in each case. The
full FastAPI / ffmpeg loop is exercised separately by the e2e suite —
here we just exercise the pure helpers + state transitions.
"""
import array
import math
import random

import audioop  # 16-bit / 24-bit PCM RMS helper, same primitive _sink uses

from routes.recordings import (
    _SILENCE_RMS_TAU_SECONDS, _silence_threshold_int, _update_smoothed_ms,
)


# Synthetic stream parameters. 16-bit mono at 48 kHz keeps the synthesised
# bytes small while the math (full-scale, audioop.rms output range) is
# identical-shape to the production 24-bit stereo signal — `_silence_threshold_int`
# branches only on bytes_per_sample.
_SR = 48_000
_BPS = 2
_FULL_SCALE = 0x7FFF
_BYTES_PER_SEC = _SR * _BPS
# 16 ms chunks — matches the upstream reader's VU_FRAME_MS, so the per-chunk
# alpha the EMA sees here is the same as what the live sink sees.
_CHUNK_MS = 16
_CHUNK_SAMPLES = _SR * _CHUNK_MS // 1000


def _db_to_amp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _smoothed_db(smoothed_ms: float) -> float:
    """Convert the EMA's mean-square state back to a dBFS RMS reading."""
    return 20.0 * math.log10(math.sqrt(smoothed_ms) / _FULL_SCALE + 1e-30)


def _make_chunk(samples: list[int]) -> bytes:
    """Pack a list of signed 16-bit samples into little-endian PCM bytes
    — what `audioop.rms(chunk, 2)` expects."""
    return array.array('h', samples).tobytes()


def _noise_chunk(rms_db: float, rng: random.Random,
                 n_samples: int = _CHUNK_SAMPLES) -> bytes:
    """`n_samples` of uniform white noise at the requested RMS.

    Uniform U(-a, +a) has RMS = a / √3, so choose a accordingly. Clamped
    to int16 range so we never overflow."""
    rms_amp = _db_to_amp(rms_db) * _FULL_SCALE
    a = rms_amp * math.sqrt(3.0)
    samples = [int(max(-_FULL_SCALE, min(_FULL_SCALE, rng.uniform(-a, a))))
               for _ in range(n_samples)]
    return _make_chunk(samples)


def _sine_chunk(freq_hz: float, peak_db: float, phase0: float,
                n_samples: int = _CHUNK_SAMPLES) -> tuple[bytes, float]:
    """Continuous sine — RMS = peak / √2, so a -15 dBFS peak sine is
    ≈ -18 dBFS RMS. Returns (chunk, next_phase) so consecutive chunks
    glue together without phase discontinuities."""
    peak_amp = _db_to_amp(peak_db) * _FULL_SCALE
    samples = []
    phase = phase0
    step = 2 * math.pi * freq_hz / _SR
    for _ in range(n_samples):
        samples.append(int(peak_amp * math.sin(phase)))
        phase += step
    return _make_chunk(samples), phase


def _click_chunk(peak_db: float, noise_db: float,
                 rng: random.Random) -> bytes:
    """One chunk whose centre sample carries a click at `peak_db`,
    surrounded by noise at `noise_db`. Mimics the brief impulse the
    needle produces at each rotation of the runout groove."""
    base = bytearray(_noise_chunk(noise_db, rng))
    centre = len(base) // 2
    # Round to an int16-aligned offset and overwrite that one sample.
    if centre % 2:
        centre -= 1
    peak_amp = int(_db_to_amp(peak_db) * _FULL_SCALE)
    sample = array.array('h', [peak_amp]).tobytes()
    base[centre:centre + 2] = sample
    return bytes(base)


def _drive_chunks(chunks, threshold_int: int,
                  initial_ms: float = 0.0,
                  initial_armed: bool = False,
                  now0: float = 0.0):
    """Walk `chunks` through the smoothed-RMS state machine that
    `_sink` runs. Returns (final_smoothed_ms, armed, silence_since,
    final_time)."""
    smoothed_ms = initial_ms
    armed = initial_armed
    silence_since = None
    t = now0
    for chunk in chunks:
        chunk_rms = audioop.rms(chunk, _BPS)
        chunk_ms = float(chunk_rms) * float(chunk_rms)
        sec = len(chunk) / _BYTES_PER_SEC
        smoothed_ms = _update_smoothed_ms(smoothed_ms, chunk_ms,
                                          sec, _SILENCE_RMS_TAU_SECONDS)
        smoothed_rms = math.sqrt(smoothed_ms)
        if smoothed_rms >= threshold_int:
            armed = True
            silence_since = None
        elif armed and silence_since is None:
            silence_since = t
        t += sec
    return smoothed_ms, armed, silence_since, t


# ── pure EMA math ────────────────────────────────────────────────────────
def test_update_smoothed_ms_step_response_reaches_63pct_at_tau():
    """A step from 0 to A should reach (1 - 1/e) ≈ 63.2% of A after one
    time constant. This is the single most diagnostic property of a
    first-order low-pass — if it fails everything else is suspect."""
    A = 10_000.0 ** 2  # mean-square of a constant 10k RMS signal
    tau = 2.0
    # Drive the EMA with chunks summing to exactly `tau` seconds.
    state = 0.0
    elapsed = 0.0
    step = 0.016
    while elapsed < tau:
        state = _update_smoothed_ms(state, A, step, tau)
        elapsed += step
    # 63.2% of the target, ±2% for the discrete-step quantisation error.
    expected = A * (1.0 - 1.0 / math.e)
    assert 0.62 * A <= state <= 0.65 * A, \
        f"step response after τ should be ~63% of target, got {state/A:.3f}"
    assert abs(state - expected) / A < 0.02


def test_update_smoothed_ms_returns_input_when_tau_zero():
    """`tau == 0` collapses the EMA to "use the latest chunk only" —
    handy for tests that want to bypass smoothing, and a safe degradation
    if the constant gets misconfigured."""
    assert _update_smoothed_ms(prev_ms=99.0, chunk_ms=42.0,
                               chunk_seconds=0.016, tau_seconds=0.0) == 42.0


def test_update_smoothed_ms_returns_prev_when_chunk_duration_zero():
    """A zero-duration chunk carries no new information — the helper
    must short-circuit instead of dividing by zero or collapsing to the
    chunk's value (which would unfairly weight a runt frame)."""
    assert _update_smoothed_ms(prev_ms=50.0, chunk_ms=999.0,
                               chunk_seconds=0.0, tau_seconds=2.0) == 50.0


# ── threshold conversion ────────────────────────────────────────────────
def test_threshold_int_for_minus_40_db_is_about_one_pct_full_scale():
    """-40 dBFS RMS = full_scale × 0.01. For 16-bit that's ≈ 327; for
    24-bit ≈ 83886. The exact value is `int(full_scale × 0.01)`, no
    rounding mystery."""
    assert _silence_threshold_int(-40.0, 2) == int(0x7FFF * 0.01)
    assert _silence_threshold_int(-40.0, 3) == int(0x7FFFFF * 0.01)


# ── endrun: synthesised runout groove ───────────────────────────────────
def test_endrun_signal_drives_smoothed_rms_below_threshold():
    """Synthesise 25 s of vinyl-runout-shaped audio (-55 dBFS noise +
    -29 dBFS click every 1.8 s) and verify the smoothed RMS converges
    *below* the -40 dBFS default threshold.

    This is the regression motivation: a peak detector at the same
    threshold would re-arm every revolution, but the runout's mean
    energy is ~-47 dBFS RMS — well below -40."""
    rng = random.Random(0xE2D)
    threshold_int = _silence_threshold_int(-40.0, _BPS)

    # Pre-arm by simulating ~3 s of music so the detector enters the
    # state the real sink would see at end-of-side. -12 dBFS peak ≈
    # -15 dBFS RMS, which is the typical level a vinyl rip masters at.
    music_chunks = []
    phase = 0.0
    n_music_chunks = int(3.0 / (_CHUNK_MS / 1000))
    for _ in range(n_music_chunks):
        c, phase = _sine_chunk(freq_hz=440.0, peak_db=-12.0, phase0=phase)
        music_chunks.append(c)
    smoothed_ms, armed, since, t = _drive_chunks(music_chunks, threshold_int)
    assert armed is True, "music should arm the detector"
    assert since is None, "music should keep silence_since cleared"

    # Now feed 25 s of synthesised endrun. One click per 1.8 s (every
    # ~113 chunks at 16 ms / chunk) on top of a -55 dBFS noise floor.
    endrun_chunks = []
    chunks_per_rev = int(round(1.8 / (_CHUNK_MS / 1000)))
    n_endrun_chunks = int(25.0 / (_CHUNK_MS / 1000))
    for i in range(n_endrun_chunks):
        if i % chunks_per_rev == chunks_per_rev // 2:
            endrun_chunks.append(_click_chunk(peak_db=-29.0, noise_db=-55.0,
                                              rng=rng))
        else:
            endrun_chunks.append(_noise_chunk(rms_db=-55.0, rng=rng))

    smoothed_ms, armed, since, t = _drive_chunks(
        endrun_chunks, threshold_int,
        initial_ms=smoothed_ms, initial_armed=armed, now0=t,
    )

    final_db = _smoothed_db(smoothed_ms)
    # The smoothed RMS should land near the runout's mean energy. The
    # synthesised noise floor is -55 dBFS RMS with a click adding a tiny
    # amount of energy per revolution; the mean settles around -55 dBFS,
    # comfortably below -40.
    assert final_db < -45.0, (
        f"smoothed RMS during endrun should be well below -40 dBFS, "
        f"got {final_db:.1f} dBFS"
    )
    # Detector must have observed silence at some point — `silence_since`
    # is the watcher's gate for auto-stop.
    assert since is not None, "silence_since should be set during endrun"


def test_endrun_signal_marks_silence_long_enough_to_autostop():
    """After music pre-arms the detector, the silence_since timestamp
    must be set in time for a default-tuned watcher (silence_seconds=20)
    to fire within a reasonable real-world window. With a -15 dBFS RMS
    music level (vinyl-typical) and ~tau=2 s smoothing, the smoothed RMS
    crosses -40 dBFS within a handful of seconds of the music→endrun
    transition."""
    rng = random.Random(0xE2D)
    threshold_int = _silence_threshold_int(-40.0, _BPS)

    # 5 s of music at vinyl-typical -12 dBFS peak (-15 dBFS RMS) →
    # smoothed RMS settles close to its asymptote.
    music_chunks = []
    phase = 0.0
    for _ in range(int(5.0 / (_CHUNK_MS / 1000))):
        c, phase = _sine_chunk(freq_hz=440.0, peak_db=-12.0, phase0=phase)
        music_chunks.append(c)
    smoothed_ms, armed, since, t = _drive_chunks(music_chunks, threshold_int)
    music_end_t = t

    # 25 s of endrun — leaves plenty of margin past the decay time to
    # confirm silence_since gets set deterministically.
    endrun_chunks = []
    chunks_per_rev = int(round(1.8 / (_CHUNK_MS / 1000)))
    for i in range(int(25.0 / (_CHUNK_MS / 1000))):
        if i % chunks_per_rev == chunks_per_rev // 2:
            endrun_chunks.append(_click_chunk(peak_db=-29.0, noise_db=-55.0,
                                              rng=rng))
        else:
            endrun_chunks.append(_noise_chunk(rms_db=-55.0, rng=rng))
    smoothed_ms, armed, since, t = _drive_chunks(
        endrun_chunks, threshold_int,
        initial_ms=smoothed_ms, initial_armed=armed, now0=music_end_t,
    )

    assert since is not None, "silence_since must be set during the endrun"
    settle_time = since - music_end_t
    # Mean-square decays at τ=2 s, so a 25 dB RMS drop (-15 → -40 dBFS)
    # = 50 dB power drop = ~5·τ = ~11 s. Allow up to 15 s for jitter
    # near the threshold caused by click chunks.
    assert 0 <= settle_time <= 15.0, (
        f"silence_since should be set within 15 s of the transition, "
        f"got {settle_time:.1f} s"
    )


# ── music: high-energy signal must KEEP the detector armed ──────────────
def test_music_signal_keeps_detector_armed_and_silence_since_cleared():
    """Constant ~-15 dBFS RMS music (a continuous 440 Hz sine at -12 dBFS
    peak ≈ -15 dBFS RMS) must keep `silence_since` unset. If the EMA
    accidentally treated music as silence we'd auto-stop tracks mid-side."""
    threshold_int = _silence_threshold_int(-40.0, _BPS)
    chunks = []
    phase = 0.0
    for _ in range(int(30.0 / (_CHUNK_MS / 1000))):
        c, phase = _sine_chunk(freq_hz=440.0, peak_db=-12.0, phase0=phase)
        chunks.append(c)
    smoothed_ms, armed, since, _ = _drive_chunks(chunks, threshold_int)
    assert armed is True
    assert since is None, "music must never enter the silent branch"
    # Smoothed RMS should sit near the sine's RMS of -15 dBFS.
    assert _smoothed_db(smoothed_ms) > -25.0


# ── absolute silence (track gap with no rotational clicks) ──────────────
def test_absolute_silence_after_priming_sets_silence_since():
    """All-zero PCM (no rotational noise either) must obviously trigger
    silence_since — covers the simpler track-gap case as a sanity check
    that the EMA decays toward zero just as it does toward the runout
    floor. 20 s is well beyond the ~12 s decay from -15 dBFS RMS to the
    -40 dBFS threshold (50 dB power drop ≈ 5·τ at τ = 2 s)."""
    threshold_int = _silence_threshold_int(-40.0, _BPS)
    # Prime the EMA with a music-level mean-square so the detector is
    # armed; then feed zeros and verify silence_since fires.
    armed_ms = (_db_to_amp(-15.0) * _FULL_SCALE) ** 2
    silent_chunks = [_make_chunk([0] * _CHUNK_SAMPLES)
                     for _ in range(int(20.0 / (_CHUNK_MS / 1000)))]
    smoothed_ms, armed, since, _ = _drive_chunks(
        silent_chunks, threshold_int,
        initial_ms=armed_ms, initial_armed=True,
    )
    assert armed is True
    assert since is not None


def test_lead_in_silence_does_not_arm():
    """The whole point of `silence_armed` — feeding only silent chunks
    from the start must NOT set silence_since (it'd auto-stop a recording
    before the needle ever touched a groove)."""
    threshold_int = _silence_threshold_int(-40.0, _BPS)
    silent_chunks = [_make_chunk([0] * _CHUNK_SAMPLES)
                     for _ in range(int(30.0 / (_CHUNK_MS / 1000)))]
    smoothed_ms, armed, since, _ = _drive_chunks(silent_chunks, threshold_int)
    assert armed is False
    assert since is None


# ── regression: the old peak detector misbehaved on this signal ─────────
def test_old_peak_detector_would_have_failed_on_endrun():
    """Sanity check that the regression we're fixing is real. Synthesise
    the same endrun signal; run the OLD peak-based gate (`audioop.max`
    against the integer cutoff). The peak detector should re-arm on the
    runout clicks and never accumulate a silent run — proving why the
    RMS detector is needed."""
    rng = random.Random(0xE2D)
    threshold_int = _silence_threshold_int(-40.0, _BPS)

    armed = True       # pretend music already armed the old detector
    silence_since = None
    t = 0.0
    chunks_per_rev = int(round(1.8 / (_CHUNK_MS / 1000)))
    for i in range(int(15.0 / (_CHUNK_MS / 1000))):
        if i % chunks_per_rev == chunks_per_rev // 2:
            chunk = _click_chunk(peak_db=-29.0, noise_db=-55.0, rng=rng)
        else:
            chunk = _noise_chunk(rms_db=-55.0, rng=rng)
        peak = audioop.max(chunk, _BPS)
        if peak >= threshold_int:
            armed = True
            silence_since = None     # peak detector keeps re-arming here
        elif armed and silence_since is None:
            silence_since = t
        t += len(chunk) / _BYTES_PER_SEC

    # The old detector would never have accumulated a continuous silent
    # run long enough to auto-stop — silence_since gets cleared every
    # ~1.8 s by a click. The longest silent run we could possibly
    # observe is one inter-click gap (~1.7 s) — well under the default
    # silence_seconds=20 s gate.
    assert silence_since is None or (t - silence_since) < 2.0, (
        "old peak detector should never see a multi-revolution silent run "
        "during endrun — this test proves the regression motivation"
    )
