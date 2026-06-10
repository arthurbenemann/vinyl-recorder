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

    The trigger is deliberately peak-based and unsmoothed — the physical
    event we want is the needle set-down thump, a sharp transient that
    peaks around -20…-5 dBFS on any record, however quiet the music that
    follows. A smoothed-RMS trigger (like the auto-stop silence detector
    uses) would average that transient away and could miss pianissimo
    openings entirely; a per-chunk peak catches the thump itself within
    one ~50 ms chunk. The default threshold (-20 dBFS, see
    ARM_SIGNAL_THRESHOLD_DB in state.py) sits well ABOVE runout-groove
    clicks (~-29 dBFS peaks once per revolution), so a side circling in
    the runout after auto-stop cannot re-trigger — only the next set-down
    (or music) can. The cost of an occasional false trigger (a bumped
    turntable) is bounded: the duration cap ends the take and the
    no-signal warning flags it within 30 s.

    Two-phase trigger:

      1. *Quiet-confirm*: every chunk's peak must stay below the threshold
         for `quiet_seconds` of continuous audio before the detector is
         ready. This is what makes the trigger an EDGE: arming while music
         is already playing does NOT fire instantly, and after a recording
         ends the next trigger needs a fresh quiet→signal transition.
         Runout clicks sit below the threshold, so post-side runout counts
         as quiet and the detector re-readies for the flip.
      2. *Onset*: once ready, the first chunk whose peak reaches the
         threshold fires (update() returns True) and the detector resets
         to not-ready.

    Time is accumulated from chunk byte-lengths, not the wall clock, so
    the detector is deterministic for tests.
    """

    def __init__(self, *, threshold_int: int, bytes_per_sample: int,
                 bytes_per_second: float, quiet_seconds: float = 1.0):
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
            # Signal while not ready (armed mid-music, or tail of the
            # previous take) — restart the quiet-confirm clock.
            self._quiet_accum = 0.0
            return False
        if not self.ready:
            self._quiet_accum += len(chunk) / self.bytes_per_second
            if self._quiet_accum >= self.quiet_seconds:
                self.ready = True
        return False
