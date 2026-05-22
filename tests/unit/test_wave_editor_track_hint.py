"""Pytest runner for `wave_editor_track_hint.test.js`.

Runs the JS sandbox tests for `_weTrackLengthHint` (the advisory short/long
track flag) via Node, surfacing the result as a single pytest case.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_FILE = REPO_ROOT / "tests" / "unit" / "wave_editor_track_hint.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_we_track_length_hint_via_node():
    r = subprocess.run(
        ["node", str(TEST_FILE)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=20,
    )
    print(r.stdout)
    if r.stderr:
        print("[stderr]", r.stderr)
    assert r.returncode == 0, (
        f"node tests failed (rc={r.returncode}). See stdout above."
    )
