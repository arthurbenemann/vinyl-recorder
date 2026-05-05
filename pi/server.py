#!/usr/bin/env python3
"""
Pi-side capture service for vinyl-recorder.

Runs on a Raspberry Pi with a HiFiBerry DAC-ADC Pro. Streams a WAV/PCM body
over HTTP that the recorder ingests via `ffmpeg -i`, and exposes a /gain
endpoint so the connected client can set the analog PGA from the browser.

Stdlib only — no pip required.
"""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ── config (env vars) ──────────────────────────────────────────────────────
PORT        = int(os.getenv("PORT", "8000"))
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "96000"))
BIT_DEPTH   = int(os.getenv("BIT_DEPTH", "24"))
CARD        = os.getenv("CARD", "0")
DEVICE      = os.getenv("DEVICE", "plughw:CARD=sndrpihifiberry,DEV=0")
CHANNELS    = 2

# arecord format string
ARECORD_FMT = {16: "S16_LE", 24: "S24_3LE", 32: "S32_LE"}.get(BIT_DEPTH, "S24_3LE")

# ── PGA gain (analog input) ────────────────────────────────────────────────
# `PGA Gain Left` and `PGA Gain Right` are ENUMERATED with 105 items each,
# spanning -12.0 to +40.0 dB in 0.5 dB steps.
GAIN_MIN_DB  = -12.0
GAIN_MAX_DB  =  40.0
GAIN_STEP_DB =   0.5
GAIN_ITEMS   = int(round((GAIN_MAX_DB - GAIN_MIN_DB) / GAIN_STEP_DB)) + 1  # 105

def db_to_item(db: float) -> int:
    db = max(GAIN_MIN_DB, min(GAIN_MAX_DB, db))
    return int(round((db - GAIN_MIN_DB) / GAIN_STEP_DB))

def item_to_db(item: int) -> float:
    return GAIN_MIN_DB + item * GAIN_STEP_DB

# ── amixer helpers ─────────────────────────────────────────────────────────
def amixer_cset(name: str, value: str):
    subprocess.run(
        ["amixer", "-c", CARD, "cset", f"name={name}", value],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def amixer_cget_first_value(name: str) -> int:
    """Return the first integer in the `: values=...` line of amixer cget."""
    try:
        out = subprocess.check_output(
            ["amixer", "-c", CARD, "cget", f"name={name}"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return 0
    m = re.search(r": values=([\-\d]+)", out)
    return int(m.group(1)) if m else 0

def amixer_cget_enum_label(name: str) -> str:
    """Return the human-readable label for the current enumerated value."""
    try:
        out = subprocess.check_output(
            ["amixer", "-c", CARD, "cget", f"name={name}"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return ""
    val = amixer_cget_first_value(name)
    m = re.search(rf"; Item #{val} '([^']*)'", out)
    return m.group(1) if m else str(val)

def get_gain_db() -> float:
    """Average of L/R PGA gains, in dB."""
    l = item_to_db(amixer_cget_first_value("PGA Gain Left"))
    r = item_to_db(amixer_cget_first_value("PGA Gain Right"))
    return (l + r) / 2.0

def set_gain_db(db: float) -> float:
    item = db_to_item(db)
    amixer_cset("PGA Gain Left", str(item))
    amixer_cset("PGA Gain Right", str(item))
    return item_to_db(item)

def init_card():
    """Enforce the user's chosen wiring at startup."""
    amixer_cset("ADC Mic Bias", "0")           # off
    amixer_cset("ADC Left Input", "1")         # VINL1[SE]  (RCA L)
    amixer_cset("ADC Right Input", "1")        # VINR1[SE]  (RCA R)
    amixer_cset("ADC Capture Volume", "24,24") # 0 dB digital trim

# ── streaming takeover ─────────────────────────────────────────────────────
# Only one /stream consumer at a time. A new /stream request kicks the old
# one — no lock to get wedged when a browser disconnects without us noticing.
_stream_state = {"proc": None}
_stream_state_lock = threading.Lock()

# ── HTTP handler ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write(f"{self.address_string()} - {fmt % args}\n")
        sys.stdout.flush()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/info":
            return self._info()
        if path == "/stream":
            return self._stream()
        if path == "/":
            return self._json(200, {
                "service": "pi-recorder",
                "endpoints": ["/stream (GET)", "/info (GET)", "/gain (POST)"],
            })
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/gain":
            return self._gain()
        self.send_error(404)

    # ── routes ─────────────────────────────────────────────────────────────
    def _info(self):
        self._json(200, {
            "card":         CARD,
            "device":       DEVICE,
            "sample_rate":  SAMPLE_RATE,
            "bit_depth":    BIT_DEPTH,
            "channels":     CHANNELS,
            "gain_db":      round(get_gain_db(), 1),
            "gain_min_db":  GAIN_MIN_DB,
            "gain_max_db":  GAIN_MAX_DB,
            "gain_step_db": GAIN_STEP_DB,
            "mic_bias":     amixer_cget_enum_label("ADC Mic Bias"),
            "left_input":   amixer_cget_enum_label("ADC Left Input"),
            "right_input":  amixer_cget_enum_label("ADC Right Input"),
        })

    def _gain(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            db = float(body["db"])
        except Exception:
            return self._json(400, {"error": "expected JSON {\"db\": <number>}"})
        applied = set_gain_db(db)
        return self._json(200, {"gain_db": round(applied, 1)})

    def _stream(self):
        # Kick out any existing stream consumer — last-writer-wins.
        with _stream_state_lock:
            old = _stream_state.get("proc")
            if old and old.poll() is None:
                try: old.terminate()
                except Exception: pass
                try: old.wait(timeout=1)
                except Exception:
                    try: old.kill()
                    except Exception: pass

        proc = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            self.wfile.flush()  # ensure headers reach the socket before arecord starts writing

            cmd = [
                "arecord",
                "-D", DEVICE,
                "-f", ARECORD_FMT,
                "-r", str(SAMPLE_RATE),
                "-c", str(CHANNELS),
                "-t", "wav",
                # Default ALSA period is ~250 ms — that's a long underrun
                # window for the browser's audio buffer. 20 ms periods × 4
                # periods of buffer flow nicely without measurable extra CPU.
                "--period-time=20000",   # 20 ms (μs)
                "--buffer-time=80000",   # 80 ms total ring buffer
            ]
            # Hand the HTTP socket's fd straight to arecord so audio bytes flow
            # kernel → arecord → socket without crossing Python — eliminates
            # the per-chunk read/write loop the naive version had.
            proc = subprocess.Popen(
                cmd,
                stdout=self.connection.fileno(),
                stderr=subprocess.DEVNULL,
                close_fds=False,
            )
            with _stream_state_lock:
                _stream_state["proc"] = proc
            proc.wait()  # blocks until client disconnects (SIGPIPE) or kicker terminates us
        finally:
            if proc and proc.poll() is None:
                try: proc.terminate()
                except Exception: pass
                try: proc.wait(timeout=2)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
            with _stream_state_lock:
                if _stream_state.get("proc") is proc:
                    _stream_state["proc"] = None


def main():
    print(f"pi-recorder: card={CARD} device={DEVICE} "
          f"format={ARECORD_FMT} rate={SAMPLE_RATE}Hz channels={CHANNELS}",
          flush=True)
    init_card()
    print(f"pi-recorder: PGA gain L={amixer_cget_first_value('PGA Gain Left')} "
          f"R={amixer_cget_first_value('PGA Gain Right')} "
          f"(={round(get_gain_db(),1)} dB avg)", flush=True)
    print(f"pi-recorder: listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
