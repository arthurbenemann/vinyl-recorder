"""App wiring: middleware, top-level health/index/config endpoints, route
registration, static files. Business logic lives in routes/ and services/."""
import asyncio
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from routes import albums, recordings, tagging, ws as ws_route
from services.eventbus import bus
from services.ffmpeg import LOW_SPACE_GB, disk_free_gb
from services.jobs import get_job
from state import (
    AUTO_CONNECT, ConnectRequest, DEFAULT_GAIN_DB, DEFAULT_SPLIT_BIT_DEPTH,
    DEFAULT_SPLIT_NORMALIZE, DEFAULT_SPLIT_TARGET_PEAK_DB, DEFAULT_STREAM_URL,
    DISCOGS_USERNAME, PRE_ROLL_SECONDS, active, upstream,
)
from version import VERSION

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
            upstream.connect(DEFAULT_STREAM_URL)
            bus.log(f"▶ Auto-connected to {DEFAULT_STREAM_URL}", "info")
        except Exception as e:
            bus.log(f"✗ Auto-connect failed: {e}", "err")


@app.on_event("shutdown")
async def _shutdown() -> None:
    recordings.stop_watcher()
    upstream.disconnect()


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
        # Boolean flag only — never leak the actual username/token to the
        # frontend; the UI just needs to know whether to show the section.
        "discogs_collection_enabled":   bool(DISCOGS_USERNAME),
    }


@app.get("/api/status")
async def status():
    sessions = []
    for sid, s in active.items():
        # Frozen elapsed while paused — pause_started captures the freeze point.
        if s.get("paused"):
            elapsed = int(s.get("pause_started", time.time()) - s["start_time"])
        else:
            elapsed = int(time.time() - s["start_time"])
        sessions.append({
            "id": sid,
            "elapsed": elapsed,
            "paused": bool(s.get("paused")),
            "outfile": Path(s["outfile"]).name,
            "meta": s["meta"],
            "duration": s["duration"],
        })
    return {
        "recording":    len(active) > 0,
        "sessions":     sessions,
        "disk_free_gb": disk_free_gb(),
        "upstream":     upstream.state(),
    }


@app.post("/api/connect")
async def connect(req: ConnectRequest):
    """Start the shared upstream pull. State is global — any tab can press it."""
    if upstream.connected:
        bus.log("connect: already connected", "info")
        return upstream.state()
    bus.log(f"Connecting to {req.stream_url}…", "info")
    try:
        fmt = upstream.connect(req.stream_url)
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
    if active:
        bus.log("✗ Disconnect refused — stop recording first", "err")
        raise HTTPException(409, "stop recording before disconnecting")
    if not upstream.connected:
        return upstream.state()
    upstream.disconnect()
    bus.log("■ Disconnected", "info")
    return upstream.state()


@app.post("/api/clip/clear")
async def clip_clear(ch: str = ""):
    """Acknowledge clip latches. ch in {"L","R",""}; "" clears both."""
    if ch not in ("", "L", "R"):
        raise HTTPException(400, "ch must be L, R, or empty")
    upstream.clear_clip(ch or None)
    return {"clipped_l": upstream.clipped_l, "clipped_r": upstream.clipped_r}


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
app.include_router(ws_route.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
