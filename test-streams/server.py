#!/usr/bin/env python3
"""Test-stream HTTP server for vinyl-recorder.

Replays pre-rendered 96 kHz / 24-bit stereo WAVs as live, infinitely-looping
HTTP audio streams. Lets developers exercise the recorder UI end-to-end
(VU, recording, splitting, CLIP detection) without a real Pi or vinyl rig.

Stdlib only — no pip required. The audio rendering is done at image build
time (see Dockerfile); at runtime each request just spawns ffmpeg to pace
and loop the file.
"""
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.getenv("PORT", "8090"))
STREAMS_DIR = "/srv/streams"

# Path -> (file, description). Descriptions feed the "/" index so users can
# discover what's on offer without grepping the source.
STREAMS = {
    "/loop":  ("loop.wav",
               "Continuous 440Hz/660Hz tones at ~-8 dBFS. "
               "Use for steady VU, basic recording, multi-tab sync."),
    "/album": ("album.wav",
               "Vinyl-side simulation: 4 tones with 2 s gaps, "
               "20 s side break in the middle. Use for wave-editor split, "
               "silence detection, auto-skip >=15 s."),
    "/clip":  ("clip.wav",
               "Sine that intentionally clips for 5 s after a 20 s lead-in. "
               "Use for CLIP badge + log line."),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write(f"{self.address_string()} - {fmt % args}\n")
        sys.stdout.flush()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._index()
        if path in STREAMS:
            return self._stream(path)
        self.send_error(404)

    def _index(self):
        lines = ["Test streams (96 kHz / 24-bit stereo PCM WAV, looped forever):", ""]
        for p, (_, desc) in STREAMS.items():
            lines.append(f"  GET {p}")
            lines.append(f"    {desc}")
            lines.append("")
        body = "\n".join(lines).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, path: str):
        fpath = os.path.join(STREAMS_DIR, STREAMS[path][0])
        # Send the response head, then hand the socket fd to ffmpeg so audio
        # bytes flow kernel -> ffmpeg -> socket without a Python copy loop.
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()

        # -re paces output at real-time so the consumer sees a steady stream
        # rather than a millisecond burst. -stream_loop -1 makes the file
        # loop forever; -c:a copy avoids re-encoding the already-PCM input.
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-re", "-stream_loop", "-1", "-i", fpath,
            "-f", "wav", "-c:a", "copy", "-",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=self.connection.fileno(),
            stderr=subprocess.DEVNULL,
            close_fds=False,
        )
        try:
            # Blocks until the client disconnects (SIGPIPE) or ffmpeg dies.
            proc.wait()
        finally:
            if proc.poll() is None:
                try: proc.terminate()
                except Exception: pass
                try: proc.wait(timeout=2)
                except Exception:
                    try: proc.kill()
                    except Exception: pass


def main():
    print(f"test-streams: listening on 0.0.0.0:{PORT}", flush=True)
    for p, (f, desc) in STREAMS.items():
        print(f"  {p:8s} -> {f}  ({desc})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
