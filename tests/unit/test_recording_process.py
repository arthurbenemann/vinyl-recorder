"""Unit tests for services.recording_process.

Covers the process-lifecycle helpers extracted from `app/routes/recordings.py`:
graceful_close (terminate → wait → kill escalation), reap (handoff to the
daemon reaper queue), and teardown_proxy (kill-before-unsubscribe ordering
already pinned in test_upstream_unit; here we cover the no-op-when-dead
fast path and the kill-then-reap-then-unsubscribe sequence end-to-end with
a fake Popen).
"""
import subprocess
import threading

from services import recording_process
from state import upstream as upstream_state


# ── graceful_close: termination escalation ───────────────────────────────
class _FakePopen:
    """Minimal Popen lookalike for unit-testing graceful_close.

    Records every call so the test can assert the escalation order:
    stdin.close → terminate → wait → kill. Each method can be configured
    to raise or to delay so we can drive every branch.
    """

    def __init__(self, *, wait_timeout: bool = False,
                 already_dead: bool = False,
                 raise_on: tuple[str, ...] = ()):
        self.calls: list[str] = []
        self._wait_timeout = wait_timeout
        self._raise_on = set(raise_on)
        self._alive = not already_dead
        self.stdin = _FakeStdin(self)

    def _maybe_raise(self, name: str):
        if name in self._raise_on:
            raise OSError(f"simulated failure in {name}")

    def terminate(self):
        self.calls.append("terminate")
        self._maybe_raise("terminate")
        if not self._wait_timeout:
            self._alive = False

    def wait(self, timeout: float | None = None):
        self.calls.append(f"wait(timeout={timeout})")
        self._maybe_raise("wait")
        if self._wait_timeout:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return 0

    def kill(self):
        self.calls.append("kill")
        self._maybe_raise("kill")
        self._alive = False

    def poll(self):
        return None if self._alive else 0


class _FakeStdin:
    def __init__(self, parent: _FakePopen):
        self._parent = parent
        self.closed = False

    def close(self):
        self._parent.calls.append("stdin.close")
        if "stdin_close" in self._parent._raise_on:
            raise OSError("stdin close")
        self.closed = True


def test_graceful_close_happy_path_terminates_and_waits():
    """terminate() succeeds and wait() returns cleanly → no kill needed."""
    proc = _FakePopen()
    recording_process.graceful_close(proc, timeout=0.1)
    assert proc.calls == ["stdin.close", "terminate", "wait(timeout=0.1)"]


def test_graceful_close_escalates_to_kill_on_timeout():
    """terminate() returns but wait() times out → escalate to kill().

    This is the SIGTERM-ignored / hung-child path: a well-behaved ffmpeg
    flushes on SIGTERM, but a wedged one needs SIGKILL.
    """
    proc = _FakePopen(wait_timeout=True)
    recording_process.graceful_close(proc, timeout=0.05)
    assert "kill" in proc.calls
    # kill must come AFTER the failed wait — otherwise we'd be SIGKILLing
    # a process that might still have been about to flush cleanly.
    assert proc.calls.index("kill") > proc.calls.index("wait(timeout=0.05)")


def test_graceful_close_already_dead_proc_does_not_raise():
    """Closing a process that's already exited is a no-op chain — every
    step swallows its own exception, and the function returns cleanly so
    the background reaper thread keeps draining its queue.
    """
    # Configure every call to raise: simulates the worst case where stdin
    # is already closed (raises), terminate races with a self-exit
    # (raises), and wait completes with no exception. graceful_close MUST
    # NOT propagate any of these.
    proc = _FakePopen(raise_on=("stdin_close", "terminate"))
    recording_process.graceful_close(proc, timeout=0.05)
    # All four steps attempted (stdin.close, terminate, wait, kill).
    assert "terminate" in proc.calls
    assert "stdin.close" in proc.calls


def test_graceful_close_no_stdin_skips_close():
    """A Popen with stdin=None (rare; subprocess.Popen without PIPE) must
    not crash trying to close it."""
    proc = _FakePopen()
    proc.stdin = None
    recording_process.graceful_close(proc, timeout=0.05)
    # Skipped stdin.close, went straight to terminate + wait.
    assert "stdin.close" not in proc.calls
    assert "terminate" in proc.calls


def test_graceful_close_swallows_terminate_failure():
    """terminate() raising (already-dead child, OSError on Windows-like
    quirks, …) must not stop the wait/kill from running."""
    proc = _FakePopen(raise_on=("terminate",))
    recording_process.graceful_close(proc, timeout=0.05)
    # We still went on to wait().
    assert any(c.startswith("wait") for c in proc.calls)


# ── reap: handoff to the daemon reaper queue ────────────────────────────
def test_reap_enqueues_proc_for_async_cleanup():
    """`reap(proc)` posts to the module-level reaper queue. The daemon
    thread drains it asynchronously, so we just need to observe that the
    proc made it to the queue (or, in the daemon's hands, that it was
    eventually `wait()`ed)."""
    seen = threading.Event()

    class _ObservableProc:
        def __init__(self):
            self._dead = False
        def poll(self):
            # poll() returning a returncode tells the reaper "already dead,
            # skip graceful_close". This is the simplest path to assert
            # the reaper actually picked us up without spawning ffmpeg.
            seen.set()
            return 0

    recording_process.reap(_ObservableProc())
    # The daemon reaper thread is spawned at import time; it should pick
    # this up within milliseconds.
    assert seen.wait(timeout=2.0), (
        "daemon reaper did not poll() the proc within 2 s — "
        "the proxy-reaper thread may have failed to start"
    )


def test_reap_full_queue_falls_back_to_inline_kill(monkeypatch):
    """If the unbounded queue somehow refuses a put (queue.Full), reap()
    falls back to an in-line proc.kill() so we don't leak the child.
    Simulated by monkey-patching the queue's put_nowait to raise."""
    import queue as _queue

    killed = threading.Event()

    class _Proc:
        def poll(self):
            return None
        def kill(self):
            killed.set()

    def _raise_full(_item):
        raise _queue.Full

    monkeypatch.setattr(recording_process._reap_q, "put_nowait", _raise_full)
    recording_process.reap(_Proc())
    assert killed.is_set(), "expected fallback proc.kill() on queue.Full"


def test_reap_full_queue_swallows_kill_failure(monkeypatch):
    """Defensive: if the queue is full AND the fallback kill() raises
    (process already dead, OSError, etc.), reap() must not propagate —
    the caller is on an HTTP path and a raised exception here would
    become a 500."""
    import queue as _queue

    class _Proc:
        def poll(self):
            return None
        def kill(self):
            raise OSError("already dead")

    monkeypatch.setattr(recording_process._reap_q, "put_nowait",
                        lambda _i: (_ for _ in ()).throw(_queue.Full))
    # Must not raise.
    recording_process.reap(_Proc())


# ── teardown_proxy: kill → unsubscribe → reap ordering ──────────────────
def test_teardown_proxy_kills_unsubscribes_then_reaps(monkeypatch):
    """End-to-end ordering with a stand-in upstream + reap queue.

    Order must be: kill ffmpeg first (so the subscriber's worker thread
    unblocks from `stdin.write`), THEN unsubscribe (which closes stdin via
    on_close — now safe because no other thread holds the write lock),
    THEN hand the proc off to the reaper.
    """
    calls: list[str] = []

    class _LiveProc:
        def __init__(self):
            self._dead = False
        def poll(self):
            return 0 if self._dead else None
        def kill(self):
            calls.append("kill")
            self._dead = True

    monkeypatch.setattr(upstream_state, "unsubscribe",
                        lambda name: calls.append(f"unsub:{name}"))
    monkeypatch.setattr(recording_process, "reap",
                        lambda p: calls.append("reap"))

    recording_process.teardown_proxy(_LiveProc(), "proxy-1")
    assert calls == ["kill", "unsub:proxy-1", "reap"]


def test_teardown_proxy_already_dead_skips_kill(monkeypatch):
    """If ffmpeg has already exited (returncode set), don't bother sending
    another signal — proc.poll() reports a returncode and we go straight
    to unsubscribe + reap. Matches the regression pinned in
    test_proxy_teardown_skips_kill_if_already_dead.
    """
    calls: list[str] = []

    class _DeadProc:
        def poll(self):
            return 0
        def kill(self):  # pragma: no cover - must not be called
            calls.append("kill")

    monkeypatch.setattr(upstream_state, "unsubscribe",
                        lambda name: calls.append(f"unsub:{name}"))
    monkeypatch.setattr(recording_process, "reap",
                        lambda p: calls.append("reap"))

    recording_process.teardown_proxy(_DeadProc(), "proxy-2")
    assert "kill" not in calls
    assert calls == ["unsub:proxy-2", "reap"]


def test_teardown_proxy_swallows_kill_failure(monkeypatch):
    """If proc.kill() raises (process exited between poll and kill), the
    teardown must still unsubscribe and reap — leaving a stale upstream
    subscription would block a future subscribe with the same id."""
    calls: list[str] = []

    class _RaceProc:
        def poll(self):
            return None  # appears alive at first…
        def kill(self):
            raise OSError("won the race")

    monkeypatch.setattr(upstream_state, "unsubscribe",
                        lambda name: calls.append(f"unsub:{name}"))
    monkeypatch.setattr(recording_process, "reap",
                        lambda p: calls.append("reap"))

    recording_process.teardown_proxy(_RaceProc(), "proxy-3")
    assert calls == ["unsub:proxy-3", "reap"]


# ── Reaper thread is alive and named ─────────────────────────────────────
def test_proxy_reaper_thread_is_running():
    """Sanity check on the module-level daemon thread spawn. If this
    regresses (e.g. someone moves the .start() call behind a flag) the
    reap queue silently fills up and child ffmpegs become zombies."""
    names = {t.name for t in threading.enumerate() if t.is_alive()}
    assert "proxy-reaper" in names, (
        f"proxy-reaper daemon thread not running; live threads: {names}"
    )


# ── Backwards-compat: routes.recordings still re-exports the helpers ────
def test_recordings_module_reexports_helpers():
    """`app/routes/recordings.py` imports the helpers under their original
    underscore-prefixed names so route handlers (and any external monkey-
    patching in tests) keep working. Pin the surface so a future cleanup
    doesn't quietly remove an alias something else depended on."""
    from routes import recordings
    assert recordings._graceful_close is recording_process.graceful_close
    assert recordings._reap is recording_process.reap
    assert recordings._teardown_proxy is recording_process.teardown_proxy
