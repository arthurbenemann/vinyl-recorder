"""Shared paths, env-driven configuration, in-process recording state, and
Pydantic request/response models. Imported by every routes/services module."""
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
# IMPORTANT: This mkdir runs at IMPORT time — the first time anything imports
# `state` (or anything that imports it transitively, which is most of the
# app), the layout under OUTPUT_DIR is created on the spot. Tests must
# therefore set `OUTPUT_DIR` to a tmp path BEFORE the first import; see
# tests/conftest.py which does exactly that via os.environ.setdefault. If
# you're running unit tests outside pytest, set OUTPUT_DIR yourself or
# you'll mkdir the developer's real recordings directory.
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
# Allowed output sample rates for the wave-editor split. 0 means "keep
# source" — the route skips the resample step entirely. Anything else must
# be one of the values offered in the UI; the route rejects out-of-set
# values as a 400 (defence in depth — the <select> already constrains the
# client side, but a hand-crafted POST mustn't slip arbitrary -ar through).
ALLOWED_SPLIT_SAMPLE_RATES: tuple[int, ...] = (0, 44100, 48000, 88200, 96000)

# Allowed output container formats for the split. The route validates the
# value against this tuple so a hand-crafted POST can't slip arbitrary
# codec/extension combos through to ffmpeg.
ALLOWED_OUTPUT_FORMATS: tuple[str, ...] = ("flac", "wav", "mp3", "ogg", "m4a-aac", "m4a-alac")

# ── Recording session state ──────────────────────────────────────────────
# Sessions are keyed by short uuid. Previously this lived in three bare
# module-level dicts (`active`, `log_lines`, `log_paths`) that routes
# mutated directly. The `RecordingSessionManager` below preserves the same
# data layout but funnels all access through typed methods so we get a
# mockable boundary, a lock around mutations, and routes that don't need
# to know the dict shape.
@dataclass
class Session:
    """One in-flight recording session.

    Mirrors the keys the old `active[sid]` dict carried, plus the
    per-session log buffer and ffmpeg log path that used to live in the
    sibling `log_lines` / `log_paths` dicts. Fields that aren't always
    populated (pre-pause `pause_started`, post-finalize `finalize_result`)
    default to `None` rather than being absent — callers should treat
    `None` as "not set yet" to match the old `dict.get(...)` semantics."""
    sid: str
    proc:           Any  = None    # subprocess.Popen feeding ffmpeg
    outfile:        str  = ""
    log_fh:         Any  = None    # open file handle for ffmpeg stderr
    start_time:     float = 0.0    # monotonic clock at start (slid forward on resume)
    started_unix:   float = 0.0    # wallclock for display only
    duration:       int  = 0       # request.duration (0 = unlimited)
    meta:           dict = field(default_factory=dict)
    filename:       str  = ""
    sess_state:     dict = field(default_factory=dict)   # shared with subscriber sink
    finalize_lock:  Any  = None    # threading.Lock; created in manager.create
    finalized:      bool = False
    upstream_hold:  Any  = None
    paused:         bool = False
    pause_started:  Optional[float] = None
    finalize_result: Optional[dict] = None
    log_lines:      list = field(default_factory=list)   # hand-written status lines
    log_path:       Optional[str]   = None               # ffmpeg stderr log file


class RecordingSessionManager:
    """Owns the in-memory map of recording sessions plus the lock that
    serialises mutation. Exposes typed helpers covering every access
    pattern the routes currently use against the bare dicts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}

    # ── lifecycle ───────────────────────────────────────────────────
    def create(self, sid: str, **fields) -> Session:
        """Insert a new session. Mirrors the old `active[sid] = {...}`.
        The finalize_lock is created here unless the caller passed one."""
        fields.setdefault("finalize_lock", threading.Lock())
        sess = Session(sid=sid, **fields)
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def insert(self, sess: Session) -> None:
        """Insert a pre-built Session (used by tests that plant a fake)."""
        with self._lock:
            self._sessions[sess.sid] = sess

    def remove(self, sid: str) -> Optional[Session]:
        """Pop the session, or return None if it was already gone.
        Mirrors the old `active.pop(sid, None)`."""
        with self._lock:
            return self._sessions.pop(sid, None)

    # ── reads ────────────────────────────────────────────────────────
    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(sid)

    def values(self) -> list[Session]:
        """Snapshot of currently in-flight sessions. Returns a list (not
        a view) so callers can iterate without holding the lock."""
        with self._lock:
            return list(self._sessions.values())

    def items(self) -> list[tuple[str, Session]]:
        with self._lock:
            return list(self._sessions.items())

    def __contains__(self, sid: str) -> bool:
        with self._lock:
            return sid in self._sessions

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._sessions)

    # ── per-session log buffer ──────────────────────────────────────
    # Replaces the old module-level `log_lines` / `log_paths` dicts.
    # Routes call these instead of indexing into a sibling dict.
    def append_log(self, sid: str, line: str) -> None:
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is not None:
                sess.log_lines.append(line)

    def get_log_lines(self, sid: str) -> list:
        """Return a shallow copy so callers can mutate without races."""
        with self._lock:
            sess = self._sessions.get(sid)
            return list(sess.log_lines) if sess is not None else []

    def get_log_path(self, sid: str) -> Optional[str]:
        with self._lock:
            sess = self._sessions.get(sid)
            return sess.log_path if sess is not None else None


# Singleton — the import sites stay short and tests can monkeypatch when
# they need to swap behaviour.
sessions = RecordingSessionManager()

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
    composer: Optional[str] = None
    conductor: Optional[str] = None


class SearchRequest(BaseModel):
    artist: str = ""
    album: str = ""


class ApplyRequest(BaseModel):
    # Exactly one of these targets must be set:
    # - `filename`:  promote a single raw side into a new in-progress album
    # - `filenames`: combine N raw sides into a new in-progress album
    # - `album_id`:  patch an existing album's manifest in place
    filename: Optional[str] = None
    filenames: Optional[list[str]] = None
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
    # 0 = keep source, otherwise resample to one of the allowed Hz values.
    # The route validates the value against ALLOWED_SPLIT_SAMPLE_RATES so a
    # malicious / malformed client can't slip arbitrary -ar values to ffmpeg.
    sample_rate: int = 0
    output_format: str = "flac"        # one of ALLOWED_OUTPUT_FORMATS; "flac" preserves prior behavior
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
    sample_rate:      Optional[int]    = None
    output_format:    Optional[str]    = None


class BulkDelete(BaseModel):
    filenames: list[str]


class RenameRequest(BaseModel):
    new_name: str  # filename stem (no extension); safe_name() is applied server-side


class ConnectRequest(BaseModel):
    stream_url: str


class PiDeployRequest(BaseModel):
    """Push the bundled pi/server.py + pi-recorder.service to a Pi over SSH.

    Replaces the manual scp / ssh flow in the README. Password is used for
    SSH auth and (when needed) sudo on the remote — never persisted server-
    side, never echoed back in the response."""
    host: str
    username: str = "pi"
    password: str
    port: int = 22
