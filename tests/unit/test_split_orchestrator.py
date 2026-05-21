"""Unit tests for `app/services/split_orchestrator.py`.

The codec-arg / metadata-flag plumbing (`_FORMAT_SETTINGS`,
`_wav_codec_for_bits`, `_media_type_for`, `_ffmpeg_metadata_args`) is
covered by `test_emit_track_format.py`. This module pins the rest:

  - `kept_duration_total` arithmetic across skip / clamp / final-track-
    fills-rest scenarios (drives the constant-rate progress bar)
  - `wipe_prior_music_dir` cleanup: unlinks every audio extension, drops
    the album dir, and prunes the empty artist parent. Idempotent + safe
    when the prior dir doesn't exist.
  - `write_track_tags` metaflac argv: required tags always written,
    optional classical-style tags only when present, cover embedded
    as a separate invocation.
  - `split_album` validation gates (`SplitValidationError` for empty
    tracks, bad sample rate, bad output format) so a hand-crafted POST
    can't slip arbitrary ffmpeg args through. Don't actually run ffmpeg.

All filesystem state lives in pytest's `tmp_path`. All subprocess calls
are stubbed.
"""
import asyncio
import json

import pytest

from services import split_orchestrator as so
from services.split_orchestrator import (
    SplitDiskSpaceError,
    SplitNotFoundError,
    SplitProcessingError,
    SplitValidationError,
    kept_duration_total,
    split_genres,
    wipe_prior_music_dir,
    write_track_tags,
)
from state import SplitRequest, SplitTrack


# ── split_genres ─────────────────────────────────────────────────────────
def test_split_genres_splits_on_semicolons():
    assert split_genres("Electronic; Techno; House") == ["Electronic", "Techno", "House"]


def test_split_genres_single_value():
    assert split_genres("Rock") == ["Rock"]


def test_split_genres_preserves_commas_within_a_genre():
    # A single Discogs genre with commas must survive intact — only ';' splits.
    assert split_genres("Folk, World, & Country") == ["Folk, World, & Country"]
    assert split_genres("Folk, World, & Country; Techno") == [
        "Folk, World, & Country", "Techno",
    ]


def test_split_genres_empty_and_blank():
    assert split_genres("") == []
    assert split_genres("  ;  ; ") == []


# ── kept_duration_total ──────────────────────────────────────────────────
def test_kept_duration_total_all_tracks_included():
    """When no track is marked `skip`, the kept duration equals the total."""
    tracks = [
        SplitTrack(title="A", duration_seconds=30.0),
        SplitTrack(title="B", duration_seconds=60.0),
        SplitTrack(title="C", duration_seconds=10.0),
    ]
    # The last track is special-cased to absorb the remainder of `total`
    # (so trailing silence/runout still gets emitted) — with these inputs
    # 30+60 = 90s consumed before the last track, last track gets the rest.
    assert kept_duration_total(tracks, total=100.0) == 100.0


def test_kept_duration_total_skips_excluded_tracks():
    """Skip flag removes that track's slice from the kept duration. Skipped
    runs still consume their requested duration window; they just don't
    count toward `kept`."""
    tracks = [
        SplitTrack(title="A", duration_seconds=10.0),
        SplitTrack(title="B", duration_seconds=15.0, skip=True),
        SplitTrack(title="C", duration_seconds=5.0),
    ]
    # Total is 50s; A=10, B=15 (skipped), C absorbs the rest = 50 - 25 = 25.
    # Kept = 10 + 25 = 35.
    assert kept_duration_total(tracks, total=50.0) == 35.0


def test_kept_duration_total_clamps_at_album_total():
    """If the sum of requested track durations exceeds `total`, the cursor
    saturates at `total` and later tracks contribute zero. Defends against
    a stale plan referencing an album that's been re-recorded shorter."""
    tracks = [
        SplitTrack(title="A", duration_seconds=40.0),
        SplitTrack(title="B", duration_seconds=40.0),
        SplitTrack(title="C", duration_seconds=40.0),
    ]
    # Album is 50s. A consumes 0-40, B clamps at 50 (cursor stuck), C contributes 0.
    # Last track special-case fills to total, but B already moved cursor to 50.
    # A: 0->40 (kept 40), B: 40->50 (kept 10), C: 50->50 (kept 0). Total = 50.
    assert kept_duration_total(tracks, total=50.0) == 50.0


def test_kept_duration_total_last_track_absorbs_remainder():
    """The orchestrator's final-track rule: `end_ = total` regardless of
    the requested duration. Without it, a plan that under-shoots leaves
    runout audio unowned."""
    tracks = [
        SplitTrack(title="A", duration_seconds=10.0),
        SplitTrack(title="B", duration_seconds=10.0),  # but album is 100s long
    ]
    # A: 0-10 (10s), B: 10-100 (90s, last-track absorbs).
    assert kept_duration_total(tracks, total=100.0) == 100.0


def test_kept_duration_total_empty_returns_zero():
    assert kept_duration_total([], total=50.0) == 0.0


def test_kept_duration_total_negative_duration_clamps_to_zero():
    """`max(0.0, t.duration_seconds)` defends against a corrupt plan
    sending a negative duration through — the slice collapses to empty."""
    tracks = [
        SplitTrack(title="A", duration_seconds=-5.0),
        SplitTrack(title="B", duration_seconds=10.0),
    ]
    # A: 0-0 (collapsed), B: 0-50 (last-track absorbs).
    assert kept_duration_total(tracks, total=50.0) == 50.0


# ── wipe_prior_music_dir ─────────────────────────────────────────────────
def test_wipe_prior_music_dir_noop_when_relpath_unchanged(tmp_path, monkeypatch):
    """Identity is the dominant case — split re-runs on the same tags
    must not nuke their own output."""
    monkeypatch.setattr(so, "MUSIC_DIR", tmp_path)
    d = tmp_path / "X" / "Y (1999)"
    d.mkdir(parents=True)
    (d / "01 - Track.flac").write_bytes(b"survivor")
    wipe_prior_music_dir("X/Y (1999)", "X/Y (1999)")
    assert (d / "01 - Track.flac").read_bytes() == b"survivor"


def test_wipe_prior_music_dir_noop_when_prior_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "MUSIC_DIR", tmp_path)
    # No directories present — must not raise.
    wipe_prior_music_dir(None, "X/Y (1999)")
    wipe_prior_music_dir("", "X/Y (1999)")


def test_wipe_prior_music_dir_unlinks_audio_and_prunes_parent(tmp_path, monkeypatch):
    """Tag change (artist or album) means the music_relpath moves. The
    old dir's audio files come down, the dir itself is rmdir'd, and the
    empty artist-level parent is pruned."""
    monkeypatch.setattr(so, "MUSIC_DIR", tmp_path)
    prior_dir = tmp_path / "OldArtist" / "OldAlbum"
    prior_dir.mkdir(parents=True)
    (prior_dir / "01 - a.flac").write_bytes(b"")
    (prior_dir / "02 - b.mp3").write_bytes(b"")
    (prior_dir / "03 - c.wav").write_bytes(b"")
    wipe_prior_music_dir("OldArtist/OldAlbum", "NewArtist/NewAlbum")
    # Album dir gone.
    assert not prior_dir.exists()
    # Empty artist parent also pruned.
    assert not (tmp_path / "OldArtist").exists()


def test_wipe_prior_music_dir_leaves_non_audio_files_blocking_rmdir(tmp_path, monkeypatch):
    """A stray non-audio file (cover.jpg the orchestrator hasn't moved
    yet, README, etc.) keeps the dir alive — the unlink loop only targets
    known audio extensions, then the rmdir fails silently. Documenting
    behaviour, not asserting correctness — refactors can change this."""
    monkeypatch.setattr(so, "MUSIC_DIR", tmp_path)
    prior_dir = tmp_path / "X" / "Y"
    prior_dir.mkdir(parents=True)
    (prior_dir / "01.flac").write_bytes(b"")
    (prior_dir / "cover.jpg").write_bytes(b"art")
    wipe_prior_music_dir("X/Y", "Z/W")
    # FLAC gone, JPG survives, dir survives because rmdir of non-empty fails.
    assert not (prior_dir / "01.flac").exists()
    assert (prior_dir / "cover.jpg").exists()
    assert prior_dir.is_dir()


def test_wipe_prior_music_dir_handles_missing_prior(tmp_path, monkeypatch):
    """If the prior dir doesn't actually exist on disk (manifest pointed
    at a path the user wiped externally), the helper must early-return."""
    monkeypatch.setattr(so, "MUSIC_DIR", tmp_path)
    # No 'OldA/OldB' subtree to begin with.
    wipe_prior_music_dir("OldA/OldB", "NewA/NewB")  # must not raise


def test_wipe_prior_music_dir_keeps_non_empty_parent(tmp_path, monkeypatch):
    """If the artist parent has OTHER album dirs, it must NOT be pruned
    — only the wiped album's dir comes down."""
    monkeypatch.setattr(so, "MUSIC_DIR", tmp_path)
    parent = tmp_path / "Artist"
    parent.mkdir()
    wiped = parent / "Wiped Album"
    sibling = parent / "Sibling Album"
    wiped.mkdir(); sibling.mkdir()
    (wiped / "01.flac").write_bytes(b"")
    (sibling / "01.flac").write_bytes(b"")
    wipe_prior_music_dir("Artist/Wiped Album", "Other/Whatever")
    assert not wiped.exists()
    assert sibling.is_dir()
    assert parent.is_dir()


# ── write_track_tags ─────────────────────────────────────────────────────
def test_write_track_tags_emits_required_set(monkeypatch, tmp_path):
    """Every required tag (ARTIST/ALBUM/DATE/GENRE/LABEL/CATALOGNUMBER/
    RELEASECOUNTRY/TITLE/TRACKNUMBER/TRACKTOTAL) is emitted, in one
    metaflac invocation. The empty-string defaults are intentional —
    `--remove-all-tags` clears the FLAC first so blanks aren't carried
    over from prior splits."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(so.subprocess, "run", fake_run)
    out = tmp_path / "01 - Song.flac"
    out.write_bytes(b"")
    tags = {
        "artist": "A", "album": "B", "year": "2020", "genre": "Rock",
        "label": "L", "catalog_number": "CN", "country": "US",
    }
    write_track_tags(out, "Song", out_idx=1, out_total=10, tags=tags,
                     cover_file=None)
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "metaflac"
    assert "--remove-all-tags" in cmd
    assert "--set-tag=ARTIST=A" in cmd
    assert "--set-tag=ALBUM=B" in cmd
    assert "--set-tag=DATE=2020" in cmd
    assert "--set-tag=GENRE=Rock" in cmd
    assert "--set-tag=LABEL=L" in cmd
    assert "--set-tag=CATALOGNUMBER=CN" in cmd
    assert "--set-tag=RELEASECOUNTRY=US" in cmd
    assert "--set-tag=TITLE=Song" in cmd
    assert "--set-tag=TRACKNUMBER=1" in cmd
    assert "--set-tag=TRACKTOTAL=10" in cmd
    # File path is the trailing positional arg.
    assert cmd[-1] == str(out)


def test_write_track_tags_emits_one_genre_tag_per_value(monkeypatch, tmp_path):
    """A ';'-joined genre string becomes repeated GENRE Vorbis comments so
    servers browse each genre independently."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(so.subprocess, "run", fake_run)
    out = tmp_path / "01 - Song.flac"
    out.write_bytes(b"")
    write_track_tags(out, "Song", 1, 1,
                     tags={"artist": "A", "genre": "Electronic; Techno; House"},
                     cover_file=None)
    cmd = calls[0]
    assert cmd.count("--set-tag=GENRE=Electronic") == 1
    assert "--set-tag=GENRE=Techno" in cmd
    assert "--set-tag=GENRE=House" in cmd
    # Three distinct GENRE tags, not one delimited blob.
    assert sum(1 for a in cmd if a.startswith("--set-tag=GENRE=")) == 3


def test_write_track_tags_no_genre_tag_when_blank(monkeypatch, tmp_path):
    """Blank genre writes no GENRE tag at all (no empty `GENRE=` litter)."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(so.subprocess, "run", fake_run)
    out = tmp_path / "01 - Song.flac"
    out.write_bytes(b"")
    write_track_tags(out, "Song", 1, 1, tags={"artist": "A"}, cover_file=None)
    cmd = calls[0]
    assert not any(a.startswith("--set-tag=GENRE=") for a in cmd)


def test_write_track_tags_skips_optional_tags_when_blank(monkeypatch, tmp_path):
    """Composer/Conductor/MusicBrainz/Discogs are only emitted when the
    user provided them — otherwise empty tags would litter every track."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(so.subprocess, "run", fake_run)
    out = tmp_path / "01 - Song.flac"
    out.write_bytes(b"")
    write_track_tags(out, "Song", 1, 1, tags={"artist": "A"}, cover_file=None)
    cmd = calls[0]
    # None of the optional tags should appear.
    assert not any("COMPOSER=" in a for a in cmd)
    assert not any("CONDUCTOR=" in a for a in cmd)
    assert not any("MUSICBRAINZ_ALBUMID=" in a for a in cmd)
    assert not any("DISCOGS_RELEASE_ID=" in a for a in cmd)


def test_write_track_tags_includes_optional_tags_when_set(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(so.subprocess, "run", fake_run)
    out = tmp_path / "01 - Song.flac"
    out.write_bytes(b"")
    tags = {
        "artist": "A", "composer": "Mozart", "conductor": "Karajan",
        "musicbrainz_albumid": "abc-mbid", "discogs_release_id": 12345,
    }
    write_track_tags(out, "Song", 1, 1, tags=tags, cover_file=None)
    cmd = calls[0]
    assert "--set-tag=COMPOSER=Mozart" in cmd
    assert "--set-tag=CONDUCTOR=Karajan" in cmd
    assert "--set-tag=MUSICBRAINZ_ALBUMID=abc-mbid" in cmd
    assert "--set-tag=DISCOGS_RELEASE_ID=12345" in cmd


def test_write_track_tags_imports_cover_as_separate_call(monkeypatch, tmp_path):
    """Cover-art embed is a second metaflac call because metaflac's
    `--import-picture-from` doesn't compose with `--set-tag` flags."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(so.subprocess, "run", fake_run)
    out = tmp_path / "01 - Song.flac"
    out.write_bytes(b"")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"fake-jpeg-bytes")
    write_track_tags(out, "Song", 1, 1, tags={}, cover_file=cover)
    # Two calls: tag-set + picture-import.
    assert len(calls) == 2
    cmd1, cmd2 = calls
    assert any(a.startswith("--set-tag=") for a in cmd1)
    assert cmd2[0] == "metaflac"
    assert any(a.startswith("--import-picture-from=") for a in cmd2)
    assert cmd2[-1] == str(out)


# ── split_album validation paths ─────────────────────────────────────────
def _run_split(req, manifest):
    """Call the async orchestrator synchronously in a fresh loop."""
    return asyncio.run(so.split_album(req, manifest))


def test_split_album_rejects_empty_track_list():
    req = SplitRequest(album_id="abc12345", tracks=[])
    with pytest.raises(SplitValidationError) as ei:
        _run_split(req, manifest={"tags": {}})
    assert "no tracks" in str(ei.value)


def test_split_album_rejects_unsupported_sample_rate():
    """Defence in depth — anything outside `ALLOWED_SPLIT_SAMPLE_RATES`
    must fail before we shell out to ffmpeg with an arbitrary -ar."""
    req = SplitRequest(
        album_id="abc12345",
        tracks=[SplitTrack(title="A", duration_seconds=10.0)],
        sample_rate=12345,  # not in the allowed set
    )
    with pytest.raises(SplitValidationError) as ei:
        _run_split(req, manifest={"tags": {}})
    assert "sample_rate" in str(ei.value)


def test_split_album_rejects_unsupported_output_format():
    req = SplitRequest(
        album_id="abc12345",
        tracks=[SplitTrack(title="A", duration_seconds=10.0)],
        output_format="aiff",  # not in ALLOWED_OUTPUT_FORMATS
    )
    with pytest.raises(SplitValidationError) as ei:
        _run_split(req, manifest={"tags": {}})
    assert "output_format" in str(ei.value)


def test_split_album_propagates_missing_album_as_404_domain_error(monkeypatch):
    """`albums_fs.album_concat_playlist` raises `FileNotFoundError` when
    the album doesn't exist (or has no sides); the orchestrator wraps that
    into `SplitNotFoundError` so the route can map to HTTP 404."""

    def boom(album_id):
        raise FileNotFoundError(f"album {album_id} has no sides")

    monkeypatch.setattr(so.albums_fs, "album_concat_playlist", boom)
    req = SplitRequest(
        album_id="abc12345",
        tracks=[SplitTrack(title="A", duration_seconds=10.0)],
    )
    with pytest.raises(SplitNotFoundError) as ei:
        _run_split(req, manifest={"tags": {}})
    assert "abc12345" in str(ei.value)


def test_split_album_raises_disk_space_error(monkeypatch, tmp_path):
    """Low free space at split time aborts with a `SplitDiskSpaceError`
    BEFORE any encode; the playlist must also be cleaned up so we don't
    litter the .cache dir on every aborted attempt."""
    playlist = tmp_path / "playlist.txt"
    playlist.write_text("")
    side = tmp_path / "side.flac"
    side.write_bytes(b"x" * 16)

    monkeypatch.setattr(
        so.albums_fs, "album_concat_playlist",
        lambda aid: (playlist, [side]),
    )
    monkeypatch.setattr(so, "disk_space_error", lambda need, op: "no space")

    req = SplitRequest(
        album_id="abc12345",
        tracks=[SplitTrack(title="A", duration_seconds=10.0)],
    )
    with pytest.raises(SplitDiskSpaceError) as ei:
        _run_split(req, manifest={"tags": {}})
    assert "no space" in str(ei.value)
    # Playlist was unlinked on the failure path.
    assert not playlist.exists()


def test_split_album_raises_processing_error_on_unreadable_duration(monkeypatch, tmp_path):
    """If every side reports a duration of 0 (metaflac failed), the
    orchestrator can't drive the progress bar — fail loudly rather than
    divide by zero downstream."""
    playlist = tmp_path / "playlist.txt"
    playlist.write_text("")
    side = tmp_path / "side.flac"
    side.write_bytes(b"x" * 16)

    monkeypatch.setattr(
        so.albums_fs, "album_concat_playlist",
        lambda aid: (playlist, [side]),
    )
    monkeypatch.setattr(so, "disk_space_error", lambda need, op: None)
    # Force the duration probe to return None (cache + flac_duration both fail).
    monkeypatch.setattr(so, "flac_duration_seconds", lambda p: None)

    req = SplitRequest(
        album_id="abc12345",
        tracks=[SplitTrack(title="A", duration_seconds=10.0)],
    )
    with pytest.raises(SplitProcessingError) as ei:
        _run_split(req, manifest={"tags": {}})
    assert "duration" in str(ei.value).lower()
    # Playlist cleaned up.
    assert not playlist.exists()


# ── split_album: track-naming + music-dir creation (happy path) ─────────
def test_split_album_emits_tracks_with_zero_padded_numbering(monkeypatch, tmp_path):
    """End-to-end name shape on the happy path. The orchestrator:

      1. computes `pad = max(2, len(str(out_total)))` so a 9-track album
         is `01..09` and a 12-track album is `01..12`.
      2. encodes via `_emit_track`, which formats the filename as
         `<NN> - <safe(title)><ext>`.

    We stub `_emit_track` to capture its kwargs so we don't need ffmpeg.
    """
    # Seed the in-progress dir so albums_fs.write_manifest can persist.
    inp = tmp_path / "in-progress"
    music = tmp_path / "music"
    inp.mkdir(); music.mkdir()
    album_id = "abcd1234"
    (inp / album_id).mkdir()
    playlist = tmp_path / "pl.txt"
    playlist.write_text("")
    side = tmp_path / "side.flac"
    side.write_bytes(b"x" * 16)

    monkeypatch.setattr(so, "MUSIC_DIR", music)
    monkeypatch.setattr(so.albums_fs, "MUSIC_DIR", music)
    monkeypatch.setattr(so.albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(
        so.albums_fs, "album_concat_playlist",
        lambda aid: (playlist, [side]),
    )
    monkeypatch.setattr(so, "disk_space_error", lambda need, op: None)
    monkeypatch.setattr(so, "flac_duration_seconds", lambda p: 60.0)
    monkeypatch.setattr(so.albums_fs, "cover_path", lambda aid: None)

    # Skip the real metaflac shell-out used to read source bit depth.
    monkeypatch.setattr(so.subprocess, "check_output", lambda *a, **k: "24\n44100\n")

    captured: list[dict] = []

    async def fake_emit_track(**kw):
        captured.append(kw)
        # Fake a successful encode by touching the output file.
        out = kw["music_dir"] / f"{str(kw['out_idx']).zfill(kw['pad'])} - Track{ kw['music_dir'].name and ''}.flac"
        # The orchestrator names the file inside _emit_track using safe_path_component;
        # for the test we don't need an exact match, just an existing file for stat.
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 100)
        return {
            "filename": out.name,
            "duration_seconds": kw["end_"] - kw["start_"],
            "size_mb": 0.0,
        }

    monkeypatch.setattr(so, "_emit_track", fake_emit_track)

    # Pre-write a manifest so _persist_split_plan can load + update.
    (inp / album_id / "album.json").write_text(json.dumps({
        "schema_version": 2,
        "tags": {"artist": "Aphex Twin", "album": "Selected Ambient Works",
                 "year": "1992"},
        "sides": ["side.flac"], "cover": None, "plan": None,
        "music_relpath": None, "sources_purged": False,
    }))

    req = SplitRequest(
        album_id=album_id,
        tracks=[
            SplitTrack(title="Xtal", duration_seconds=20.0),
            SplitTrack(title="Tha", duration_seconds=20.0),
            SplitTrack(title="Pulsewidth", duration_seconds=20.0),
        ],
    )
    manifest = so.albums_fs.read_manifest(album_id)
    result = _run_split(req, manifest)

    # music_relpath built from manifest tags.
    assert "Aphex Twin" in result["music_relpath"]
    assert "(1992)" in result["music_relpath"]
    # Three tracks were emitted.
    assert len(captured) == 3
    # Track ordering: out_idx is 1-based, contiguous.
    assert [c["out_idx"] for c in captured] == [1, 2, 3]
    # out_total reflects the kept count (no skips here).
    assert all(c["out_total"] == 3 for c in captured)
    # pad ≥ 2 even though we only have 3 tracks.
    assert all(c["pad"] == 2 for c in captured)


def test_split_album_skip_tracks_renumbers_output(monkeypatch, tmp_path):
    """Skipped tracks don't get filenames or out_idx slots — the orchestrator
    advances `out_idx` only for non-skip entries. A plan A(skip)/B/C should
    emit `01 - B`, `02 - C`."""
    inp = tmp_path / "in-progress"
    music = tmp_path / "music"
    inp.mkdir(); music.mkdir()
    album_id = "abcd5678"
    (inp / album_id).mkdir()
    playlist = tmp_path / "pl.txt"
    playlist.write_text("")
    side = tmp_path / "side.flac"
    side.write_bytes(b"x" * 16)

    monkeypatch.setattr(so, "MUSIC_DIR", music)
    monkeypatch.setattr(so.albums_fs, "MUSIC_DIR", music)
    monkeypatch.setattr(so.albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(
        so.albums_fs, "album_concat_playlist",
        lambda aid: (playlist, [side]),
    )
    monkeypatch.setattr(so, "disk_space_error", lambda need, op: None)
    monkeypatch.setattr(so, "flac_duration_seconds", lambda p: 60.0)
    monkeypatch.setattr(so.albums_fs, "cover_path", lambda aid: None)
    monkeypatch.setattr(so.subprocess, "check_output", lambda *a, **k: "16\n44100\n")

    captured: list[dict] = []

    async def fake_emit_track(**kw):
        captured.append({"out_idx": kw["out_idx"], "title": kw["t"].title,
                         "out_total": kw["out_total"]})
        kw["music_dir"].mkdir(parents=True, exist_ok=True)
        out = kw["music_dir"] / f"{kw['out_idx']:02d}.flac"
        out.write_bytes(b"")
        return {"filename": out.name, "duration_seconds": 1.0, "size_mb": 0.0}

    monkeypatch.setattr(so, "_emit_track", fake_emit_track)

    (inp / album_id / "album.json").write_text(json.dumps({
        "schema_version": 2,
        "tags": {"artist": "X", "album": "Y"},
        "sides": ["side.flac"], "cover": None, "plan": None,
        "music_relpath": None,
    }))

    req = SplitRequest(
        album_id=album_id,
        tracks=[
            SplitTrack(title="Intro", duration_seconds=10.0, skip=True),
            SplitTrack(title="Song B", duration_seconds=20.0),
            SplitTrack(title="Song C", duration_seconds=30.0),
        ],
    )
    manifest = so.albums_fs.read_manifest(album_id)
    result = _run_split(req, manifest)

    # Only two emits (the skipped intro is gone). Numbering is contiguous
    # starting at 1, not "02, 03".
    assert [c["title"] for c in captured] == ["Song B", "Song C"]
    assert [c["out_idx"] for c in captured] == [1, 2]
    assert all(c["out_total"] == 2 for c in captured)
    assert len(result["tracks"]) == 2


def test_split_album_persists_plan_on_success(monkeypatch, tmp_path):
    """After a successful split, the orchestrator writes the resolved plan
    + the new music_relpath into album.json so the wave editor can re-load
    the album with its cuts intact."""
    inp = tmp_path / "in-progress"
    music = tmp_path / "music"
    inp.mkdir(); music.mkdir()
    album_id = "abcdef99"
    (inp / album_id).mkdir()
    playlist = tmp_path / "pl.txt"
    playlist.write_text("")
    side = tmp_path / "side.flac"
    side.write_bytes(b"x" * 16)

    monkeypatch.setattr(so, "MUSIC_DIR", music)
    monkeypatch.setattr(so.albums_fs, "MUSIC_DIR", music)
    monkeypatch.setattr(so.albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(
        so.albums_fs, "album_concat_playlist",
        lambda aid: (playlist, [side]),
    )
    monkeypatch.setattr(so, "disk_space_error", lambda need, op: None)
    monkeypatch.setattr(so, "flac_duration_seconds", lambda p: 30.0)
    monkeypatch.setattr(so.albums_fs, "cover_path", lambda aid: None)
    monkeypatch.setattr(so.subprocess, "check_output", lambda *a, **k: "16\n44100\n")

    async def fake_emit_track(**kw):
        out_dir = kw["music_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{kw['out_idx']:02d}.flac"
        out.write_bytes(b"")
        return {"filename": out.name, "duration_seconds": 1.0, "size_mb": 0.0}

    monkeypatch.setattr(so, "_emit_track", fake_emit_track)

    (inp / album_id / "album.json").write_text(json.dumps({
        "schema_version": 2,
        "tags": {"artist": "Foo", "album": "Bar", "year": "2001"},
        "sides": ["side.flac"], "cover": None, "plan": None,
        "music_relpath": None,
    }))

    req = SplitRequest(
        album_id=album_id,
        tracks=[
            SplitTrack(title="One", duration_seconds=15.0),
            SplitTrack(title="Two", duration_seconds=15.0),
        ],
        normalize=True,
        target_peak_db=-1.0,
        measured_peak_db=-3.0,
        bit_depth=16,
        sample_rate=44100,
        output_format="flac",
    )
    manifest = so.albums_fs.read_manifest(album_id)
    _run_split(req, manifest)

    # Manifest now carries the plan AND the music_relpath.
    persisted = so.albums_fs.read_manifest(album_id)
    assert persisted["music_relpath"] == "Foo/Bar (2001)"
    plan = persisted["plan"]
    assert plan["normalize"] is True
    assert plan["target_peak_db"] == -1.0
    assert plan["measured_peak_db"] == -3.0
    assert plan["bit_depth"] == 16
    assert plan["sample_rate"] == 44100
    assert plan["output_format"] == "flac"
    assert [t["title"] for t in plan["tracks"]] == ["One", "Two"]


def test_split_album_cleans_playlist_after_run(monkeypatch, tmp_path):
    """The playlist file lives under .cache/ as a transient artifact —
    must be unlinked whether the split succeeded or failed mid-encode."""
    inp = tmp_path / "in-progress"
    music = tmp_path / "music"
    inp.mkdir(); music.mkdir()
    album_id = "deadbeef"
    (inp / album_id).mkdir()
    playlist = tmp_path / "playlist.txt"
    playlist.write_text("file 'side.flac'\n")
    side = tmp_path / "side.flac"
    side.write_bytes(b"x" * 16)

    monkeypatch.setattr(so, "MUSIC_DIR", music)
    monkeypatch.setattr(so.albums_fs, "MUSIC_DIR", music)
    monkeypatch.setattr(so.albums_fs, "IN_PROGRESS_DIR", inp)
    monkeypatch.setattr(
        so.albums_fs, "album_concat_playlist",
        lambda aid: (playlist, [side]),
    )
    monkeypatch.setattr(so, "disk_space_error", lambda need, op: None)
    monkeypatch.setattr(so, "flac_duration_seconds", lambda p: 30.0)
    monkeypatch.setattr(so.albums_fs, "cover_path", lambda aid: None)
    monkeypatch.setattr(so.subprocess, "check_output", lambda *a, **k: "16\n")

    async def fake_emit_track(**kw):
        kw["music_dir"].mkdir(parents=True, exist_ok=True)
        out = kw["music_dir"] / "x.flac"
        out.write_bytes(b"")
        return {"filename": "x.flac", "duration_seconds": 1.0, "size_mb": 0.0}

    monkeypatch.setattr(so, "_emit_track", fake_emit_track)

    (inp / album_id / "album.json").write_text(json.dumps({
        "schema_version": 2,
        "tags": {"artist": "X", "album": "Y"},
        "sides": ["side.flac"], "cover": None,
        "plan": None, "music_relpath": None,
    }))

    req = SplitRequest(
        album_id=album_id,
        tracks=[SplitTrack(title="T", duration_seconds=30.0)],
    )
    manifest = so.albums_fs.read_manifest(album_id)
    _run_split(req, manifest)
    assert not playlist.exists()
