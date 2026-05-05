"""End-to-end progress-bar lifecycle test.

Exercises the full path that the in-process unit tests can't reach:

  POST /api/album/detect-silences {job_id}
    -> route calls start_job
    -> ffmpeg subprocess writes -progress events; reader thread parses
       and calls update_job
    -> route returns + finish_job
    -> GET /api/jobs/{id} returns done=true progress=1.0

Records a short clip from /album (which has built-in silences), promotes
it into albums/, then runs silence detection + measure with a job_id
attached. After each call returns, the registry must show the job as
done. This covers most of the "still manual" backend items from the
test plan on PR #41.
"""
import time

import pytest

from .conftest import RECORDER_URL, STREAM_URL, http_json

pytestmark = pytest.mark.e2e


def _record_clip(seconds: int, album: str) -> str:
    """Record `seconds` of /album, return the resulting filename in untagged/."""
    started = http_json(
        f"{RECORDER_URL}/api/record/start", method="POST",
        body={
            "stream_url": STREAM_URL,
            "artist": "e2e", "album": album, "year": "2026",
            "duration": 0,
        },
    )
    sid = started["session_id"]
    fname = started["filename"]
    time.sleep(seconds)
    http_json(f"{RECORDER_URL}/api/record/stop/{sid}", method="POST")
    return fname


def _promote(filename: str, album: str) -> str:
    """Promote a recording in untagged/ → albums/. Returns the album filename."""
    body = http_json(
        f"{RECORDER_URL}/api/promote", method="POST",
        body={
            "filename": filename,
            "album": {
                "artist": "e2e", "album": album, "year": "2026",
            },
        },
    )
    return body["filename"]


def test_silence_detect_progress_lifecycle(stack):
    """Real ffmpeg silencedetect run on a captured clip, with progress
    reporting. Asserts the job reaches done=true with no error."""
    # /album opens with 5 s of silence + a 30 s tone — recording 10 s gives
    # silencedetect at least one interval to surface.
    rec_fname = _record_clip(seconds=10, album="progress-silence")
    album_fname = _promote(rec_fname, "progress-silence")
    job_id = "e2e-silence-1"

    body = http_json(
        f"{RECORDER_URL}/api/album/detect-silences", method="POST",
        body={
            "filename": album_fname,
            "noise_db": -40.0,
            "min_silence": 1.0,
            "job_id":     job_id,
        },
        timeout=60,
    )
    # Sanity: the call returned a normal response shape.
    assert "silences" in body

    # After the call returns the registry should show the job done.
    j = http_json(f"{RECORDER_URL}/api/jobs/{job_id}")
    assert j["done"] is True
    assert j["error"] is None
    assert j["progress"] == pytest.approx(1.0, abs=0.01)
    assert j["label"] == "detect silences"


def test_measure_progress_lifecycle(stack):
    """astats measure with job_id. Same shape as silence detect — confirms
    the progress wiring isn't endpoint-specific."""
    rec_fname = _record_clip(seconds=8, album="progress-measure")
    album_fname = _promote(rec_fname, "progress-measure")
    job_id = "e2e-measure-1"

    http_json(
        f"{RECORDER_URL}/api/album/measure", method="POST",
        body={"filename": album_fname, "job_id": job_id},
        timeout=60,
    )
    j = http_json(f"{RECORDER_URL}/api/jobs/{job_id}")
    assert j["done"] is True
    assert j["error"] is None
    assert j["progress"] == pytest.approx(1.0, abs=0.01)
    assert j["label"] == "measure"


def test_jobs_endpoint_404_on_unknown_id(stack):
    """End-to-end shape of the 404 path — the frontend reads it as
    "no progress to draw"."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{RECORDER_URL}/api/jobs/not-a-real-id")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404
