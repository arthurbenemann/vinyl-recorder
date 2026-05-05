"""Crash-recovery e2e: kill the upstream mid-recording, verify the
recorder reaps the session cleanly.

The path under test:

  upstream ffmpeg dies (test-streams container stopped)
    -> reader thread sees EOF, drops subscribers, flips state to disconnected
    -> recording's ffmpeg sees stdin EOF (or BrokenPipe) and exits
    -> watcher thread (1 Hz) sees `proc.poll() != None` for the active session
    -> _finalize_session() reason="auto" or "crash" depending on exit shape
    -> /api/status no longer carries the session
    -> /api/recordings shows the (truncated) FLAC on disk

A regression in any link of that chain is a "session shows recording
forever" UX nightmare — exactly the kind we'd rather catch in CI than
during a Sunday-afternoon listening session.

This test mutates the compose stack (stops `test-streams`); it restores
it in the cleanup so subsequent tests in the session see a healthy
stack again.
"""
import json
import subprocess
import time

import pytest

from .conftest import (
    RECORDER_URL,
    STREAM_URL,
    compose,
    http_json,
    wait_for_upstream_connected,
)

pytestmark = pytest.mark.e2e


def _wait_for_session_reaped(sid: str, timeout: float = 30.0) -> dict:
    """Poll /api/status until the watcher has finalized the session."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = http_json(f"{RECORDER_URL}/api/status", timeout=3)
        active_ids = {s["id"] for s in last.get("sessions", [])}
        if sid not in active_ids:
            return last
        time.sleep(0.5)
    raise AssertionError(
        f"session {sid} still active after {timeout:.0f} s. last: {last!r}"
    )


def test_kill_upstream_mid_recording_reaps_session(stack):
    untagged = stack["untagged"]

    # Confirm we're starting from a healthy connected stack — the session
    # fixture set this up, but a previous test in this module could have
    # left a stale session around.
    pre = http_json(f"{RECORDER_URL}/api/status")
    assert pre["upstream"]["connected"] is True
    assert pre["sessions"] == [], f"unexpected leftover sessions: {pre['sessions']}"

    pre_files = set(untagged.glob("*.flac")) if untagged.exists() else set()

    started = http_json(
        f"{RECORDER_URL}/api/record/start", method="POST",
        body={
            "stream_url": STREAM_URL,
            "artist": "e2e", "album": "crash", "year": "2026",
            "duration": 0,
        },
    )
    sid = started["session_id"]
    fname = started["filename"]

    # Let a few seconds of audio accumulate so the FLAC has actual content.
    time.sleep(3)

    try:
        # Simulate upstream death. `kill` (vs `stop`) sends SIGKILL with no
        # graceful shutdown window — closer to a network drop / Pi reboot
        # than `stop`'s 10 s SIGTERM grace. The container ends up "exited"
        # rather than "stopped", which `start` handles either way.
        r = compose("kill", "test-streams", timeout=30)
        assert r.returncode == 0, f"compose kill failed: {r.stderr}"

        # Watcher polls at 1 Hz; budget 30 s for the cascade (upstream EOF
        # propagates -> ffmpeg flushes -> watcher sees exit -> finalize).
        post = _wait_for_session_reaped(sid, timeout=30)
        assert post["recording"] is False
        assert post["sessions"] == []
        # Upstream itself should now report disconnected.
        assert post["upstream"]["connected"] is False, \
            "upstream stayed connected after the source died"

        # FLAC should exist on disk — could be a clean auto-stop or a
        # truncated crash file. Either way, must be non-empty and parseable.
        fpath = untagged / fname
        assert fpath.exists(), f"FLAC missing at {fpath}"
        assert fpath not in pre_files
        assert fpath.stat().st_size > 0, "FLAC is 0 bytes — finalize didn't flush"

        info = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(fpath)],
            text=True,
        ))
        s = info["streams"][0]
        assert s["codec_name"] == "flac"
        # ~3 s of recording before kill + watcher latency. The upper bound is
        # only a sanity check; widen it to absorb GHA jitter (kill itself is
        # instant but the watcher tick + ffmpeg flush can take a few seconds).
        duration = float(info["format"].get("duration", 0))
        assert 1.0 <= duration <= 30.0, f"unexpected FLAC duration: {duration}"

        # The status payload should advertise the recording in the library.
        recordings = http_json(f"{RECORDER_URL}/api/recordings")["files"]
        assert any(f["filename"] == fname for f in recordings), \
            f"crashed recording not in library: {[f['filename'] for f in recordings]}"

    finally:
        # Always restore the stack so other tests in the session can run.
        # Bring test-streams back, wait for it to be healthy, then trigger
        # a server-side reconnect so the upstream is live again.
        compose("start", "test-streams", timeout=30)

        # Wait for the test-streams healthcheck to flip to "healthy".
        deadline = time.time() + 30
        while time.time() < deadline:
            inspect = subprocess.run(
                ["docker", "inspect",
                 "--format={{.State.Health.Status}}",
                 "vinyl-test-streams"],
                capture_output=True, text=True,
            )
            if inspect.stdout.strip() == "healthy":
                break
            time.sleep(1)

        # Reconnect the recorder's upstream — the recorder doesn't auto-
        # retry, that's a separate (out-of-scope here) feature.
        try:
            http_json(
                f"{RECORDER_URL}/api/connect", method="POST",
                body={"stream_url": STREAM_URL},
            )
        except Exception:
            pass  # Best-effort; wait_for_upstream_connected will surface it.
        wait_for_upstream_connected(timeout=30)
