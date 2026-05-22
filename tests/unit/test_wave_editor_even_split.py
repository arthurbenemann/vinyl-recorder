"""Pytest runner for `wave_editor_even_split.test.js`.

Same shape as `test_wave_editor_tracklist.py` — runs the JS sandbox tests
via Node and surfaces the result as a single pytest case.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_FILE = REPO_ROOT / "tests" / "unit" / "wave_editor_even_split.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_we_even_cuts_via_node():
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
