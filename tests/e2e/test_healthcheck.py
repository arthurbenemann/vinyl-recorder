"""Verify the Dockerfile HEALTHCHECK actually flips the container to
`healthy` once the FastAPI app is serving. The session-scoped `stack`
fixture brings the stack up with `compose up --wait`, which already
blocks on healthchecks — so by the time we run, the recorder must be
healthy. This test makes that contract explicit and would catch a
regression where the healthcheck command stops working (e.g. /health
removed, python missing from the image, port mismatch)."""
import json
import subprocess

import pytest

pytestmark = pytest.mark.e2e


def _inspect_health(container: str) -> dict:
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{json .State.Health}}", container],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)


def test_recorder_container_reports_healthy(stack):
    health = _inspect_health("vinyl-recorder")
    # If this is None, the image has no HEALTHCHECK at all — regression
    # against the Dockerfile directive.
    assert health is not None, "vinyl-recorder has no healthcheck configured"
    assert health["Status"] == "healthy", (
        f"expected healthy, got {health['Status']!r}; log={health.get('Log')!r}"
    )
    # At least one probe must have actually run and returned 0.
    assert any(entry.get("ExitCode") == 0 for entry in health.get("Log", [])), (
        f"no successful healthcheck probes recorded: {health.get('Log')!r}"
    )
