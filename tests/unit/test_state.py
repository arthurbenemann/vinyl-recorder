"""Unit tests for `app/state.py`.

`state` is mostly env-driven configuration + the in-memory recording
session manager. The pure-function `_infer_auto_stop_on_silence` truth
table is covered by `test_auto_stop_silence.py`; this module pins the
remaining surface:

  - the `RecordingSessionManager` CRUD/log API (insert/remove/get/values/
    items/__contains__/__len__/__bool__/append_log/get_log_lines/get_log_path)
  - the `Session` dataclass defaults (must not regress — the manager
    relies on the field set matching the old bare-dict shape)
  - the Pydantic request models' default-value contract (silent default
    drift breaks the routes' env-fallback logic)
  - module-level invariants the rest of the app depends on (singleton
    `sessions`, `ALLOWED_OUTPUT_FORMATS` membership, sample-rate guard set)

The directory-init side effect (`mkdir` at import time under OUTPUT_DIR)
is exercised indirectly by `conftest.py` — every test run boots a fresh
tmp OUTPUT_DIR and asserts the resulting tree exists.
"""
import threading

import pytest

import state
from state import (
    ALLOWED_OUTPUT_FORMATS,
    ALLOWED_SPLIT_SAMPLE_RATES,
    DURATION_EDIT_MIN_SLACK_SECONDS,
    IN_PROGRESS_DIR,
    LOG_DIR,
    MUSIC_DIR,
    OUTPUT_DIR,
    RAW_DIR,
    DurationEditRequest,
    PlanUpdateRequest,
    RecordingSessionManager,
    RecordRequest,
    Session,
    SilenceEditRequest,
    SplitRequest,
    SplitTrack,
    TagEdit,
)


# ── directory layout: conftest pins OUTPUT_DIR to a tmp; import-time
# mkdir should have created the four subdirs.
def test_import_time_mkdir_created_layout():
    assert OUTPUT_DIR.is_dir()
    assert RAW_DIR.is_dir()
    assert IN_PROGRESS_DIR.is_dir()
    assert MUSIC_DIR.is_dir()
    assert LOG_DIR.is_dir()


def test_raw_in_progress_under_output_dir():
    assert RAW_DIR.parent == OUTPUT_DIR
    assert IN_PROGRESS_DIR.parent == OUTPUT_DIR
    assert LOG_DIR.parent == OUTPUT_DIR


# ── module-level constants the routes pin against ────────────────────────
def test_allowed_output_formats_contains_expected_codecs():
    # The split route validates `output_format` against this set as defence
    # in depth (UI <select> also restricts) — pin so a removal here doesn't
    # silently break the validation surface.
    assert "flac" in ALLOWED_OUTPUT_FORMATS
    assert "wav" in ALLOWED_OUTPUT_FORMATS
    assert "mp3" in ALLOWED_OUTPUT_FORMATS
    assert "ogg" in ALLOWED_OUTPUT_FORMATS
    assert "m4a-aac" in ALLOWED_OUTPUT_FORMATS
    assert "m4a-alac" in ALLOWED_OUTPUT_FORMATS


def test_allowed_split_sample_rates_includes_zero_and_canonical_set():
    # 0 means "keep source"; the rest must match what the UI <select>
    # offers. A user-tweaked POST gets rejected with a 400.
    assert 0 in ALLOWED_SPLIT_SAMPLE_RATES
    assert 44100 in ALLOWED_SPLIT_SAMPLE_RATES
    assert 48000 in ALLOWED_SPLIT_SAMPLE_RATES
    assert 96000 in ALLOWED_SPLIT_SAMPLE_RATES


def test_duration_edit_min_slack_seconds_is_positive_int():
    # Reducing a live recording's cap inside this slack window returns 409.
    # The route reads the constant from this module; the value mustn't
    # silently flip to 0 (would let clumsy clicks terminate recordings).
    assert isinstance(DURATION_EDIT_MIN_SLACK_SECONDS, int)
    assert DURATION_EDIT_MIN_SLACK_SECONDS > 0


def test_singleton_sessions_is_recording_session_manager():
    # Routes import `sessions` directly — mocking happens at the
    # `state.sessions` attribute path, so the singleton type matters.
    assert isinstance(state.sessions, RecordingSessionManager)


# ── Session dataclass: default-field contract ────────────────────────────
def test_session_defaults_match_legacy_dict_shape():
    s = Session(sid="abc")
    assert s.sid == "abc"
    assert s.proc is None
    assert s.outfile == ""
    assert s.log_fh is None
    assert s.start_time == 0.0
    assert s.started_unix == 0.0
    assert s.duration == 0
    assert s.meta == {}
    assert s.filename == ""
    assert s.sess_state == {}
    # Stays None — the manager fills these in via `create()`.
    assert s.finalize_lock is None
    assert s.finalized is False
    assert s.upstream_hold is None
    assert s.paused is False
    assert s.pause_started is None
    assert s.finalize_result is None
    assert s.log_lines == []
    assert s.log_path is None
    # Silence-detector defaults — must start in the disabled state.
    assert s.silence_seconds == 0
    assert s.silence_threshold_int == 0
    assert s.silence_ms_smoothed == 0.0
    assert s.silence_armed is False
    assert s.silence_since is None


def test_session_default_factories_are_independent():
    # `meta` / `sess_state` / `log_lines` use field(default_factory=...)
    # so two Sessions must not share the same list/dict instance.
    a = Session(sid="a")
    b = Session(sid="b")
    a.meta["k"] = "v"
    a.log_lines.append("line")
    assert b.meta == {}
    assert b.log_lines == []


# ── RecordingSessionManager: lifecycle helpers ───────────────────────────
@pytest.fixture
def mgr():
    """A fresh manager per test — avoids cross-test bleed via the singleton."""
    return RecordingSessionManager()


def test_manager_create_inserts_and_assigns_finalize_lock(mgr):
    sess = mgr.create("sid1", duration=42)
    assert sess.sid == "sid1"
    assert sess.duration == 42
    # The manager auto-creates a Lock when not provided.
    assert sess.finalize_lock is not None
    # And the session is now resolvable via get().
    assert mgr.get("sid1") is sess


def test_manager_create_respects_caller_supplied_finalize_lock(mgr):
    my_lock = threading.Lock()
    sess = mgr.create("sid2", finalize_lock=my_lock)
    assert sess.finalize_lock is my_lock


def test_manager_insert_preserves_prebuilt_session(mgr):
    # Used by route tests that plant a fake session: insert() must not
    # touch the dataclass it's handed.
    sess = Session(sid="planted", outfile="/tmp/x.flac")
    mgr.insert(sess)
    assert mgr.get("planted") is sess
    assert mgr.get("planted").outfile == "/tmp/x.flac"


def test_manager_remove_returns_popped_session(mgr):
    mgr.create("sid3")
    popped = mgr.remove("sid3")
    assert popped is not None
    assert popped.sid == "sid3"
    assert mgr.get("sid3") is None


def test_manager_remove_missing_returns_none(mgr):
    # Mirrors `dict.pop(sid, None)` — must not raise.
    assert mgr.remove("does-not-exist") is None


# ── RecordingSessionManager: read helpers ────────────────────────────────
def test_manager_get_returns_none_for_unknown(mgr):
    assert mgr.get("ghost") is None


def test_manager_values_returns_snapshot_list(mgr):
    mgr.create("a")
    mgr.create("b")
    vals = mgr.values()
    assert isinstance(vals, list)
    sids = {s.sid for s in vals}
    assert sids == {"a", "b"}
    # Snapshot semantics: mutating the returned list doesn't affect the manager.
    vals.clear()
    assert len(mgr) == 2


def test_manager_items_returns_id_session_pairs(mgr):
    mgr.create("x")
    items = mgr.items()
    assert len(items) == 1
    sid, sess = items[0]
    assert sid == "x"
    assert isinstance(sess, Session)
    assert sess.sid == "x"


def test_manager_contains_len_bool(mgr):
    assert "any" not in mgr
    assert len(mgr) == 0
    assert bool(mgr) is False
    mgr.create("only")
    assert "only" in mgr
    assert "nope" not in mgr
    assert len(mgr) == 1
    assert bool(mgr) is True


# ── RecordingSessionManager: per-session log buffer ──────────────────────
def test_manager_append_log_pushes_into_session(mgr):
    mgr.create("with-log")
    mgr.append_log("with-log", "first")
    mgr.append_log("with-log", "second")
    assert mgr.get_log_lines("with-log") == ["first", "second"]


def test_manager_append_log_unknown_sid_is_silent(mgr):
    # Mirrors the old `if sid in log_lines: log_lines[sid].append(...)`
    # pattern; routes occasionally append after a session was removed
    # mid-finalize and must not crash.
    mgr.append_log("ghost", "ignored")
    assert mgr.get_log_lines("ghost") == []


def test_manager_get_log_lines_returns_copy(mgr):
    mgr.create("copy")
    mgr.append_log("copy", "line")
    lines = mgr.get_log_lines("copy")
    lines.append("mutated-externally")
    # Mutating the returned list must not affect the manager's storage.
    assert mgr.get_log_lines("copy") == ["line"]


def test_manager_get_log_path_returns_none_for_unset(mgr):
    sess = mgr.create("nopath")
    assert mgr.get_log_path("nopath") is None
    sess.log_path = "/tmp/ffmpeg.log"
    assert mgr.get_log_path("nopath") == "/tmp/ffmpeg.log"


def test_manager_get_log_path_unknown_returns_none(mgr):
    assert mgr.get_log_path("ghost") is None


# ── Pydantic request models ──────────────────────────────────────────────
def test_record_request_defaults_match_module_contract():
    # The route layer relies on these defaults to detect "field unsent so
    # fall back to env" — silent drift would change observable behaviour.
    r = RecordRequest(stream_url="http://x")
    assert r.artist == ""
    assert r.album == ""
    assert r.year == ""
    assert r.genre == ""
    assert r.label == ""
    assert r.duration == 0
    assert r.sample_rate == 0
    assert r.bit_depth == 0
    assert r.auto_stop_on_silence is False
    assert r.silence_threshold_db == -40.0
    assert r.silence_seconds == 10


def test_record_request_requires_stream_url():
    # `stream_url` is the only required field; everything else has a
    # default so the recorder can boot off env vars alone.
    with pytest.raises(Exception):
        RecordRequest()  # type: ignore[call-arg]


def test_duration_edit_request_accepts_zero_for_unlimited():
    # 0 = no cap. Negative values aren't rejected at the Pydantic layer
    # (the route does the bounds check); pin to today's behaviour.
    assert DurationEditRequest(duration=0).duration == 0
    assert DurationEditRequest(duration=300).duration == 300


def test_silence_edit_request_round_trip():
    assert SilenceEditRequest(silence_seconds=15).silence_seconds == 15


def test_tag_edit_all_fields_optional():
    # Empty payload is valid — the apply route uses this for partial PATCHes.
    e = TagEdit()
    assert e.artist is None
    assert e.tracks is None
    assert e.composer is None
    e2 = TagEdit(artist="X", tracks=["t1", "t2"])
    assert e2.artist == "X"
    assert e2.tracks == ["t1", "t2"]


def test_split_track_defaults_skip_false():
    # `skip` defaults to False — a track without an explicit skip flag is
    # always emitted. Flipping this default would silently drop tracks.
    t = SplitTrack(title="A", duration_seconds=120.5)
    assert t.skip is False
    assert t.title == "A"
    assert t.duration_seconds == 120.5


def test_split_request_defaults():
    req = SplitRequest(album_id="abc", tracks=[])
    assert req.normalize is False
    assert req.target_peak_db == -1.0
    assert req.measured_peak_db is None
    assert req.bit_depth == 0
    assert req.sample_rate == 0
    assert req.output_format == "flac"  # "flac" preserves prior behaviour
    assert req.job_id is None


def test_plan_update_request_all_fields_optional_except_tracks():
    # Editor saves an in-progress draft with whatever it has — every
    # field except `tracks` is optional so a partial PATCH doesn't 422.
    p = PlanUpdateRequest(tracks=[])
    assert p.normalize is None
    assert p.target_peak_db is None
    assert p.measured_peak_db is None
    assert p.bit_depth is None
    assert p.sample_rate is None
    assert p.output_format is None
