"""Pure unit tests for the multi-format export plumbing — no ffmpeg
subprocess calls. The helpers under test build the codec arg list,
metadata flags, and media-type mapping; integration with ffmpeg is
covered in tests/api/test_album_split.py."""

from services.split_orchestrator import (
    _FORMAT_SETTINGS, _ffmpeg_metadata_args, _media_type_for, _pan_filter,
    _wav_codec_for_bits,
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
