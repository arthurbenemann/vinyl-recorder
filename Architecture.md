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
                                 /output on disk
                          (raw/ in-progress/ music/)
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
| Services | `app/services/`       | ffmpeg wrappers, upstream session, MB/Discogs, jobs, bus    |
| State    | `app/state.py`        | Pydantic models, module-level singletons (`upstream`, etc.) |
| Static   | `app/static/`         | The single-page UI (no bundler)                             |

Key services:

- **`UpstreamSession`** (`services/upstream.py`) — owns one ffmpeg subprocess
  pulling from the Pi, dispatches each chunk to N subscribers via per-subscriber
  bounded queues. Slow subscribers drop chunks; the reader thread never blocks.
- **`eventbus`** (`services/eventbus.py`) — pub/sub event broadcaster. Every
  connected WebSocket replays the last "hello" snapshot and then receives new
  events.
- **`ffmpeg` helpers** (`services/ffmpeg.py`) — encode/decode, `metaflac` tag
  read/write, file listing with cached tag parse, silence detection.
- **`musicbrainz` / `discogs`** (`services/{musicbrainz,discogs}.py`) — public
  API clients via stdlib `urllib`. Discogs accepts an optional token; MB needs
  only a User-Agent.
- **`albums_fs`** (`services/albums_fs.py`) — `in-progress/{album_id}/`
  workspace layer: reads/writes `album.json` manifests, manages side files,
  enforces the no-mutate-source-audio invariant.
- **`peaks`** (`services/peaks.py`) — generates and parses
  [audiowaveform](https://github.com/bbc/audiowaveform) peak data so the wave
  editor can render long FLACs without shipping raw PCM to the browser.
- **`jobs`** (`services/jobs.py`) — in-process registry for long-running ops
  (combine, split, cover-art fetch); routes return a job id and the UI polls
  status over the WebSocket.
- **`pi_deploy`** (`services/pi_deploy.py`) — drives the in-app "deploy to Pi"
  flow over SSH (uploads `pi/server.py`, installs the systemd unit).

Routes:

- `recordings.py` — `/api/record/{start,stop/{sid},pause/{sid},resume/{sid}}`,
  `/api/recordings` + rename/delete/bulk-delete, `/api/download/{file}`,
  `/api/stream-proxy` (browser playback), `/api/log/{sid}`,
  `/api/test-stream`.
- `tagging.py` — `/api/search`, `/api/release/{mbid}`,
  `/api/release/discogs/{id}`, `/api/apply`, `/api/collection/refresh`,
  `/api/cover/{mbid}`, `/api/file-cover/{album_id}`.
- `albums.py` — `/api/albums`, `/api/combine`, `/api/promote`,
  `/api/album/{id}/{demote,plan,sides/reorder,peaks/{idx},sides/{idx}/audio,tracks,track/{name}}`,
  `/api/album/{detect-silences,measure,split}`.
- `pi_deploy.py` — `/api/pi/deploy` (streams install logs back over the
  WebSocket).
- `ws.py` — `/ws` WebSocket. Event types: `hello`, `vu`, `clip`, `upstream`,
  `record`, `health`, `log`, `ping`.

### Frontend — `app/static/`

`index.html` + `main.js` + `wave-editor.js` + `peaks.js` + `style.css` +
`favicon.svg`. Vanilla JS, no bundler. The WebSocket carries live state (VU,
recording timer, health, logs); user actions go through `fetch()` to the JSON
API. `peaks.js` decodes the audiowaveform binary blob the server returns for
the wave editor.

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

### Upstream session lifecycle

Why this exists: the Pi-side capture service only accepts ONE `/stream`
consumer at a time (new connection kicks the old). Without a server-side
fan-out, recording, playback proxy, and VU each pull `/stream` independently
and constantly evict each other. With this module there is a SINGLE ffmpeg
pulling raw PCM from the upstream URL; recording, playback, and VU all
subscribe to its byte stream, so the Pi only sees one consumer regardless
of how many tabs / sessions are running.

Also computes peak L/R every ~50 ms and tracks sticky CLIP latches per
channel — these are the inputs the WebSocket broadcaster sends to clients
in lieu of a per-client `<audio>` analyser.

**Lifecycle (configured vs live)**

The session has two distinct booleans:

    configured  — user has set up a stream URL.  Survives ffmpeg teardown.
    live        — ffmpeg subprocess is currently alive (the old "connected").

The Pi consumes power whenever ffmpeg pulls /stream (arecord runs as a
side-effect on the Pi). To make idle CPU ~0% when nobody is watching,
the ffmpeg subprocess is now demand-driven: holders ref-count the desire
for a live stream. When the count drops to zero we tear ffmpeg down after
a short grace period; when it rises again we respawn. `configured` stays
true across these cycles so the UI keeps showing "connected" — the only
lie that matters for the user is "is the session set up", not "is a
subprocess running this exact millisecond".

Holders are owned by:
  - each WS client whose tab is visible
  - each active recording session (kept alive across tab close)
  - each active playback proxy response

The existing `connected` API is preserved as a backwards-compat alias for
`live` so code that hasn't migrated still works.

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

1. User selects N recordings → `/api/combine` moves them into a fresh
   `in-progress/{album_id}/` folder and writes an `album.json` manifest
   recording the side order. The source side FLACs are kept untouched; a
   concatenated cache (`.cache/concat.flac`) is built on demand for the
   wave editor.
2. The album opens in `wave-editor.js`. The server returns audiowaveform
   peak data via `/api/album/{id}/peaks/{side_idx}`; auto-detected silences
   (`/api/album/detect-silences`) seed suggested cuts; the user adjusts and
   labels tracks. The split plan is persisted into `album.json` via
   `/api/album/{id}/plan`.
3. `/api/album/split` writes one FLAC per track into
   `music/{Artist}/{Album} (Year)/NN - Title.flac` (Jellyfin-shaped) and
   embeds tags + cover at that step. The original sides remain in
   `in-progress/{album_id}/` so the album can be re-split later without
   re-recording.

### Wave editor

`static/wave-editor.js` is the unified album-split editor.

State, viewport math, and waveform/minimap rendering. Cuts, tracks, audio
playback, and the suggest popovers live in the lower halves of this file
(kept in one module so the closures share state).

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

The `eventbus` is thread-safe; everything else uses an `RLock` around the
subscribers list and pre-roll ring.

## Configuration

Environment variables (read at startup, surfaced via `GET /api/config`):

| Var                          | Default          | Notes                                |
|------------------------------|------------------|--------------------------------------|
| `DEFAULT_STREAM_URL`         | SomaFM fallback  | Pi `/stream` URL in production       |
| `AUTO_CONNECT`               | `0`              | connect on page load                 |
| `DEFAULT_GAIN_DB`            | unset            | initial `/gain` POST after connect   |
| `DEFAULT_SPLIT_NORMALIZE`    | `0`              | EBU R128 on split                    |
| `DEFAULT_SPLIT_TARGET_PEAK_DB` | `-1.0`         | target peak when normalising         |
| `DEFAULT_SPLIT_BIT_DEPTH`    | source bit depth | force 16/24-bit on split output      |
| `PRE_ROLL_SECONDS`           | `5`              | ring buffer size; `0` disables       |
| `DISCOGS_USERNAME`           | unset            | enables collection-aware tagging     |
| `DISCOGS_TOKEN`              | unset            | optional; raises Discogs rate limits |
| `MUSIC_OUTPUT_DIR`           | `/output/music`  | relocate Jellyfin tree (e.g. NAS)    |

`DISCOGS_TOKEN` is never sent to the browser. `/api/config` reports
`discogs_username` (boolean: configured?) so the UI can toggle the collection
section.

## Deployment

`docker-compose.yml` runs the published GHCR image. `docker-compose.dev.yml`
overlays a local source build; `docker-compose.test.yml` overlays a synthetic
stream (`test-streams/`) so you can develop without a Pi. Pi service is
deployed standalone via `pi/pi-recorder.service` (systemd) — either through
the in-app **deploy to pi…** flow (which calls `/api/pi/deploy`) or via the
manual recipe in [CONTRIBUTING.md](CONTRIBUTING.md).

## Testing

- `tests/unit/` — service-layer (ffmpeg wrappers, upstream pre-roll & health,
  Discogs collection fuzzy-matcher, jobs).
- `tests/api/` — route-level via FastAPI `TestClient`, including the new
  `/api/collection/*` endpoints.
- `tests/e2e/` — Playwright against a docker-compose stack with the test
  stream.

`make test` runs unit + API; `make test-e2e` adds Playwright.
