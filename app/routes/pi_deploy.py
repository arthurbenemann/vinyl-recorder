"""POST /api/pi/deploy — install pi/server.py + the systemd unit on a Pi
over SSH. The browser collects hostname / username / password from a small
modal in the toolbar; this route shells the deploy through paramiko and
streams a list of progress lines back so the modal can render them."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException

from services import pi_deploy
from services.eventbus import bus
from state import PiDeployRequest

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/pi/deploy")
async def deploy_pi(req: PiDeployRequest):
    """Install / update the Pi capture service on `host`. Returns the
    list of progress lines (also visible in the modal as they accumulate
    on the server). The password is consumed locally and never echoed."""
    bus.log(f"▶ Deploying pi-recorder to {req.username}@{req.host}:{req.port}…", "info")
    try:
        # paramiko is fully blocking — push to a thread so we don't stall
        # the event loop for the full deploy duration (~5–15 s).
        lines = await asyncio.to_thread(
            pi_deploy.deploy,
            req.host, req.username, req.password, req.port,
        )
    except pi_deploy.DeployError as e:
        # Friendly user-facing error from the service layer.
        bus.log(f"✗ Pi deploy failed: {e}", "err")
        raise HTTPException(502, str(e))
    except Exception as e:
        log.exception("unexpected pi_deploy failure")
        bus.log(f"✗ Pi deploy crashed: {e}", "err")
        raise HTTPException(500, f"unexpected error: {e}")
    bus.log(f"✓ Pi deploy ok — {req.host}", "ok")
    return {"ok": True, "log": lines}
