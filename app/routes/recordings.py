"""Recording sessions, library file ops, stream proxy + probe."""
import asyncio
import json
import queue
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from services.eventbus import bus
from services.ffmpeg import (
    LOW_SPACE_GB, disk_free_gb, disk_space_error, find_side, list_recordings,
    safe_name,
)
from state import (
    BulkDelete, LOG_DIR, RAW_DIR, RecordRequest, RenameRequest, sessions,
    upstream,
)

router = APIRouter()


def _graceful_close(proc, timeout: float = 2.0) -> None:
    """Best-effort graceful teardown of a Popen child: close stdin so the
    child sees EOF (lets ffmpeg flush its trailers), terminate, wait up
    to `timeout`, and only kill if wait timed out / failed. Each step is
    individually exception-safe so a half-dead proc (already closed pipe,
    already exited) never raises out."""
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


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


def _teardown_proxy(proc: subprocess.Popen, sub_id: str) -> None:
    """Tear down a stream-proxy ffmpeg + its upstream subscription.

    Order matters and is enforced here by code structure (see
    test_proxy_teardown_kills_before_unsubscribe in
    tests/unit/test_upstream_unit.py): the subscriber's worker thread
    may be blocked inside `proc.stdin.write/flush` waiting for ffmpeg
    to drain its stdin pipe, holding the BufferedWriter `_write_lock`.
    If we went unsubscribe → `_on_close` → `proc.stdin.close` from this
    HTTP worker thread, close would try to acquire the same `_write_lock`
    and deadlock. Killing ffmpeg first breaks the worker out of its
    blocked write with BrokenPipeError, releasing `_write_lock`; the
    subsequent unsubscribe + close then run cleanly."""
    if proc.poll() is None:
        try: proc.kill()
        except Exception: pass
    upstream.unsubscribe(sub_id)
    _reap(proc)


# Background reaper for proxy ffmpegs. A single dedicated thread waits on
# whatever procs the request handlers hand off, so HTTP workers never block
# on cleanup. Daemon -> dies with the process.
_reap_q: "queue.Queue[subprocess.Popen]" = queue.Queue()


def _reap(proc: subprocess.Popen) -> None:
    """Schedule `proc` for asynchronous cleanup and waitpid."""
    try:
        _reap_q.put_nowait(proc)
    except queue.Full:
        # Should never happen — queue is unbounded — but if it does, do an
        # in-line best-effort kill rather than leak the process.
        try: proc.kill()
        except Exception: pass


def _reaper_loop() -> None:
    while True:
        proc = _reap_q.get()
        if proc.poll() is None:
            _graceful_close(proc, timeout=2.0)
            # `_graceful_close` does not wait after kill; do one more wait
            # here so a kill-9'd child gets reaped instead of lingering as
            # a zombie until process exit.
            if proc.poll() is None:
                try: proc.wait(timeout=2)
                except Exception: pass


threading.Thread(target=_reaper_loop, daemon=True, name="proxy-reaper").start()


@router.post("/api/test-stream")
async def test_stream(body: dict):
    """Probe an upstream URL with ffprobe and surface its stream parameters.
    Failure modes (ffprobe nonzero, timeout, OSError) raise 502 with the
    underlying message in `detail` — clients rely on HTTP status to branch."""
    url = body.get("stream_url", "")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-i", url],
            capture_output=True, text=True, timeout=10
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

    The recording's ffmpeg reads raw PCM from stdin (matching the upstream's
    sample_format / rate / channels) and encodes lossless FLAC. Recording
    only requires that something is connected — the `stream_url` field on
    the request is ignored if it disagrees with the active upstream, since
    that's the only stream we're actually pulling.
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
    # Offloaded to a worker thread (spawn does sync probe + subprocess
    # startup); see stream-proxy comment above for the lockup details.
    rec_hold = await asyncio.to_thread(upstream.acquire, f"record:{sid}")
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

    cmd = [
        "ffmpeg", "-y",
        # Input is raw PCM piped from the upstream reader thread. Telling
        # ffmpeg the format up front saves it from probing stdin.
        "-f", sample_format,
        "-ar", str(fmt["sample_rate"]),
        "-ac", str(fmt["channels"]),
        "-i", "pipe:0",
    ]
    if req.duration > 0:
        cmd += ["-t", str(req.duration)]
    cmd += [
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
        log_lines=[f"▶ Started recording → {fname}"],
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
        end_time = s.pause_started if s.paused else time.monotonic()
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


def _classify_exit(s) -> str:
    """Decide whether a self-exited ffmpeg counts as an auto-stop or a
    crash. Used by the per-session watcher and any explicit reaper."""
    outfile = Path(s.outfile)
    duration = s.duration
    elapsed = time.monotonic() - s.start_time
    if duration > 0 and elapsed >= duration - 1 and outfile.exists() and outfile.stat().st_size > 0:
        return "auto"
    if outfile.exists() and outfile.stat().st_size > 0 and s.proc.returncode == 0:
        return "auto"
    return "crash"


def _watch_session(sid: str) -> None:
    """One thread per recording session — `proc.wait()` blocks in the OS
    until ffmpeg exits, so the reap is event-driven (≤ a few ms) instead
    of polled at 1 Hz. The thread also serves as the canonical reaper for
    its child, so stop_recording's wait() doesn't race the watcher."""
    s = sessions.get(sid)
    if not s:
        return
    try:
        s.proc.wait()
    except Exception:
        pass
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
    return {"files": list_recordings(), "disk_free_gb": disk_free_gb()}


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


@router.delete("/api/recordings/{filename}")
async def delete_recording(filename: str):
    path = find_side(filename)
    if not path:
        raise HTTPException(404)
    path.unlink()
    return {}


@router.post("/api/recordings/bulk-delete")
async def bulk_delete(req: BulkDelete):
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
