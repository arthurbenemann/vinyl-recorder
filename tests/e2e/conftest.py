"""Shared compose-stack fixture for the e2e suite.

Spinning the docker compose stack up is the slowest part of the e2e
flow (~30-60 s build + boot). Hoisting the fixture here so every test
file in tests/e2e/ shares one stack — session scope — keeps the suite
within ~3 min instead of a few minutes per file.

Tests that intentionally damage the stack (e.g. test_crash_recovery
stops the test-streams container) are responsible for restoring it
before yielding control back to the runner.
"""
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# Distinct compose project name so the e2e stack doesn't share state with
# whatever a developer has up via `make` / `make test`. Without this,
# `compose down -v` at session start would wipe their dev volumes.
COMPOSE_PROJECT = "vinyl-e2e-test"
COMPOSE_FILES = [
    "-p", COMPOSE_PROJECT,
    "-f", "docker-compose.yml",
    "-f", "docker-compose.test.yml",
]
# Recorder UI is published to the host on 8080 by docker-compose.test.yml.
RECORDER_URL = "http://127.0.0.1:8080"
# Recorder reaches test-streams via container DNS on the bridge network.
# This URL is sent in /api/connect bodies so the recorder uses it; it is
# NOT reachable from the test runner / host directly.
STREAM_URL = "http://test-streams:8090/loop"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "info"], capture_output=True, timeout=5,
    ).returncode == 0


def compose(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *COMPOSE_FILES, *args],
        cwd=REPO_ROOT, capture_output=True, text=True, **kw,
    )


def http_json(url: str, method: str = "GET", body: dict | None = None,
              timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_for_upstream_configured(timeout: float = 45.0) -> dict:
    """Poll /api/status until upstream.configured flips true. Returns the
    final status payload, or raises if the deadline expires.

    Note: `configured` (URL set up + probe succeeded) is the readiness
    signal under the demand-driven lifecycle; `connected` / `live`
    (ffmpeg subprocess up) only flips true once a holder acquires —
    e.g. a visible WS tab, an active recording, or a playback proxy."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = http_json(f"{RECORDER_URL}/api/status", timeout=3)
            if last.get("upstream", {}).get("configured"):
                return last
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError(
        f"upstream not configured within {timeout:.0f} s. last status: {last!r}"
    )


def ffprobe(host_path: Path) -> dict:
    """Probe a FLAC using ffprobe inside the vinyl-recorder container.

    The container mounts ./output:/output, so host paths under REPO_ROOT/output
    map directly to /output/... inside the container. This avoids any host-side
    ffmpeg dependency.
    """
    rel = Path(host_path).relative_to(REPO_ROOT / "output")
    container_path = f"/output/{rel}"
    r = subprocess.run(
        ["docker", "exec", "vinyl-recorder",
         "ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", container_path],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)


# Narrow set of error names that almost certainly indicate a real JS bug —
# the dead-`draft` ReferenceError, accidental `undefined.foo` accesses,
# `not a function` blunders, etc. Other pageerror messages (e.g. AbortError
# from cancelled fetches, "ResizeObserver loop limit exceeded", browser
# extension noise that occasionally rides into headless chromium) are
# logged for visibility but not fatal — they mostly indicate environment
# noise, not regressions in our code.
_FATAL_PAGEERROR_RE = re.compile(
    r"^(?:ReferenceError|TypeError|SyntaxError|RangeError):"
)


@pytest.fixture
def page(page, request):  # noqa: F811 — intentional override of pytest-playwright's `page`
    """Wrap pytest-playwright's `page` fixture to surface uncaught JS
    exceptions and capture a Playwright trace on failure. Console errors
    aren't enough — a `ReferenceError` thrown inside an event handler
    aborts the handler silently and only shows up via
    `page.on('pageerror')`. The dead-`draft` bug that shipped pre-#71 was
    exactly this class; trapping in the fixture catches every future
    regression of that class at the door without each test having to
    remember to register a listener.

    The trace is started at the top of every test and stopped on
    teardown. If the test failed, we save it to `test-results/` so CI
    can upload it as an artifact and a developer can replay the run in
    Playwright's trace viewer. On a passing test the trace is discarded.

    Errors are always printed at teardown for visibility. Only errors
    whose message matches `_FATAL_PAGEERROR_RE` (the "this is definitely
    your code" classes) fail the test — see the regex's docstring for
    why. Tests that need stricter checking can still register their own
    listener inline."""
    pageerrors: list[str] = []
    page.on("pageerror", lambda e: pageerrors.append(e.message))
    page.context.tracing.start(screenshots=True, snapshots=True, sources=True)

    # First-run onboarding overlay: pre-seed the `vr.onboarded` localStorage
    # flag so the overlay does NOT auto-show over the page in tests that
    # immediately drive UI behind it (combine, delete, rename, …). Without
    # this, the z-index:100 backdrop would intercept their first click. The
    # init script runs before any page script on every navigation in this
    # context. test_onboarding.py opts out (it exercises the genuine first-
    # run path) by matching on the module name, so it sees empty storage.
    if "test_onboarding" not in request.node.nodeid:
        page.add_init_script("try{localStorage.setItem('vr.onboarded','1')}catch(e){}")

    yield page

    failed = bool(getattr(request.node, "rep_call", None) and request.node.rep_call.failed)
    if failed:
        trace_dir = REPO_ROOT / "test-results"
        trace_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize the test id for filesystem use — pytest's nodeids contain
        # `::` / `[]` / `/` that some artifact uploaders mangle.
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", request.node.nodeid)
        path = trace_dir / f"{safe_id}.zip"
        page.context.tracing.stop(path=str(path))
        print(f"[playwright trace saved] {path}")
    else:
        page.context.tracing.stop()

    if pageerrors:
        # Always print so future failures are diagnosable from the run log.
        for err in pageerrors:
            print(f"[pageerror] {err}")
        fatal = [e for e in pageerrors if _FATAL_PAGEERROR_RE.match(e)]
        if fatal:
            joined = " · ".join(fatal[:5])
            more = "" if len(fatal) <= 5 else f" (+{len(fatal) - 5} more)"
            pytest.fail(f"uncaught JS exceptions in page: {joined}{more}")


# Hook to expose the test phase report (setup/call/teardown) on the
# `request.node` so the `page` fixture above can tell whether the test
# failed and decide whether to save its trace.
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(scope="session")
def stack():
    """Bring the full vinyl-recorder + test-streams compose stack up
    once for the session, tear it down at the end."""
    if not docker_available():
        pytest.skip("docker not available")

    output_dir = REPO_ROOT / "output"
    raw = output_dir / "raw"

    # Always bring down any leftover stack from a previous run.
    print("[e2e] compose down -v…", flush=True)
    compose("down", "-v", timeout=60)

    # --wait blocks until each service's healthcheck flips to "healthy"
    # (test-streams + vinyl-recorder both have one). By the time `up`
    # returns, /api/status is serving and AUTO_CONNECT has run.
    print("[e2e] compose up -d --build --wait…", flush=True)
    r = compose("up", "-d", "--build", "--wait", timeout=600)
    # Surface compose output regardless of success — `--wait` failures
    # produce the most useful diagnostic in stderr.
    if r.stdout: print(f"[e2e] compose up stdout:\n{r.stdout}", flush=True)
    if r.stderr: print(f"[e2e] compose up stderr:\n{r.stderr}", flush=True)
    if r.returncode != 0:
        ps = compose("ps", "-a").stdout
        logs = compose("logs", "--no-color", "--tail", "100").stdout
        pytest.fail(
            f"docker compose up failed (rc={r.returncode}):\n"
            f"stdout={r.stdout}\nstderr={r.stderr}\n"
            f"ps:\n{ps}\nlogs:\n{logs}"
        )

    try:
        try:
            wait_for_upstream_configured()
        except RuntimeError as e:
            logs = compose("logs", "vinyl-recorder", "--tail", "50").stdout
            pytest.fail(f"{e}\nrecorder logs:\n{logs}")

        yield {"output_dir": output_dir, "raw": raw}
    finally:
        compose("down", "-v", timeout=60)
