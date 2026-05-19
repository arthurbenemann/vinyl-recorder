"""Pytest runner for `wave_editor_save.test.js`.

Runs the JS sandbox tests via Node and surfaces the result as a single
pytest case. The JS file pins both PR-31's `_savePlanNow` in-flight
coalesce behaviour AND PR-33's 409 (plan-version conflict) path:
fake-fetch responses drive both, and the assertions cover that the
editor (a) coalesces concurrent saves, (b) on 409 shows a toast,
keeps `we.dirty` true so the user can manually retry, and latches
`we.planConflict` so the debounce loop doesn't spam guaranteed-409
writes.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_FILE = REPO_ROOT / "tests" / "unit" / "wave_editor_save.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_wave_editor_save_via_node():
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
