"""Pytest runner for `wave_editor_save_coalesce.test.js`.

Mirrors `test_wave_editor_save.py` (the 409-path runner) — runs the
companion JS sandbox via Node and surfaces the result as a single
pytest case. The JS file pins `_savePlanNow`'s in-flight coalesce
contract introduced in PR-31: rapid edits never produce two concurrent
fetches, the reschedule picks up the latest state, and `_flushPlanSave`
awaits any in-flight save before issuing its flush POST.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_FILE = REPO_ROOT / "tests" / "unit" / "wave_editor_save_coalesce.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_wave_editor_save_coalesce_via_node():
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
