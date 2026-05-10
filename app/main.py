"""App wiring: middleware, top-level health/index/config endpoints, route
registration, static files. Business logic lives in routes/ and services/."""
import asyncio
import logging
import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from routes import albums, pi_deploy, recordings, tagging, ws as ws_route
from services.eventbus import bus
from services.ffmpeg import LOW_SPACE_GB, disk_free_gb
from services.jobs import get_job
from services.upstream import (
    UPSTREAM_IDLE_GRACE_SECONDS, UPSTREAM_MIN_UPTIME_SECONDS,
)
from state import (
    AUTO_CONNECT, ConnectRequest, DEFAULT_GAIN_DB, DEFAULT_SPLIT_BIT_DEPTH,
    DEFAULT_SPLIT_NORMALIZE, DEFAULT_SPLIT_TARGET_PEAK_DB, DEFAULT_STREAM_URL,
    DISCOGS_USERNAME, PRE_ROLL_SECONDS, sessions, upstream,
)
from version import VERSION


# Structured logging — JSON lines on stdout when LOG_FORMAT=json, plain
# human text otherwise. Reads `LOG_LEVEL` (default INFO) at startup. The
# upstream / route layers already use `logging.getLogger(__name__)`, so
# wiring the formatter here gives every log line a consistent shape that
# `docker compose logs --tail | jq` can drink.
def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv("LOG_FORMAT", "text").lower()
    handler = logging.StreamHandler()
    if fmt == "json":
        # Keep this dependency-free — adding python-json-logger would mean
        # another wheel to install in the runtime image.
        import json
        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                payload = {
                    "ts":     self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                    "level":  record.levelname,
                    "logger": record.name,
                    "msg":    record.getMessage(),
                }
                if record.exc_info:
                    payload["exc"] = self.formatException(record.exc_info)
                return json.dumps(payload)
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
    root = logging.getLogger()
    root.setLevel(level)
    # Replace any default handlers (uvicorn installs its own; we leave it
    # alone here and just set the root so first-party `logging.getLogger`
    # calls show up).
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)


_configure_logging()

# Resolved relative to this file so the app runs inside the container
# (where main.py lives at /app/main.py) and from a checkout (e.g. pytest
# importing it from app/main.py) without an explicit cwd.
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Vinyl Recorder")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def _startup() -> None:
    """Wire the cross-thread event bus to this process's asyncio loop and
    start the recording watcher. Optionally auto-connect upstream so a
    fresh page load already shows live VU."""
    bus.attach_loop(asyncio.get_running_loop())
    recordings.start_watcher()
    if AUTO_CONNECT:
        try:
            # Configure-only — ffmpeg comes up the first time a holder
            # acquires (visible WS tab, recording, playback proxy). Idle
            # CPU stays at ~0% until somebody asks for audio.
            # Offloaded so the probe (urllib /info, then ffprobe fallback)
            # doesn't block the loop during startup.
            await asyncio.to_thread(upstream.connect, DEFAULT_STREAM_URL)
            bus.log(f"▶ Auto-configured upstream {DEFAULT_STREAM_URL}", "info")
        except Exception as e:
            bus.log(f"✗ Auto-connect failed: {e}", "err")


@app.on_event("shutdown")
async def _shutdown() -> None:
    recordings.stop_watcher()
    # disconnect() forces ffmpeg teardown (proc.terminate + wait up to 2 s);
    # offload so the loop can keep servicing in-flight shutdown work.
    await asyncio.to_thread(upstream.disconnect)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def get_config():
    return {
        "default_stream_url":           DEFAULT_STREAM_URL,
        "auto_connect":                 AUTO_CONNECT,
        "default_gain_db":              DEFAULT_GAIN_DB,
        "version":                      VERSION,
        "low_space_gb":                 LOW_SPACE_GB,
        "default_split_normalize":      DEFAULT_SPLIT_NORMALIZE,
        "default_split_target_peak_db": DEFAULT_SPLIT_TARGET_PEAK_DB,
        "default_split_bit_depth":      DEFAULT_SPLIT_BIT_DEPTH,
        "pre_roll_seconds":             PRE_ROLL_SECONDS,
        "upstream_idle_grace_seconds":  UPSTREAM_IDLE_GRACE_SECONDS,
        "upstream_min_uptime_seconds":  UPSTREAM_MIN_UPTIME_SECONDS,
        # Boolean flag only — never leak the actual username/token to the
        # frontend; the UI just needs to know whether to show the section.
        "discogs_collection_enabled":   bool(DISCOGS_USERNAME),
    }


@app.get("/api/status")
async def status():
    snapshot = []
    for s in sessions.values():
        # Frozen elapsed while paused — pause_started captures the freeze
        # point. Use monotonic so a system-clock step (NTP correction at
        # midnight, etc.) doesn't make the timer jump.
        if s.paused:
            elapsed = int((s.pause_started if s.pause_started is not None
                           else time.monotonic()) - s.start_time)
        else:
            elapsed = int(time.monotonic() - s.start_time)
        snapshot.append({
            "id": s.sid,
            "elapsed": elapsed,
            "paused": bool(s.paused),
            "outfile": Path(s.outfile).name,
            "meta": s.meta,
            "duration": s.duration,
        })
    return {
        "recording":    len(sessions) > 0,
        "sessions":     snapshot,
        "disk_free_gb": disk_free_gb(),
        "upstream":     upstream.state(),
    }


@app.post("/api/connect")
async def connect(req: ConnectRequest):
    """Configure the shared upstream pull. State is global — any tab can
    press it. Configures + probes only; ffmpeg comes up on demand once a
    holder (visible WS tab, recording, playback proxy) acquires."""
    if upstream.configured:
        bus.log("connect: already connected", "info")
        return upstream.state()
    bus.log(f"Connecting to {req.stream_url}…", "info")
    try:
        # Offload — connect() probes the format synchronously (urllib +
        # possible ffprobe), and inline would freeze the loop while the
        # probe runs.
        fmt = await asyncio.to_thread(upstream.connect, req.stream_url)
    except Exception as e:
        bus.log(f"✗ Connect failed: {e}", "err")
        raise HTTPException(502, str(e))
    bus.log(
        f"✓ {fmt['sample_rate']} Hz / {fmt['channels']}ch / {fmt['codec']}",
        "ok",
    )
    bus.log("▶ Streaming live", "info")
    return upstream.state()


@app.post("/api/disconnect")
async def disconnect():
    """Stop the shared upstream pull. Refused while recording (any tab)."""
    if sessions:
        bus.log("✗ Disconnect refused — stop recording first", "err")
        raise HTTPException(409, "stop recording before disconnecting")
    if not upstream.configured:
        return upstream.state()
    # Offload — disconnect() blocks while terminating ffmpeg (up to 2 s).
    await asyncio.to_thread(upstream.disconnect)
    bus.log("■ Disconnected", "info")
    return upstream.state()


@app.post("/api/clip/clear")
async def clip_clear(ch: str = ""):
    """Acknowledge clip latches. ch in {"L","R",""}; "" clears both."""
    if ch not in ("", "L", "R"):
        raise HTTPException(400, "ch must be L, R, or empty")
    upstream.clear_clip(ch or None)
    return {"clipped_l": upstream.clipped_l, "clipped_r": upstream.clipped_r}


@app.get("/api/metrics", response_class=PlainTextResponse)
async def metrics():
    """Tiny Prometheus-text-format scrape endpoint. Surfaces a handful of
    counters/gauges from the upstream session and recording state — enough
    for "is the Pi flapping?" / "are we missing audio?" dashboards. Doesn't
    require the prometheus-client dependency; we render the text directly."""
    h = upstream.state().get("health") or {}
    lines = [
        "# HELP vinyl_upstream_connected 1 if the shared upstream ffmpeg is up.",
        "# TYPE vinyl_upstream_connected gauge",
        f"vinyl_upstream_connected {1 if upstream.live else 0}",
        "# HELP vinyl_upstream_configured 1 if the shared upstream URL is set up (may be idle if no holders).",
        "# TYPE vinyl_upstream_configured gauge",
        f"vinyl_upstream_configured {1 if upstream.configured else 0}",
        "# HELP vinyl_upstream_bytes_per_sec Recent measured bytes/sec from the upstream reader.",
        "# TYPE vinyl_upstream_bytes_per_sec gauge",
        f"vinyl_upstream_bytes_per_sec {int(h.get('bytes_per_sec') or 0)}",
        "# HELP vinyl_upstream_expected_bps Expected bytes/sec for the active stream format.",
        "# TYPE vinyl_upstream_expected_bps gauge",
        f"vinyl_upstream_expected_bps {int(h.get('expected_bps') or 0)}",
        "# HELP vinyl_upstream_gap_count Total stream gaps seen since the most recent connect.",
        "# TYPE vinyl_upstream_gap_count counter",
        f"vinyl_upstream_gap_count {int(h.get('gap_count') or 0)}",
        "# HELP vinyl_upstream_reconnect_count Reconnect attempts since process start.",
        "# TYPE vinyl_upstream_reconnect_count counter",
        f"vinyl_upstream_reconnect_count {int(h.get('reconnect_count') or 0)}",
        "# HELP vinyl_active_recordings Recording sessions currently running.",
        "# TYPE vinyl_active_recordings gauge",
        f"vinyl_active_recordings {len(sessions)}",
        "# HELP vinyl_disk_free_gb Free space on the output volume, in GB.",
        "# TYPE vinyl_disk_free_gb gauge",
        f"vinyl_disk_free_gb {disk_free_gb()}",
    ]
    return "\n".join(lines) + "\n"


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    """Polled by the frontend to drive the per-operation progress bar.
    Returns 404 only for completely unknown ids — finished jobs stay around
    briefly so a slow last poll still sees `done: true`."""
    j = get_job(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return {
        "progress": j["progress"],
        "phase":    j["phase"],
        "label":    j["label"],
        "done":     j["done"],
        "error":    j["error"],
    }


app.include_router(recordings.router)
app.include_router(albums.router)
app.include_router(tagging.router)
app.include_router(pi_deploy.router)
app.include_router(ws_route.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
