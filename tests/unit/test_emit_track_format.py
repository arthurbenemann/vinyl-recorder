"""Pure unit tests for the multi-format export plumbing — no ffmpeg
subprocess calls. The helpers under test build the codec arg list,
metadata flags, and media-type mapping; integration with ffmpeg is
covered in tests/api/test_album_split.py."""

from services.split_orchestrator import (
    _FORMAT_SETTINGS, _disc_for_time, _disc_total, _ffmpeg_metadata_args,
    _media_type_for, _side_index_for_time, _wav_codec_for_bits,
)
from state import ALLOWED_OUTPUT_FORMATS


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
