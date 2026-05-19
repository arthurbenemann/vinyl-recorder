"""Unit tests for `app/services/ffmpeg.py` focused on the load-bearing
behaviour not already covered by `test_ffmpeg_helpers.py` (parsers /
path sanitizers) and `test_ffmpeg_disk_helpers.py` (disk-space, list,
find_side).

This module pins:

  - `run_ffmpeg_with_progress` process-spawn argv construction (we patch
    `subprocess.Popen` + `subprocess.run` so no real ffmpeg is spawned).
  - `run_ffmpeg_with_progress` progress-line parsing -> jobs registry.
  - `disk_free_gb` shells out to `shutil.disk_usage` correctly.
  - The mtime-keyed `_DURATION_CACHE` / `_FORMAT_CACHE` caches (cache hit
    vs cache miss; reissues metaflac when the mtime changes).
  - `TAG_KEY_MAP` -> `write_tags` mapping for every supported field.

The pure parsing helpers (`parse_silencedetect`, `parse_astats`,
`_parse_db`) and the sanitizer pair (`safe_name`,
`safe_path_component`) are exercised in `test_ffmpeg_helpers.py`. The
disk-space + list_recordings + find_side surface lives in
`test_ffmpeg_disk_helpers.py`. Don't double-cover.
"""
from pathlib import Path

import pytest

from services import ffmpeg as ffmpeg_mod
from services import jobs as jobs_mod
from services.ffmpeg import (
    TAG_KEY_MAP,
    disk_free_gb,
    run_ffmpeg_with_progress,
)


# ── shared subprocess fakes ──────────────────────────────────────────────
class _FakePopen:
    """Mimics the slice of subprocess.Popen that run_ffmpeg_with_progress uses.

    stdout: iterable of `<key>=<value>\\n` byte lines (-progress channel).
    stderr: a single bytes blob the drain thread reads with .read().
    """

    instances: list = []  # captures every Popen() invocation for assertion

    def __init__(self, argv, stdout=None, stderr=None):
        # Record args + kwargs for the test to inspect.
        type(self).instances.append({"argv": list(argv), "stdout": stdout, "stderr": stderr})
        self.returncode = 0
        # stdout: cycles through the canned progress lines on readline().
        self._stdout_lines = list(self._default_progress_lines())
        self.stdout = self
        self.stderr = self
        # stderr: a single .read() returns the full payload then EOF.
        self._stderr_payload = b""
        self._stderr_drained = False

    @staticmethod
    def _default_progress_lines():
        # Two ticks then progress=end. Both keys (out_time_us / out_time_ms)
        # are recognised by the helper.
        return [
            b"out_time_us=500000\n",   # 0.5s of 10s = 5%
            b"out_time_us=5000000\n",  # 5.0s of 10s = 50%
            b"progress=end\n",
        ]

    # stdout
    def readline(self):
        if self._stdout_lines:
            return self._stdout_lines.pop(0)
        return b""

    # stderr
    def read(self, n):
        if self._stderr_drained:
            return b""
        self._stderr_drained = True
        return self._stderr_payload

    def wait(self):
        return self.returncode


@pytest.fixture(autouse=True)
def _reset_popen_log():
    _FakePopen.instances.clear()
    # Each test starts with an empty jobs registry too.
    with jobs_mod._lock:
        jobs_mod._jobs.clear()
    yield
    _FakePopen.instances.clear()


# ── run_ffmpeg_with_progress: fallback when no job_id / no duration ──────
def test_run_ffmpeg_no_jobid_falls_back_to_blocking_run(monkeypatch):
    """Without a `job_id`, we don't need the progress channel — the helper
    drops back to a plain `subprocess.run`. This path is exercised by
    callers that don't care about the progress UI (e.g. astats probes)."""
    calls = []

    class _R:
        returncode = 7
        stderr = b"oops"
        stdout = b""

    def fake_run(argv, **kw):
        calls.append({"argv": list(argv), "kw": kw})
        return _R()

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    rc, stderr = run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "in.flac", "out.flac"],
        total_sec=10.0,
        job_id=None,
    )
    assert rc == 7
    assert stderr == b"oops"
    # No -progress flags were injected — the cmd passes through unchanged.
    assert calls[0]["argv"] == ["ffmpeg", "-i", "in.flac", "out.flac"]
    assert calls[0]["kw"]["capture_output"] is True


def test_run_ffmpeg_no_duration_falls_back_to_blocking_run(monkeypatch):
    # Same fallback when we have a job_id but no usable duration — without
    # a denominator the progress fraction is meaningless, so skip the pipe.
    class _R:
        returncode = 0
        stderr = b""
        stdout = b""

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", lambda *a, **k: _R())
    rc, stderr = run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "in.flac"], total_sec=0.0, job_id="j1",
    )
    assert rc == 0
    # The fallback path still finalises the job slice at the high end of
    # phase_range — otherwise the bar would stall at 0%.
    # (jobs.update_job swallows updates on unknown job_id so this is silent.)


def test_run_ffmpeg_no_duration_finalises_phase_on_known_job(monkeypatch):
    # When the job exists, the fallback path still snaps the bar to the
    # phase upper bound so multi-step bars advance cleanly.
    class _R:
        returncode = 0
        stderr = b""
        stdout = b""

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", lambda *a, **k: _R())
    jobs_mod.start_job("j-fallback", label="combine")
    run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "in.flac"], total_sec=-1, job_id="j-fallback",
        phase_range=(0.2, 0.6), phase_label="encoding",
    )
    j = jobs_mod.get_job("j-fallback")
    assert j["progress"] == pytest.approx(0.6, abs=1e-6)
    assert j["phase"] == "encoding"


# ── run_ffmpeg_with_progress: argv injection ─────────────────────────────
def test_run_ffmpeg_injects_progress_flags_after_binary(monkeypatch):
    """The `-progress pipe:1 -nostats` pair must land right after the
    ffmpeg binary token so caller-supplied -loglevel / -hide_banner still
    take effect."""
    monkeypatch.setattr(ffmpeg_mod.subprocess, "Popen", _FakePopen)
    jobs_mod.start_job("j-inject", label="split")
    rc, _ = run_ffmpeg_with_progress(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", "in.flac", "out.flac"],
        total_sec=10.0,
        job_id="j-inject",
    )
    assert rc == 0
    assert len(_FakePopen.instances) == 1
    argv = _FakePopen.instances[0]["argv"]
    # Order: ffmpeg, -progress, pipe:1, -nostats, <rest>.
    assert argv[0] == "ffmpeg"
    assert argv[1] == "-progress"
    assert argv[2] == "pipe:1"
    assert argv[3] == "-nostats"
    # Tail mirrors the caller-supplied args after the binary.
    assert argv[4:] == ["-y", "-loglevel", "error", "-i", "in.flac", "out.flac"]


def test_run_ffmpeg_returns_drained_stderr(monkeypatch):
    """The drain thread copies the ffmpeg subprocess stderr into a list of
    chunks the helper joins on return. We check the bytes round-trip."""

    class _PopenWithStderr(_FakePopen):
        def __init__(self, argv, **kw):
            super().__init__(argv, **kw)
            self._stderr_payload = b"ffmpeg: oh dear\nmore stderr\n"

    monkeypatch.setattr(ffmpeg_mod.subprocess, "Popen", _PopenWithStderr)
    jobs_mod.start_job("j-stderr")
    rc, stderr = run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "x.flac"], total_sec=10.0, job_id="j-stderr",
    )
    assert rc == 0
    assert b"ffmpeg: oh dear" in stderr


def test_run_ffmpeg_reports_progress_into_jobs_registry(monkeypatch):
    """`out_time_us=...` lines should drive the job's progress fraction,
    mapped into phase_range when the helper is one step in a multi-step
    bar."""
    monkeypatch.setattr(ffmpeg_mod.subprocess, "Popen", _FakePopen)
    jobs_mod.start_job("j-progress", label="split")
    run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "x.flac"],
        total_sec=10.0,
        job_id="j-progress",
        phase_range=(0.0, 1.0),
        phase_label="track 1/3",
    )
    j = jobs_mod.get_job("j-progress")
    # Final call sets progress to phase_range[1] = 1.0.
    assert j["progress"] == pytest.approx(1.0, abs=1e-6)
    assert j["phase"] == "track 1/3"


def test_run_ffmpeg_phase_range_maps_progress(monkeypatch):
    """A `phase_range = (0.5, 0.75)` slice should bracket the bar at the
    final tick — completion never escapes the slice."""
    monkeypatch.setattr(ffmpeg_mod.subprocess, "Popen", _FakePopen)
    jobs_mod.start_job("j-slice")
    run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "x.flac"], total_sec=10.0, job_id="j-slice",
        phase_range=(0.5, 0.75),
    )
    j = jobs_mod.get_job("j-slice")
    assert j["progress"] == pytest.approx(0.75, abs=1e-6)


def test_run_ffmpeg_handles_out_time_ms_key(monkeypatch):
    """ffmpeg emits both out_time_us and out_time_ms; both are µs in
    practice (legacy naming). The helper has to accept either spelling."""

    class _MsPopen(_FakePopen):
        @staticmethod
        def _default_progress_lines():
            return [
                b"out_time_ms=5000000\n",  # legacy key, same units
                b"progress=end\n",
            ]

    monkeypatch.setattr(ffmpeg_mod.subprocess, "Popen", _MsPopen)
    jobs_mod.start_job("j-ms")
    rc, _ = run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "x.flac"], total_sec=10.0, job_id="j-ms",
    )
    assert rc == 0
    # Final-set still fires at phase_range[1].
    assert jobs_mod.get_job("j-ms")["progress"] == pytest.approx(1.0, abs=1e-6)


def test_run_ffmpeg_ignores_malformed_progress_lines(monkeypatch):
    """A garbled progress key shouldn't crash the loop — the parser uses
    a try/except around int() and skips the line."""

    class _GarbagePopen(_FakePopen):
        @staticmethod
        def _default_progress_lines():
            return [
                b"out_time_us=not-a-number\n",
                b"out_time_us=2000000\n",
                b"progress=end\n",
            ]

    monkeypatch.setattr(ffmpeg_mod.subprocess, "Popen", _GarbagePopen)
    jobs_mod.start_job("j-garbage")
    rc, _ = run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "x.flac"], total_sec=10.0, job_id="j-garbage",
    )
    assert rc == 0


def test_run_ffmpeg_clamps_progress_to_unit_range(monkeypatch):
    """If ffmpeg's out_time_us briefly overshoots total_sec on a rounding
    edge, the helper must clamp the fraction to [0,1] — otherwise the
    progress fraction propagated to update_job would go past 1.0."""

    class _OverPopen(_FakePopen):
        @staticmethod
        def _default_progress_lines():
            return [
                b"out_time_us=15000000\n",  # 15s of 10s = 150%
                b"progress=end\n",
            ]

    monkeypatch.setattr(ffmpeg_mod.subprocess, "Popen", _OverPopen)
    jobs_mod.start_job("j-clamp")
    run_ffmpeg_with_progress(
        ["ffmpeg", "-i", "x.flac"], total_sec=10.0, job_id="j-clamp",
    )
    # update_job clamps too; both layers must agree that progress stays at 1.
    assert jobs_mod.get_job("j-clamp")["progress"] == pytest.approx(1.0)


# ── disk_free_gb: real shutil shell-out ──────────────────────────────────
def test_disk_free_gb_rounds_to_one_decimal(monkeypatch):
    """disk_usage().free is bytes; the helper divides by 1e9 and rounds
    to a single decimal. A 2.749 GB free → 2.7."""

    class _Usage:
        total = 100_000_000_000
        used = 97_251_000_000
        free = 2_749_000_000

    monkeypatch.setattr(ffmpeg_mod.shutil, "disk_usage", lambda p: _Usage)
    assert disk_free_gb() == 2.7


def test_disk_free_gb_uses_output_dir(monkeypatch):
    """The helper inspects OUTPUT_DIR (not /, not cwd) so a docker mount
    is measured rather than the host root."""
    seen: list[str] = []

    class _Usage:
        total = 0
        used = 0
        free = 10_000_000_000

    def spy(path):
        seen.append(str(path))
        return _Usage

    monkeypatch.setattr(ffmpeg_mod.shutil, "disk_usage", spy)
    disk_free_gb()
    # The recorded path matches state.OUTPUT_DIR (set by conftest to a tmp).
    from state import OUTPUT_DIR
    assert seen == [str(OUTPUT_DIR)]


# ── mtime-keyed caches: flac_duration_seconds + flac_format ──────────────
def test_flac_duration_cache_hit_skips_metaflac(monkeypatch, tmp_path):
    """A second call against the same path + mtime must NOT shell out
    again — the cache keys on st_mtime_ns specifically to skip repeated
    metaflac probes during /api/albums listings."""
    f = tmp_path / "x.flac"
    f.write_bytes(b"not a real flac")
    calls: list = []

    def fake_check_output(argv, **kw):
        calls.append(list(argv))
        # `--show-total-samples --show-sample-rate` emits two lines.
        return "44100\n44100\n"

    monkeypatch.setattr(ffmpeg_mod.subprocess, "check_output", fake_check_output)
    # Clear the module-level caches so the first call is a guaranteed miss.
    ffmpeg_mod._DURATION_CACHE.clear()
    ffmpeg_mod._FORMAT_CACHE.clear()

    d1 = ffmpeg_mod.flac_duration_seconds(f)
    d2 = ffmpeg_mod.flac_duration_seconds(f)
    assert d1 == d2
    assert d1 == pytest.approx(1.0, abs=1e-6)
    # Only one subprocess invocation despite two calls.
    assert len(calls) == 1


def test_flac_duration_cache_invalidates_on_mtime_change(monkeypatch, tmp_path):
    """Touching the file (new mtime) must bust the cache. Mirrors what
    happens after a split runs and re-encodes the side in place."""
    import os
    import time
    f = tmp_path / "y.flac"
    f.write_bytes(b"first")
    calls: list = []

    def fake_check_output(argv, **kw):
        calls.append(list(argv))
        return "44100\n44100\n"

    monkeypatch.setattr(ffmpeg_mod.subprocess, "check_output", fake_check_output)
    ffmpeg_mod._DURATION_CACHE.clear()

    ffmpeg_mod.flac_duration_seconds(f)
    # Bump mtime by 60s — guaranteed st_mtime_ns delta.
    new_mtime = time.time() + 60
    os.utime(f, (new_mtime, new_mtime))
    ffmpeg_mod.flac_duration_seconds(f)
    assert len(calls) == 2


def test_flac_format_returns_keys_with_metaflac(monkeypatch, tmp_path):
    """`flac_format` shells out to metaflac and parses three numbers
    into bit_depth/sample_rate_khz/channels."""
    f = tmp_path / "z.flac"
    f.write_bytes(b"placeholder")

    def fake_check_output(argv, **kw):
        # `--show-bps --show-sample-rate --show-channels`
        return "24\n96000\n2\n"

    monkeypatch.setattr(ffmpeg_mod.subprocess, "check_output", fake_check_output)
    ffmpeg_mod._FORMAT_CACHE.clear()

    out = ffmpeg_mod.flac_format(f)
    assert out == {"bit_depth": 24, "sample_rate_khz": 96.0, "channels": 2}


def test_flac_format_swallows_metaflac_failure(monkeypatch, tmp_path):
    """If metaflac errors out (subprocess raises), the helper returns an
    empty dict and the listing pipeline gracefully degrades to no-format."""
    f = tmp_path / "bad.flac"
    f.write_bytes(b"x")

    def boom(*a, **k):
        raise OSError("metaflac not on PATH")

    monkeypatch.setattr(ffmpeg_mod.subprocess, "check_output", boom)
    ffmpeg_mod._FORMAT_CACHE.clear()
    assert ffmpeg_mod.flac_format(f) == {}


# ── TAG_KEY_MAP coverage ─────────────────────────────────────────────────
def test_tag_key_map_covers_every_TagEdit_string_field():
    """The route layer keys off TAG_KEY_MAP to know which model fields
    metaflac understands — drift between this dict and the apply route
    silently drops user input."""
    expected = {
        "artist", "album", "year", "genre", "label",
        "catalog_number", "country", "composer", "conductor",
    }
    assert set(TAG_KEY_MAP.keys()) == expected
    # Vorbis comment names are all uppercase (FLAC convention).
    for v in TAG_KEY_MAP.values():
        assert v == v.upper()


def test_write_tags_emits_all_known_fields(monkeypatch):
    """One metaflac call for the full mapping — `--remove-tag` clears the
    prior pass, `--set-tag` writes the new values, file path at the tail."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    fields = {
        "artist": "A", "album": "B", "year": "2020", "genre": "Rock",
        "label": "L", "catalog_number": "CN1", "country": "US",
        "composer": "Cmp", "conductor": "Cnd", "tracks": ["t1", "t2"],
    }
    ffmpeg_mod.write_tags(Path("/tmp/x.flac"), fields)
    assert len(calls) == 1
    cmd = calls[0]
    # Every TAG_KEY_MAP key resolves to a `--set-tag=<VORBIS>=<value>` arg.
    assert "--set-tag=ARTIST=A" in cmd
    assert "--set-tag=ALBUM=B" in cmd
    assert "--set-tag=DATE=2020" in cmd
    assert "--set-tag=GENRE=Rock" in cmd
    assert "--set-tag=LABEL=L" in cmd
    assert "--set-tag=CATALOGNUMBER=CN1" in cmd
    assert "--set-tag=RELEASECOUNTRY=US" in cmd
    assert "--set-tag=COMPOSER=Cmp" in cmd
    assert "--set-tag=CONDUCTOR=Cnd" in cmd
    # `tracks` is special-cased into TRACKLIST="t1 / t2".
    assert "--set-tag=TRACKLIST=t1 / t2" in cmd
    # The path is the final positional arg.
    assert cmd[-1] == "/tmp/x.flac"
