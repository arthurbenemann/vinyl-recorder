"""End-to-end smoke test: bring up the full compose stack with the
test-streams overlay, record from /loop for a few seconds, and verify
the FLAC drops cleanly into output/untagged/.

This is the only test that exercises the real ffmpeg pipeline
(test-streams ffmpeg -> upstream ffmpeg fan-out -> recording ffmpeg ->
FLAC). The compose stack itself is brought up by the session-scoped
`stack` fixture in conftest.py.
"""
import time

import pytest

from .conftest import RECORDER_URL, STREAM_URL, ffprobe, http_json

pytestmark = pytest.mark.e2e


def test_record_3s_from_loop(stack):
    """Record 3 s of /loop, stop, verify a FLAC dropped with the right format."""
    untagged = stack["untagged"]

    # Snapshot files that already existed (e.g. from a developer's run) so
    # the assertions only consider files this test created.
    pre = set(untagged.glob("*.flac")) if untagged.exists() else set()

    started = http_json(
        f"{RECORDER_URL}/api/record/start", method="POST",
        body={
            "stream_url": STREAM_URL,
            "artist": "e2e", "album": "smoke", "year": "2026",
            "duration": 0,
        },
    )
    sid = started["session_id"]
    fname = started["filename"]

    # Let ~3s of real-time audio accumulate in the FLAC.
    time.sleep(3)

    stop = http_json(
        f"{RECORDER_URL}/api/record/stop/{sid}", method="POST",
    )
    # Allow some slack for the SIGINT-flush window — anywhere in [2, 6] is fine.
    assert 2 <= stop["elapsed"] <= 6, f"unexpected duration: {stop}"
    assert stop["filename"] == fname

    # File should exist in untagged/ and be new (vs pre-snapshot).
    fpath = untagged / fname
    assert fpath.exists(), f"FLAC missing at {fpath}"
    assert fpath not in pre, "test FLAC was already there before the test ran"

    # Probe the FLAC — confirms ffmpeg actually finalized the file rather
    # than leaving a 0-byte stub on a SIGINT race.
    info = ffprobe(fpath)
    s = info["streams"][0]
    assert s["codec_name"] == "flac"
    assert int(s["sample_rate"]) == 96000
    assert int(s["channels"]) == 2
    duration = float(info["format"]["duration"])
    assert 2.0 <= duration <= 6.0, f"unexpected FLAC duration: {duration}"


def test_status_reflects_upstream_format(stack):
    """Once connected, /api/status reports the upstream's detected format —
    sanity-check that 96 kHz / 24-bit / pcm_s24le round-trips through the
    UpstreamSession state."""
    body = http_json(f"{RECORDER_URL}/api/status")
    assert body["upstream"]["connected"] is True
    fmt = body["upstream"]["format"]
    assert fmt["sample_rate"] == 96000
    assert fmt["channels"] == 2
    assert fmt["bit_depth"] == 24
    assert fmt["codec"] == "pcm_s24le"


def test_recordings_lists_the_recording(stack):
    """The FLAC produced by test_record_5s_from_loop should show up in
    /api/recordings (which scans untagged/ + tagged/ on disk)."""
    body = http_json(f"{RECORDER_URL}/api/recordings")
    files = body["files"]
    assert any(f["filename"].startswith("e2e - smoke") for f in files), \
        f"recording not in library: {[f['filename'] for f in files]}"
