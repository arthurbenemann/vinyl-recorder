"""Shared paths, env-driven configuration, in-process recording state, and
Pydantic request/response models. Imported by every routes/services module."""
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from services.eventbus import bus
from services.upstream import UpstreamSession

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/output"))
RAW_DIR = OUTPUT_DIR / "raw"
# in-progress/ holds one folder per album: side FLACs (untagged) + an
# `album.json` manifest that owns metadata, side order, and the optional
# split plan. The folder persists across multiple splits — there's no
# separate post-split directory.
IN_PROGRESS_DIR = OUTPUT_DIR / "in-progress"
MUSIC_DIR = Path(os.getenv("MUSIC_OUTPUT_DIR", str(OUTPUT_DIR / "music")))
LOG_DIR = OUTPUT_DIR / ".logs"
for _d in (RAW_DIR, IN_PROGRESS_DIR, MUSIC_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MB_BASE = "https://musicbrainz.org/ws/2"
MB_UA = "VinylRecorder/0.1 ( https://github.com/arthurbenemann/vinyl-recorder )"
CAA_BASE = "https://coverartarchive.org"
DISCOGS_BASE = "https://api.discogs.com"

DEFAULT_STREAM_URL = os.getenv("DEFAULT_STREAM_URL", "https://ice1.somafm.com/groovesalad-256-mp3")
AUTO_CONNECT = os.getenv("AUTO_CONNECT", "").strip().lower() in ("1", "true", "yes", "on")
_dg = os.getenv("DEFAULT_GAIN_DB", "").strip()
DEFAULT_GAIN_DB: Optional[float] = float(_dg) if _dg else None

# Pre-roll: when starting a recording, prepend the last N seconds of audio
# captured by the shared upstream. 0 disables. The ring buffer lives in
# UpstreamSession; size in bytes is computed once the upstream format is
# known (see upstream.connect).
try:
    PRE_ROLL_SECONDS = max(0, int(os.getenv("PRE_ROLL_SECONDS", "5")))
except ValueError:
    PRE_ROLL_SECONDS = 5

# Discogs collection-aware tagging. When set, the auto-tag candidate panel
# surfaces matches from the user's Discogs collection in a separate section
# above the MusicBrainz results. DISCOGS_TOKEN is optional but raises rate
# limits and is required if the collection is private.
DISCOGS_USERNAME = os.getenv("DISCOGS_USERNAME", "").strip()
DISCOGS_TOKEN    = os.getenv("DISCOGS_TOKEN",    "").strip()

DEFAULT_SPLIT_NORMALIZE = os.getenv("DEFAULT_SPLIT_NORMALIZE", "true").strip().lower() in ("1", "true", "yes", "on")
DEFAULT_SPLIT_TARGET_PEAK_DB = float(os.getenv("DEFAULT_SPLIT_TARGET_PEAK_DB", "-1.0"))
DEFAULT_SPLIT_BIT_DEPTH = int(os.getenv("DEFAULT_SPLIT_BIT_DEPTH", "0"))

# Recording sessions, keyed by short uuid.
active: dict = {}          # session_id -> {proc, meta, start_time, outfile, log_fh}
log_lines: dict = {}       # session_id -> [str]   (hand-written status lines)
log_paths: dict = {}       # session_id -> str     (ffmpeg stderr log file)

# Single shared upstream pull (see services/upstream.py for why). Wired to
# the event bus so VU/CLIP/state events fan out to all WS clients.
upstream = UpstreamSession(on_event=bus.publish, preroll_seconds=PRE_ROLL_SECONDS)


class RecordRequest(BaseModel):
    stream_url: str
    artist: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    label: str = ""
    duration: int = 0      # 0 = unlimited
    sample_rate: int = 0   # 0 = auto-detect from stream
    bit_depth: int = 0     # 0 = auto-detect


class TagEdit(BaseModel):
    artist: Optional[str] = None
    album: Optional[str] = None
    year: Optional[str] = None
    genre: Optional[str] = None
    label: Optional[str] = None
    tracks: Optional[list[str]] = None
    catalog_number: Optional[str] = None
    country: Optional[str] = None


class SearchRequest(BaseModel):
    artist: str = ""
    album: str = ""


class ApplyRequest(BaseModel):
    # Either `filename` (raw side, will be promoted) or `album_id` (existing
    # album, manifest patched in place). Exactly one must be set.
    filename: Optional[str] = None
    album_id: Optional[str] = None
    fields: TagEdit
    mbid: Optional[str] = None  # if set, fetch + embed cover art via CAA / Discogs
    discogs_release_id: Optional[int] = None  # persisted as DISCOGS_RELEASE_ID


class CombineRequest(BaseModel):
    filenames: list[str]              # ordered list of raw/ side filenames
    album: TagEdit                    # tags written into album.json
    job_id: Optional[str] = None      # reserved; combine no longer encodes


class PromoteRequest(BaseModel):
    filename: str                     # a side in raw/ to promote into a 1-side album
    album: TagEdit                    # tags written into album.json


class SplitTrack(BaseModel):
    title: str
    duration_seconds: float
    skip: bool = False                # if true, region is dropped (no file written, not numbered)


class SplitRequest(BaseModel):
    album_id: str                     # the in-progress/ folder slug
    tracks: list[SplitTrack]
    normalize: bool = False           # apply gain to hit target peak across all tracks
    target_peak_db: float = -1.0      # only used when normalize=True
    measured_peak_db: Optional[float] = None  # peak from /api/album/measure; required for normalize
    bit_depth: int = 0                # 0 = keep source, 16, or 24
    job_id: Optional[str] = None      # progress reporting (see CombineRequest)


class SilenceDetectRequest(BaseModel):
    album_id: str
    noise_db: float = -40.0           # legacy dB threshold (clients pre-slider)
    threshold_int8: Optional[int] = None  # 1..127 — 8-bit quantised threshold matching .peaks.dat resolution
    min_silence: float = 1.5
    job_id: Optional[str] = None


class MeasureRequest(BaseModel):
    album_id: str
    included_ranges: Optional[list[list[float]]] = None  # [[start, end], ...] in seconds; None = whole album
    job_id: Optional[str] = None


class ReorderSidesRequest(BaseModel):
    sides: list[str]                  # permutation of the album's current sides[]


class PlanUpdateRequest(BaseModel):
    """Editor draft state — saved to album.json.plan WITHOUT running split.
    Lets users close the modal mid-edit (or move to another browser) without
    losing their in-progress cuts. The wave-editor calls this on a debounced
    timer as cuts/titles/skip flags change."""
    tracks: list[SplitTrack]
    normalize:        Optional[bool]   = None
    target_peak_db:   Optional[float]  = None
    measured_peak_db: Optional[float]  = None
    bit_depth:        Optional[int]    = None


class BulkDelete(BaseModel):
    filenames: list[str]


class RenameRequest(BaseModel):
    new_name: str  # filename stem (no extension); safe_name() is applied server-side


class ConnectRequest(BaseModel):
    stream_url: str
