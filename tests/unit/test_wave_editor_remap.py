"""Pytest runner for the JS unit tests in `wave_editor_remap.test.js`.

The remap function inside `app/static/wave-editor.js` is pure JS but
critical to the sides-reorder UX — getting it wrong loses user titles
or scrambles cut positions. We test it in Node via vm.runInContext
(see the .test.js file) and surface failures as a single pytest case
so the existing CI job picks it up alongside the Python tests.

Skips cleanly when Node isn't available (older dev sandboxes).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_FILE = REPO_ROOT / "tests" / "unit" / "wave_editor_remap.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_we_remap_for_sides_via_node():
    r = subprocess.run(
        ["node", str(TEST_FILE)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=20,
    )
    # Always print stdout so a failing assertion in the JS test surfaces
    # the same row-by-row report a developer would see locally.
    print(r.stdout)
    if r.stderr:
        print("[stderr]", r.stderr)
    assert r.returncode == 0, (
        f"node tests failed (rc={r.returncode}). See stdout above."
    )
