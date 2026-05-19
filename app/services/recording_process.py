"""Process-lifecycle helpers for recording/stream-proxy ffmpeg children.

This module owns the "how do we shut down a Popen child cleanly" logic that
was historically tangled into `app/routes/recordings.py`:

  * `graceful_close` — flush stdin → SIGTERM → wait → SIGKILL escalation
  * `reap`           — hand a Popen off to the background reaper thread
  * `teardown_proxy` — kill ffmpeg first, THEN unsubscribe from upstream
                       (order matters: see docstring for the deadlock
                       window the reverse order opens)

A single daemon thread (`proxy-reaper`) drains the reaper queue so HTTP
worker threads never block waiting for `proc.wait()`. The thread is
spawned at import time and is process-lived; nothing here ever needs
explicit teardown (daemon=True dies with the process).

The only external dependency is `services.upstream.upstream.unsubscribe`,
which `teardown_proxy` calls after killing ffmpeg — and it's looked up
lazily inside the function so the import graph stays one-way
(routes → services → state, never services → routes).
"""
from __future__ import annotations

import logging
import queue
import subprocess
import threading


_log = logging.getLogger(__name__)


def graceful_close(proc: subprocess.Popen, timeout: float = 2.0) -> None:
    """Best-effort graceful teardown of a Popen child.

    Closes stdin so the child sees EOF (lets ffmpeg flush its trailers),
    sends SIGTERM, waits up to `timeout`, and only kills if wait timed
    out / failed. Each step is individually exception-safe so a
    half-dead proc (already closed pipe, already exited) never raises
    out — this is called from background reaper threads where an
    uncaught exception would silently kill the reaper and leak every
    subsequent child.
    """
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


# Background reaper for proxy ffmpegs. A single dedicated thread waits on
# whatever procs the request handlers hand off, so HTTP workers never block
# on cleanup. Daemon → dies with the process.
_reap_q: "queue.Queue[subprocess.Popen]" = queue.Queue()


def reap(proc: subprocess.Popen) -> None:
    """Schedule `proc` for asynchronous cleanup and waitpid."""
    try:
        _reap_q.put_nowait(proc)
    except queue.Full:
        # Should never happen — queue is unbounded — but if it does, do an
        # in-line best-effort kill rather than leak the process.
        try:
            proc.kill()
        except Exception:
            pass


def _reaper_loop() -> None:
    while True:
        proc = _reap_q.get()
        if proc.poll() is None:
            graceful_close(proc, timeout=2.0)
            # `graceful_close` does not wait after kill; do one more wait
            # here so a kill-9'd child gets reaped instead of lingering as
            # a zombie until process exit.
            if proc.poll() is None:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass


def teardown_proxy(proc: subprocess.Popen, sub_id: str) -> None:
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
    subsequent unsubscribe + close then run cleanly.
    """
    # Lazy import: services.upstream pulls in the state module which has a
    # hefty import-time cost (mkdirs OUTPUT_DIR, etc.); deferring keeps
    # `from services.recording_process import …` cheap for tests that
    # don't need the full app surface.
    from state import upstream

    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    upstream.unsubscribe(sub_id)
    reap(proc)


# Module-level daemon thread that drains the reap queue. Spawned at import
# time so any subsequent `reap(proc)` call has a consumer. Named for
# easy identification in `ps -L` / py-spy.
threading.Thread(
    target=_reaper_loop,
    daemon=True,
    name="proxy-reaper",
).start()
