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

Lifecycle (configured vs live)
------------------------------
The session has two distinct booleans:

    configured  — user has set up a stream URL.  Survives ffmpeg teardown.
    live        — ffmpeg subprocess is currently alive (the old "connected").

The Pi consumes power whenever ffmpeg pulls /stream (arecord runs as a
side-effect on the Pi). To make idle CPU ~0% when nobody is watching,
the ffmpeg subprocess is now demand-driven: holders ref-count the desire
for a live stream. When the count drops to zero we tear ffmpeg down after
a short grace period; when it rises again we respawn. `configured` stays
true across these cycles so the UI keeps showing "connected" — the only
lie that matters for the user is "is the session set up", not "is a
subprocess running this exact millisecond".

Holders are owned by:
  - each WS client whose tab is visible
  - each active recording session (kept alive across tab close)
  - each active playback proxy response

The existing `connected` API is preserved as a backwards-compat alias for
`live` so code that hasn't migrated still works.
"""
import json
import logging
import os
import queue
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
import warnings
from collections import deque
from typing import Callable, Optional

# `audioop` is a stdlib C module that decodes PCM samples in one call —
# orders of magnitude faster than per-sample Python on the VU hot path
# (called every ~16 ms). It's deprecated in 3.12 (silenced here) and
# slated for removal in 3.13; the project targets 3.12 (see Dockerfile)
# so the replacement story (`audioop-lts` or numpy) only matters when we
# bump the runtime.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning,
                            message=r".*audioop.*")
    import audioop


_log = logging.getLogger(__name__)


# Idle lifecycle tuning. The grace gives a "next acquire arrives in a
# moment" pattern (e.g. tab refresh dropping then re-establishing the WS)
# room to skip the spawn entirely. The min-uptime guard prevents a flap
# loop if a lone holder rapidly acquire/release/acquires (e.g. a browser
# rapidly toggling visibility) — we never tear down before a grace-from-
# spawn so a fresh ffmpeg gets a chance to do useful work.
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


UPSTREAM_IDLE_GRACE_SECONDS = _env_float("UPSTREAM_IDLE_GRACE_SECONDS", 10.0)
UPSTREAM_MIN_UPTIME_SECONDS = _env_float("UPSTREAM_MIN_UPTIME_SECONDS", 3.0)


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


def _probe_via_pi_info(url: str, timeout: float = 2.0) -> dict:
    """Probe a Pi-recorder-style upstream by hitting its `/info` endpoint.

    Strips the path off `url` and asks for `<base>/info`; returns the
    same fmt dict shape as `probe_stream`. Much cheaper than spawning
    ffprobe (which itself opens a /stream connection on the Pi, kicking
    any in-flight consumer for a second). Caller is responsible for
    falling back to ffprobe on any failure here — we raise a plain
    RuntimeError (or let urllib's own errors propagate) so the fallback
    site can wrap with a single except.
    """
    # Build base = scheme://host[:port]. Drop path/query/fragment.
    from urllib.parse import urlparse, urlunparse
    parts = urlparse(url)
    if not parts.scheme or not parts.netloc:
        raise RuntimeError("not an http(s) URL")
    base = urlunparse((parts.scheme, parts.netloc, "", "", "", ""))
    info_url = base + "/info"
    req = urllib.request.Request(info_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if getattr(resp, "status", 200) != 200:
            raise RuntimeError(f"/info returned HTTP {resp.status}")
        body = resp.read()
    info = json.loads(body)
    sample_rate = int(info["sample_rate"])
    channels    = int(info["channels"])
    bit_depth   = int(info["bit_depth"])
    # The Pi serves raw PCM little-endian; map bit depth → pcm_s{NN}le for
    # parity with what ffprobe would have returned (downstream consumers
    # only inspect codec for logging, but stay consistent).
    codec = "pcm_s24le" if bit_depth >= 24 else "pcm_s16le"
    return {
        "sample_rate": sample_rate,
        "channels":    channels,
        "bit_depth":   bit_depth,
        "codec":       codec,
    }


def _probe_format(url: str) -> dict:
    """Probe the upstream format. Tries the Pi's /info endpoint first (cheap,
    ~20 ms over LAN, doesn't kick the active /stream consumer); falls back
    to ffprobe on any failure — wrong host, missing endpoint, network error,
    JSON parse, missing keys, anything. Logs which path produced the result
    at debug level so a confused operator can grep for it."""
    try:
        fmt = _probe_via_pi_info(url)
        _log.debug("probe via /info succeeded for %s", url)
        return fmt
    except (urllib.error.URLError, OSError, RuntimeError, ValueError,
            KeyError, TypeError) as e:
        _log.debug("probe via /info failed for %s: %s — falling back to ffprobe",
                   url, e)
    # Fallback path. Let ffprobe's own RuntimeError surface to the caller
    # so the user-facing connect message stays informative.
    fmt = probe_stream(url)
    _log.debug("probe via ffprobe succeeded for %s", url)
    return fmt


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


class _HoldToken:
    """Opaque object returned by `acquire`; passed back to `release`.

    Carrying the reason on the token (rather than just being an `object()`)
    makes /api/status snapshots and debug dumps actually informative when
    investigating "why is upstream still alive?" in production.
    """
    __slots__ = ("reason", "_released")

    def __init__(self, reason: str):
        self.reason = reason
        self._released = False


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
        """Configure the upstream URL — probes the format and marks the
        session as configured. Does NOT spawn ffmpeg unless there's already
        a holder; ffmpeg comes up on the first acquire. Existing holders
        get the new url honoured on the next spawn (after a teardown)."""
        with self._lock:
            if self.configured:
                # Disallow racing reconnects with a different URL; a caller
                # that wants to switch URLs must disconnect first. Matches
                # the prior single-shot connect contract.
                raise RuntimeError("already connected")
        # Run probe outside the lock — probe_stream / urllib calls block.
        fmt = _probe_format(url)
        sample_format = "s24le" if fmt["bit_depth"] >= 24 else "s16le"
        spawn_after_set = False
        with self._lock:
            self.url = url
            self.fmt = fmt
            self.sample_format = sample_format
            # Connect resets latched clips (matches the old client behavior
            # of clearClip() in connect() — fresh session, fresh slate).
            self.clipped_l = self.clipped_r = False
            self.configured = True
            spawn_after_set = bool(self._holders) and not (
                self.proc is not None and self.proc.poll() is None)
        self._on_event({"type": "upstream", "configured": True,
                        "connected": False, "live": False,
                        "url": url, "format": fmt})
        if spawn_after_set:
            try:
                self._spawn()
            except Exception as e:
                _log.error("spawn after connect failed: %s", e)
        return fmt

    def disconnect(self) -> None:
        """Stop the upstream ffmpeg, drop all subscribers, clear
        configured. Holders themselves are NOT cleared — callers manage
        their own tokens. After disconnect, those tokens become inert
        (releasing them is a no-op since there's nothing live to schedule)."""
        with self._lock:
            had_proc = self.proc is not None
            self.configured = False
            self._cancel_grace_locked()
        if had_proc:
            self._teardown(force=True)
        else:
            # Even with no live ffmpeg, surface the state flip so clients
            # see configured drop to false.
            with self._lock:
                self.url = None
                self.fmt = {}
                self.sample_format = ""
            self._on_event({"type": "upstream", "configured": False,
                            "connected": False, "live": False})

    # ── holder ref-count ──────────────────────────────────────────────────
    def acquire(self, reason: str) -> _HoldToken:
        """Bump the holder count. If this is the 0→1 transition AND we're
        configured, spawn ffmpeg synchronously (~400 ms). If a grace timer
        was scheduled, cancel it (no teardown needed — ffmpeg stays alive).

        Returns an opaque token that must eventually be passed to release()."""
        token = _HoldToken(reason)
        with self._lock:
            had_grace = self._grace_timer is not None
            self._holders[token] = reason
            self._cancel_grace_locked()
            # `_stopping` is True from the brief window where _teardown
            # has dropped the lock to terminate ffmpeg but hasn't yet
            # cleared `self.proc`. Treat that as "not live" so we drive
            # a fresh spawn instead of subscribing to a dying process.
            live_for_acquire = (self.proc is not None
                                and self.proc.poll() is None
                                and not self._stopping)
            need_spawn = (self.configured
                          and not had_grace
                          and not live_for_acquire)
        if need_spawn:
            try:
                self._spawn()
            except Exception:
                # Spawn failure: drop the hold so a future retry can try
                # again, and re-raise so the caller sees the error rather
                # than discovering it later via subscribe() failing.
                with self._lock:
                    self._holders.pop(token, None)
                    token._released = True
                raise
        return token

    def release(self, token: _HoldToken) -> None:
        """Drop a holder. Idempotent. When the count hits zero AND ffmpeg
        is live, schedule a grace teardown (deadline = max(now+grace,
        spawn_time+min_uptime))."""
        if token is None or token._released:
            return
        with self._lock:
            if self._holders.pop(token, None) is None:
                return
            token._released = True
            still_holders = bool(self._holders)
            live = self.proc is not None and self.proc.poll() is None
            if not still_holders and live:
                self._schedule_grace_teardown_locked()

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

    # ── lifecycle internals ───────────────────────────────────────────────
    def _spawn(self) -> None:
        """Bring ffmpeg up. Re-probes the format (the design treats every
        spawn as fresh — ~20 ms over LAN, removes the "stale fmt" failure
        mode entirely, keeps the spawn path stateless). Resets all health
        + preroll state so a warm reconnect looks like a fresh start."""
        # Wait out any in-flight teardown so we don't spawn a second ffmpeg
        # alongside the dying one. _teardown briefly drops the lock to
        # terminate the child; the locked phase that follows clears
        # `proc` and resets `_stopping`, after which we can safely spawn.
        # Polling here is bounded by `proc.wait(timeout=2)` inside teardown
        # plus a kill, so 5 s is comfortably above the worst case.
        deadline = time.monotonic() + 5.0
        while True:
            with self._lock:
                if not (self._stopping and self.proc is not None):
                    break
            if time.monotonic() > deadline:
                raise RuntimeError("teardown did not finish within 5s")
            time.sleep(0.005)
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            url = self.url
            if not url or not self.configured:
                raise RuntimeError("cannot spawn without a configured URL")
        # Probe outside the lock — network call, must not block other ops.
        fmt = _probe_format(url)
        sample_format = "s24le" if fmt["bit_depth"] >= 24 else "s16le"
        # The probe ran without the lock; if disconnect() raced in and
        # cleared `configured`, abort rather than spawning a doomed
        # subprocess against a stale URL. Holders that are still around
        # will see live=false and either re-acquire (driving a fresh
        # _spawn) or stay idle.
        with self._lock:
            if not self.configured or self.url != url:
                return
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-fflags", "nobuffer",
            "-i", url,
            "-f", sample_format,
            "-ar", str(fmt["sample_rate"]),
            "-ac", str(fmt["channels"]),
            "-",
        ]
        with self._lock:
            self.fmt = fmt
            self.sample_format = sample_format
            self._stopping = False
            self.peak_l = self.peak_r = 0.0
            # Reset the pre-roll ring so stale bytes from a previous spawn
            # (potentially in a different format) never leak into a recording.
            self._preroll_chunks.clear()
            self._preroll_total_bytes = 0
            bps = 3 if sample_format == "s24le" else 2
            self._expected_bps = fmt["sample_rate"] * fmt["channels"] * bps
            self._preroll_capacity_bytes = self._expected_bps * self._preroll_seconds
            # Reset health metrics.
            self._bytes_in_window = 0
            self._window_start = time.monotonic()
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
            self._spawn_time = time.monotonic()
            t = threading.Thread(target=self._read_loop, name="upstream-reader",
                                 daemon=True)
            self._reader = t
            t.start()
            self._health_stop.clear()
            ht = threading.Thread(target=self._health_loop,
                                  name="upstream-health", daemon=True)
            self._health_thread = ht
            ht.start()
        self._on_event({"type": "upstream", "configured": True,
                        "connected": True, "live": True,
                        "url": url, "format": fmt})

    def _schedule_grace_teardown_locked(self) -> None:
        """Schedule _teardown to run after the grace expires (or after the
        min-uptime guard, whichever is later). Caller must hold self._lock.

        The timer fires off-lock so a racing acquire on another thread
        isn't blocked behind our teardown."""
        # Cancel any prior pending timer first; only one in flight at a time.
        if self._grace_timer is not None:
            try: self._grace_timer.cancel()
            except Exception: pass
            self._grace_timer = None
        now = time.monotonic()
        deadline = max(now + self._grace_seconds,
                       self._spawn_time + self._min_uptime)
        delay = max(0.0, deadline - now)
        timer = threading.Timer(delay, self._on_grace_expired)
        timer.daemon = True
        self._grace_timer = timer
        timer.start()

    def _cancel_grace_locked(self) -> None:
        if self._grace_timer is not None:
            try: self._grace_timer.cancel()
            except Exception: pass
            self._grace_timer = None

    def _on_grace_expired(self) -> None:
        """Timer callback. May race against an acquire that just bumped the
        holder count back to nonzero — we re-check under the lock and bail
        out if so (the cancel might have been too late)."""
        with self._lock:
            self._grace_timer = None
            if self._holders:
                return  # raced — a holder slipped in after the timer fired
            if not (self.proc is not None and self.proc.poll() is None):
                return  # already torn down by some other path
        self._teardown(force=False)

    def _teardown(self, force: bool = False) -> None:
        """Stop ffmpeg + drop all subscribers, leaving configured untouched.

        `force=True` is for `disconnect()` (user wants it dead, holders
        be damned). `force=False` is the grace path; we double-check that
        there are still no holders before pulling the plug."""
        with self._lock:
            if not force and self._holders:
                return
            if self.proc is None:
                # Nothing to tear down. Still emit the state event when
                # forced so disconnect() observers see configured=false.
                proc = None
                subs: list[_Subscriber] = []
            else:
                self._stopping = True
                proc = self.proc
                subs = list(self._subscribers.values())
        if proc is not None:
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
            self._reader = None
            self._subscribers.clear()
            self.peak_l = self.peak_r = 0.0
            self.clipped_l = self.clipped_r = False
            self._preroll_chunks.clear()
            self._preroll_total_bytes = 0
            self._last_health = {}
            configured = self.configured
            url = self.url if configured else None
            fmt = dict(self.fmt) if configured else {}
            if not configured:
                self.url = None
                self.fmt = {}
                self.sample_format = ""
        self._on_event({"type": "clip", "clipped_l": False,
                        "clipped_r": False, "cleared": True})
        self._on_event({"type": "upstream", "configured": configured,
                        "connected": False, "live": False,
                        "url": url, "format": fmt})

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
