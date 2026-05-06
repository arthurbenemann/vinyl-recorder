"""Single shared upstream session.

Why this exists: the Pi-side capture service only accepts ONE `/stream`
consumer at a time (new connection kicks the old). Without a server-side
fan-out, recording, playback proxy, and VU each pull `/stream` independently
and constantly evict each other. With this module there is a SINGLE ffmpeg
pulling raw PCM from the upstream URL; recording, playback, and VU all
subscribe to its byte stream, so the Pi only sees one consumer regardless
of how many tabs / sessions are running.

Also computes peak L/R every ~50 ms and tracks sticky CLIP latches per
channel — these are the inputs the WebSocket broadcaster sends to clients
in lieu of a per-client `<audio>` analyser.
"""
import json
import queue
import subprocess
import threading
from collections import deque
from typing import Callable, Optional


def probe_stream(url: str, timeout: float = 10.0) -> dict:
    """Run ffprobe against `url` and return {sample_rate, channels, codec,
    bit_depth}. Raises on failure with a user-facing message."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-i", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ffprobe failed").strip()[:300])
    info = json.loads(r.stdout or "{}")
    streams = info.get("streams", [])
    if not streams:
        raise RuntimeError("no streams reported by ffprobe")
    s = streams[0]
    bd = s.get("bits_per_sample") or 0
    return {
        "sample_rate": int(s.get("sample_rate") or 44100),
        "channels":    int(s.get("channels") or 2),
        "bit_depth":   int(bd) if bd else 16,
        "codec":       s.get("codec_name", ""),
    }


# CLIP fires when a sample is within ~0.087 dBFS of full scale, matching the
# previous client-side threshold. Server-side detection sees raw upstream
# samples (e.g. true 24-bit), so it's strictly more accurate than the old
# downsampled-proxy detector.
CLIP_THRESHOLD = 0.99
VU_FRAME_MS = 16  # ~60 Hz peak window


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
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            # Slow consumer — drop the oldest queued chunk to make room
            # for a fresher one. The reader never blocks here.
            try: self._queue.get_nowait()
            except queue.Empty: pass
            try: self._queue.put_nowait(chunk)
            except queue.Full: pass

    def close(self) -> None:
        self.alive = False
        # Closing the underlying sink unblocks a worker stuck on a write
        # to a full pipe (the next write returns BrokenPipeError). The
        # sentinel wakes a worker that's blocked on get().
        if self._on_close:
            try: self._on_close()
            except Exception: pass
        try: self._queue.put_nowait(None)
        except queue.Full:
            try: self._queue.get_nowait()
            except queue.Empty: pass
            try: self._queue.put_nowait(None)
            except queue.Full: pass

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
        sess.connect("http://pi:8000/stream")  # spawns ffmpeg, starts reader
        sub = sess.subscribe("rec-abc", lambda b: rec_proc.stdin.write(b))
        ...
        sess.unsubscribe("rec-abc")
        sess.disconnect()  # waits for reader thread to drain
    """

    def __init__(self, on_event: Optional[Callable[[dict], None]] = None,
                 preroll_seconds: int = 0):
        # Called from the reader thread for VU frames (`{type: "vu", ...}`),
        # state changes (`{type: "upstream", connected: bool, ...}`), and
        # CLIP transitions (`{type: "clip", ch: "L", clipped: bool}`). Must
        # be cheap and thread-safe — typically forwards to an asyncio queue.
        self._on_event = on_event or (lambda _: None)
        self._lock = threading.RLock()

        # Connection state.
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
        self._preroll_capacity_bytes = 0  # set in connect() once fmt known

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

    # ── public API ────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def state(self) -> dict:
        """Snapshot for `/api/status` and the WS connect-replay."""
        with self._lock:
            return {
                "connected":  self.connected,
                "url":        self.url,
                "format":     dict(self.fmt),
                "peak_l":     self.peak_l,
                "peak_r":     self.peak_r,
                "clipped_l":  self.clipped_l,
                "clipped_r":  self.clipped_r,
                "subscribers": [s.name for s in self._subscribers.values() if s.alive],
                "health":     dict(self._last_health),
            }

    def connect(self, url: str) -> dict:
        """Probe + spawn the upstream ffmpeg. Returns the detected format.
        Raises RuntimeError if probe fails or already connected."""
        with self._lock:
            if self.connected:
                raise RuntimeError("already connected")
            fmt = probe_stream(url)
            # We need a self-describing raw byte format so subscribers can
            # decode without a fresh probe. 24-bit if the source delivers it
            # (Pi default), 16-bit otherwise — small files for low-rate sources.
            sample_format = "s24le" if fmt["bit_depth"] >= 24 else "s16le"
            cmd = [
                "ffmpeg", "-loglevel", "error",
                "-fflags", "nobuffer",
                "-i", url,
                "-f", sample_format,
                "-ar", str(fmt["sample_rate"]),
                "-ac", str(fmt["channels"]),
                "-",
            ]
            self.url = url
            self.fmt = fmt
            self.sample_format = sample_format
            self._stopping = False
            self.peak_l = self.peak_r = 0.0
            # Connect resets latched clips (matches the old client behavior
            # of clearClip() in connect() — fresh session, fresh slate).
            self.clipped_l = self.clipped_r = False
            # Reset the pre-roll ring so stale bytes from a previous session
            # (potentially in a different format) never leak into a recording.
            self._preroll_chunks.clear()
            self._preroll_total_bytes = 0
            bps = 3 if sample_format == "s24le" else 2
            self._expected_bps = fmt["sample_rate"] * fmt["channels"] * bps
            self._preroll_capacity_bytes = self._expected_bps * self._preroll_seconds
            # Reset health metrics.
            import time as _time
            self._bytes_in_window = 0
            self._window_start = _time.monotonic()
            self._last_frame_ts = 0.0  # 0.0 sentinel: no frame received yet
            self._gap_count = 0
            self._gap_window.clear()
            if self._has_connected_before:
                self._reconnect_count += 1
            self._has_connected_before = True
            self._last_health = {}
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            t = threading.Thread(target=self._read_loop, name="upstream-reader",
                                 daemon=True)
            self._reader = t
            t.start()
            self._health_stop.clear()
            ht = threading.Thread(target=self._health_loop,
                                  name="upstream-health", daemon=True)
            self._health_thread = ht
            ht.start()
        self._on_event({"type": "upstream", "connected": True,
                        "url": url, "format": fmt})
        return fmt

    def disconnect(self) -> None:
        """Stop the upstream ffmpeg, drop all subscribers."""
        with self._lock:
            if not self.proc:
                return
            self._stopping = True
            proc = self.proc
            subs = list(self._subscribers.values())
        try: proc.terminate()
        except Exception: pass
        try: proc.wait(timeout=2)
        except Exception:
            try: proc.kill()
            except Exception: pass
        for s in subs:
            s.close()
        self._health_stop.set()
        with self._lock:
            self.proc = None
            self._subscribers.clear()
            self.peak_l = self.peak_r = 0.0
            self.clipped_l = self.clipped_r = False
            self._preroll_chunks.clear()
            self._preroll_total_bytes = 0
            self._last_health = {}
        self._on_event({"type": "clip", "clipped_l": False,
                        "clipped_r": False, "cleared": True})
        self._on_event({"type": "upstream", "connected": False})

    def subscribe(self, name: str, sink: Callable[[bytes], None],
                  on_close: Optional[Callable[[], None]] = None) -> _Subscriber:
        sub = _Subscriber(name, sink, on_close)
        with self._lock:
            if not self.connected:
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
            if not self.connected:
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
        import time as _time
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
                    now = _time.monotonic()
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
            pass
        finally:
            with self._lock:
                stopping = self._stopping
            if not stopping:
                # Upstream died unexpectedly (Pi reboot, network drop, etc).
                # Mark disconnected and let clients see a flat VU + state flip.
                self._on_event({
                    "type": "log",
                    "level": "err",
                    "msg": "⚠ upstream stream ended unexpectedly",
                })
                # Wrap up state without re-entering disconnect's lock dance.
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
                self._on_event({"type": "clip", "clipped_l": False,
                                "clipped_r": False, "cleared": True})
                self._on_event({"type": "upstream", "connected": False})

    # ── health ticker ─────────────────────────────────────────────────────
    def _health_loop(self) -> None:
        """Emit a `health` event every ~500 ms with stream-quality stats.

        Levels:
          green  — bytes/sec ≥ 80% of expected, no recent gaps.
          yellow — bytes/sec 50-80%, or one recent gap (last 5 s).
          red    — no bytes for >2 s, or upstream not connected.
        """
        import time as _time
        TICK = 0.5
        while not self._health_stop.wait(TICK):
            now = _time.monotonic()
            with self._lock:
                if not self.connected:
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
        """Compute peak L/R from one frame of interleaved PCM. Pure-Python
        loop — at 50 ms × 96 kHz × 2 ch this is ~9 600 samples, ~3 ms on a
        Pi 4. Kept simple: no numpy dependency."""
        if channels < 2:
            # Mono: feed the same value to both meters.
            channels_to_scan = 1
        else:
            channels_to_scan = 2
        n_pairs = len(chunk) // (bps * channels)
        max_l = 0
        max_r = 0
        if bps == 3:
            full_scale = 0x7FFFFF
            for i in range(n_pairs):
                off = i * channels * 3
                # Left channel
                v = chunk[off] | (chunk[off+1] << 8) | (chunk[off+2] << 16)
                if v >= 0x800000: v -= 0x1000000
                if v < 0: v = -v
                if v > max_l: max_l = v
                if channels_to_scan == 2:
                    off2 = off + 3
                    v = chunk[off2] | (chunk[off2+1] << 8) | (chunk[off2+2] << 16)
                    if v >= 0x800000: v -= 0x1000000
                    if v < 0: v = -v
                    if v > max_r: max_r = v
        else:  # s16le
            full_scale = 0x7FFF
            for i in range(n_pairs):
                off = i * channels * 2
                v = chunk[off] | (chunk[off+1] << 8)
                if v >= 0x8000: v -= 0x10000
                if v < 0: v = -v
                if v > max_l: max_l = v
                if channels_to_scan == 2:
                    off2 = off + 2
                    v = chunk[off2] | (chunk[off2+1] << 8)
                    if v >= 0x8000: v -= 0x10000
                    if v < 0: v = -v
                    if v > max_r: max_r = v
        peak_l = max_l / full_scale
        peak_r = (max_r / full_scale) if channels_to_scan == 2 else peak_l

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
