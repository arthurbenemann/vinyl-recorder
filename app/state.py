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
# Soft-delete trash for raw recordings. `DELETE /api/recordings/{name}`
# moves the FLAC here rather than unlinking it; the UI then offers an
# Undo toast for ~5 s. Entries older than `TRASH_TTL_SECONDS` are
# considered expired and purged on the next trash-touching request
# (no background thread — see app/routes/recordings.py for the
# opportunistic sweep). The directory is intentionally hidden
# (leading dot) so the recorder's own /api/recordings listing never
# surfaces it, and it shares the same volume as raw/ so the move is
# atomic (an os.rename, not a copy).
TRASH_DIR = OUTPUT_DIR / ".trash"
TRASH_TTL_SECONDS = 300  # 5 minutes — generous buffer beyond the 5 s toast.
for _d in (RAW_DIR, IN_PROGRESS_DIR, MUSIC_DIR, LOG_DIR, TRASH_DIR):
    _d.mkdir(parents=True, exist_ok=True)
# IMPORTANT: This mkdir runs at IMPORT time — the first time anything imports
# `state` (or anything that imports it transitively, which is most of the
# app), the layout under OUTPUT_DIR is created on the spot. Tests must
# therefore set `OUTPUT_DIR` to a tmp path BEFORE the first import; see
# tests/conftest.py which does exactly that via os.environ.setdefault. If
# you're running unit tests outside pytest, set OUTPUT_DIR yourself or
# you'll mkdir the developer's real recordings directory.

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

# Auto-stop on silence: when the upstream's smoothed RMS stays below
# `silence_threshold_db` for `silence_seconds` continuous seconds, finalize
# the recording with reason="auto". The session must have seen at least
# one above-threshold chunk first — lead-in silence (cueing the needle,
# pre-roll, dead air before the first track) can never trigger an
# auto-stop. The detector uses a ~2 s EMA on the per-chunk RMS so vinyl
# runout-groove clicks (~-29 dBFS peaks every revolution at 33⅓ RPM)
# can't keep re-arming it — the mean RMS of the runout sits ~-47 dBFS,
# well separated from music (~-15 dBFS RMS) by the default -40 dBFS
# threshold. These env vars pre-fill the per-recording defaults; each
# `POST /api/record/start` can override them via `auto_stop_on_silence`
# / `silence_threshold_db` / `silence_seconds`.
def _infer_auto_stop_on_silence(auto_stop_env: str, threshold_env: str,
                                seconds_env: str) -> bool:
    """Decide whether auto-stop-on-silence defaults to on.

    Contract:
      * `AUTO_STOP_ON_SILENCE` truthy → True (explicit on).
      * `AUTO_STOP_ON_SILENCE` falsy  → False (explicit off; wins over inference).
      * unset / unrecognized          → True iff EITHER silence-tuning var
                                         is set (implicit consent: the user
                                         wouldn't tune those if they didn't
                                         want the feature). Empty otherwise.

    Pure function so unit tests can drive the truth table directly without
    reloading `state` — module-reload causes test-isolation grief because
    the `sessions` singleton swaps under route-level tests."""
    flag = auto_stop_env.strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return bool(threshold_env.strip()) or bool(seconds_env.strip())


_silence_db_env  = os.getenv("SILENCE_THRESHOLD_DB", "")
_silence_sec_env = os.getenv("SILENCE_SECONDS", "")
DEFAULT_AUTO_STOP_ON_SILENCE = _infer_auto_stop_on_silence(
    os.getenv("AUTO_STOP_ON_SILENCE", ""),
    _silence_db_env, _silence_sec_env,
)
try:
    DEFAULT_SILENCE_THRESHOLD_DB = float(_silence_db_env.strip() or "-40.0")
except ValueError:
    DEFAULT_SILENCE_THRESHOLD_DB = -40.0
try:
    DEFAULT_SILENCE_SECONDS = max(1, int(_silence_sec_env.strip() or "10"))
except ValueError:
    DEFAULT_SILENCE_SECONDS = 10

# Discogs collection-aware tagging. When set, the auto-tag candidate panel
# surfaces matches from the user's Discogs collection in a separate section
# above the MusicBrainz results. DISCOGS_TOKEN is optional but raises rate
# limits and is required if the collection is private.
DISCOGS_USERNAME = os.getenv("DISCOGS_USERNAME", "").strip()
DISCOGS_TOKEN    = os.getenv("DISCOGS_TOKEN",    "").strip()

DEFAULT_SPLIT_NORMALIZE = os.getenv("DEFAULT_SPLIT_NORMALIZE", "true").strip().lower() in ("1", "true", "yes", "on")
DEFAULT_SPLIT_TARGET_PEAK_DB = float(os.getenv("DEFAULT_SPLIT_TARGET_PEAK_DB", "-1.0"))
# ReplayGain 2.0 tagging on FLAC split output. When on, after the per-track
# encode the orchestrator runs a single `metaflac --add-replay-gain` pass
# over all emitted tracks, writing per-track gain (REPLAYGAIN_TRACK_GAIN/
# _PEAK) AND a shared album gain (REPLAYGAIN_ALBUM_GAIN/_PEAK). Players read
# these to normalise loudness at playback without ever touching the audio —
# the album values keep the LP's intra-side dynamics intact. FLAC only
# (metaflac is the writer); other containers skip the pass. Defaults on:
# vinyl side-to-side and rip-to-rip levels vary widely and RG is
# non-destructive, so it's the right default for a music-server library.
DEFAULT_SPLIT_REPLAYGAIN = os.getenv("DEFAULT_SPLIT_REPLAYGAIN", "true").strip().lower() in ("1", "true", "yes", "on")
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
    # Auto-stop on silence (per-session config, set by start_recording).
    # silence_seconds == 0 disables. silence_threshold_int is the integer
    # RMS cutoff matching the upstream's sample_format (full_scale ×
    # 10**(threshold_db/20)). silence_ms_smoothed is the EMA-smoothed
    # mean-square (RMS²) the sink maintains across chunks so a single
    # ~-29 dBFS runout click can't re-arm the detector — see the
    # `_update_smoothed_ms` and `_sink` comments in routes.recordings.
    # silence_armed flips true the first time the smoothed RMS rises
    # above the threshold — pre-arming silence (lead-in, cueing the
    # needle) is ignored. silence_since is the monotonic clock at which
    # the current run of below-threshold smoothed RMS started, or None
    # when the most recent value was above threshold.
    silence_seconds:      int   = 0
    silence_threshold_int: int  = 0
    silence_ms_smoothed:  float = 0.0
    silence_armed:        bool  = False
    silence_since:        Optional[float] = None
    # Latched once the "recording but no input signal" warning has been
    # emitted, so the watcher warns at most once per session (cleared
    # implicitly — a session is one recording).
    no_signal_warned:     bool  = False


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
    # Auto-stop on silence. Defaults track the env vars
    # (DEFAULT_AUTO_STOP_ON_SILENCE / DEFAULT_SILENCE_THRESHOLD_DB /
    # DEFAULT_SILENCE_SECONDS); start_recording reads each field with the
    # env-default as fallback so an unsent field falls back to ops policy.
    auto_stop_on_silence: bool  = False
    silence_threshold_db: float = -40.0
    silence_seconds:      int   = 10


class DurationEditRequest(BaseModel):
    """PATCH-style edit of a live recording's duration cap.

    0 = unlimited; positive = seconds. The endpoint validates that
    reductions leave at least DURATION_EDIT_MIN_SLACK_SECONDS of headroom
    so a user clicking the dropdown by accident can't terminate a
    recording with no warning."""
    duration: int


class SilenceEditRequest(BaseModel):
    """PATCH-style edit of a live recording's silence-auto-stop cap.

    0 = ∞ disabled; positive = seconds of continuous below-threshold
    smoothed RMS that triggers finalize. Unlike the duration cap there's
    no slack guard — reducing the cap mid-recording can at most finalize
    the recording the moment silence is already accumulating, which is
    exactly what the user is asking for by editing the dropdown. The
    detection threshold (dBFS) is intentionally NOT editable here — it
    stays an ops/calibration knob set via SILENCE_THRESHOLD_DB."""
    silence_seconds: int


# Slack required when reducing a live recording's duration cap. A new cap
# that lands within this many seconds of `now` would auto-stop the
# recording on the next watcher tick — and a clumsy click on the dropdown
# shouldn't be enough to do that. The endpoint returns 409 otherwise.
DURATION_EDIT_MIN_SLACK_SECONDS = 300


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
    # Structured: `artist` + `album` build a precise MB Lucene query. Used
    # when the tag panel's left-column fields are already filled in.
    artist: str = ""
    album: str = ""
    # Generic free-text alternative. When set, takes precedence: MB scores
    # across all release fields and the Discogs collection match runs on
    # the full string. This is what the search bars send by default — the
    # user no longer needs to split their query into artist/album halves.
    q: str = ""


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
    # Write ReplayGain track+album tags after a FLAC split (one metaflac
    # --add-replay-gain pass over all tracks). Non-destructive; FLAC only.
    # Defaults False on the model so a hand-crafted POST is conservative —
    # the UI sends the real value seeded from DEFAULT_SPLIT_REPLAYGAIN.
    replaygain: bool = False
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
    replaygain:       Optional[bool]   = None
    # Optimistic-concurrency token. When supplied, the server compares it
    # against the manifest's current `plan_version` and returns 409 on
    # mismatch so two tabs can't silently clobber each other. Omit it to
    # write unconditionally — keeps callers that don't track versions
    # backward-compatible.
    expected_version: Optional[int]    = None


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
