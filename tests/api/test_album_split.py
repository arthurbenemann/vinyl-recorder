"""Orchestration tests for `POST /api/album/split`.

The real split pipeline shells out to ffmpeg + metaflac for every kept
track. We can't run those in the unit job (no ffmpeg on PATH there), but
the OTHER half of the endpoint is pure orchestration logic that's
absolutely worth pinning:

  - input validation (no tracks / unknown album / album with no sides
    on disk / disk-low / unreadable durations)
  - the slice-cursor walk (start/end seconds per track, last track
    extends to total, skipped tracks advance the cursor without
    producing output)
  - the output filename naming and zero-padding
  - the ffmpeg cmd shape (concat input, -ss/-to bounds, -af volume +
    aformat as the user requested)
  - metaflac tag-arg shape (per-track tags, MB/Discogs IDs only when
    the manifest carries them, cover.jpg import only when present)
  - idempotent re-run cleanup (prior music dir gets wiped, parent dir
    pruned when empty)
  - manifest persistence at the end (plan + music_relpath)

The orchestration logic lives in `services.split_orchestrator`; the
route is a thin shim that maps domain exceptions to HTTP. We mock
`run_ffmpeg_with_progress` and the metaflac subprocess calls there so
the tests don't need a real ffmpeg/metaflac on PATH.
"""
from pathlib import Path

from fastapi.testclient import TestClient


def _client():
    from main import app
    return TestClient(app)


# ── Test scaffolding ─────────────────────────────────────────────────────

class _MockSplitEnv:
    """Records every ffmpeg cmd, every subprocess.run call (the metaflac
    tag/cover passes), and writes a fake FLAC for each ffmpeg invocation
    so the post-split `out.stat().st_size` calls don't blow up."""

    def __init__(self, monkeypatch, *, ffmpeg_rc: int = 0,
                 src_bit_depth: int | None = 24,
                 disk_free_gb_value: float = 1000.0):
        self.ffmpeg_calls: list[list[str]] = []
        self.metaflac_calls: list[list[str]] = []
        self.ffmpeg_rc = ffmpeg_rc

        from services import ffmpeg as ffmpeg_mod
        from services import split_orchestrator as orch

        # Every invocation creates a tiny placeholder FLAC at the output
        # path so `out.stat().st_size` and the `if cover_file` branch
        # both work.
        def fake_run_ffmpeg(cmd, total_sec, job_id, slice_, label):
            self.ffmpeg_calls.append(list(cmd))
            # `cmd` ends with the output path — write a stub there.
            out_path = Path(cmd[-1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x66\x4c\x61\x43" + b"x" * 100)  # fLaC magic
            return self.ffmpeg_rc, b"" if self.ffmpeg_rc == 0 else b"ffmpeg blew up"

        monkeypatch.setattr(orch, "run_ffmpeg_with_progress", fake_run_ffmpeg)

        # Mock the metaflac probe (subprocess.check_output) to return a
        # fixed bit depth / sample rate.
        def fake_check_output(cmd, **kw):
            if src_bit_depth is None:
                raise OSError("metaflac missing")
            # `metaflac --show-bps --show-sample-rate <path>` → "24\n96000\n"
            return f"{src_bit_depth}\n96000\n"

        monkeypatch.setattr(orch.subprocess, "check_output", fake_check_output)

        # Mock the metaflac tag + picture passes (subprocess.run).
        def fake_run(cmd, **kw):
            self.metaflac_calls.append(list(cmd))

            class _R:
                returncode = 0
            return _R()

        monkeypatch.setattr(orch.subprocess, "run", fake_run)

        # We don't depend on real flac_duration_seconds — yield a fixed
        # total so we know what the slicing cursor sees. Patch on the
        # orchestrator module (it imported by name).
        monkeypatch.setattr(
            orch, "flac_duration_seconds", lambda p: 600.0,
        )

        # Disk-space helper. Patch the underlying disk_free_gb because the
        # orchestrator imports `disk_space_error` by name, and the helper
        # itself reads disk_free_gb at call time.
        monkeypatch.setattr(
            ffmpeg_mod, "disk_free_gb", lambda: disk_free_gb_value,
        )


def _make_album_with_side(filename: str = "side1.flac",
                          tags: dict | None = None) -> str:
    """Drop a fake FLAC into raw/, combine into a new album, return its
    album_id. The flac_duration mock comes from _MockSplitEnv."""
    from state import RAW_DIR
    from services import albums_fs

    p = RAW_DIR / filename
    p.write_bytes(b"\x66\x4c\x61\x43" + b"x" * 200)  # fLaC magic
    aid, _ = albums_fs.create_album([filename], tags or {})
    return aid


def _cleanup_album(aid: str) -> None:
    from services import albums_fs
    from state import MUSIC_DIR

    d = albums_fs.album_dir(aid)
    if d.is_dir():
        for f in d.rglob("*"):
            if f.is_file():
                try: f.unlink()
                except Exception: pass
        for sub in sorted(d.rglob("*"), key=lambda x: -len(str(x))):
            if sub.is_dir():
                try: sub.rmdir()
                except Exception: pass
        try: d.rmdir()
        except Exception: pass

    # Music dir cleanup — try every relpath the test may have produced.
    if MUSIC_DIR.is_dir():
        for sub in MUSIC_DIR.rglob("*"):
            if sub.is_file():
                try: sub.unlink()
                except Exception: pass
        for sub in sorted(MUSIC_DIR.rglob("*"), key=lambda x: -len(str(x))):
            if sub.is_dir():
                try: sub.rmdir()
                except Exception: pass


# ── Validation / 4xx paths ───────────────────────────────────────────────

def test_split_unknown_album_returns_404(monkeypatch):
    _MockSplitEnv(monkeypatch)
    r = _client().post("/api/album/split", json={
        "album_id": "not-a-real-id",
        "tracks": [{"title": "T1", "duration_seconds": 60}],
    })
    assert r.status_code == 404


def test_split_no_tracks_returns_400(monkeypatch):
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side()
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid, "tracks": [],
        })
        assert r.status_code == 400
        assert env.ffmpeg_calls == []  # never reached
    finally:
        _cleanup_album(aid)


def test_split_album_with_no_sides_returns_404(monkeypatch):
    """An album dir whose `sides[]` is empty must surface as 404 (matches
    `album_concat_playlist`'s FileNotFoundError → HTTP 404 mapping)."""
    _MockSplitEnv(monkeypatch)
    from services import albums_fs

    # Make an album dir with NO sides on disk.
    aid = albums_fs.new_album_id()
    albums_fs.album_dir(aid).mkdir(parents=True)
    albums_fs.write_manifest(aid, {"sides": [], "tags": {}})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid, "tracks": [{"title": "T1", "duration_seconds": 10}],
        })
        assert r.status_code == 404
    finally:
        _cleanup_album(aid)


def test_split_disk_full_returns_507(monkeypatch):
    _MockSplitEnv(monkeypatch, disk_free_gb_value=0.1)
    aid = _make_album_with_side()
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid, "tracks": [{"title": "T1", "duration_seconds": 10}],
        })
        assert r.status_code == 507
    finally:
        _cleanup_album(aid)


def test_split_unreadable_duration_returns_500(monkeypatch):
    """If `flac_duration_seconds` returns 0/None for every side, the
    endpoint can't compute slice ranges → 500 (not silent garbage out)."""
    _MockSplitEnv(monkeypatch)
    from services import split_orchestrator as orch
    monkeypatch.setattr(orch, "flac_duration_seconds", lambda p: 0.0)

    aid = _make_album_with_side()
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid, "tracks": [{"title": "T1", "duration_seconds": 10}],
        })
        assert r.status_code == 500
    finally:
        _cleanup_album(aid)


def test_split_ffmpeg_failure_surfaces_as_500(monkeypatch):
    env = _MockSplitEnv(monkeypatch, ffmpeg_rc=1)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 500
        # The failing track was attempted exactly once.
        assert len(env.ffmpeg_calls) == 1
    finally:
        _cleanup_album(aid)


# ── Cursor walk: track-end clamps to total, last extends ─────────────────

def test_split_last_track_extends_to_total(monkeypatch):
    """The last track always runs to `total` regardless of its declared
    duration — that's how user-edited cuts shorter than the actual side
    don't drop the tail of the recording."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        # Two tracks of 100 s each; total = 600 s. Track 2 should run from
        # 100 → 600, not 100 → 200.
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [
                {"title": "T1", "duration_seconds": 100},
                {"title": "T2", "duration_seconds": 100},
            ],
        })
        assert r.status_code == 200, r.text
        # Track 2 cmd: -ss 100.000 -to 600.000
        cmd2 = env.ffmpeg_calls[1]
        ss = cmd2[cmd2.index("-ss") + 1]
        to = cmd2[cmd2.index("-to") + 1]
        assert ss == "100.000"
        assert to == "600.000"
    finally:
        _cleanup_album(aid)


def test_split_skip_track_advances_cursor_without_output(monkeypatch):
    """A skip:true region drops cleanly — no output file, no ffmpeg call,
    but the cursor still advances so the next track's -ss is correct."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [
                {"title": "Intro Skip", "duration_seconds": 30, "skip": True},
                {"title": "Real",       "duration_seconds": 100},
            ],
        })
        assert r.status_code == 200, r.text
        # Only ONE ffmpeg call (the skipped track produced no output).
        assert len(env.ffmpeg_calls) == 1
        cmd = env.ffmpeg_calls[0]
        # The kept track starts at 30 (after the skip) and runs to total
        # because it's the last track in the list.
        assert cmd[cmd.index("-ss") + 1] == "30.000"
        assert cmd[cmd.index("-to") + 1] == "600.000"
        # And the response only lists the kept one.
        body = r.json()
        assert len(body["tracks"]) == 1
        assert body["tracks"][0]["filename"].endswith("Real.flac")
    finally:
        _cleanup_album(aid)


def test_split_zero_duration_track_skipped(monkeypatch):
    """A track of duration_seconds=0 is degenerate — the cursor doesn't
    move and the start==end check elides the output. Pinned because the
    UI's drag-handles can momentarily produce zero-width regions."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [
                {"title": "Z",  "duration_seconds": 0},
                {"title": "T1", "duration_seconds": 50},
            ],
        })
        assert r.status_code == 200
        # Only the second track produces an ffmpeg call.
        assert len(env.ffmpeg_calls) == 1
        # And it runs from 0 to total (it's the last track).
        cmd = env.ffmpeg_calls[0]
        assert cmd[cmd.index("-ss") + 1] == "0.000"
        assert cmd[cmd.index("-to") + 1] == "600.000"
    finally:
        _cleanup_album(aid)


# ── Output filename construction ─────────────────────────────────────────

def test_split_output_filenames_zero_padded_by_total(monkeypatch):
    """Track index padding scales with the number of OUTPUT (not requested)
    tracks. With 12 outputs we want "01 - ..." etc."""
    _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        # 12 outputs (1 skip + 12 keeps; 12 → 2-digit padding).
        tracks = [{"title": "Skip", "duration_seconds": 5, "skip": True}]
        tracks += [{"title": f"T{i}", "duration_seconds": 40} for i in range(1, 13)]
        r = _client().post("/api/album/split", json={
            "album_id": aid, "tracks": tracks,
        })
        assert r.status_code == 200, r.text
        names = [t["filename"] for t in r.json()["tracks"]]
        # 12 outputs, 2-digit pad.
        assert names[0].startswith("01 - ")
        assert names[-1].startswith("12 - ")
        # Skipped track is NOT in the response.
        assert all("Skip" not in n for n in names)
    finally:
        _cleanup_album(aid)


def test_split_sanitizes_track_titles_in_filenames(monkeypatch):
    """Filesystem-hostile chars in titles must be stripped — the resulting
    filename can't escape the music dir."""
    _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": 'a/b\\c"d', "duration_seconds": 60}],
        })
        assert r.status_code == 200
        fname = r.json()["tracks"][0]["filename"]
        # All FS-hostile chars stripped, just letters left.
        assert "abcd" in fname
        assert "/" not in fname.replace(" - ", "")
        assert "\\" not in fname
    finally:
        _cleanup_album(aid)


def test_split_empty_title_falls_back_to_safe_label(monkeypatch):
    """A title that sanitizes to empty falls back to a non-empty label
    (`safe_path_component` returns "Unknown"; the route also has a
    `or 'Track'` belt-and-suspenders) so we don't write `01 - .flac`."""
    _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "///", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        fname = r.json()["tracks"][0]["filename"]
        # Whatever the fallback label is, the filename must be `<NN> - <label>.flac`
        # with a non-empty label.
        assert fname.endswith(".flac")
        assert fname.startswith("01 - ")
        # The body between "01 - " and ".flac" is non-empty.
        body = fname[len("01 - "):-len(".flac")]
        assert body  # non-empty
    finally:
        _cleanup_album(aid)


# ── ffmpeg cmd shape: -af volume + aformat ───────────────────────────────

def test_split_normalize_adds_volume_filter(monkeypatch):
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        # measured peak -4 dB, target -1 dB → +3 dB gain.
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "normalize": True,
            "target_peak_db": -1.0,
            "measured_peak_db": -4.0,
        })
        assert r.status_code == 200
        cmd = env.ffmpeg_calls[0]
        af = cmd[cmd.index("-af") + 1]
        assert "volume=" in af
        # +3 dB gain; format is "%.4f".
        assert "3.0000" in af
    finally:
        _cleanup_album(aid)


def test_split_no_normalize_no_volume_filter(monkeypatch):
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "normalize": False,
        })
        assert r.status_code == 200
        cmd = env.ffmpeg_calls[0]
        # No `-af` flag at all when nothing needs filtering.
        assert "-af" not in cmd
    finally:
        _cleanup_album(aid)


def test_split_aformat_skipped_when_target_matches_source(monkeypatch):
    """If the user asks for 24-bit and the source is already 24-bit, we
    skip the `aformat` filter — needless re-encode."""
    env = _MockSplitEnv(monkeypatch, src_bit_depth=24)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "bit_depth": 24,
        })
        assert r.status_code == 200
        cmd = env.ffmpeg_calls[0]
        assert "-af" not in cmd
    finally:
        _cleanup_album(aid)


def test_split_16bit_downconvert_dithers_via_aresample(monkeypatch):
    """24-bit source → 16-bit output → the reduction goes through aresample
    with shaped TPDF dither (osf=s16 + dither_method), not a hard-truncating
    aformat. Truncating without dither leaves audible quantisation noise in
    the quiet passages a vinyl rip is full of."""
    env = _MockSplitEnv(monkeypatch, src_bit_depth=24)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "bit_depth": 16,
        })
        assert r.status_code == 200
        cmd = env.ffmpeg_calls[0]
        af = cmd[cmd.index("-af") + 1]
        assert "aresample=" in af
        assert "osf=s16" in af
        assert "dither_method=triangular_hp" in af
        # No hard-truncating aformat=s16 on the 16-bit path.
        assert "aformat=sample_fmts=s16" not in af
    finally:
        _cleanup_album(aid)


def test_split_sample_rate_default_keeps_source(monkeypatch):
    """sample_rate omitted (or 0) → no `-ar` on the ffmpeg cmd, no
    aresample filter. The source rate flows through untouched."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        cmd = env.ffmpeg_calls[0]
        assert "-ar" not in cmd
        # When neither normalize nor bit-depth nor sample-rate is set, no
        # `-af` flag is emitted at all.
        assert "-af" not in cmd
    finally:
        _cleanup_album(aid)


def test_split_sample_rate_adds_ar_and_soxr_filter(monkeypatch):
    """Non-zero sample_rate → `-ar <rate>` and a SoX-resampler aresample
    filter on the per-track encode."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "sample_rate": 44100,
        })
        assert r.status_code == 200, r.text
        cmd = env.ffmpeg_calls[0]
        # `-ar 44100` lands on the output side.
        assert "-ar" in cmd
        assert cmd[cmd.index("-ar") + 1] == "44100"
        # And the SoX-resampler aresample filter is in the -af chain.
        af = cmd[cmd.index("-af") + 1]
        assert "aresample=resampler=soxr" in af
        assert "precision=28" in af
    finally:
        _cleanup_album(aid)


def test_split_sample_rate_combines_with_bit_depth(monkeypatch):
    """Both knobs together: a single aresample carries the SoX rate change
    AND the dithered 24→16 reduction (resample in high precision, then dither
    once on the final s16 step), and `-ar` still lands on the output side."""
    env = _MockSplitEnv(monkeypatch, src_bit_depth=24)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "bit_depth": 16,
            "sample_rate": 48000,
        })
        assert r.status_code == 200, r.text
        cmd = env.ffmpeg_calls[0]
        assert cmd[cmd.index("-ar") + 1] == "48000"
        af = cmd[cmd.index("-af") + 1]
        assert "aresample=resampler=soxr" in af
        assert "osf=s16" in af
        assert "dither_method=triangular_hp" in af
    finally:
        _cleanup_album(aid)


def test_split_unsupported_sample_rate_rejected(monkeypatch):
    """Server-side defence in depth: an unsupported `-ar` value (one not
    in the allowed set) must be rejected. The UI's <select> already pins
    the client side, but a hand-crafted POST mustn't slip arbitrary
    values to ffmpeg."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "sample_rate": 12345,
        })
        assert r.status_code == 400
        # ffmpeg never invoked.
        assert env.ffmpeg_calls == []
    finally:
        _cleanup_album(aid)


def test_split_unsupported_output_format_rejected(monkeypatch):
    """Defence in depth: an unsupported output_format value (one not in
    ALLOWED_OUTPUT_FORMATS) must be rejected. The UI's <select> already
    pins the client side, but a hand-crafted POST mustn't slip arbitrary
    codec/extension combos through to ffmpeg."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "output_format": "wma",  # not in the allowed set
        })
        assert r.status_code == 400
        # ffmpeg never invoked.
        assert env.ffmpeg_calls == []
    finally:
        _cleanup_album(aid)


def test_split_default_output_format_is_flac(monkeypatch):
    """When `output_format` is omitted from the request, the default kicks
    in: tracks land as .flac and metaflac runs (existing behaviour)."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 200, r.text
        # Output filename ends in .flac.
        out = env.ffmpeg_calls[0][-1]
        assert out.endswith(".flac")
        # FLAC encoder selected.
        cmd = env.ffmpeg_calls[0]
        assert cmd[cmd.index("-c:a") + 1] == "flac"
        # metaflac runs (one --remove-all-tags per track) — the post-encode
        # tag pass that's specific to FLAC.
        assert any("--remove-all-tags" in c for c in env.metaflac_calls)
    finally:
        _cleanup_album(aid)


def test_split_mp3_uses_libmp3lame_and_inline_metadata(monkeypatch):
    """MP3 output: encoder is libmp3lame, no metaflac post-pass, tags are
    inline via -metadata flags."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B", "year": "2024"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "output_format": "mp3",
        })
        assert r.status_code == 200, r.text
        cmd = env.ffmpeg_calls[0]
        out = cmd[-1]
        assert out.endswith(".mp3")
        assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
        # Inline tags via -metadata flags.
        flat = " ".join(cmd)
        assert "artist=A" in flat
        assert "album=B" in flat
        assert "date=2024" in flat
        assert "title=T1" in flat
        assert "track=1/1" in flat
        # No metaflac post-pass for non-FLAC output.
        assert env.metaflac_calls == []
    finally:
        _cleanup_album(aid)


def test_split_wav_24bit_uses_pcm_s24le(monkeypatch):
    """WAV at 24-bit picks pcm_s24le directly (not aformat). Pinned because
    aformat doesn't change WAV's container precision — the codec name itself
    is what determines the on-disk bit depth."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "output_format": "wav",
            "bit_depth": 24,
        })
        assert r.status_code == 200, r.text
        cmd = env.ffmpeg_calls[0]
        assert cmd[-1].endswith(".wav")
        assert cmd[cmd.index("-c:a") + 1] == "pcm_s24le"
        # No aformat in the filter chain (lossy/lossless WAV bit-depth is
        # the codec choice).
        flat = " ".join(cmd)
        assert "aformat=sample_fmts=s32" not in flat
    finally:
        _cleanup_album(aid)


def test_split_output_format_persisted_in_plan(monkeypatch):
    """The chosen output_format flows back into album.json.plan so a re-edit
    reload sees the same selector position."""
    _MockSplitEnv(monkeypatch)
    from services import albums_fs

    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "output_format": "m4a-alac",
        })
        assert r.status_code == 200, r.text
        manifest = albums_fs.read_manifest(aid)
        assert manifest["plan"]["output_format"] == "m4a-alac"
    finally:
        _cleanup_album(aid)


def test_split_sample_rate_persisted_in_plan(monkeypatch):
    """The chosen sample rate flows back into album.json.plan alongside
    bit_depth, so a re-edit reload sees the same knob position."""
    _MockSplitEnv(monkeypatch)
    from services import albums_fs

    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "sample_rate": 96000,
        })
        assert r.status_code == 200, r.text
        manifest = albums_fs.read_manifest(aid)
        assert manifest["plan"]["sample_rate"] == 96000
    finally:
        _cleanup_album(aid)


def test_split_concat_input_used(monkeypatch):
    """Every per-track ffmpeg invocation reads the SAME album-wide concat
    playlist — that's how -ss/-to act in album time across side
    boundaries."""
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [
                {"title": "T1", "duration_seconds": 100},
                {"title": "T2", "duration_seconds": 100},
            ],
        })
        assert r.status_code == 200
        for cmd in env.ffmpeg_calls:
            i = cmd.index("-i")
            playlist_path = cmd[i + 1]
            # The concat playlist lives under the album's .cache dir.
            assert ".cache" in playlist_path
            # `-f concat -safe 0` is always present.
            assert "-f" in cmd
            assert cmd[cmd.index("-f") + 1] == "concat"
    finally:
        _cleanup_album(aid)


# ── Tag arg shape ────────────────────────────────────────────────────────

def test_split_writes_tag_args_per_track(monkeypatch):
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={
        "artist": "Pink Floyd",
        "album":  "The Wall",
        "year":   "1979",
        "genre":  "Rock",
        "label":  "Harvest",
        "catalog_number": "SHDW 411",
        "country": "UK",
    })
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "Comfortably Numb", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        # The metaflac tag pass is the first call AFTER ffmpeg returns.
        # Each call is a list starting with "metaflac".
        tag_calls = [c for c in env.metaflac_calls if any(
            s.startswith("--set-tag=") for s in c)]
        assert tag_calls
        first = tag_calls[0]
        assert "--remove-all-tags" in first
        assert "--set-tag=ARTIST=Pink Floyd" in first
        assert "--set-tag=ALBUM=The Wall" in first
        assert "--set-tag=DATE=1979" in first
        assert "--set-tag=TITLE=Comfortably Numb" in first
        assert "--set-tag=TRACKNUMBER=1" in first
        assert "--set-tag=TRACKTOTAL=1" in first
        # Optional fields not in this manifest are NOT in the tag list.
        assert not any(s.startswith("--set-tag=MUSICBRAINZ_ALBUMID")
                       for s in first)
        assert not any(s.startswith("--set-tag=DISCOGS_RELEASE_ID")
                       for s in first)
    finally:
        _cleanup_album(aid)


def test_split_includes_mb_and_discogs_ids_when_present(monkeypatch):
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={
        "artist": "X",
        "album":  "Y",
        "musicbrainz_albumid": "3c1c2dab-fcc1-4d1c-9d6f-9ef00bf1f9d7",
        "discogs_release_id":  9999,
    })
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        all_tag_args = [s for c in env.metaflac_calls for s in c]
        assert any("MUSICBRAINZ_ALBUMID=3c1c2dab" in s for s in all_tag_args)
        assert any("DISCOGS_RELEASE_ID=9999" in s for s in all_tag_args)
    finally:
        _cleanup_album(aid)


def test_split_imports_cover_when_present(monkeypatch):
    """If `cover.jpg` exists in the album dir, every track gets a
    `--import-picture-from=...` metaflac call after the tag pass."""
    env = _MockSplitEnv(monkeypatch)
    from services import albums_fs

    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        # Drop a fake cover.jpg into the album dir.
        albums_fs.write_cover(aid, b"\xff\xd8\xff fake jpeg")

        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        cover_calls = [
            c for c in env.metaflac_calls
            if any(s.startswith("--import-picture-from=") for s in c)
        ]
        assert len(cover_calls) == 1
    finally:
        _cleanup_album(aid)


def test_split_no_cover_skips_picture_import(monkeypatch):
    env = _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        cover_calls = [
            c for c in env.metaflac_calls
            if any(s.startswith("--import-picture-from=") for s in c)
        ]
        assert cover_calls == []
    finally:
        _cleanup_album(aid)


# ── Manifest persistence ─────────────────────────────────────────────────

def test_split_writes_plan_and_music_relpath_to_manifest(monkeypatch):
    """After a successful split the manifest carries both the plan (so a
    re-edit reload sees the same cuts) and the music_relpath (the
    "split has been emitted" marker the UI reads)."""
    _MockSplitEnv(monkeypatch)
    from services import albums_fs

    aid = _make_album_with_side(tags={"artist": "Foo", "album": "Bar",
                                      "year": "2020"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
            "normalize": True,
            "target_peak_db": -1.0,
            "measured_peak_db": -3.0,
            "bit_depth": 16,
        })
        assert r.status_code == 200, r.text
        manifest = albums_fs.read_manifest(aid)
        assert manifest["music_relpath"] == "Foo/Bar (2020)"
        plan = manifest["plan"]
        assert plan["normalize"] is True
        assert plan["target_peak_db"] == -1.0
        assert plan["measured_peak_db"] == -3.0
        assert plan["bit_depth"] == 16
        assert plan["tracks"] == [
            {"title": "T1", "duration_seconds": 60.0, "skip": False},
        ]
    finally:
        _cleanup_album(aid)


def test_split_response_body_shape(monkeypatch):
    _MockSplitEnv(monkeypatch)
    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["music_relpath"] == "A/B"  # no year tag → no "(YYYY)" suffix
        assert len(body["tracks"]) == 1
        # Each track row exposes filename + duration + size_mb.
        t = body["tracks"][0]
        assert "filename" in t and t["filename"].endswith(".flac")
        assert isinstance(t["duration_seconds"], (int, float))
        assert isinstance(t["size_mb"], (int, float))
    finally:
        _cleanup_album(aid)


# ── Re-run cleanup (idempotency) ─────────────────────────────────────────

def test_split_clears_prior_music_dir_before_re_emit(monkeypatch):
    """Re-running split when the music dir already has files must wipe
    them first — otherwise stale tracks from a prior run linger and
    confuse the library view."""
    _MockSplitEnv(monkeypatch)
    from state import MUSIC_DIR

    aid = _make_album_with_side(tags={"artist": "A", "album": "B"})
    try:
        # First split.
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        music_dir = MUSIC_DIR / "A" / "B"
        before = sorted(music_dir.glob("*.flac"))
        assert len(before) == 1

        # Drop a stale file the user couldn't have produced this run.
        stale = music_dir / "stale.flac"
        stale.write_bytes(b"old")
        assert stale.exists()

        # Re-split — same plan; the stale file should disappear and only
        # the freshly-written track remains.
        r = _client().post("/api/album/split", json={
            "album_id": aid,
            "tracks": [{"title": "T1", "duration_seconds": 60}],
        })
        assert r.status_code == 200
        after = sorted(music_dir.glob("*.flac"))
        assert len(after) == 1
        assert all("stale" not in p.name for p in after)
    finally:
        _cleanup_album(aid)
