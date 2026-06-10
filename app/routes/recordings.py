"""Recording sessions, library file ops, stream proxy + probe."""
import asyncio
import json
import math
import re
import signal
import subprocess
import threading
import time
import uuid
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

# audioop drives the per-chunk peak calc on the silence-watcher hot path.
# It's deprecated in 3.12 (silenced here) and slated for removal in 3.13;
# the project targets 3.12 (see Dockerfile) so the replacement story only
# matters when we bump the runtime — same story as services/upstream.py.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning,
                            message=r".*audioop.*")
    import audioop

from services.arm import ArmDetector
from services.eventbus import bus
from services.ffmpeg import (
    LOW_SPACE_GB, disk_free_gb, disk_space_error, find_side, list_recordings,
    recording_headroom_minutes, safe_name,
)
# Process-lifecycle helpers for the proxy ffmpeg children live in their own
# service module so the route file stays focused on HTTP glue. The
# background reaper thread is spawned at recording_process import time.
# `_graceful_close` is re-exported under its old underscore name so any
# external monkey-patching (in tests, ops scripts) keeps working.
from services.recording_process import (
    graceful_close as _graceful_close,  # noqa: F401 — public re-export
    reap as _reap,
    teardown_proxy as _teardown_proxy,
)
from state import (
    ARM_AUTO_DISARM_HOURS, ARM_SIGNAL_THRESHOLD_DB, BulkDelete,
    DEFAULT_AUTO_STOP_ON_SILENCE, DEFAULT_SILENCE_SECONDS,
    DEFAULT_SILENCE_THRESHOLD_DB, DURATION_EDIT_MIN_SLACK_SECONDS,
    DurationEditRequest, LOG_DIR, RAW_DIR, RecordRequest, RenameRequest,
    SilenceEditRequest, TRASH_DIR, TRASH_TTL_SECONDS, sessions, upstream,
)


def _silence_threshold_int(threshold_db: float, bytes_per_sample: int) -> int:
    """Convert a dBFS threshold to the integer cutoff audioop.rms returns
    for `bytes_per_sample`-wide samples. amp = 10**(db/20); the cutoff is
    full_scale * amp, floored at 1 so an extremely-quiet threshold never
    becomes "everything is silent" (audioop.rms returns 0 only on truly
    zero chunks). Used by start_recording to precompute the per-session
    silence-detection threshold.

    The conversion matches `audioop.max` too — the integer scale is
    identical — but the live sink compares the cutoff against a smoothed
    RMS rather than a per-chunk peak, because vinyl runout grooves emit
    a ~-29 dBFS click every revolution (~1.8 s at 33⅓ RPM) that would
    keep re-arming a peak detector even though the average energy of the
    runout is well below music levels (~-47 dBFS RMS)."""
    full_scale = 0x7FFFFF if bytes_per_sample == 3 else 0x7FFF
    amp = 10.0 ** (max(-200.0, min(0.0, float(threshold_db))) / 20.0)
    return max(1, int(full_scale * amp))


# Time constant of the EMA that smooths the per-chunk RMS into a stable
# "energy level" for the silence detector. ~2 s is wide enough to average
# over a 33⅓ RPM runout-groove click period (~1.8 s) so the smoothed RMS
# converges to the mean noise floor instead of tracking the click peaks,
# and short enough that the detector reacts within a few seconds of a
# music→runout transition. Per-chunk alpha is computed from chunk
# duration, so the time constant is correct regardless of upstream
# sample rate / bit depth / frame size.
_SILENCE_RMS_TAU_SECONDS = 2.0


def _update_smoothed_ms(prev_ms: float, chunk_ms: float,
                        chunk_seconds: float, tau_seconds: float) -> float:
    """One-step exponential-moving-average update on a mean-square value.

    Mean-square (RMS²) is what we smooth — it's additive over time, so
    averaging samples in mean-square space yields the same result as
    computing RMS over the whole window directly. Time-constant `tau`
    matches a single-pole low-pass: a step input reaches ~63% of its
    new level after `tau` seconds, ~95% after 3·tau, regardless of how
    the underlying chunks are sized.

    Edge cases:
      * `tau_seconds <= 0` collapses to "use the latest chunk" — handy
        for tests that want to bypass smoothing.
      * `chunk_seconds <= 0` carries no new information (zero-length
        frame); return prev_ms unchanged rather than unfairly weighting
        an empty chunk.

    Pulled out so unit tests can drive the math directly without
    spinning up a recording session."""
    if tau_seconds <= 0:
        return chunk_ms
    if chunk_seconds <= 0:
        return prev_ms
    alpha = 1.0 - math.exp(-chunk_seconds / tau_seconds)
    return prev_ms * (1.0 - alpha) + chunk_ms * alpha


router = APIRouter()


@router.get("/api/stream-proxy")
async def stream_proxy():
    """Browser-compatible playback feed for unmuted tabs.

    Subscribes to the shared upstream session and re-encodes its raw PCM
    (24-bit/96 kHz from a Pi, or whatever the source delivers) to MP3 for
    the browser. Multiple unmuted tabs each spawn their own re-encoder but
    read from the SAME upstream pull, so the Pi only ever sees one /stream
    consumer.

    Acquires a lifecycle hold so an unmuted playback tab keeps ffmpeg up
    even if no other holder cares; released in the response generator's
    finally block so the upstream can drop back to idle when the last
    listener disconnects.
    """
    if not upstream.configured:
        bus.log("✗ stream-proxy: upstream not configured", "err")
        raise HTTPException(409, "stream not connected")
    # Acquire BEFORE touching fmt — acquire() is what spawns ffmpeg if it
    # was idle, and fmt is only populated once a spawn has run at least
    # once. Failing here (probe failed, etc.) leaves the holder count
    # untouched (acquire already cleaned up its token on raise).
    # Offloaded to a worker thread because spawn does sync probe + ffmpeg
    # subprocess startup (~1-2 s); inline would freeze the asyncio loop.
    hold = await asyncio.to_thread(
        upstream.acquire, f"stream-proxy:{uuid.uuid4().hex[:8]}")
    try:
        if not upstream.live:
            raise HTTPException(503, "upstream failed to start")
        fmt = upstream.fmt
        sample_format = upstream.sample_format
    except BaseException:
        upstream.release(hold)
        raise
    # MP3 because it's a self-synchronising frame format with very small
    # frames (~26 ms at 44.1 kHz). Browsers can start playback after just a
    # handful of frames, which gets startup latency down to a few hundred
    # ms — much better than WAV's ~1 s of pre-buffer.
    cmd = ["ffmpeg", "-loglevel", "error",
           "-fflags", "nobuffer",
           "-f", sample_format,
           "-ar", str(fmt["sample_rate"]),
           "-ac", str(fmt["channels"]),
           "-i", "pipe:0",
           "-ac", "2", "-ar", "44100",
           "-c:a", "libmp3lame", "-b:a", "192k",
           "-flush_packets", "1",
           "-f", "mp3", "-"]
    # Default bufsize gives us a BufferedWriter on stdin so write() does the
    # write-all loop for us. With bufsize=0 (FileIO) writes can be partial on
    # chunks larger than PIPE_BUF, which would corrupt ffmpeg's input.
    # stderr=DEVNULL: with -loglevel error there's nothing to capture in
    # steady state, and capturing it (even with a drain thread) just adds
    # surface area for races with the BufferedWriter close path.
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    sub_id = f"proxy-{uuid.uuid4().hex[:8]}"

    def _sink(chunk: bytes) -> None:
        # Raises BrokenPipeError when proc dies; UpstreamSession marks the
        # subscriber dead and unsubscribes us.
        proc.stdin.write(chunk)
        proc.stdin.flush()

    def _on_sub_close() -> None:
        # Closing stdin from the subscriber's worker thread (the same one
        # that's been writing to it) is the only safe place — Python's io
        # is not thread-safe, and closing from a different thread while a
        # write is in flight can deadlock.
        try: proc.stdin.close()
        except Exception: pass

    try:
        upstream.subscribe(sub_id, _sink, on_close=_on_sub_close)
    except RuntimeError:
        proc.terminate()
        _reap(proc)
        upstream.release(hold)
        bus.log("✗ stream-proxy: subscribe failed (upstream gone)", "err")
        raise HTTPException(409, "stream not connected")

    def generate():
        try:
            while True:
                chunk = proc.stdout.read1(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            _teardown_proxy(proc, sub_id)
            # Release the upstream hold last — after the subscriber is
            # already torn down — so the grace timer (if this was the
            # last holder) starts from a clean state.
            upstream.release(hold)

    return StreamingResponse(generate(), media_type="audio/mpeg")


@router.post("/api/test-stream")
async def test_stream(body: dict):
    """Probe an upstream URL with ffprobe and surface its stream parameters.
    Failure modes (ffprobe nonzero, timeout, OSError) raise 502 with the
    underlying message in `detail` — clients rely on HTTP status to branch."""
    url = body.get("stream_url", "")
    try:
        # Offload to a worker thread so a slow/unreachable URL can't block
        # the asyncio loop (and every other request on this worker) for up
        # to 10 s. Matches the pattern used elsewhere in this module.
        result = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-i", url],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(
                502, (result.stderr or "ffprobe failed").strip()[:300],
            )
        info = json.loads(result.stdout)
        streams = info.get("streams", [{}])
        s = streams[0] if streams else {}
        return {
            "sample_rate": s.get("sample_rate", "?"),
            "channels":    s.get("channels", "?"),
            "codec":       s.get("codec_name", "?"),
            "bit_depth":   s.get("bits_per_sample", "?"),
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(502, "Timeout — is the stream URL reachable?")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/api/record/start")
async def start_recording(req: RecordRequest):
    """Start a FLAC recording fed from the shared upstream session.

    Thin async shim — the whole start sequence is sync I/O (lifecycle
    acquire probes + spawns ffmpeg, then subprocess.Popen for the encoder),
    so it runs on a worker thread to keep the event loop responsive. The
    sync impl is shared with the armed auto-record trigger, which fires
    from a plain thread with no event loop at all."""
    return await asyncio.to_thread(_start_recording_impl, req)


def _start_recording_impl(req: RecordRequest) -> dict:
    """Start a FLAC recording fed from the shared upstream session.

    The recording's ffmpeg reads raw PCM from stdin (matching the upstream's
    sample_format / rate / channels) and encodes lossless FLAC. Recording
    only requires that something is connected — the `stream_url` field on
    the request is ignored if it disagrees with the active upstream, since
    that's the only stream we're actually pulling.

    Sync + thread-safe: called via asyncio.to_thread from the route and
    directly from the arm-fire thread. Raises HTTPException for the typed
    failure cases; the arm path catches and logs them instead of having an
    HTTP response to map onto.
    """
    err = disk_space_error(LOW_SPACE_GB, "recording")
    if err:
        raise HTTPException(507, err)
    if not upstream.configured:
        raise HTTPException(409, "stream not connected — connect first")
    sid = str(uuid.uuid4())[:8]
    # Acquire the lifecycle hold up front. This spawns ffmpeg if it was
    # idle (record start is the canonical reason to bring upstream up,
    # whether or not any tab was visible), and guarantees the hold survives
    # the lifetime of this recording — closing the tab won't tear ffmpeg
    # down mid-FLAC. Released in `_finalize_session`.
    rec_hold = upstream.acquire(f"record:{sid}")
    if not upstream.live:
        upstream.release(rec_hold)
        raise HTTPException(503, "upstream failed to start")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    artist = req.artist or "Unknown"
    album  = req.album  or ts
    year   = req.year   or datetime.now().strftime("%Y")

    fname = f"{safe_name(artist)} - {safe_name(album)} ({year}).flac"
    outfile = str(RAW_DIR / fname)
    fmt = upstream.fmt
    sample_format = upstream.sample_format

    # ffmpeg runs unbounded; the per-session watcher owns the duration
    # cap and proactively finalizes via _finalize_session(sid, "auto") on
    # its 500 ms poll tick. Owning the cap server-side (instead of via
    # ffmpeg's `-t`) is what lets the user edit it mid-recording — see
    # POST /api/record/{sid}/duration. We deliberately accept the ≤500 ms
    # overshoot vs ffmpeg's `-t` (sub-second, dwarfed by the SIGINT-flush
    # path) for the editability win.
    cmd = [
        "ffmpeg", "-y",
        # Input is raw PCM piped from the upstream reader thread. Telling
        # ffmpeg the format up front saves it from probing stdin.
        "-f", sample_format,
        "-ar", str(fmt["sample_rate"]),
        "-ac", str(fmt["channels"]),
        "-i", "pipe:0",
        "-c:a", "flac", "-compression_level", "8",
        "-map_metadata", "-1",
        "-metadata", f"artist={req.artist}",
        "-metadata", f"album={req.album}",
        "-metadata", f"date={year}",
        "-metadata", f"genre={req.genre}",
        "-metadata", f"label={req.label}",
    ]
    cmd += [outfile]

    log_path = LOG_DIR / f"{sid}.log"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=log_fh,
                            bufsize=0)

    # Pause works by flipping the per-session paused flag; the sink then drops
    # bytes instead of forwarding them to ffmpeg. The resulting FLAC has a
    # hard cut at each pause/resume pair (no silence in the middle, file length
    # equals the un-paused recording time). We deliberately do NOT SIGSTOP
    # ffmpeg: with bufsize=0 stdin pipes, a stopped consumer would hang the
    # upstream reader thread on its next pipe write.
    sess_state = {"paused": False}

    # Auto-stop on silence: any unset request field falls back to the env
    # default. silence_seconds == 0 disables the watcher entirely (the sink
    # then skips the audioop.rms call too — zero overhead on the hot path).
    auto_stop = (req.auto_stop_on_silence
                 if req.auto_stop_on_silence is not None
                 else DEFAULT_AUTO_STOP_ON_SILENCE)
    silence_seconds = max(0, int(req.silence_seconds)) if auto_stop else 0
    if silence_seconds == 0 and auto_stop:
        # The user asked for auto-stop but gave a non-positive duration —
        # honour the spirit of the request with the configured default
        # rather than silently dropping the flag.
        silence_seconds = DEFAULT_SILENCE_SECONDS
    threshold_db = (req.silence_threshold_db
                    if req.silence_threshold_db is not None
                    else DEFAULT_SILENCE_THRESHOLD_DB)
    bytes_per_sample = 3 if sample_format == "s24le" else 2
    # Always compute the threshold even when silence-auto-stop is off
    # at start-of-recording. The watcher's silence_seconds==0 short
    # circuit keeps the feature disabled, but storing a valid cutoff
    # means a mid-recording edit that ENABLES auto-stop has a working
    # threshold to compare against immediately — without it the field
    # would be 0 and the sink would treat every chunk as above
    # threshold, latching silence_armed=True with no way to ever fire.
    silence_threshold_int = _silence_threshold_int(threshold_db,
                                                   bytes_per_sample)
    # Bytes/sec drives the per-chunk EMA time-step. Guarded against a
    # zero denominator if the upstream probe somehow returned a bogus
    # format — `_update_smoothed_ms` also short-circuits on zero, so
    # this clamp is just defence in depth.
    bytes_per_second = max(1, (fmt.get("sample_rate", 0) or 0)
                              * (fmt.get("channels", 0) or 0)
                              * bytes_per_sample)

    # Pre-roll: live bytes must wait until the buffered pre-roll is written
    # to ffmpeg's stdin first, otherwise the timeline is scrambled. The
    # subscriber's worker thread is gated on this Event.
    preroll_done = threading.Event()

    def _sink(chunk: bytes) -> None:
        # Block until preroll_done is set or the recording is torn down. The
        # bounded subscriber queue absorbs incoming chunks during this wait;
        # in practice it's a few-ms delay (preroll is a few MB max).
        preroll_done.wait()
        if sess_state["paused"]:
            return
        sess = sessions.get(sid)
        # Read silence_seconds LIVE off the session so a mid-recording
        # POST /api/record/silence/{sid} takes effect on the very next
        # chunk. silence_threshold_int doesn't need the same treatment —
        # the dB threshold is env-only, can't change while a session is
        # in flight. Smoothed RMS rather than per-chunk peak: vinyl
        # runout grooves produce a click every revolution (~1.8 s at
        # 33⅓ RPM) peaking at ~-29 dBFS over a ~-55 dBFS noise floor. A
        # peak detector keeps re-arming every click; the runout's mean
        # energy is ~-47 dBFS RMS, well below music's ~-15 dBFS, so
        # smoothing the RMS over ~2 s lets the detector cleanly separate
        # the two. Session fields are written without a lock — the GIL
        # makes single-attribute writes atomic enough for the watcher's
        # purpose; worst case is a one-tick delay before auto-stop fires.
        if sess is not None and sess.silence_seconds > 0:
            chunk_rms = audioop.rms(chunk, bytes_per_sample)
            chunk_ms = float(chunk_rms) * float(chunk_rms)
            chunk_seconds = len(chunk) / bytes_per_second
            sess.silence_ms_smoothed = _update_smoothed_ms(
                sess.silence_ms_smoothed, chunk_ms,
                chunk_seconds, _SILENCE_RMS_TAU_SECONDS,
            )
            smoothed_rms = math.sqrt(sess.silence_ms_smoothed)
            if smoothed_rms >= silence_threshold_int:
                sess.silence_armed = True
                sess.silence_since = None
            elif sess.silence_armed and sess.silence_since is None:
                sess.silence_since = time.monotonic()
        proc.stdin.write(chunk)

    def _on_sub_close() -> None:
        # Unblock the worker thread if it's still waiting for preroll_done so
        # it can exit cleanly.
        preroll_done.set()
        try: proc.stdin.close()
        except Exception: pass

    try:
        _, preroll_bytes = upstream.subscribe_with_preroll(
            f"rec-{sid}", _sink, on_close=_on_sub_close,
        )
    except RuntimeError:
        try: proc.kill()
        except Exception: pass
        log_fh.close()
        upstream.release(rec_hold)
        raise HTTPException(409, "stream not connected")

    # Write the captured pre-roll first, then release the live gate. If the
    # ring is empty (e.g. PRE_ROLL_SECONDS=0 or upstream just connected),
    # this is a no-op and the seam is unchanged from the old behavior.
    if preroll_bytes:
        try:
            proc.stdin.write(preroll_bytes)
        except (BrokenPipeError, OSError):
            # ffmpeg died before we could write — let the watcher reap it.
            pass
    preroll_done.set()

    # `start_time` and `pause_started` are wall-time deltas displayed as
    # elapsed; using monotonic protects them against system clock changes
    # (NTP step at midnight, container TZ surprises) that would otherwise
    # corrupt the timer mid-recording. `started_unix` keeps the human-
    # readable wallclock-of-record-start for display only.
    #
    # The per-session `finalize_lock` (owned by Session) serialises finalize
    # between the user-stop request handler and the per-session watcher
    # thread. Both can race to reap a session whose ffmpeg has just exited
    # (SIGINT from user-stop wakes both `proc.wait()` calls); without this
    # lock the loser would either double-publish the stop event or KeyError
    # on the duplicate remove. The `upstream_hold` is released by
    # `_finalize_session`; storing it on the session means the user-stop
    # handler, the per-session watcher, and the reaper all reach the same
    # token through `sessions.get(sid)`.
    start_log = f"▶ Started recording → {fname}"
    init_log_lines = [start_log]
    if silence_seconds > 0:
        init_log_lines.append(
            f"⏱ Auto-stop on silence: {silence_seconds}s under "
            f"{threshold_db:.1f} dBFS"
        )

    sessions.create(
        sid,
        proc=proc, outfile=outfile, log_fh=log_fh,
        start_time=time.monotonic(),
        started_unix=time.time(),
        duration=req.duration,
        meta={"artist": req.artist, "album": req.album, "year": year},
        filename=fname,
        sess_state=sess_state,
        upstream_hold=rec_hold,
        log_path=str(log_path),
        log_lines=init_log_lines,
        silence_seconds=silence_seconds,
        silence_threshold_int=silence_threshold_int,
    )

    # Spawn a per-session blocking waiter so a self-exit (auto-stop on `-t`,
    # crash, kill -9) gets reaped within milliseconds — replaces the old
    # 1 Hz polling loop.
    threading.Thread(target=_watch_session, args=(sid,),
                     name=f"rec-watch-{sid}", daemon=True).start()
    bus.log(f"▶ Recording → {fname}", "info")
    bus.publish({"type": "record", "event": "start",
                 "session_id": sid, "filename": fname,
                 "duration": req.duration})
    return {"session_id": sid, "filename": fname}


# ── Armed auto-record ────────────────────────────────────────────────────
# "Arm" = user-enabled standby: hold the upstream live, watch its RMS, and
# start a normal recording on the first silence→signal transition (see
# services/arm.py for the detector semantics). State is server-side and
# broadcast over WS (`record` events `armed`/`disarmed` + the hello
# snapshot) so every tab agrees, exactly like recording state.
#
# CPU cost of staying armed is one audioop.rms per ~50 ms chunk — the
# upstream already computes peaks at the same cadence for the VU meter.
# The real cost is the lifecycle hold (arecord runs on the Pi while
# armed), which is why an arm self-expires after ARM_AUTO_DISARM_HOURS:
# never-triggered standby must not keep the Pi busy forever. The arm stays
# active across recordings it starts — with auto-stop-on-silence enabled
# this gives hands-free multi-side capture (side ends on runout silence,
# flipping the record re-fires on the next needle drop).
_arm_lock = threading.Lock()
_arm: dict = {"armed": False, "hold": None, "detector": None, "req": None,
              "deadline": 0.0, "firing": False}


def arm_is_armed() -> bool:
    """Read-only accessor for the WS hello snapshot."""
    with _arm_lock:
        return bool(_arm["armed"])


@router.post("/api/record/arm")
async def arm_record(req: RecordRequest):
    # Sync work (lifecycle acquire spawns/probes ffmpeg) → worker thread,
    # same rationale as start_recording.
    return await asyncio.to_thread(_arm_impl, req)


@router.post("/api/record/disarm")
async def disarm_record():
    return await asyncio.to_thread(_disarm_impl, "user")


def _arm_impl(req: RecordRequest) -> dict:
    err = disk_space_error(LOW_SPACE_GB, "recording")
    if err:
        raise HTTPException(507, err)
    if not upstream.configured:
        raise HTTPException(409, "stream not connected — connect first")
    with _arm_lock:
        if _arm["armed"]:
            # Idempotent — a second tab arming is a no-op, mirroring how
            # both tabs share one recording session.
            return {"armed": True}
    hold = upstream.acquire("arm")
    if not upstream.live:
        upstream.release(hold)
        raise HTTPException(503, "upstream failed to start")
    fmt = upstream.fmt
    bytes_per_sample = 3 if upstream.sample_format == "s24le" else 2
    bytes_per_second = max(1, (fmt.get("sample_rate", 0) or 0)
                              * (fmt.get("channels", 0) or 0)
                              * bytes_per_sample)
    # Trigger threshold: a per-chunk PEAK boundary (default -20 dBFS),
    # independent of the silence auto-stop knob. The detector fires on the
    # needle set-down transient itself — see services/arm.py for why peak
    # beats smoothed RMS here (quiet albums) and how the default rejects
    # runout-groove clicks. `_silence_threshold_int` converts dBFS to the
    # integer scale shared by audioop.rms and audioop.max.
    threshold_db = ARM_SIGNAL_THRESHOLD_DB
    detector = ArmDetector(
        threshold_int=_silence_threshold_int(threshold_db, bytes_per_sample),
        bytes_per_sample=bytes_per_sample,
        bytes_per_second=bytes_per_second,
    )
    with _arm_lock:
        if _arm["armed"]:
            # Lost an arm race with another tab — theirs is equivalent.
            upstream.release(hold)
            return {"armed": True}
        _arm.update(armed=True, hold=hold, detector=detector, req=req,
                    deadline=time.monotonic() + ARM_AUTO_DISARM_HOURS * 3600,
                    firing=False)
    try:
        upstream.subscribe("arm", _arm_sink, on_close=_arm_on_upstream_close)
    except RuntimeError:
        # Upstream died between acquire and subscribe — roll back.
        _disarm_impl("upstream")
        raise HTTPException(409, "stream not connected")
    bus.log(f"◉ Auto-record armed — starts on peak ≥ {threshold_db:g} dBFS "
            f"(self-disarms after {ARM_AUTO_DISARM_HOURS:g} h)", "info")
    bus.publish({"type": "record", "event": "armed"})
    return {"armed": True}


def _disarm_impl(reason: str = "user") -> dict:
    """Tear down the armed state. Idempotent; safe from any thread.

    Order matters: clear `armed` BEFORE unsubscribing, so the subscriber's
    on_close (which fires inside unsubscribe) sees armed=False and doesn't
    schedule a second, recursive disarm."""
    with _arm_lock:
        was_armed = _arm["armed"]
        hold = _arm["hold"]
        _arm.update(armed=False, hold=None, detector=None, req=None,
                    firing=False)
    if not was_armed:
        return {"armed": False}
    try:
        upstream.unsubscribe("arm")
    except Exception:
        pass
    if hold is not None:
        try:
            upstream.release(hold)
        except Exception:
            pass
    msg = {
        "user":     "○ Auto-record disarmed",
        "timeout":  f"○ Auto-record disarmed — no signal for "
                    f"{ARM_AUTO_DISARM_HOURS:g} h",
        "upstream": "○ Auto-record disarmed — stream disconnected",
    }.get(reason, "○ Auto-record disarmed")
    bus.log(msg, "info")
    bus.publish({"type": "record", "event": "disarmed", "reason": reason})
    return {"armed": False}


def _arm_sink(chunk: bytes) -> None:
    """Upstream subscriber sink while armed. Runs on the subscriber's
    worker thread at ~50 ms cadence; must stay cheap and must never call
    back into upstream subscribe/unsubscribe inline (lock re-entrancy) —
    state transitions are handed to fresh threads."""
    with _arm_lock:
        if not _arm["armed"] or _arm["firing"]:
            return
        detector = _arm["detector"]
        deadline = _arm["deadline"]
    if detector is None:
        return
    if time.monotonic() >= deadline:
        threading.Thread(target=_disarm_impl, args=("timeout",),
                         name="arm-timeout", daemon=True).start()
        return
    if sessions.values():
        # A recording is in flight (ours or manual) — idle the detector so
        # the next trigger needs a fresh quiet-confirm after it ends.
        detector.reset()
        return
    if detector.update(chunk):
        with _arm_lock:
            if not _arm["armed"] or _arm["firing"]:
                return
            _arm["firing"] = True
        threading.Thread(target=_arm_fire, name="arm-fire",
                         daemon=True).start()


def _arm_fire() -> None:
    """Start a recording from the armed trigger. The arm stays active
    afterwards (the sink idles while the session runs), so auto-stop +
    re-trigger gives hands-free multi-side capture."""
    with _arm_lock:
        req = _arm["req"]
        detector = _arm["detector"]
    if req is None:
        with _arm_lock:
            _arm["firing"] = False
        return
    try:
        bus.log("◉ Signal detected — auto-record starting", "info")
        _start_recording_impl(req)
    except HTTPException as e:
        bus.log(f"✗ Auto-record failed to start: {e.detail}", "err")
    except Exception as e:
        bus.log(f"✗ Auto-record failed to start: {e}", "err")
    finally:
        # Reset BEFORE clearing `firing`: if the start failed with signal
        # still hot, an un-reset detector would re-fire on the very next
        # chunk and busy-loop the failure. A reset forces a fresh
        # quiet-confirm first.
        if detector is not None:
            detector.reset()
        with _arm_lock:
            _arm["firing"] = False


def _arm_on_upstream_close() -> None:
    """Subscriber on_close: the upstream tore down underneath us (user
    disconnect, Pi reboot, stream error — the read loop closes every
    subscriber on unexpected EOF). Disarm so the UI reflects reality; the
    user re-arms after reconnecting. Off-thread because on_close runs
    inside upstream teardown paths that may hold the session lock."""
    if arm_is_armed():
        threading.Thread(target=_disarm_impl, args=("upstream",),
                         name="arm-upstream-close", daemon=True).start()


# Recently-finalized session payloads, keyed by session_id. The user-stop
# request handler and the per-session watcher thread can both reach
# `_finalize_session` for the same session (e.g. SIGINT from user-stop
# wakes both `proc.wait()` calls). The first thread does the real
# cleanup, removes the entry from the session manager, and stashes its
# return payload here; the second thread looks here so it can return the
# same {elapsed, filename, size_mb} body to its caller instead of zeros.
# Bounded so a long-running server doesn't accumulate state for sessions
# that ended hours ago.
_RECENT_RESULTS_MAX = 32
_recent_results: dict[str, dict] = {}
_recent_results_order: list[str] = []
_recent_results_lock = threading.Lock()


def _record_recent_result(session_id: str, result: dict) -> None:
    with _recent_results_lock:
        if session_id in _recent_results:
            return
        _recent_results[session_id] = result
        _recent_results_order.append(session_id)
        while len(_recent_results_order) > _RECENT_RESULTS_MAX:
            old = _recent_results_order.pop(0)
            _recent_results.pop(old, None)


def _finalize_session(session_id: str, reason: str) -> dict:
    """Common cleanup for user-stop, auto-stop, and crash. Returns the
    {elapsed, filename, size_mb} payload sent to the caller / WS.

    Idempotent + serialized: the user-stop request handler and the
    per-session watcher thread can both observe a just-exited ffmpeg
    (SIGINT from user-stop wakes both `proc.wait()` calls). The first
    caller does the cleanup under the session's `finalize_lock`, removes
    the session from the manager, and stashes its result in
    `_recent_results`. A later caller for the same session_id finds the
    stashed payload and returns it — so the HTTP response stays meaningful
    (matching the real elapsed/filename) instead of degenerating to zeros,
    and we never KeyError on a duplicate remove."""
    s = sessions.get(session_id)
    if s is None:
        # Session already gone — return whatever the winning caller
        # produced so the HTTP body still tells the user the truth.
        with _recent_results_lock:
            cached = _recent_results.get(session_id)
        if cached is not None:
            return dict(cached)
        return {"elapsed": 0, "filename": "", "size_mb": 0}
    # Sessions written by tests / fallbacks may not carry a finalize_lock.
    # Synthesise one so the with-block below stays uniform.
    lock = s.finalize_lock or threading.Lock()
    with lock:
        if s.finalized:
            return dict(s.finalize_result or
                        {"elapsed": 0, "filename": "", "size_mb": 0})
        # Detach from the upstream so its bytes stop being written into a
        # half-closed stdin. Closing the subscription closes proc.stdin which
        # makes ffmpeg flush + exit on its own when we then SIGINT it (or
        # naturally if it already finished from `-t duration`).
        upstream.unsubscribe(f"rec-{session_id}")
        # Snapshot the pause state BEFORE clearing it — the `end_time`
        # selection below depends on whether we stopped while paused, and
        # we still need to clear the live flags now so any in-flight sink
        # callback observes a non-paused session before we tear down.
        was_paused = s.paused
        pause_started_at = s.pause_started
        if s.paused:
            s.paused = False
            s.sess_state["paused"] = False
        if s.proc.poll() is None:
            # SIGINT lets ffmpeg finalize the FLAC trailer cleanly; SIGTERM truncates.
            try: s.proc.send_signal(signal.SIGINT)
            except Exception:
                try: s.proc.terminate()
                except Exception: pass
            try: s.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try: s.proc.kill()
                except Exception: pass
        try: s.log_fh.close()
        except Exception: pass
        # If we stopped while paused, start_time hasn't been advanced for the
        # current pause window — only resume does that. Use pause_started so the
        # reported elapsed matches the FLAC duration (un-paused recording time).
        end_time = pause_started_at if was_paused else time.monotonic()
        elapsed = int(end_time - s.start_time)
        fname = Path(s.outfile).name
        fsize = round(Path(s.outfile).stat().st_size / 1e6, 1) if Path(s.outfile).exists() else 0
        s.log_lines.append(f"■ Stopped — {elapsed}s  |  {fsize} MB  |  {fname}")
        icon = {"user": "■ Saved", "auto": "■ Auto-stopped", "crash": "✗ Recording crashed"}.get(reason, "■ Stopped")
        level = "err" if reason == "crash" else "ok"
        bus.log(f"{icon} — {elapsed}s · {fsize} MB · {fname}", level)
        bus.publish({"type": "record", "event": "stop", "reason": reason,
                     "session_id": session_id, "filename": fname,
                     "elapsed": elapsed, "size_mb": fsize})
        result = {"elapsed": elapsed, "filename": fname, "size_mb": fsize}
        # Cache on both the session (for any caller still holding `s`)
        # and the module-level recent-results table (for callers whose
        # `sessions.get(sid)` happens to fire AFTER our remove below).
        s.finalized = True
        s.finalize_result = result
        # Release the lifecycle hold so the upstream session can drop to
        # idle if no other holder (visible WS tab, playback proxy, another
        # recording) keeps it alive. Idempotent on the token.
        hold = s.upstream_hold
        if hold is not None:
            try: upstream.release(hold)
            except Exception: pass
        _record_recent_result(session_id, result)
        sessions.remove(session_id)
        return result


@router.post("/api/record/stop/{session_id}")
async def stop_recording(session_id: str):
    if sessions.get(session_id) is None:
        raise HTTPException(404, "Session not found")
    return _finalize_session(session_id, "user")


@router.post("/api/record/pause/{session_id}")
async def pause_recording(session_id: str):
    """Pause an in-flight recording. We flip the session's `paused` flag —
    the upstream subscriber's sink then discards chunks instead of forwarding
    them to ffmpeg's stdin. ffmpeg keeps its stdin drained, so the upstream
    reader thread never blocks (which used to deadlock the whole server when
    we previously SIGSTOPped ffmpeg). The resulting FLAC has a clean cut: file
    duration equals only the un-paused recording time."""
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s.paused:
        return {"paused": True}
    s.paused = True
    s.sess_state["paused"] = True
    s.pause_started = time.monotonic()
    s.log_lines.append("‖ Paused")
    bus.log("‖ Recording paused", "info")
    bus.publish({"type": "record", "event": "pause",
                 "session_id": session_id,
                 "elapsed": int(s.pause_started - s.start_time)})
    return {"paused": True}


@router.post("/api/record/resume/{session_id}")
async def resume_recording(session_id: str):
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not s.paused:
        return {"paused": False}
    # Slide start_time forward by the pause duration so the reported elapsed
    # excludes the time spent paused.
    now = time.monotonic()
    paused_for = now - (s.pause_started if s.pause_started is not None else now)
    s.start_time += paused_for
    s.paused = False
    s.sess_state["paused"] = False
    s.pause_started = None
    s.log_lines.append("▶ Resumed")
    bus.log("▶ Recording resumed", "info")
    bus.publish({"type": "record", "event": "resume",
                 "session_id": session_id,
                 "elapsed": int(time.monotonic() - s.start_time)})
    return {"paused": False}


def _elapsed_seconds(s) -> float:
    """Wallclock elapsed for a live session, frozen at `pause_started`
    when paused so paused time doesn't consume the duration budget."""
    end = s.pause_started if (s.paused and s.pause_started is not None) \
        else time.monotonic()
    return end - s.start_time


@router.post("/api/record/duration/{session_id}")
async def edit_duration(session_id: str, req: DurationEditRequest):
    """Edit the duration cap of a live recording without restarting ffmpeg.

    The cap lives entirely in `session.duration`; the per-session watcher
    reads it on every ~500 ms tick. The endpoint just mutates the field.

    Extension (or → unlimited) is always allowed. Reduction (or stepping
    DOWN from unlimited to a bounded value) requires at least
    `DURATION_EDIT_MIN_SLACK_SECONDS` of remaining headroom — without
    that guard, a stray click on the dropdown could terminate the
    recording within the next watcher tick. The 409 path returns the
    actual slack so the UI can render a useful error.

    Pause-aware: `_elapsed_seconds` freezes at `pause_started` while
    paused, so editing the cap of a paused recording uses the same
    elapsed-budget math the timer + status snapshot use."""
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if req.duration < 0:
        raise HTTPException(400,
            "duration must be 0 (unlimited) or a positive number of seconds")

    new_duration = int(req.duration)
    lock = s.finalize_lock or threading.Lock()
    with lock:
        if s.finalized:
            raise HTTPException(409, "Session already finalized")
        old_duration = s.duration
        # No-op edits return 200 with the unchanged value — keeps the UI's
        # optimistic "set the dropdown then POST" flow idempotent.
        if new_duration == old_duration:
            return {"duration": new_duration}
        # Reduction guard. "Reduction" = the new cap could fire sooner
        # than the old one would have, which covers two cases:
        #   * new > 0 AND old > 0 AND new < old  (literal reduction)
        #   * new > 0 AND old == 0               (bounded from unlimited)
        if new_duration > 0 and (old_duration == 0 or new_duration < old_duration):
            elapsed = _elapsed_seconds(s)
            slack = new_duration - elapsed
            if slack < DURATION_EDIT_MIN_SLACK_SECONDS:
                # 409 conflict: the new cap is too close to "now". Includes
                # the slack budget so a UI can tell the user "need N s more".
                raise HTTPException(
                    409,
                    f"reducing to {new_duration}s would leave only "
                    f"{int(slack)}s of headroom (need "
                    f"{DURATION_EDIT_MIN_SLACK_SECONDS}s)",
                )
        s.duration = new_duration
        s.log_lines.append(
            f"⏱ Duration cap → {('∞' if new_duration == 0 else f'{new_duration}s')}"
        )
    bus.log(
        f"⏱ Recording {session_id}: duration cap "
        f"{('unlimited' if new_duration == 0 else f'{new_duration}s')}",
        "info",
    )
    # WS broadcast so every tab re-anchors its progress bar against the
    # new cap; the elapsed field lets a freshly-rejoined tab compute its
    # share of the bar without a separate /api/status fetch.
    bus.publish({
        "type":       "record",
        "event":      "duration",
        "session_id": session_id,
        "duration":   new_duration,
        "elapsed":    int(_elapsed_seconds(s)),
    })
    return {"duration": new_duration}


@router.post("/api/record/silence/{session_id}")
async def edit_silence(session_id: str, req: SilenceEditRequest):
    """Edit the silence-auto-stop cap of a live recording.

    Mirrors `edit_duration`: mutates session.silence_seconds and emits a
    `record:silence` event so every tab's dropdown re-anchors. The
    per-session watcher reads silence_seconds on its ~500 ms tick; the
    sink reads it live too (every chunk), so the new cap takes effect
    within milliseconds.

    No slack guard (unlike duration). The worst a reduction can do is
    finalize "now" if silence is already accumulating — that's exactly
    what the user is asking for when they shrink the cap mid-recording,
    so there's no surprise to guard against. The detection threshold
    (dBFS) is intentionally not editable here; it stays an
    ops/calibration knob set via SILENCE_THRESHOLD_DB.

    Transitioning 0 → positive resets the smoothing state. The sink
    skips audioop.rms while silence_seconds==0, so silence_ms_smoothed,
    silence_armed, and silence_since carry stale values from either
    start-of-recording or a previous enable/disable cycle. Resetting
    means the freshly-enabled detector arms cleanly against the next
    chunks instead of immediately firing on stale state."""
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if req.silence_seconds < 0:
        raise HTTPException(400,
            "silence_seconds must be 0 (disabled) or a positive number of seconds")

    new_secs = int(req.silence_seconds)
    lock = s.finalize_lock or threading.Lock()
    with lock:
        if s.finalized:
            raise HTTPException(409, "Session already finalized")
        old_secs = s.silence_seconds
        # No-op edits return 200 unchanged — keeps the dropdown's
        # optimistic "set then POST" flow idempotent across tabs that
        # might race the same value.
        if new_secs == old_secs:
            return {"silence_seconds": new_secs}
        if old_secs == 0 and new_secs > 0:
            # Re-enabling auto-stop: drop any stale smoothing carried
            # over from when the feature was off (the sink wasn't
            # updating these fields). Forces a fresh arming cycle.
            s.silence_ms_smoothed = 0.0
            s.silence_armed = False
            s.silence_since = None
        s.silence_seconds = new_secs
        label = '∞ disabled' if new_secs == 0 else f'{new_secs}s'
        s.log_lines.append(f"⏱ Silence cap → {label}")
    bus.log(
        f"⏱ Recording {session_id}: silence cap "
        f"{('disabled' if new_secs == 0 else f'{new_secs}s')}",
        "info",
    )
    # WS broadcast so every other tab's silence-sel dropdown re-anchors
    # to the new cap (matching the duration sync). The silence-progress
    # bar itself already follows the watcher-emitted `silence` events,
    # so no separate progress reset is needed here.
    bus.publish({
        "type":            "record",
        "event":           "silence",
        "session_id":      session_id,
        "silence_seconds": new_secs,
    })
    return {"silence_seconds": new_secs}


def _classify_exit(s) -> str:
    """Decide whether a self-exited ffmpeg counts as an auto-stop or a
    crash. Used by the per-session watcher and any explicit reaper.

    Note: ffmpeg no longer self-exits on a duration cap — the Python
    watcher proactively finalizes via `_finalize_session(sid, "auto")`
    before ffmpeg can reach an `-t` boundary (we no longer pass `-t`).
    So in steady state this function only sees the crash / upstream-died
    paths, returning "crash". The clean-exit branch is kept as a
    belt-and-braces fallback for any future code path that does let
    ffmpeg exit on its own."""
    outfile = Path(s.outfile)
    if outfile.exists() and outfile.stat().st_size > 0 and s.proc.returncode == 0:
        return "auto"
    return "crash"


# Watcher tick. ~500 ms is fast enough that the user-facing "auto-stop at
# 30:00" or the silence trigger lands within sub-second of its target, and
# slow enough that the per-session thread spends ~all its time blocked in
# proc.wait(timeout=).
_WATCH_TICK_SECONDS = 0.5


def _silence_should_autostop(s, now: float) -> bool:
    """Decide whether the per-session watcher should finalize `s` with
    reason="auto" right now because the upstream has been silent long
    enough. Pulled out so unit tests can drive the decision logic without
    spinning a real ffmpeg subprocess.

    The contract:
      * silence_seconds == 0 → feature off, never trigger.
      * paused              → silence accumulation pauses too (the sink
                              stops writing, but it also stops updating
                              `silence_since` while paused).
      * silence_armed       → at least one above-threshold chunk has been
                              seen; lead-in / pre-roll silence cannot
                              trigger an auto-stop.
      * silence_since None  → most recent chunk was above threshold.
      * elapsed gate        → `now - silence_since >= silence_seconds`."""
    if s.silence_seconds <= 0:
        return False
    if s.paused:
        return False
    if not s.silence_armed:
        return False
    since = s.silence_since  # snapshot — sink writes from another thread
    if since is None:
        return False
    return (now - since) >= s.silence_seconds


# "Recording but no input signal" guard. The auto-stop-on-silence detector
# only fires AFTER it has seen signal (silence_armed) — so a take that never
# has any signal (turntable off, wrong input, unplugged phono) never trips
# it and silently records a dead 20-minute file. This warns instead.
#
# The threshold is generous on purpose: the pre-roll workflow means it's
# NORMAL to hit record, then walk over and drop the needle several seconds
# later — so a short window would false-positive on every careful capture.
# 30 s comfortably exceeds needle-cueing while still catching a dead take
# long before a side finishes. The peak floor (~-50 dBFS amplitude) is well
# below any real groove (even a quiet lead-in is louder) so genuine audio
# always clears it.
NO_SIGNAL_WARN_SECONDS = 30.0
_NO_SIGNAL_PEAK_FLOOR = 0.003


def _should_warn_no_signal(signal_seen: bool, elapsed: float, paused: bool,
                           already_warned: bool) -> bool:
    """Decide whether the watcher should emit the no-signal warning now.

    Pure so unit tests can drive the truth table. Fires once when a
    recording has run `NO_SIGNAL_WARN_SECONDS` without the upstream peak
    ever rising above the floor — i.e. nothing is coming in."""
    if already_warned or paused or signal_seen:
        return False
    return elapsed >= NO_SIGNAL_WARN_SECONDS


def _duration_cap_reached(s, now: float) -> bool:
    """Pure decision function: should the watcher finalize `s` with
    reason="auto" right now because the duration cap has elapsed?

    Contract:
      * duration == 0 → unlimited, never trigger.
      * paused        → elapsed is frozen (pause_started anchors it), so
                        time spent paused does not consume the cap.
      * monotonic time only — a wallclock NTP step at midnight must not
                        change which side of the cap the recording is on.

    Pulled out so unit tests can drive the truth table without spinning a
    real ffmpeg subprocess."""
    if s.duration <= 0:
        return False
    if s.paused:
        end = s.pause_started if s.pause_started is not None else now
    else:
        end = now
    return (end - s.start_time) >= s.duration


def _silence_progress_payload(s, now: float):
    """Snapshot of the silence-countdown state the watcher publishes each
    tick so the UI can fill a progress bar without re-implementing the
    arming + silence_since state machine.

    Returns None when auto-stop is disabled (silence_seconds == 0) — the
    watcher then skips the publish entirely so idle WS traffic stays low.

    Mirrors `_silence_should_autostop`'s gates: paused / not armed /
    silence_since None all pin `elapsed_seconds` at 0 so the bar drains
    back to empty the moment audio comes back above threshold. Progress
    is `elapsed / cap`, clamped to [0, 1]. Pulled out so unit tests can
    drive the truth table without spinning ffmpeg."""
    if s.silence_seconds <= 0:
        return None
    cap = s.silence_seconds
    elapsed = 0.0
    if (not s.paused) and s.silence_armed and (s.silence_since is not None):
        elapsed = max(0.0, now - s.silence_since)
    return {
        "armed":           bool(s.silence_armed),
        "elapsed_seconds": elapsed,
        "cap_seconds":     cap,
        "progress":        min(1.0, elapsed / cap) if cap else 0.0,
    }


def _watch_session(sid: str) -> None:
    """One thread per recording session — polls every ~500 ms for any
    finalize trigger: a self-exited ffmpeg (crash, kill -9, upstream
    died), an elapsed duration cap, or an accumulated run of silent
    chunks long enough to auto-stop-on-silence. The thread also serves
    as the canonical reaper for its child, so stop_recording's wait()
    doesn't race the watcher.

    Owning both the duration cap AND the silence detector here (vs
    ffmpeg's `-t` or a separate timer) is what lets the user edit the
    cap mid-recording and lets silence accumulate in step with the
    same poll cadence — `session.duration` / `session.silence_since`
    are plain Python fields the sink and endpoints mutate, and the next
    tick picks up the new values."""
    s = sessions.get(sid)
    if not s:
        return
    proc = s.proc
    # Latches true the first tick the upstream peak clears the floor — read
    # from the always-on upstream VU peaks, so this never touches the sink
    # hot path and works even when auto-stop-on-silence is disabled.
    signal_seen = False
    while True:
        try:
            proc.wait(timeout=_WATCH_TICK_SECONDS)
            break  # ffmpeg exited — fall through to _classify_exit
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            break
        live = sessions.get(sid)
        if live is None:
            # user-stop / disconnect finalized us between ticks
            return
        now = time.monotonic()
        if max(upstream.peak_l or 0.0, upstream.peak_r or 0.0) > _NO_SIGNAL_PEAK_FLOOR:
            signal_seen = True
        if _should_warn_no_signal(signal_seen, now - live.start_time,
                                  live.paused, live.no_signal_warned):
            live.no_signal_warned = True
            bus.log(
                "⚠ Recording, but no audio detected — check the turntable / input",
                "err",
            )
            bus.publish({"type": "record", "event": "no_signal",
                         "session_id": sid})
        # Surface the silence-countdown for the UI to render a progress
        # bar that fills as silence accumulates toward the auto-stop
        # threshold. Emitted every tick (~2 Hz) so the bar moves with the
        # watcher; CSS transition on the receiving end smooths the steps.
        # When auto-stop is off (silence_seconds == 0) the helper returns
        # None and we skip the publish so idle WS traffic stays low.
        sp = _silence_progress_payload(live, now)
        if sp is not None:
            bus.publish({"type": "silence", "session_id": sid, **sp})
        # Order matters only for the log line — both triggers route into
        # the same _finalize_session(sid, "auto") call site. Duration is
        # checked first so the user-visible reason "duration cap" wins
        # when a recording happens to end at silence at exactly the cap
        # boundary (rare; the cap is the more authoritative signal).
        if _duration_cap_reached(live, now):
            bus.log(f"⏱ Auto-stop: duration cap {live.duration}s reached",
                    "info")
            try:
                _finalize_session(sid, "auto")
            except Exception:
                sessions.remove(sid)
            return
        if _silence_should_autostop(live, now):
            bus.log(
                f"⏱ Auto-stop: {live.silence_seconds}s of silence",
                "info",
            )
            try:
                _finalize_session(sid, "auto")
            except Exception:
                sessions.remove(sid)
            return
    s = sessions.get(sid)
    if not s:
        # User-stop already finalized us before ffmpeg exited.
        return
    reason = _classify_exit(s)
    try:
        _finalize_session(sid, reason)
    except Exception:
        sessions.remove(sid)


def start_watcher() -> None:
    """Kept for compatibility with the main.py startup hook. The legacy
    polling watcher has been replaced by per-session blocking waiters
    spawned from `start_recording`, so this is now a no-op."""


def stop_watcher() -> None:
    """No-op — see `start_watcher`. Per-session threads are daemons that
    die when the process does; nothing to clean up here."""


@router.get("/api/log/{session_id}")
async def get_log(session_id: str):
    lines = sessions.get_log_lines(session_id)
    log_path = sessions.get_log_path(session_id)
    if log_path and Path(log_path).exists():
        try:
            with open(log_path, "rb") as f:
                content = f.read().decode("utf-8", errors="replace")
            ffmpeg_tail = content.splitlines()[-100:]
            if ffmpeg_tail:
                lines.append("── ffmpeg ──")
                lines.extend(ffmpeg_tail)
        except Exception:
            pass
    return {"lines": lines}


@router.get("/api/recordings")
async def get_recordings():
    fmt = upstream.fmt if upstream.configured else None
    return {
        "files":            list_recordings(),
        "disk_free_gb":     disk_free_gb(),
        "headroom_minutes": recording_headroom_minutes(fmt),
    }


@router.post("/api/recordings/{filename}/rename")
async def rename_recording(filename: str, req: RenameRequest):
    """Rename a raw side. Albums never appear here — those are managed
    by /api/album endpoints keyed on album_id."""
    src = find_side(filename)
    if not src:
        raise HTTPException(404)
    stem = safe_name(req.new_name).strip()
    if not stem:
        raise HTTPException(400, "name cannot be empty")
    target = src.parent / f"{stem}.flac"
    if target.exists() and target.resolve() != src.resolve():
        raise HTTPException(409, "a file with that name already exists")
    src.rename(target)
    return {"filename": target.name}


def _sweep_trash() -> None:
    """Purge any entries in TRASH_DIR older than TRASH_TTL_SECONDS.

    Called opportunistically from the soft-delete / restore endpoints
    so we don't need a background thread. Failure to remove individual
    files is swallowed — they'll get another shot on the next sweep."""
    if not TRASH_DIR.exists():
        return
    now = time.time()
    for entry in TRASH_DIR.iterdir():
        try:
            age = now - entry.stat().st_mtime
        except FileNotFoundError:
            continue
        if age <= TRASH_TTL_SECONDS:
            continue
        try:
            if entry.is_file():
                entry.unlink()
            # We never recursively delete subdirs from here — the trash
            # only stores top-level FLACs today; defensive in case the
            # contract widens later.
        except OSError:
            pass


def _trash_token_for(filename: str) -> str:
    """Build the on-disk trash filename for `filename`. Includes the
    original name + a unique token so concurrent soft-deletes of the
    same logical name don't clobber each other.

    The token shape `{stem}__trash_{uuid}.flac` is read back by the
    restore endpoint to recover the original name."""
    p = Path(filename)
    token = uuid.uuid4().hex[:8]
    return f"{p.stem}__trash_{token}{p.suffix}"


def _restore_target_for(trash_name: str) -> Optional[str]:
    """Inverse of `_trash_token_for`: given a trash filename, derive the
    original name we'd restore it as. Returns None for filenames that
    don't carry the `__trash_<token>` marker (i.e. anything we didn't
    place there ourselves)."""
    m = re.match(r"^(.+)__trash_[0-9a-f]+(\.[^.]+)$", trash_name)
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}"


@router.delete("/api/recordings/{filename}")
async def delete_recording(filename: str):
    """Soft-delete a raw side: move it to TRASH_DIR and return a token
    the client can pass to `/restore` for ~5 s of undo. Entries are
    purged automatically after TRASH_TTL_SECONDS (300 s).

    The Undo UX in the library replaced the legacy `confirm()` dialog —
    immediate delete with a 5 s undo window is faster than a blocking
    modal for the common case, and safer than the old unrecoverable
    `path.unlink()`."""
    path = find_side(filename)
    if not path:
        raise HTTPException(404)
    _sweep_trash()
    trash_name = _trash_token_for(filename)
    trash_path = TRASH_DIR / trash_name
    try:
        path.rename(trash_path)
    except OSError as e:
        # Rename across filesystems is rare here (TRASH_DIR is under
        # OUTPUT_DIR alongside raw/), but fall back to a hard delete if
        # something exotic prevents the move. The old behaviour was a
        # hard delete anyway, so this is no worse.
        try:
            path.unlink()
        except OSError:
            raise HTTPException(500, f"could not move to trash: {e}")
        return {"trash_token": None}
    return {"trash_token": trash_name, "original": filename, "ttl_seconds": TRASH_TTL_SECONDS}


@router.post("/api/recordings/restore")
async def restore_recording(req: dict):
    """Restore a soft-deleted recording from `TRASH_DIR`. The body
    carries the `trash_token` returned by the DELETE endpoint.

    422 if the body is malformed, 404 if the token is unknown
    (already swept or never existed), 409 if a file with the
    original name has reappeared in raw/ in the meantime."""
    token = (req or {}).get("trash_token")
    if not isinstance(token, str) or not token:
        raise HTTPException(422, "trash_token is required")
    # Guard the same way find_side does — token is supposed to be a
    # filename we generated, but never trust client input.
    if "/" in token or "\\" in token or ".." in token:
        raise HTTPException(400, "invalid trash_token")
    _sweep_trash()
    trash_path = TRASH_DIR / token
    if not trash_path.exists():
        raise HTTPException(404, "trash entry not found (expired or unknown)")
    target_name = _restore_target_for(token)
    if not target_name:
        raise HTTPException(400, "trash_token has unexpected shape")
    target = RAW_DIR / target_name
    if target.exists():
        raise HTTPException(409, "a file with that name already exists")
    try:
        trash_path.rename(target)
    except OSError as e:
        raise HTTPException(500, f"could not restore: {e}")
    return {"filename": target_name}


@router.post("/api/recordings/bulk-delete")
async def bulk_delete(req: BulkDelete):
    """Bulk delete raw recordings. Currently hard-deletes (no per-item
    undo) — the UI shows a single 'Deleted N file(s)' toast on success.

    A future iteration could route every entry through the trash so a
    bulk delete is also undoable, but the storage cost (full copy of N
    FLACs for up to 5 min) and the UI complexity of restoring N items
    via one Undo button aren't worth it for the current usage shape."""
    deleted, missing = [], []
    for fn in req.filenames:
        p = find_side(fn)
        if p:
            try:
                p.unlink()
                deleted.append(fn)
            except Exception:
                missing.append(fn)
        else:
            missing.append(fn)
    return {"deleted": deleted, "missing": missing}


@router.get("/api/download/{filename}")
async def download(filename: str):
    """Download a raw side by filename. Album-level downloads (tracks) go
    through `/api/album/{album_id}/track/{trackname}` in albums.py."""
    path = find_side(filename)
    if not path:
        raise HTTPException(404)
    return FileResponse(str(path), media_type="audio/flac", filename=filename)
