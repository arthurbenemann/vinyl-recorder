"""Shared test setup.

Runs at conftest import time, BEFORE any test module is imported, so that
`from services.foo import ...` works in test files. Two things matter:

1. The app uses bare imports (`from state import ...`); it expects `app/`
   to be on sys.path, not as a package. We prepend it here.
2. Importing `state` mkdirs `OUTPUT_DIR`. Point it at a throwaway tmp
   dir so the test suite can't touch the developer's recordings.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Pin the test output dir BEFORE any app import — `state.py` mkdirs the
# layout at import time. setdefault so a parent suite can override.
#
# Under xdist (-n auto), every worker inherits the controller's environment,
# including any OUTPUT_DIR the controller's conftest already set. If we left
# `setdefault` alone, all workers would share one OUTPUT_DIR and races on
# in-progress/ (one test enumerating while another cleans up) would surface
# as FileNotFoundError. Detect the xdist worker and force a per-worker tmp
# dir; the controller / non-xdist run keeps the original setdefault behaviour.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
_TEST_OUTPUT = tempfile.mkdtemp(prefix=f"vinyl-recorder-test-{_XDIST_WORKER or 'main'}-")
if _XDIST_WORKER:
    os.environ["OUTPUT_DIR"] = _TEST_OUTPUT
else:
    os.environ.setdefault("OUTPUT_DIR", _TEST_OUTPUT)

# Tests should never trigger the auto-connect path. The app reads this at
# import time too.
os.environ.setdefault("AUTO_CONNECT", "")
os.environ.setdefault("DEFAULT_GAIN_DB", "")

atexit.register(shutil.rmtree, _TEST_OUTPUT, ignore_errors=True)

_APP = REPO_ROOT / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))
