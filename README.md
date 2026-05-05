# Vinyl Recorder

A small web UI for capturing an HTTP audio stream (e.g. an analogue vinyl rip
sent over the network) and saving it as a tagged FLAC file. Records via
`ffmpeg`, edits tags via `metaflac`, and looks up albums on MusicBrainz to
auto-fill metadata + embed cover art from the Cover Art Archive.

## Features

- One-click record / stop with live VU meters and a duration timer
- Library view with edit, bulk delete, download, and per-row auto-tag actions
- Recordings split between `output/untagged/` and `output/tagged/`; files are
  renamed on disk to `Artist - Album (Year).flac` once tagged
- Direct MusicBrainz lookup, enriched with Discogs (catalog #, country,
  format, matrix/runout) when MB has linked the release. No tokens or
  setup — uses both services' public APIs.
- Configurable default stream URL and optional auto-connect on page load

## Run it

Requires Docker or Podman with `compose` support.

```bash
# build and start in the background
make            # alias for: docker compose up --build -d
```

Then open <http://localhost:8080>. Recordings persist in
`./output/` on the host.

## Configuration

All runtime options are environment variables in
[docker-compose.yml](docker-compose.yml) — each one is commented in place
explaining what it does and the accepted values. Edit that file, then recreate
the container.

## Pi capture service ([pi/](pi/))

For a turntable rig, a Raspberry Pi with a HiFiBerry DAC-ADC Pro hat acts as
the network audio source. [pi/server.py](pi/server.py) is a tiny stdlib-only
HTTP service that:

- streams 96 kHz / 24-bit stereo WAV from the HiFiBerry ADC at `GET /stream`
- exposes `GET /info` and `POST /gain` so the recorder UI can drive the
  HiFiBerry's analog PGA (−12 to +40 dB) from a slider in the browser
- enforces RCA (single-ended) input + Mic Bias off + 0 dB digital trim on
  every start, so the wiring assumptions stay correct after reboots

### Install on the Pi

The Pi only needs Python 3 and `alsa-utils` (both already on a fresh
Raspberry Pi OS install — no `pip install` step). Then:

```bash
# from this repo on your dev machine
scp pi/server.py pi/pi-recorder.service pi@pi-recorder:/tmp/

# on the Pi
ssh pi@pi-recorder
sudo mkdir -p /opt/pi-recorder
sudo mv /tmp/server.py /opt/pi-recorder/
sudo mv /tmp/pi-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-recorder
systemctl status pi-recorder
```

### Verify

```bash
curl http://pi-recorder:8000/info | jq         # gain + wiring state
ffprobe http://pi-recorder:8000/stream         # pcm_s24le / 96000 Hz / stereo
curl -X POST -H 'Content-Type: application/json' \
     -d '{"db": 12}' http://pi-recorder:8000/gain
```

In the recorder UI, set `DEFAULT_STREAM_URL=http://pi-recorder:8000/stream`
in `docker-compose.yml` (already the default) and the gain slider auto-appears
in the sidebar after `connect`. Non-Pi streams (e.g. SomaFM) work the same
way — the slider stays hidden when `/info` isn't reachable.

## Test streams ([test-streams/](test-streams/))

For exercising the recorder UI without a real Pi, an opt-in compose overlay
spins up a synthetic audio source alongside the recorder:

```bash
make test            # docker compose -f docker-compose.yml -f docker-compose.test.yml up --build -d
```

(Or run the underlying `docker compose` command directly.) Then open
<http://localhost:8080>. The overlay also overrides the default host
networking with a private bridge network, so the same command works on
Linux, macOS, and Windows.

Stop the stack with `make test-down`; tail logs with `make test-logs`.

The `test-streams` container serves three pre-rendered 96 kHz / 24-bit stereo
WAVs on its `:8090`, looped forever, and `DEFAULT_STREAM_URL` is wired to
`/loop`. Switch streams from the UI's "Stream source" field to hit the others:

| Path     | What it is                                        | What it tests                                                    |
| -------- | ------------------------------------------------- | ---------------------------------------------------------------- |
| `/loop`  | 60 s of 440 Hz on L + 660 Hz on R at ~−8 dBFS     | VU meter, basic recording, multi-tab connect/disconnect sync     |
| `/album` | 4 tones (30/25/30/25 s) with 2 s gaps + 20 s side break | Wave-editor split, silence detection, auto-skip ≥15 s rule |
| `/clip`  | 50 s sine that intentionally clips for 5 s after a 20 s lead-in | CLIP latch, badge, log line, clip-during-record path |

## Contributing

PR title conventions and the release flow live in
[CONTRIBUTING.md](CONTRIBUTING.md).

