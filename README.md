# Vinyl Recorder

A small web UI for capturing an HTTP audio stream (e.g. an analogue vinyl rip
sent over the network) and saving it as a tagged FLAC file. Records via
`ffmpeg`, edits tags via `metaflac`, and looks up albums on MusicBrainz to
auto-fill metadata + embed cover art from the Cover Art Archive.

## Features

- One-click record / stop with live VU meters and a duration timer
- Three-stage library: **Raw** (just recorded) → **In-progress**
  (tagged + combined) → **Music** (per-track FLACs, ready for Jellyfin)
- MusicBrainz tag lookup, enriched with Discogs (catalog #, country,
  matrix/runout) — no tokens, uses both services' public APIs
- Wave editor splits a combined LP into per-track FLACs with auto-detected
  silences as seed cuts

## Screenshots

![Library view — raw sides, in-progress albums, finished music](images/library.png)
*Library: raw side recordings on top, in-progress albums under tagging in
the middle, the finished Jellyfin tree on the bottom.*

![Combine modal — bulk-select raw sides into one album](images/album-combine.png)
*Combining: bulk-select raw side recordings into a single album, then tag
once instead of per-side.*

![Wave editor — split a combined LP into per-track FLACs](images/split-editor.png)
*Wave editor: auto-detected silences seed track splits; nudge the markers
and label each track before exporting.*

## How it works

A vinyl rip moves through three stages, each its own row in the
[library](images/library.png) and its own folder under `./output/`:

1. **Raw** — capture each side. Hit **record** while connected to the
   audio stream (the Pi rig below, or any HTTP source) to drop a side
   into `raw/`. Already have side recordings? Copy `*.flac` straight into
   `output/raw/` and they show up the same way.
2. **In-progress** — [combine](images/album-combine.png) the sides of one
   record into a single album, then tag it once (artist / album / year).
   A MusicBrainz + Discogs lookup auto-fills the metadata and cover art so
   you usually just confirm. The album lives in `in-progress/` until you
   split it.
3. **Music** — open the [wave editor](images/split-editor.png) to slice
   the album into per-track FLACs (auto-detected silences seed the cuts),
   then export to `music/<Artist>/<Album (Year)>/` — a tagged, Jellyfin-ready
   tree. Drop an already-tagged album into that same layout and it's
   auto-imported as a locked row, no editing required.

```text
output/
├── raw/          # individual side recordings
├── in-progress/  # combined + tagged albums, awaiting split
└── music/        # finished per-track FLACs (<Artist>/<Album (Year)>/)
```

## Run it

Requires Docker or Podman with `compose` support.

```bash
# pulls ghcr.io/arthurbenemann/vinyl-recorder:latest and starts in the background
docker compose up -d
```

Then open <http://localhost:8080>. Recordings persist under `./output/`
(`raw/` → `in-progress/` → `music/`) on the host.

To override settings, copy [.env.example](.env.example) to `.env` and
recreate the container — every var is documented inline. For one-off
compose tweaks, drop a `docker-compose.override.yml` next to
`docker-compose.yml`.

## Pi capture service ([pi/](pi/))

For a turntable rig, a Raspberry Pi with a HiFiBerry DAC-ADC Pro hat acts
as the network audio source. [pi/server.py](pi/server.py) streams 96 kHz /
24-bit stereo WAV at `GET /stream`, and exposes the HiFiBerry's analog PGA
gain to the recorder UI.

Open the **⋮** menu in the recorder header → **deploy to pi…**, fill in
host / username / password, hit deploy. Host + username persist locally;
the password is used per-request and never stored. SSH port is fixed at
22; for a custom port or a fully manual install see
[CONTRIBUTING.md](CONTRIBUTING.md#pi-capture-service--manual-deploy).

The default `DEFAULT_STREAM_URL` already points at
`http://pi-recorder:8000/stream`, so once the deploy reports active, hit
**connect** in the recorder. Non-Pi streams (e.g. SomaFM) work the same way.

## Network / trust model

This app is designed for a **trusted, single-user home LAN** and ships with
no authentication. If you expose it beyond a private network, put it
behind your own auth layer (reverse-proxy basic-auth, SSO, etc.).

## Contributing

Local-dev setup, the synthetic test-streams stack, PR conventions, and the
release flow all live in [CONTRIBUTING.md](CONTRIBUTING.md).
The deeper system overview is in [Architecture.md](Architecture.md).
