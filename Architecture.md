# Architecture

A self-hosted recorder for vinyl LPs. Audio flows turntable → phono pre →
Raspberry Pi (HiFiBerry DAC-ADC Pro) → HTTP stream → server (ffmpeg) →
tagged FLAC file in a browsable library.

Single-user, no auth, runs in Docker. Three components:

```
┌──────────┐   HTTP /stream    ┌────────────────┐   WebSocket   ┌──────────┐
│  Pi      │ ────PCM/WAV────▶  │  FastAPI app   │ ─────────────▶│  Browser │
│ pi/      │ ◀─POST /gain───   │  app/          │   /api/* &    │ static/  │
└──────────┘                   │                │   stream-proxy└──────────┘
   arecord                     │  ffmpeg, tags  │ ──MP3 audio──▶
                               │  MB / Discogs  │
                               └────────────────┘
                                       │
                                       ▼
                                 /data on disk
                       (raw/ in-progress/ raw-album/ music/)
```

## Components

### Pi capture service — `pi/server.py`

Stdlib-only Python HTTP server on port 8000. Designed to run on a Pi without
pip or a venv.

- `GET  /stream` — `arecord -D hw:CARD=sndrpihifiberry` piped through; returns
  raw 96 kHz / 24-bit / stereo WAV by default (configurable via env).
- `POST /gain` — sets ADC gain via `amixer`. Body: `{"db": <float>}`.
- One concurrent `/stream` consumer is enough — the server fans out internally.

Deployed via a systemd unit, not Docker.

### Server — `app/main.py` (FastAPI on :8080)

| Layer    | Path                  | Responsibility                                              |
|----------|-----------------------|-------------------------------------------------------------|
| Routes   | `app/routes/`         | HTTP endpoints; thin handlers                               |
| Services | `app/services/`       | ffmpeg wrappers, upstream session, MB/Discogs, jobs, bus   |
| State    | `app/state.py`        | Pydantic models, module-level singletons (`upstream`, etc.) |
| Static   | `app/static/`         | The single-page UI (no bundler)                             |

Key services:

- **`UpstreamSession`** (`services/upstream.py`) — owns one ffmpeg subprocess
  pulling from the Pi, dispatches each chunk to N subscribers via per-subscriber
  bounded queues. Slow subscribers drop chunks; the reader thread never blocks.
- **`bus`** (`services/bus.py`) — pub/sub event broadcaster. Every connected
  WebSocket replays the last "hello" snapshot and then receives new events.
- **`ffmpeg` helpers** (`services/ffmpeg.py`) — encode/decode, `metaflac` tag
  read/write, file listing with cached tag parse, silence detection.
- **`musicbrainz` / `discogs`** (`services/{musicbrainz,discogs}.py`) — public
  API clients via stdlib `urllib`. Discogs accepts an optional token; MB needs
  only a User-Agent.

Routes:

- `recordings.py` — `/api/record/{start,stop,pause,resume}`, `/api/recordings`,
  `/api/download/<file>`, `/api/stream-proxy` (browser playback).
- `tagging.py` — `/api/search`, `/api/release/{mbid}`,
  `/api/release/discogs/{id}`, `/api/apply`, `/api/collection/refresh`.
- `albums.py` — combine selected recordings into one FLAC, then split via the
  waveform editor.
- `ws.py` — `/ws` WebSocket. Event types: `hello`, `vu`, `clip`, `upstream`,
  `record`, `health`, `log`, `ping`.

### Frontend — `app/static/`

`index.html` + `main.js` + `wave-editor.js` + `style.css`. Vanilla JS. The
WebSocket carries live state (VU, recording timer, health, logs); user actions
go through `fetch()` to the JSON API.

## Core data flows

### Audio fan-out (the central pattern)

`UpstreamSession` is the heart of the server. One ffmpeg pulls raw PCM from the
Pi forever; multiple unrelated consumers subscribe to the byte stream:

```
                       UpstreamSession._read_loop
                              │
                ┌─────────────┼──────────────┐
                ▼             ▼              ▼
          recording sub   playback sub    VU sub        + a 5s ring buffer
          (FLAC ffmpeg)   (MP3 ffmpeg)    (peak calc)    (preroll, internal)
```

Each subscriber has a worker thread that drains its queue and calls a sink
callback. A subscriber that raises (e.g. `BrokenPipeError` when its ffmpeg
dies) is auto-removed.

### Recording with pre-roll

1. The reader thread always appends the last chunk to a ring buffer sized to
   `PRE_ROLL_SECONDS × sample_rate × channels × (bit_depth/8)` (default 5s).
   Older chunks are evicted from the left.
2. `POST /api/record/start` spawns a FLAC ffmpeg encoder, then calls
   `upstream.subscribe_with_preroll(...)`. Under the upstream lock this
   atomically (a) snapshots the ring contents, (b) registers the subscriber.
3. The route writes the snapshot to ffmpeg's stdin **first**, then signals a
   `threading.Event` that gates the subscriber's worker — preserving timeline
   ordering across the seam.
4. `POST /api/record/stop` unsubscribes and closes ffmpeg's stdin. The file
   ends at the click moment — pre-roll only prepends to the start.

### Browser playback

Each unmuted tab calls `GET /api/stream-proxy`. The server spawns a per-tab
ffmpeg that re-encodes raw PCM → MP3 192k and pipes the bytes back as a
`StreamingResponse`. `<audio src="/api/stream-proxy">` autoplays. Mute toggles
this connection — the `muted` flag is purely client-side per tab.

Pre-roll never affects playback: `subscribe_with_preroll` is only called by
the recording path. The playback subscriber starts at "now" with no prepend.

### Auto-tagging

```
   /api/search ──▶ MusicBrainz search
        │     ──▶ Discogs collection match (if DISCOGS_USERNAME set)
        ▼
   {candidates: [...mb], collection_candidates: [...discogs]}
        │
   user picks one
        │
        ├─ MB pick → /api/release/{mbid}      (MB detail + Discogs enrich + cover)
        └─ collection pick → /api/release/discogs/{id}  (Discogs only)
        │
        ▼
   /api/apply → metaflac writes Vorbis tags + embeds cover
              → file moves raw/ → in-progress/ (tagging promotes)
              → renamed "Artist - Album (Year).flac"
```

The Discogs collection (`/users/{username}/collection/folders/0/releases`) is
fetched on first use and cached in memory for 1 hour;
`POST /api/collection/refresh` forces a refetch. Fuzzy matching of recording
artist/album against owned releases uses normalised token overlap (stdlib only).

### Album combine / split

For multi-side LP rips:

1. User selects N recordings → `/api/album/combine` concatenates them into one
   FLAC in `in-progress/` (re-encoded so concat boundaries are clean).
2. The combined file opens in `wave-editor.js`. Auto-detected silences seed
   suggested cuts; the user adjusts and labels tracks.
3. `/api/album/split` writes one FLAC per track into
   `music/{Artist}/{Album} (Year)/NN - Title.flac` (Jellyfin-shaped),
   persists the full plan as a `<stem>.split.json` sidecar next to the source FLAC,
   and moves the source from `in-progress/` into `raw-album/` so the editor
   can still re-load + re-edit it later without duplicating audio.

## WebSocket event reference

| Type       | Frequency        | Payload                                                                        |
|------------|------------------|--------------------------------------------------------------------------------|
| `hello`    | on connect       | replay of last upstream/record state, recent log lines, latches               |
| `vu`       | ~50 ms           | `{peak_l, peak_r, clip_l, clip_r}` floats 0..1                                 |
| `clip`     | on latch change  | `{ch: "L"\|"R", clipped: bool}`                                                |
| `upstream` | on connect/drop  | `{connected: bool, fmt: {...}}`                                                |
| `record`   | on state change  | `{state: "idle"\|"recording"\|"paused", filename, duration_seconds}`           |
| `health`   | ~500 ms          | `{level: "green"\|"yellow"\|"red", bytes_per_sec, expected_bps, gap_count, reconnect_count, ms_since_last_frame}` |
| `log`      | on event         | `{level, msg}`                                                                 |
| `ping`     | keepalive        | `{}`                                                                           |

## File layout (`/output` inside the container)

```
/output
├── raw/                        fresh side recordings, untagged
│   └── 20251104_191205.flac
├── in-progress/                ONE FOLDER PER ALBUM (workspace)
│   └── 7f3a8c91/               opaque hex slug, stable URL handle
│       ├── album.json          tags, side order, optional split plan
│       ├── 20251104_141522.flac   original side filename, no Vorbis tags
│       ├── 20251104_142105.flac
│       ├── cover.jpg           optional, written at tag-time
│       └── .cache/concat.flac  rebuilt on demand for the wave editor
└── music/                      FINAL Jellyfin tree
    └── Artist Name/
        └── Album Name (2003)/
            ├── 01 - Track1.flac    tags + cover embedded HERE only
            └── 02 - Track2.flac
```

Tags, cover art, and split plans live in `album.json` while the album is
in progress. The side FLACs are never touched. At the split-emit step the
wave editor concatenates the sides per the manifest's order, slices into
per-track FLACs in `music/`, and embeds tags + cover into each one.

`/api/album/{album_id}/demote` moves the sides back to `raw/` and removes
the album dir; if the album was already split, the existing `music/`
subtree is preserved (the user's already-finished export).

The location of `music/` can be overridden with `MUSIC_OUTPUT_DIR` so
the Jellyfin tree can sit on a network share separate from the workspace
data.

Disk-free is monitored; recording start is blocked under 2 GB free.

## Concurrency model

- 1 reader thread per `UpstreamSession` (blocking `proc.stdout.read`)
- 1 worker thread per active subscriber (drain queue → call sink)
- 1 health-ticker thread emitting `health` events every ~500 ms
- 1 daemon reaper thread (`recordings.py:_reaper_loop`) `waitpid()`-ing dead
  ffmpegs without blocking HTTP workers
- FastAPI runs request handlers on uvicorn's threadpool; long ops use
  `asyncio.to_thread`

The `bus` is thread-safe; everything else uses an `RLock` around the
subscribers list and pre-roll ring.

## Configuration

Environment variables (read at startup, surfaced via `GET /api/config`):

| Var                          | Default          | Notes                                |
|------------------------------|------------------|--------------------------------------|
| `DEFAULT_STREAM_URL`         | SomaFM fallback  | Pi `/stream` URL in production       |
| `AUTO_CONNECT`               | `0`              | connect on page load                 |
| `DEFAULT_GAIN_DB`            | unset            | initial `/gain` POST after connect   |
| `DEFAULT_SPLIT_NORMALIZE`    | `0`              | EBU R128 on split                    |
| `DEFAULT_SPLIT_PEAK_DB`      | `-1.0`           | target peak                          |
| `PRE_ROLL_SECONDS`           | `5`              | ring buffer size; `0` disables       |
| `DISCOGS_USERNAME`           | unset            | enables collection-aware tagging     |
| `DISCOGS_TOKEN`              | unset            | optional; raises Discogs rate limits |

`DISCOGS_TOKEN` is never sent to the browser. `/api/config` reports
`discogs_username` (boolean: configured?) so the UI can toggle the collection
section.

## Deployment

`docker-compose.yml` runs the app. `docker-compose.test.yml` overlays a
synthetic stream (`test-streams/`) so you can develop without a Pi. Pi service
is deployed standalone via `pi/vinyl-recorder.service` (systemd).

## Testing

- `tests/unit/` — service-layer (ffmpeg wrappers, upstream pre-roll & health,
  Discogs collection fuzzy-matcher, jobs).
- `tests/api/` — route-level via FastAPI `TestClient`, including the new
  `/api/collection/*` endpoints.
- `tests/e2e/` — Playwright against a docker-compose stack with the test
  stream.

`make test` runs unit + API; `make test-e2e` adds Playwright.
