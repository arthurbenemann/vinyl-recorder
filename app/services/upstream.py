"""Single shared upstream session: subscriber fan-out + VU/CLIP analysis.

The lifecycle (holders, idle grace, spawn/teardown) lives in
`services.upstream_lifecycle`; stream-format probing lives in
`services.stream_probe`. Public symbols from those siblings are re-exported
here so external callers can keep `from services.upstream import ...` as-is.

See Architecture.md § Upstream session lifecycle for design rationale.
"""
import logging
import queue
import subprocess  # noqa: F401 (kept for tests that monkeypatch up_mod.subprocess)
import threading
import time
import traceback
import urllib.error  # noqa: F401 (kept for tests that monkeypatch up_mod.urllib)
import urllib.request  # noqa: F401 (kept for tests that monkeypatch up_mod.urllib)
from collections import deque
from typing import Callable, Optional

# `audioop` is a C module that decodes PCM samples in one call — orders of
# magnitude faster than per-sample Python on the VU hot path (called every
# ~16 ms). It was a stdlib module deprecated in 3.12 and removed in 3.13;
# on 3.14 (our current runtime) we depend on `audioop-lts`, a drop-in
# replacement that re-exposes the same C API under the `audioop` name.
import audioop

# Re-exports — keep external imports (`from services.upstream import ...`)
# working unchanged after the split. The flake8 noqa is because these are
# imported solely to be re-exposed at module scope.
from services.stream_probe import (  # noqa: F401
    _probe_format, _probe_via_pi_info, probe_stream,
)
from services.upstream_lifecycle import (  # noqa: F401
    UPSTREAM_IDLE_GRACE_SECONDS, UPSTREAM_MIN_UPTIME_SECONDS,
    _env_float, _HoldToken,
    acquire_hold, cancel_grace_locked, connect_session, disconnect_session,
    on_grace_expired, release_hold, schedule_grace_teardown_locked,
    spawn_ffmpeg, teardown_ffmpeg,
)


_log = logging.getLogger(__name__)


# CLIP fires when a sample is within ~0.087 dBFS of full scale, matching the
# previous client-side threshold. Server-side detection sees raw upstream
# samples (e.g. true 24-bit), so it's strictly more accurate than the old
# downsampled-proxy detector.
CLIP_THRESHOLD = 0.99
VU_FRAME_MS = 16  # ~60 Hz peak window


def _drop_oldest_put(q: "queue.Queue", item) -> None:
    """Bounded queue best-effort put. On `Full`, drop the oldest queued
    item to make room for `item` and try once more — if that still fails
    (another producer raced and refilled), give up rather than spin. The
    reader thread never blocks here; a permanently-stalled subscriber
    converges on dropping its freshest chunks within a second."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


class _Subscriber:
    """A single byte consumer attached to the upstream session.

    Each subscriber owns a bounded queue and a worker thread. The reader
    thread does `put_nowait` (never blocks); the worker drains the queue
    into the sink at whatever pace the sink can take. If the sink stalls
    (e.g. a paused <audio> stops reading the proxy response, ffmpeg's
    output pipe fills, ffmpeg stops draining its stdin), the queue fills
    and we drop the oldest chunk on every new put — better a brief audio
    glitch than head-of-line blocking that wedges every other consumer.
    """
    # ~1 s of audio at 60 Hz frames; leaves room to absorb a short stall
    # without dropping but small enough that a permanently-stalled sink
    # converges on dropping fresh chunks within a second.
    _QUEUE_MAX = 64

    def __init__(self, name: str, sink: Callable[[bytes], None],
                 on_close: Optional[Callable[[], None]] = None):
        self.name = name
        self._sink = sink
        self._on_close = on_close
        self.alive = True
        self._queue: queue.Queue = queue.Queue(maxsize=self._QUEUE_MAX)
        self._thread = threading.Thread(target=self._drain, daemon=True,
                                        name=f"sub-{name}")
        self._thread.start()

    def write(self, chunk: bytes) -> None:
        if not self.alive:
            return
        # Slow consumer — `_drop_oldest_put` ejects the oldest queued
        # chunk to make room for a fresher one. The reader never blocks.
        _drop_oldest_put(self._queue, chunk)

    def close(self) -> None:
        self.alive = False
        # Closing the underlying sink unblocks a worker stuck on a write
        # to a full pipe (the next write returns BrokenPipeError). The
        # sentinel wakes a worker that's blocked on get().
        if self._on_close:
            try: self._on_close()
            except Exception: pass
        # Sentinel must reach the worker even if the queue is currently
        # full — `_drop_oldest_put` ejects an old chunk to make room.
        _drop_oldest_put(self._queue, None)

    def _drain(self) -> None:
        while True:
            try:
                chunk = self._queue.get(timeout=2.0)
            except queue.Empty:
                if not self.alive:
                    return
                continue
            if chunk is None:
                return
            if not self.alive:
                return
            try:
                self._sink(chunk)
            except (BrokenPipeError, OSError, ValueError):
                self.alive = False
                return


class UpstreamSession:
    """One ffmpeg pulling raw PCM from upstream; fan-out to N subscribers.

    Lifecycle:
        sess = UpstreamSession(on_event=cb)
        sess.connect("http://pi:8000/stream")  # probes; sets configured=true
        token = sess.acquire("ws:42")          # bumps ffmpeg up if needed
        sub = sess.subscribe("rec-abc", lambda b: rec_proc.stdin.write(b))
        ...
        sess.unsubscribe("rec-abc")
        sess.release(token)                    # may schedule grace teardown
        sess.disconnect()                      # tears ffmpeg down, clears configured
    """

    def __init__(self, on_event: Optional[Callable[[dict], None]] = None,
                 preroll_seconds: int = 0,
                 idle_grace_seconds: Optional[float] = None,
                 min_uptime_seconds: Optional[float] = None):
        # Called from the reader thread for VU frames (`{type: "vu", ...}`),
        # state changes (`{type: "upstream", live: bool, ...}`), and CLIP
        # transitions (`{type: "clip", ch: "L", clipped: bool}`). Must be
        # cheap and thread-safe — typically forwards to an asyncio queue.
        self._on_event = on_event or (lambda _: None)
        self._lock = threading.RLock()

        # Configured = user wants this URL up; survives ffmpeg lifecycle
        # cycles. Live = ffmpeg subprocess is currently alive.
        self.configured: bool = False
        self.url: Optional[str] = None
        self.fmt: dict = {}            # {sample_rate, channels, bit_depth, codec}
        self.sample_format: str = ""   # "s24le" or "s16le"
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stopping = False

        # Subscribers (recording, playback proxies). VU is computed inline
        # by the reader thread — not a subscriber — so it can never lag.
        self._subscribers: dict[str, _Subscriber] = {}

        # VU + CLIP state. Peaks are normalized 0..1.
        self.peak_l = 0.0
        self.peak_r = 0.0
        self.clipped_l = False
        self.clipped_r = False

        # Pre-roll ring buffer. Holds the last N seconds of raw PCM so a
        # recording started "now" can be prepended with audio captured
        # before the user clicked Record — never miss the first groove.
        self._preroll_seconds = max(0, int(preroll_seconds))
        self._preroll_chunks: deque[bytes] = deque()
        self._preroll_total_bytes = 0
        self._preroll_capacity_bytes = 0  # set once fmt known (in _spawn)

        # Stream-health metrics. Updated by the reader thread, read from a
        # background ticker that emits a `health` event every ~500 ms.
        self._bytes_in_window = 0
        self._window_start = 0.0
        self._last_frame_ts = 0.0
        self._gap_count = 0
        self._gap_window: deque[float] = deque()  # gap event timestamps, last 5s
        self._reconnect_count = 0
        self._expected_bps = 0
        self._health_thread: Optional[threading.Thread] = None
        self._health_stop = threading.Event()
        self._last_health: dict = {}
        self._has_connected_before = False

        # Holders (ref-count). Token identity is the dict key so the same
        # reason can appear multiple times without collisions.
        self._holders: dict[_HoldToken, str] = {}
        self._grace_timer: Optional[threading.Timer] = None
        self._spawn_time: float = 0.0
        self._grace_seconds = (idle_grace_seconds
                               if idle_grace_seconds is not None
                               else UPSTREAM_IDLE_GRACE_SECONDS)
        self._min_uptime = (min_uptime_seconds
                            if min_uptime_seconds is not None
                            else UPSTREAM_MIN_UPTIME_SECONDS)

    # ── public API ────────────────────────────────────────────────────────
    @property
    def live(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    @property
    def connected(self) -> bool:
        """Backwards-compat alias for `live`. Existing callers (route
        handlers checking "is upstream up?", state snapshots, metrics)
        keep working unchanged."""
        return self.live

    def state(self) -> dict:
        """Snapshot for `/api/status` and the WS connect-replay."""
        with self._lock:
            live = self.proc is not None and self.proc.poll() is None
            return {
                # `connected` retained for backwards-compat with existing
                # WS clients. Equivalent to `live`. Frontend should prefer
                # `configured` for the persistent UI affordance.
                "connected":  live,
                "live":       live,
                "configured": self.configured,
                "url":        self.url,
                "format":     dict(self.fmt),
                "peak_l":     self.peak_l,
                "peak_r":     self.peak_r,
                "clipped_l":  self.clipped_l,
                "clipped_r":  self.clipped_r,
                "subscribers": [s.name for s in self._subscribers.values() if s.alive],
                "holders":    [t.reason for t in self._holders],
                "health":     dict(self._last_health),
            }

    def connect(self, url: str) -> dict:
        return connect_session(self, url)

    def disconnect(self) -> None:
        disconnect_session(self)

    # ── holder ref-count ──────────────────────────────────────────────────
    def acquire(self, reason: str) -> _HoldToken:
        return acquire_hold(self, reason)

    def release(self, token: _HoldToken) -> None:
        release_hold(self, token)

    # ── subscribe (live-only) ─────────────────────────────────────────────
    def subscribe(self, name: str, sink: Callable[[bytes], None],
                  on_close: Optional[Callable[[], None]] = None) -> _Subscriber:
        """Register a byte-consumer subscriber. REQUIRES the session to
        already be live — callers should `acquire()` first to ensure ffmpeg
        is up. Raises RuntimeError if not live, mirroring the prior
        contract."""
        sub = _Subscriber(name, sink, on_close)
        with self._lock:
            if not (self.proc is not None and self.proc.poll() is None):
                sub.close()
                raise RuntimeError("upstream not connected")
            self._subscribers[name] = sub
        return sub

    def subscribe_with_preroll(
        self, name: str, sink: Callable[[bytes], None],
        on_close: Optional[Callable[[], None]] = None,
    ) -> tuple[_Subscriber, bytes]:
        """Atomically register a subscriber AND snapshot the pre-roll ring.

        Returns (sub, preroll_bytes). The caller is responsible for writing
        `preroll_bytes` to its sink target BEFORE letting live chunks flow.
        Concretely: gate the live `sink` on a threading.Event that is set
        only after the preroll has been written. The atomic snapshot+add
        guarantees no upstream byte is lost or duplicated at the seam."""
        sub = _Subscriber(name, sink, on_close)
        with self._lock:
            if not (self.proc is not None and self.proc.poll() is None):
                sub.close()
                raise RuntimeError("upstream not connected")
            snapshot = b"".join(self._preroll_chunks)
            self._subscribers[name] = sub
        return sub, snapshot

    def unsubscribe(self, name: str) -> None:
        with self._lock:
            sub = self._subscribers.pop(name, None)
        if sub:
            sub.close()

    def clear_clip(self, ch: Optional[str] = None) -> None:
        """User-acknowledge clip latches. ch in {"L","R",None}; None clears both."""
        with self._lock:
            if ch in (None, "L"): self.clipped_l = False
            if ch in (None, "R"): self.clipped_r = False
        self._on_event({"type": "clip",
                        "clipped_l": self.clipped_l,
                        "clipped_r": self.clipped_r,
                        "cleared":   True})

    # ── lifecycle internals (delegated to services.upstream_lifecycle) ───
    def _spawn(self) -> None:
        spawn_ffmpeg(self)

    def _schedule_grace_teardown_locked(self) -> None:
        schedule_grace_teardown_locked(self)

    def _cancel_grace_locked(self) -> None:
        cancel_grace_locked(self)

    def _on_grace_expired(self) -> None:
        on_grace_expired(self)

    def _teardown(self, force: bool = False) -> None:
        teardown_ffmpeg(self, force=force)

    # ── reader thread ─────────────────────────────────────────────────────
    def _read_loop(self) -> None:
        """Pump bytes from ffmpeg stdout to subscribers + compute peaks.

        VU is computed inline so a stuck subscriber can never starve the
        meter. Subscribers with broken pipes are dropped silently."""
        proc = self.proc
        assert proc and proc.stdout
        bytes_per_sample = 3 if self.sample_format == "s24le" else 2
        channels = self.fmt["channels"]
        rate = self.fmt["sample_rate"]
        # Frame size aligned to a whole number of sample-pairs so every read
        # ends on a sample boundary — saves us tracking partial samples.
        samples_per_frame = max(1, int(rate * VU_FRAME_MS / 1000))
        frame_bytes = samples_per_frame * channels * bytes_per_sample
        try:
            while True:
                chunk = proc.stdout.read(frame_bytes)
                if not chunk:
                    break
                # Snapshot the subscribers dict under the lock, but do the
                # actual writes OUTSIDE the lock. Holding the lock during a
                # blocking write into a stalled subscriber's pipe used to
                # deadlock the whole session: unsubscribe (which is what
                # would unblock the write by closing the pipe) couldn't run
                # because it needs the same lock.
                with self._lock:
                    if self._stopping:
                        break
                    # Append to the pre-roll ring under the lock so
                    # subscribe_with_preroll's snapshot is consistent with
                    # the subscriber registration that follows it.
                    if self._preroll_capacity_bytes > 0:
                        self._preroll_chunks.append(chunk)
                        self._preroll_total_bytes += len(chunk)
                        while (self._preroll_total_bytes >
                               self._preroll_capacity_bytes
                               and self._preroll_chunks):
                            popped = self._preroll_chunks.popleft()
                            self._preroll_total_bytes -= len(popped)
                    # Health metrics — update the rolling-byte counter and
                    # detect stalls. Done under the lock so the health
                    # ticker reads a consistent snapshot.
                    now = time.monotonic()
                    if self._last_frame_ts and (now - self._last_frame_ts) > 0.5:
                        self._gap_count += 1
                        self._gap_window.append(now)
                    self._last_frame_ts = now
                    self._bytes_in_window += len(chunk)
                    subs = list(self._subscribers.values())
                dead = []
                for sub in subs:
                    sub.write(chunk)
                    if not sub.alive:
                        dead.append(sub.name)
                if dead:
                    with self._lock:
                        for name in dead:
                            self._subscribers.pop(name, None)
                self._update_peaks(chunk, bytes_per_sample, channels)
        except Exception:
            # Don't swallow silently — the reader thread is the heart of the
            # whole audio path, and a crash here mimics an upstream EOF (the
            # finally block flips us to "not live"). Log so postmortem
            # has something to chase.
            _log.error("upstream reader crashed: %s",
                       traceback.format_exc())
        finally:
            with self._lock:
                stopping = self._stopping
            if not stopping:
                # Upstream died unexpectedly (Pi reboot, network drop, etc).
                # Mark not-live and let clients see a flat VU + state flip.
                # Configured stays — the user's intent for the URL hasn't
                # changed and a fresh acquire (or a watchful WS client) can
                # re-spawn. Existing subscribers (e.g. a recording) get
                # broken pipes via close() and clean themselves up.
                self._on_event({
                    "type": "log",
                    "level": "err",
                    "msg": "⚠ upstream stream ended unexpectedly",
                })
                self._health_stop.set()
                with self._lock:
                    self.proc = None
                    self.peak_l = self.peak_r = 0.0
                    self.clipped_l = self.clipped_r = False
                    for s in self._subscribers.values():
                        s.close()
                    self._subscribers.clear()
                    self._preroll_chunks.clear()
                    self._preroll_total_bytes = 0
                    self._last_health = {}
                    configured = self.configured
                    url = self.url if configured else None
                    fmt = dict(self.fmt) if configured else {}
                self._on_event({"type": "clip", "clipped_l": False,
                                "clipped_r": False, "cleared": True})
                self._on_event({"type": "upstream", "configured": configured,
                                "connected": False, "live": False,
                                "url": url, "format": fmt})

    # ── health ticker ─────────────────────────────────────────────────────
    def _health_loop(self) -> None:
        """Emit a `health` event every ~500 ms with stream-quality stats.

        Levels:
          green  — bytes/sec ≥ 80% of expected, no recent gaps.
          yellow — bytes/sec 50-80%, or one recent gap (last 5 s).
          red    — no bytes for >2 s, or upstream not connected.
        """
        TICK = 0.5
        while not self._health_stop.wait(TICK):
            now = time.monotonic()
            with self._lock:
                live = self.proc is not None and self.proc.poll() is None
                if not live:
                    return
                window = max(0.001, now - self._window_start)
                bps = int(self._bytes_in_window / window)
                self._bytes_in_window = 0
                self._window_start = now
                # Trim gap window to the last 5 s.
                while self._gap_window and (now - self._gap_window[0]) > 5.0:
                    self._gap_window.popleft()
                recent_gaps = len(self._gap_window)
                ms_since = int((now - (self._last_frame_ts or self._window_start)) * 1000)
                expected = self._expected_bps
                reconnects = self._reconnect_count
                gap_total = self._gap_count
            if ms_since > 2000 or expected == 0:
                level = "red"
            elif (expected and bps < expected * 0.5) or recent_gaps >= 2:
                level = "yellow"
            elif (expected and bps < expected * 0.8) or recent_gaps == 1:
                level = "yellow"
            else:
                level = "green"
            evt = {
                "type":               "health",
                "bytes_per_sec":      bps,
                "expected_bps":       expected,
                "gap_count":          gap_total,
                "gap_count_recent":   recent_gaps,
                "reconnect_count":    reconnects,
                "ms_since_last_frame": ms_since,
                "level":              level,
            }
            with self._lock:
                self._last_health = {k: v for k, v in evt.items() if k != "type"}
            self._on_event(evt)

    def _update_peaks(self, chunk: bytes, bps: int, channels: int) -> None:
        """Compute peak L/R from one frame of interleaved PCM. Delegates the
        per-sample work to `audioop`'s C-level `tomono` (deinterleave one
        channel) + `max` (absolute-max of all samples), so the only Python
        cost per frame is two function calls + a divide. Replaces a
        per-sample padding+sign-extend loop that dominated the VU thread
        on Pi-class CPUs at 96 kHz / 24-bit / stereo."""
        channels_to_scan = 1 if channels < 2 else 2
        n_pairs = len(chunk) // (bps * channels)
        if n_pairs <= 0:
            peak_l = peak_r = 0.0
        elif bps in (2, 3):
            full_scale = 0x7FFF if bps == 2 else 0x7FFFFF
            data = chunk[:n_pairs * channels * bps]
            if channels == 1:
                m = audioop.max(data, bps)
                max_l = max_r = m
            elif channels == 2:
                # tomono(buf, width, lfactor, rfactor) returns one channel
                # as a contiguous mono buffer; (1, 0) keeps L, (0, 1) keeps R.
                max_l = audioop.max(audioop.tomono(data, bps, 1, 0), bps)
                if channels_to_scan == 2:
                    max_r = audioop.max(audioop.tomono(data, bps, 0, 1), bps)
                else:
                    max_r = max_l
            else:
                # >2 channels: scale all to a single mono mix for the L
                # value, fall back to mirroring on R. Should not happen with
                # the current capture path (always mono or stereo) but keeps
                # the contract intact.
                m = audioop.max(data, bps)
                max_l = max_r = m
            peak_l = max_l / full_scale
            peak_r = max_r / full_scale
        else:
            peak_l = peak_r = 0.0

        with self._lock:
            self.peak_l = peak_l
            self.peak_r = peak_r
            new_clip_l = self.clipped_l or (peak_l >= CLIP_THRESHOLD)
            new_clip_r = self.clipped_r or (peak_r >= CLIP_THRESHOLD)
            clip_l_rose = new_clip_l and not self.clipped_l
            clip_r_rose = new_clip_r and not self.clipped_r
            self.clipped_l = new_clip_l
            self.clipped_r = new_clip_r

        # Emit a VU frame. The broadcaster downstream coalesces if it falls
        # behind — we don't try to skip emits here.
        self._on_event({
            "type":      "vu",
            "peak_l":    peak_l,
            "peak_r":    peak_r,
            "clipped_l": new_clip_l,
            "clipped_r": new_clip_r,
        })
        if clip_l_rose:
            self._on_event({"type": "log", "level": "err",
                            "msg": "⚠ CLIP on L — reduce ADC gain"})
        if clip_r_rose:
            self._on_event({"type": "log", "level": "err",
                            "msg": "⚠ CLIP on R — reduce ADC gain"})
