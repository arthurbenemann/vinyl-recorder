"""Signal-onset detector for armed auto-record ("drop the needle and walk
away").

The arm feature keeps the upstream live (the arm endpoint holds a lifecycle
ref) and runs every PCM chunk through this detector; when it fires, the
route layer starts a normal recording — pre-roll then back-fills the
detection latency, so the lead-in groove is never lost.

Kept free of route/upstream imports so unit tests can drive the detection
math directly with synthetic PCM, no threads or subprocesses involved.
"""
import audioop


class ArmDetector:
    """Edge-triggered PEAK detector over raw PCM chunks.

    START and STOP deliberately share one level — SILENCE_THRESHOLD_DB —
    but measure it differently:

      * STOP (auto-stop, in the recording sink) compares a ~2 s smoothed
        RMS against it: sustained quiet ends a side, and runout clicks
        average away.
      * START (this class) compares each chunk's PEAK against it: the
        needle set-down is a sharp transient that registers instantly on
        any record, however quiet the music that follows — an RMS measure
        would average the thump away and could miss pianissimo openings.

    Two-phase trigger:

      1. *Quiet-confirm*: every chunk's peak must stay below the threshold
         for `quiet_seconds` of CONTINUOUS audio before the detector is
         ready. This makes the trigger an edge — arming while music plays
         does not fire instantly — and it is also the runout-groove guard:
         runout clicks peak ~-29 dBFS, ABOVE a -40 threshold, roughly once
         per revolution (~1.8 s at 33⅓ RPM, ~1.3 s at 45). With
         `quiet_seconds` at 2.5 s — longer than a revolution — each click
         resets the clock before it can complete, so a side circling in
         the runout after auto-stop can never re-ready the detector. Only
         lifting the needle yields the continuous quiet that re-arms; the
         next set-down (or lead-in surface noise) then fires.
      2. *Onset*: once ready, the first chunk whose peak reaches the
         threshold fires (update() returns True) and the detector resets
         to not-ready.

    Time is accumulated from chunk byte-lengths, not the wall clock, so
    the detector is deterministic for tests.
    """

    def __init__(self, *, threshold_int: int, bytes_per_sample: int,
                 bytes_per_second: float, quiet_seconds: float = 2.5):
        self.threshold_int = max(1, int(threshold_int))
        self.bytes_per_sample = bytes_per_sample
        self.bytes_per_second = max(1.0, float(bytes_per_second))
        self.quiet_seconds = quiet_seconds
        self.ready = False
        self._quiet_accum = 0.0

    def reset(self) -> None:
        """Back to not-ready. Called whenever a recording session is active
        so the post-recording state always starts from a clean
        quiet-confirm, and after a failed fire so a hot signal can't retry
        in a tight loop."""
        self.ready = False
        self._quiet_accum = 0.0

    def update(self, chunk: bytes) -> bool:
        """Feed one PCM chunk; returns True exactly when the onset fires."""
        if not chunk:
            return False
        peak = audioop.max(chunk, self.bytes_per_sample)
        if peak >= self.threshold_int:
            if self.ready:
                self.reset()
                return True
            # Signal while not ready (armed mid-music, runout click, tail
            # of the previous take) — restart the quiet-confirm clock.
            self._quiet_accum = 0.0
            return False
        if not self.ready:
            self._quiet_accum += len(chunk) / self.bytes_per_second
            if self._quiet_accum >= self.quiet_seconds:
                self.ready = True
        return False
