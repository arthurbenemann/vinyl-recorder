"""Signal-onset detector for armed auto-record ("drop the needle and walk
away").

The arm feature keeps the upstream live (the arm endpoint holds a lifecycle
ref) and runs every PCM chunk through this detector; when it fires, the
route layer starts a normal recording — pre-roll then back-fills the few
hundred milliseconds the detection took, so the lead-in groove is never
lost.

Kept free of route/upstream imports so unit tests can drive the detection
math directly with synthetic PCM, no threads or subprocesses involved.
"""
import audioop
import math


class ArmDetector:
    """Edge-triggered onset detector over raw PCM chunks.

    Two-phase trigger:

      1. *Quiet-confirm*: the smoothed RMS must stay below the threshold
         for `quiet_seconds` of continuous audio before the detector is
         ready. This is what makes the trigger an EDGE: arming while music
         is already playing (radio left on, mid-song) does NOT fire
         instantly, and after a recording ends the next onset needs a
         fresh silence→signal transition. After an auto-stop-on-silence
         the line is already quiet, so the detector re-readies within
         `quiet_seconds` — flip the record, drop the needle, side B
         records itself.
      2. *Onset*: once ready, the first chunk whose smoothed RMS reaches
         the threshold fires (update() returns True) and the detector
         resets to not-ready.

    The RMS is smoothed in mean-square space with a single-pole EMA, same
    math as the auto-stop silence detector (see routes/recordings.py
    `_update_smoothed_ms`), but with a much shorter time constant: silence
    detection smooths over ~2 s to average across runout-groove clicks,
    while onset detection wants a fast attack — music reaches the smoothed
    threshold within a couple hundred ms at tau=0.5 s, and the recording
    pre-roll absorbs that latency entirely. The smoothing still rejects
    one-off pops (a few-ms needle-drop click barely moves a 0.5 s mean).
    Time is accumulated from chunk byte-lengths, not the wall clock, so
    the detector is deterministic for tests.
    """

    def __init__(self, *, threshold_int: int, bytes_per_sample: int,
                 bytes_per_second: float, quiet_seconds: float = 1.0,
                 tau_seconds: float = 0.5):
        self.threshold_int = max(1, int(threshold_int))
        self.bytes_per_sample = bytes_per_sample
        self.bytes_per_second = max(1.0, float(bytes_per_second))
        self.quiet_seconds = quiet_seconds
        self.tau_seconds = tau_seconds
        self.ready = False
        self._ms_smoothed = 0.0
        self._quiet_accum = 0.0

    def reset(self) -> None:
        """Back to not-ready. Called whenever a recording session is active
        so the post-recording state always starts from a clean
        quiet-confirm, and after a failed fire so a hot signal can't retry
        in a tight loop."""
        self.ready = False
        self._ms_smoothed = 0.0
        self._quiet_accum = 0.0

    def update(self, chunk: bytes) -> bool:
        """Feed one PCM chunk; returns True exactly when the onset fires."""
        if not chunk:
            return False
        chunk_seconds = len(chunk) / self.bytes_per_second
        rms = audioop.rms(chunk, self.bytes_per_sample)
        chunk_ms = float(rms) * float(rms)
        if self.tau_seconds <= 0:
            self._ms_smoothed = chunk_ms
        else:
            alpha = 1.0 - math.exp(-chunk_seconds / self.tau_seconds)
            self._ms_smoothed = (self._ms_smoothed * (1.0 - alpha)
                                 + chunk_ms * alpha)
        smoothed_rms = math.sqrt(self._ms_smoothed)
        if smoothed_rms >= self.threshold_int:
            if self.ready:
                self.reset()
                return True
            # Signal while not ready (armed mid-music, or tail of the
            # previous take) — restart the quiet-confirm clock.
            self._quiet_accum = 0.0
            return False
        if not self.ready:
            self._quiet_accum += chunk_seconds
            if self._quiet_accum >= self.quiet_seconds:
                self.ready = True
        return False
