"""Auto-stop e2e: ffmpeg's `-t N` exits naturally at N seconds; the
watcher (1 Hz) should detect this and finalize the session with
reason="auto" — distinct from the crash path covered in
test_crash_recovery.py.

This is one of PR #36's unchecked "60-second duration limit" test items.
We use a 5 s duration here instead of 60 — same code path, faster CI.
"""
import time

import pytest

from .conftest import RECORDER_URL, STREAM_URL, ffprobe, http_json

pytestmark = pytest.mark.e2e


def _wait_for_session_reaped(sid: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = http_json(f"{RECORDER_URL}/api/status", timeout=3)
        if not any(s["id"] == sid for s in last.get("sessions", [])):
            return last
        time.sleep(0.5)
    raise AssertionError(
        f"session {sid} still active after {timeout:.0f} s. last: {last!r}"
    )


def test_duration_limit_auto_stops(stack):
    untagged = stack["untagged"]
    pre = http_json(f"{RECORDER_URL}/api/status")
    assert pre["upstream"]["connected"] is True
    assert pre["sessions"] == [], f"unexpected leftover sessions: {pre['sessions']}"

    started = http_json(
        f"{RECORDER_URL}/api/record/start", method="POST",
        body={
            "stream_url": STREAM_URL,
            "artist": "e2e", "album": "auto-stop", "year": "2026",
            # ffmpeg `-t 3` exits cleanly after 3 s; watcher reaps and the
            # finalize path tags the session as reason="auto".
            "duration": 3,
        },
    )
    sid = started["session_id"]
    fname = started["filename"]

    # 3 s record + ~1 s ffmpeg flush + 1 Hz watcher tick → 5 s should be
    # plenty; budget 20 s to absorb GHA jitter.
    after = _wait_for_session_reaped(sid, timeout=20)
    assert after["recording"] is False
    assert after["sessions"] == []
    # Upstream should remain connected — this isn't a crash, just a clean exit.
    assert after["upstream"]["connected"] is True, \
        "upstream disconnected after a clean auto-stop"

    fpath = untagged / fname
    assert fpath.exists(), f"FLAC missing at {fpath}"
    assert fpath.stat().st_size > 0

    info = ffprobe(fpath)
    assert info["streams"][0]["codec_name"] == "flac"
    duration = float(info["format"]["duration"])
    # ffmpeg's `-t 3` produces ~3 s; allow some slack on slow runners but
    # reject anything that strays too far in either direction.
    assert 2.5 <= duration <= 5.0, f"unexpected duration: {duration}"
