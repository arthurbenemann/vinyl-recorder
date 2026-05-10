"""Demand-driven lifecycle for the shared upstream ffmpeg subprocess.

Holders ref-count the desire for a live stream; the helpers here own
spawn/teardown, the grace timer, and the connect/disconnect transitions.
`UpstreamSession` (in services/upstream.py) calls these via thin shim
methods so the lock discipline and event ordering match the original.
"""
import logging
import os
import subprocess
import threading
import time

from services import stream_probe

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


# ── connect / disconnect ────────────────────────────────────────────────
def connect_session(sess, url: str) -> dict:
    """Configure the upstream URL — probes the format and marks the
    session as configured. Does NOT spawn ffmpeg unless there's already
    a holder; ffmpeg comes up on the first acquire. Existing holders
    get the new url honoured on the next spawn (after a teardown)."""
    with sess._lock:
        if sess.configured:
            # Disallow racing reconnects with a different URL; a caller
            # that wants to switch URLs must disconnect first. Matches
            # the prior single-shot connect contract.
            raise RuntimeError("already connected")
    # Run probe outside the lock — probe_stream / urllib calls block.
    fmt = stream_probe._probe_format(url)
    sample_format = "s24le" if fmt["bit_depth"] >= 24 else "s16le"
    spawn_after_set = False
    with sess._lock:
        sess.url = url
        sess.fmt = fmt
        sess.sample_format = sample_format
        # Connect resets latched clips (matches the old client behavior
        # of clearClip() in connect() — fresh session, fresh slate).
        sess.clipped_l = sess.clipped_r = False
        sess.configured = True
        spawn_after_set = bool(sess._holders) and not (
            sess.proc is not None and sess.proc.poll() is None)
    sess._on_event({"type": "upstream", "configured": True,
                    "connected": False, "live": False,
                    "url": url, "format": fmt})
    if spawn_after_set:
        try:
            sess._spawn()
        except Exception as e:
            _log.error("spawn after connect failed: %s", e)
    return fmt


def disconnect_session(sess) -> None:
    """Stop the upstream ffmpeg, drop all subscribers, clear
    configured. Holders themselves are NOT cleared — callers manage
    their own tokens. After disconnect, those tokens become inert
    (releasing them is a no-op since there's nothing live to schedule)."""
    with sess._lock:
        had_proc = sess.proc is not None
        sess.configured = False
        cancel_grace_locked(sess)
    if had_proc:
        sess._teardown(force=True)
    else:
        # Even with no live ffmpeg, surface the state flip so clients
        # see configured drop to false.
        with sess._lock:
            sess.url = None
            sess.fmt = {}
            sess.sample_format = ""
        sess._on_event({"type": "upstream", "configured": False,
                        "connected": False, "live": False})


# ── holder ref-count ────────────────────────────────────────────────────
def acquire_hold(sess, reason: str) -> _HoldToken:
    """Bump the holder count. If this is the 0→1 transition AND we're
    configured, spawn ffmpeg synchronously (~400 ms). If a grace timer
    was scheduled, cancel it (no teardown needed — ffmpeg stays alive).

    Returns an opaque token that must eventually be passed to release()."""
    token = _HoldToken(reason)
    with sess._lock:
        had_grace = sess._grace_timer is not None
        sess._holders[token] = reason
        cancel_grace_locked(sess)
        # `_stopping` is True from the brief window where _teardown
        # has dropped the lock to terminate ffmpeg but hasn't yet
        # cleared `self.proc`. Treat that as "not live" so we drive
        # a fresh spawn instead of subscribing to a dying process.
        live_for_acquire = (sess.proc is not None
                            and sess.proc.poll() is None
                            and not sess._stopping)
        need_spawn = (sess.configured
                      and not had_grace
                      and not live_for_acquire)
    if need_spawn:
        try:
            sess._spawn()
        except Exception:
            # Spawn failure: drop the hold so a future retry can try
            # again, and re-raise so the caller sees the error rather
            # than discovering it later via subscribe() failing.
            with sess._lock:
                sess._holders.pop(token, None)
                token._released = True
            raise
    return token


def release_hold(sess, token: _HoldToken) -> None:
    """Drop a holder. Idempotent. When the count hits zero AND ffmpeg
    is live, schedule a grace teardown (deadline = max(now+grace,
    spawn_time+min_uptime))."""
    if token is None or token._released:
        return
    with sess._lock:
        if sess._holders.pop(token, None) is None:
            return
        token._released = True
        still_holders = bool(sess._holders)
        live = sess.proc is not None and sess.proc.poll() is None
        if not still_holders and live:
            schedule_grace_teardown_locked(sess)


# ── grace-timer plumbing ────────────────────────────────────────────────
def schedule_grace_teardown_locked(sess) -> None:
    """Schedule _teardown to run after the grace expires (or after the
    min-uptime guard, whichever is later). Caller must hold sess._lock.

    The timer fires off-lock so a racing acquire on another thread
    isn't blocked behind our teardown."""
    # Cancel any prior pending timer first; only one in flight at a time.
    if sess._grace_timer is not None:
        try: sess._grace_timer.cancel()
        except Exception: pass
        sess._grace_timer = None
    now = time.monotonic()
    deadline = max(now + sess._grace_seconds,
                   sess._spawn_time + sess._min_uptime)
    delay = max(0.0, deadline - now)
    timer = threading.Timer(delay, sess._on_grace_expired)
    timer.daemon = True
    sess._grace_timer = timer
    timer.start()


def cancel_grace_locked(sess) -> None:
    if sess._grace_timer is not None:
        try: sess._grace_timer.cancel()
        except Exception: pass
        sess._grace_timer = None


def on_grace_expired(sess) -> None:
    """Timer callback. May race against an acquire that just bumped the
    holder count back to nonzero — we re-check under the lock and bail
    out if so (the cancel might have been too late)."""
    with sess._lock:
        sess._grace_timer = None
        if sess._holders:
            return  # raced — a holder slipped in after the timer fired
        if not (sess.proc is not None and sess.proc.poll() is None):
            return  # already torn down by some other path
    sess._teardown(force=False)


# ── spawn / teardown ────────────────────────────────────────────────────
def spawn_ffmpeg(sess) -> None:
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
        with sess._lock:
            if not (sess._stopping and sess.proc is not None):
                break
        if time.monotonic() > deadline:
            raise RuntimeError("teardown did not finish within 5s")
        time.sleep(0.005)
    with sess._lock:
        if sess.proc is not None and sess.proc.poll() is None:
            return
        url = sess.url
        if not url or not sess.configured:
            raise RuntimeError("cannot spawn without a configured URL")
    # Probe outside the lock — network call, must not block other ops.
    fmt = stream_probe._probe_format(url)
    sample_format = "s24le" if fmt["bit_depth"] >= 24 else "s16le"
    # The probe ran without the lock; if disconnect() raced in and
    # cleared `configured`, abort rather than spawning a doomed
    # subprocess against a stale URL. Holders that are still around
    # will see live=false and either re-acquire (driving a fresh
    # _spawn) or stay idle.
    with sess._lock:
        if not sess.configured or sess.url != url:
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
    with sess._lock:
        sess.fmt = fmt
        sess.sample_format = sample_format
        sess._stopping = False
        sess.peak_l = sess.peak_r = 0.0
        # Reset the pre-roll ring so stale bytes from a previous spawn
        # (potentially in a different format) never leak into a recording.
        sess._preroll_chunks.clear()
        sess._preroll_total_bytes = 0
        bps = 3 if sample_format == "s24le" else 2
        sess._expected_bps = fmt["sample_rate"] * fmt["channels"] * bps
        sess._preroll_capacity_bytes = sess._expected_bps * sess._preroll_seconds
        # Reset health metrics.
        sess._bytes_in_window = 0
        sess._window_start = time.monotonic()
        sess._last_frame_ts = 0.0  # 0.0 sentinel: no frame received yet
        sess._gap_count = 0
        sess._gap_window.clear()
        if sess._has_connected_before:
            sess._reconnect_count += 1
        sess._has_connected_before = True
        sess._last_health = {}
        sess.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        sess._spawn_time = time.monotonic()
        t = threading.Thread(target=sess._read_loop, name="upstream-reader",
                             daemon=True)
        sess._reader = t
        t.start()
        sess._health_stop.clear()
        ht = threading.Thread(target=sess._health_loop,
                              name="upstream-health", daemon=True)
        sess._health_thread = ht
        ht.start()
    sess._on_event({"type": "upstream", "configured": True,
                    "connected": True, "live": True,
                    "url": url, "format": fmt})


def teardown_ffmpeg(sess, force: bool = False) -> None:
    """Stop ffmpeg + drop all subscribers, leaving configured untouched.

    `force=True` is for `disconnect()` (user wants it dead, holders
    be damned). `force=False` is the grace path; we double-check that
    there are still no holders before pulling the plug."""
    with sess._lock:
        if not force and sess._holders:
            return
        if sess.proc is None:
            # Nothing to tear down. Still emit the state event when
            # forced so disconnect() observers see configured=false.
            proc = None
            subs: list = []
        else:
            sess._stopping = True
            proc = sess.proc
            subs = list(sess._subscribers.values())
    if proc is not None:
        try: proc.terminate()
        except Exception: pass
        try: proc.wait(timeout=2)
        except Exception:
            try: proc.kill()
            except Exception: pass
        for s in subs:
            s.close()
        sess._health_stop.set()
    with sess._lock:
        sess.proc = None
        sess._reader = None
        sess._subscribers.clear()
        sess.peak_l = sess.peak_r = 0.0
        sess.clipped_l = sess.clipped_r = False
        sess._preroll_chunks.clear()
        sess._preroll_total_bytes = 0
        sess._last_health = {}
        # Clear `_stopping` here (not just in _spawn) so a future spawn
        # doesn't have to rely on the `proc is None` half of the predicate
        # to escape its poll loop. Symmetric with the lifecycle: stopping
        # belongs to the teardown that just finished.
        sess._stopping = False
        configured = sess.configured
        url = sess.url if configured else None
        fmt = dict(sess.fmt) if configured else {}
        if not configured:
            sess.url = None
            sess.fmt = {}
            sess.sample_format = ""
    sess._on_event({"type": "clip", "clipped_l": False,
                    "clipped_r": False, "cleared": True})
    sess._on_event({"type": "upstream", "configured": configured,
                    "connected": False, "live": False,
                    "url": url, "format": fmt})
