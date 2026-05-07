"""App version string. Resolved once at import time:
1. The `/app/VERSION` file baked in by the Dockerfile (build arg).
2. `git describe --tags --always --dirty` against the repo (running outside Docker).
3. The literal string ``"dev"`` as a last resort.
"""
import subprocess
from pathlib import Path


def _resolve_version() -> str:
    f = Path(__file__).parent / "VERSION"
    try:
        if f.exists():
            v = f.read_text().strip()
            if v:
                return v
    except Exception:
        pass
    try:
        repo = Path(__file__).resolve().parent.parent
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(repo), stderr=subprocess.DEVNULL, text=True, timeout=2,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return "dev"


VERSION: str = _resolve_version()
