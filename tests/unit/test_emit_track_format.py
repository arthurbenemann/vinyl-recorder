"""Pure unit tests for the multi-format export plumbing — no ffmpeg
subprocess calls. The helpers under test build the codec arg list,
metadata flags, and media-type mapping; integration with ffmpeg is
covered in tests/api/test_album_split.py."""

from services.split_orchestrator import (
    _FORMAT_SETTINGS, _disc_for_time, _disc_total, _ffmpeg_metadata_args,
    _is_compilation, _media_type_for, _pan_filter, _side_index_for_time,
    _wav_codec_for_bits, build_audio_filters,
)
from state import ALLOWED_CHANNEL_MODES, ALLOWED_OUTPUT_FORMATS


# ── _pan_filter ──────────────────────────────────────────────────────────
def test_pan_filter_stereo_is_noop():
    assert _pan_filter("stereo") == ""
    # Unknown / default → no filter (never smuggle a bad pan expr).
    assert _pan_filter("bogus") == ""


def test_pan_filter_mono_sums_channels():
    assert _pan_filter("mono") == "pan=mono|c0=0.5*c0+0.5*c1"


def test_pan_filter_left_and_right_pick_one_channel():
    assert _pan_filter("left") == "pan=mono|c0=c0"
    assert _pan_filter("right") == "pan=mono|c0=c1"


def test_pan_filter_covers_all_non_stereo_modes():
    # Every allowed non-stereo mode yields a filter; stereo yields none.
    for m in ALLOWED_CHANNEL_MODES:
        out = _pan_filter(m)
        assert (out == "") == (m == "stereo")


# ── build_audio_filters ──────────────────────────────────────────────────
def _af(**kw):
    base = dict(apply_gain=False, gain_db=0.0, target_rate=None,
                sample_fmt=None, lossless=True)
    base.update(kw)
    return build_audio_filters(**base)


def test_build_af_empty_when_nothing_requested():
    assert _af() == []


def test_build_af_gain_only():
    assert _af(apply_gain=True, gain_db=3.0) == ["volume=3.0000dB"]


def test_build_af_24bit_uses_aformat_no_dither():
    # Going to 24-bit is lossless — no dither, plain aformat.
    assert _af(sample_fmt="s32") == ["aformat=sample_fmts=s32"]


def test_build_af_16bit_dithers_via_aresample():
    # 24→16 truncation must be dithered; done by aresample (osf+dither),
    # NOT a hard-truncating aformat.
    af = _af(sample_fmt="s16")
    assert af == ["aresample=osf=s16:dither_method=triangular_hp"]
    assert not any(a.startswith("aformat=") for a in af)


def test_build_af_resample_only():
    assert _af(target_rate=44100) == ["aresample=resampler=soxr:precision=28"]


def test_build_af_resample_plus_16bit_single_aresample():
    # One aresample carries both the rate change and the dithered reduction.
    af = _af(target_rate=48000, sample_fmt="s16")
    assert af == ["aresample=resampler=soxr:precision=28:osf=s16:dither_method=triangular_hp"]


def test_build_af_resample_plus_24bit():
    af = _af(target_rate=88200, sample_fmt="s32")
    assert af == ["aresample=resampler=soxr:precision=28", "aformat=sample_fmts=s32"]


def test_build_af_lossy_ignores_bit_depth_and_dither():
    # Lossy codecs pick their own precision — no aformat, no dither.
    assert _af(sample_fmt="s16", lossless=False) == []
    assert _af(target_rate=44100, sample_fmt="s16", lossless=False) == [
        "aresample=resampler=soxr:precision=28",
    ]


def test_allowed_formats_match_settings_keys():
    assert set(_FORMAT_SETTINGS.keys()) == set(ALLOWED_OUTPUT_FORMATS)


def test_wav_codec_for_bits_defaults_to_16le():
    assert _wav_codec_for_bits(None) == "pcm_s16le"
    assert _wav_codec_for_bits(0)    == "pcm_s16le"
    assert _wav_codec_for_bits(16)   == "pcm_s16le"


def test_wav_codec_for_bits_24_picks_24le():
    assert _wav_codec_for_bits(24) == "pcm_s24le"


def test_media_type_for_known_extensions():
    assert _media_type_for(".flac") == "audio/flac"
    assert _media_type_for(".mp3")  == "audio/mpeg"
    assert _media_type_for(".m4a")  == "audio/mp4"
    assert _media_type_for(".wav")  == "audio/wav"
    assert _media_type_for(".ogg")  == "audio/ogg"


def test_media_type_for_unknown_falls_back():
    assert _media_type_for(".xyz") == "application/octet-stream"


def test_media_type_for_uppercase_extension():
    # Suffix from pathlib is lowercased only when the file system is, so
    # keep the helper case-insensitive.
    assert _media_type_for(".FLAC") == "audio/flac"


def test_ffmpeg_metadata_args_omits_empty_fields():
    args = _ffmpeg_metadata_args(
        "Song", 1, 10,
        {"artist": "A", "album": "X", "year": "", "genre": ""}, None,
    )
    flat = " ".join(args)
    assert "artist=A" in flat
    assert "album=X" in flat
    assert "date=" not in flat   # year was empty
    assert "genre=" not in flat


def test_ffmpeg_metadata_args_sets_album_artist_from_artist():
    """Non-FLAC encodes bake ALBUMARTIST in via the ffmpeg `album_artist`
    metadata key (TPE2 / aART / vorbis ALBUMARTIST), defaulting to ARTIST so
    the album groups correctly in music servers."""
    args = _ffmpeg_metadata_args("Song", 1, 10, {"artist": "A"}, None)
    flat = " ".join(args)
    assert "album_artist=A" in flat
    # Single-artist album: no compilation flag.
    assert "compilation=" not in flat


def test_ffmpeg_metadata_args_sets_compilation_for_various_artists():
    args = _ffmpeg_metadata_args(
        "Song", 1, 10, {"artist": "Various Artists"}, None,
    )
    flat = " ".join(args)
    assert "album_artist=Various Artists" in flat
    assert "compilation=1" in flat


def test_is_compilation_detects_various_artists():
    assert _is_compilation({"artist": "Various Artists"}) is True
    assert _is_compilation({"artist": "various artists"}) is True
    assert _is_compilation({"artist": "The Beatles"}) is False
    assert _is_compilation({}) is False


def test_ffmpeg_metadata_args_includes_composer_conductor():
    args = _ffmpeg_metadata_args(
        "Song", 3, 8,
        {"composer": "Beethoven", "conductor": "Karajan"}, None,
    )
    flat = " ".join(args)
    assert "composer=Beethoven" in flat
    assert "conductor=Karajan"  in flat
    assert "track=3/8" in flat


def test_ffmpeg_metadata_args_always_includes_title_and_track():
    args = _ffmpeg_metadata_args("Hello", 2, 5, {}, None)
    flat = " ".join(args)
    assert "title=Hello" in flat
    assert "track=2/5" in flat


def test_ffmpeg_metadata_args_uses_flag_pairs():
    # Ensure each metadata pair is two ffmpeg tokens: -metadata KEY=VAL.
    args = _ffmpeg_metadata_args("Song", 1, 1, {"artist": "A"}, None)
    # -metadata title=Song -metadata track=1/1 -metadata artist=A
    assert args.count("-metadata") == len(args) // 2


# ── disc tags (multi-LP sets) ────────────────────────────────────────────
def test_ffmpeg_metadata_args_emits_disc_for_multidisc():
    args = _ffmpeg_metadata_args("Song", 1, 4, {}, None, disc=2, disc_total=2)
    assert "disc=2/2" in " ".join(args)


def test_ffmpeg_metadata_args_omits_disc_for_single_disc():
    # disc_total <= 1 → no disc tag (Jellyfin treats absent as disc 1).
    args = _ffmpeg_metadata_args("Song", 1, 4, {}, None, disc=1, disc_total=1)
    assert "disc=" not in " ".join(args)
    # And the default (no disc args supplied at all) omits it too.
    assert "disc=" not in " ".join(_ffmpeg_metadata_args("Song", 1, 4, {}, None))


# ── disc derivation helpers ──────────────────────────────────────────────
def test_disc_total_pairs_sides_into_lps():
    assert _disc_total(0) == 1
    assert _disc_total(1) == 1   # one-sided capture
    assert _disc_total(2) == 1   # single LP (A/B)
    assert _disc_total(3) == 2   # 2-LP with a blank/etched 4th side
    assert _disc_total(4) == 2   # 2-LP (A/B/C/D)
    assert _disc_total(6) == 3   # 3-LP


def test_side_index_for_time_locates_the_side():
    # Three 100s sides → boundaries at 100, 200.
    sides = [100.0, 100.0, 100.0]
    assert _side_index_for_time(0.0, sides)   == 0
    assert _side_index_for_time(99.0, sides)  == 0
    assert _side_index_for_time(100.0, sides) == 1   # boundary belongs to next
    assert _side_index_for_time(150.0, sides) == 1
    assert _side_index_for_time(250.0, sides) == 2
    # At/after the end clamps to the last side (e.g. an end-of-album cut).
    assert _side_index_for_time(300.0, sides) == 2
    assert _side_index_for_time(999.0, sides) == 2


def test_disc_for_time_maps_sides_to_discs():
    # 2-LP: sides A,B (disc 1) then C,D (disc 2), 100s each.
    sides = [100.0, 100.0, 100.0, 100.0]
    assert _disc_for_time(50.0,  sides) == 1   # side A
    assert _disc_for_time(150.0, sides) == 1   # side B
    assert _disc_for_time(250.0, sides) == 2   # side C
    assert _disc_for_time(350.0, sides) == 2   # side D


def test_ffmpeg_metadata_args_emits_originaldate_when_present():
    args = _ffmpeg_metadata_args("Song", 1, 10, {"original_year": "1973"}, None)
    assert "originaldate=1973" in " ".join(args)
    # Absent when not supplied.
    assert "originaldate=" not in " ".join(_ffmpeg_metadata_args("Song", 1, 10, {}, None))


def test_format_settings_have_required_keys():
    for fmt, settings in _FORMAT_SETTINGS.items():
        assert "ext" in settings,         f"{fmt} missing ext"
        assert "ffmpeg_args" in settings, f"{fmt} missing ffmpeg_args"
        assert settings["ext"].startswith("."), f"{fmt} ext lacks leading dot"
        assert "lossless" in settings,    f"{fmt} missing lossless flag"


def test_format_settings_lossless_flags():
    """Sanity-check the lossless-vs-lossy partition. The frontend's bit-
    depth-disabled UI keys on the same set, so a mistake here would surface
    as a confusing UX bug."""
    assert _FORMAT_SETTINGS["flac"]["lossless"]     is True
    assert _FORMAT_SETTINGS["wav"]["lossless"]      is True
    assert _FORMAT_SETTINGS["m4a-alac"]["lossless"] is True
    assert _FORMAT_SETTINGS["mp3"]["lossless"]      is False
    assert _FORMAT_SETTINGS["ogg"]["lossless"]      is False
    assert _FORMAT_SETTINGS["m4a-aac"]["lossless"]  is False


def test_format_settings_extensions():
    """Both AAC and ALAC variants share the .m4a container."""
    assert _FORMAT_SETTINGS["m4a-aac"]["ext"]  == ".m4a"
    assert _FORMAT_SETTINGS["m4a-alac"]["ext"] == ".m4a"
    assert _FORMAT_SETTINGS["flac"]["ext"]     == ".flac"
    assert _FORMAT_SETTINGS["wav"]["ext"]      == ".wav"
    assert _FORMAT_SETTINGS["mp3"]["ext"]      == ".mp3"
    assert _FORMAT_SETTINGS["ogg"]["ext"]      == ".ogg"


def test_only_flac_supports_metaflac():
    """metaflac handles FLAC tag writing; other containers need ffmpeg
    -metadata flags. Pinned so a future format addition has to update
    both this test and _emit_track's branch logic together."""
    assert _FORMAT_SETTINGS["flac"]["supports_metaflac"]     is True
    assert _FORMAT_SETTINGS["wav"]["supports_metaflac"]      is False
    assert _FORMAT_SETTINGS["mp3"]["supports_metaflac"]      is False
    assert _FORMAT_SETTINGS["ogg"]["supports_metaflac"]      is False
    assert _FORMAT_SETTINGS["m4a-aac"]["supports_metaflac"]  is False
    assert _FORMAT_SETTINGS["m4a-alac"]["supports_metaflac"] is False
