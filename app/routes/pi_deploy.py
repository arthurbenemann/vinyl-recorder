"""POST /api/pi/deploy — install pi/server.py + the systemd unit on a Pi
over SSH.

Streams progress to the browser as NDJSON: one JSON object per line, in
the response body. The deploy modal renders each `{"type":"log",...}`
frame as it arrives so the user sees apt fetching, sudo running, and the
systemctl handshake in real time rather than only when the whole
operation finishes (which can be ~15-30 s on a fresh Pi OS Lite image
where apt actually has work to do)."""
import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from services import pi_deploy
from services.eventbus import bus
from state import PiDeployRequest

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/pi/deploy")
async def deploy_pi(req: PiDeployRequest):
    """Stream deploy progress as NDJSON.

    Each line in the body is one JSON object:
        {"type": "log",   "line": "..."}    progress line, render in modal
        {"type": "done"}                    deploy succeeded
        {"type": "error", "detail": "..."}  deploy failed (clean message)

    HTTP status is always 200 once we begin streaming — request validation
    can still surface as a 422 from FastAPI before the response starts,
    but deploy-time failures are reported in-band so the client can render
    the partial log it already has."""
    bus.log(f"▶ Deploying pi-recorder to {req.username}@{req.host}:{req.port}…", "info")

    # Bridge the (synchronous, thread-bound) pi_deploy.deploy progress
    # callback back to this request's event loop via an asyncio.Queue.
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_log(line: str) -> None:
        # Called from the paramiko worker thread spawned by asyncio.to_thread.
        # `run_coroutine_threadsafe(...).result()` schedules the put on the
        # event loop and blocks the worker until it lands. The fire-and-forget
        # `call_soon_threadsafe(queue.put_nowait, ...)` it replaced raced with
        # the `to_thread` future-completion path on 3.14: the future could win
        # and `run_deploy` enqueued "done" before the scheduled log puts had
        # fired, so the stream consumer drained "done" first and exited
        # without yielding any logs.
        asyncio.run_coroutine_threadsafe(
            queue.put(("log", line)), loop
        ).result()

    async def run_deploy() -> None:
        try:
            await asyncio.to_thread(
                pi_deploy.deploy,
                req.host, req.username, req.password, req.port,
                on_log=on_log,
            )
            await queue.put(("done", None))
        except pi_deploy.DeployError as e:
            await queue.put(("error", str(e)))
        except Exception as e:
            log.exception("unexpected pi_deploy failure")
            await queue.put(("error", f"unexpected error: {e}"))

    task = asyncio.create_task(run_deploy())

    async def stream():
        # `media_type=application/x-ndjson` already nudges intermediaries
        # to stop buffering; explicit `\n` after each JSON object means
        # the consumer can split the body on newlines without a parser.
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "log":
                    yield json.dumps({"type": "log", "line": payload}) + "\n"
                elif kind == "done":
                    bus.log(f"✓ Pi deploy ok — {req.host}", "ok")
                    yield json.dumps({"type": "done"}) + "\n"
                    return
                elif kind == "error":
                    bus.log(f"✗ Pi deploy failed: {payload}", "err")
                    yield json.dumps({"type": "error", "detail": payload}) + "\n"
                    return
        finally:
            # Make sure run_deploy completes (it should already have, by
            # the time we exit the loop above) so any unhandled
            # exception inside it surfaces in the logs rather than
            # being silently swallowed by the cancelled task.
            try:
                await task
            except Exception:
                pass

    return StreamingResponse(stream(), media_type="application/x-ndjson")
