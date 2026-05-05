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
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.test.yml"]
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


def wait_for_upstream_connected(timeout: float = 45.0) -> dict:
    """Poll /api/status until upstream.connected flips true. Returns the
    final status payload, or raises if the deadline expires."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = http_json(f"{RECORDER_URL}/api/status", timeout=3)
            if last.get("upstream", {}).get("connected"):
                return last
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError(
        f"upstream not connected within {timeout:.0f} s. last status: {last!r}"
    )


@pytest.fixture(scope="session")
def stack():
    """Bring the full vinyl-recorder + test-streams compose stack up
    once for the session, tear it down at the end."""
    if not docker_available():
        pytest.skip("docker not available")

    output_dir = REPO_ROOT / "output"
    untagged = output_dir / "untagged"

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
            wait_for_upstream_connected()
        except RuntimeError as e:
            logs = compose("logs", "vinyl-recorder", "--tail", "50").stdout
            pytest.fail(f"{e}\nrecorder logs:\n{logs}")

        yield {"output_dir": output_dir, "untagged": untagged}
    finally:
        compose("down", "-v", timeout=60)
