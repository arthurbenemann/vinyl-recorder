"""Unit tests for the pure helpers in services/ffmpeg.py.
Album-folder + manifest helpers live in tests/unit/test_albums_fs.py."""
from services.ffmpeg import (
    _parse_db,
    parse_astats,
    parse_silencedetect,
    safe_name,
    safe_path_component,
)


# ── safe_path_component ──────────────────────────────────────────────────
# The Jellyfin-tree path sanitizer keeps spaces (so albums/dirs read
# naturally) but drops the chars that would break filesystems.
def test_safe_path_component_preserves_spaces():
    assert safe_path_component("Hello World") == "Hello World"


def test_safe_path_component_strips_filesystem_hostile_chars():
    # `<>:"/\|?*` are reserved on at least one of NTFS/HFS/FAT.
    assert safe_path_component('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"


def test_safe_path_component_strips_control_chars():
    assert safe_path_component("a\x00b\x1fc") == "abc"


def test_safe_path_component_strips_trailing_dots():
    # Trailing dots are illegal on Windows (silently stripped). Strip them
    # ourselves so cross-OS-mounted output dirs behave the same.
    assert safe_path_component("Album.") == "Album"


def test_safe_path_component_empty_falls_back_to_unknown():
    assert safe_path_component("") == "Unknown"
    assert safe_path_component("///") == "Unknown"


# ── safe_name ────────────────────────────────────────────────────────────
def test_safe_name_replaces_spaces_with_underscore():
    assert safe_name("Hello World") == "Hello_World"


def test_safe_name_strips_path_and_punctuation():
    # The regex keeps word chars, whitespace, hyphen, dot. Slashes, colons,
    # and shell metas are dropped — ensures filenames can't escape OUTPUT_DIR.
    assert safe_name("foo/bar:baz") == "foobarbaz"
    # `.strip()` runs before space->underscore, so trailing whitespace from
    # dropped punctuation doesn't become a trailing underscore.
    assert safe_name("a; rm -rf /") == "a_rm_-rf"


def test_safe_name_preserves_dots_and_hyphens():
    assert safe_name("Side A - Track.01") == "Side_A_-_Track.01"


def test_safe_name_preserves_unicode_word_chars():
    # Python regex \w is unicode-aware on str patterns, so accented chars stay.
    assert safe_name("Mötley Crüe") == "Mötley_Crüe"


def test_safe_name_empty_input_falls_back_to_untitled():
    assert safe_name("") == "untitled"
    assert safe_name("   ") == "untitled"
    assert safe_name("///") == "untitled"


# ── _parse_db ────────────────────────────────────────────────────────────
def test_parse_db_finite_values():
    assert _parse_db("-12.5") == -12.5
    assert _parse_db("0") == 0.0
    assert _parse_db("  -1.0  ") == -1.0


def test_parse_db_sentinels_become_none():
    # ffmpeg emits "-inf" for digital silence; parser must not raise.
    assert _parse_db("-inf") is None
    assert _parse_db("inf") is None
    assert _parse_db("nan") is None
    assert _parse_db("") is None


def test_parse_db_garbage_returns_none():
    assert _parse_db("not a number") is None


# ── parse_silencedetect ──────────────────────────────────────────────────
SILENCEDETECT_FIXTURE = """\
[silencedetect @ 0xdeadbeef] silence_start: 0
[silencedetect @ 0xdeadbeef] silence_end: 5.00003 | silence_duration: 5.00003
[silencedetect @ 0xdeadbeef] silence_start: 35
[silencedetect @ 0xdeadbeef] silence_end: 37 | silence_duration: 2.00005
[silencedetect @ 0xdeadbeef] silence_start: 62
[silencedetect @ 0xdeadbeef] silence_end: 82 | silence_duration: 20
"""


def test_parse_silencedetect_extracts_all_intervals():
    silences = parse_silencedetect(SILENCEDETECT_FIXTURE)
    assert len(silences) == 3
    assert silences[0] == {"start": 0.0, "end": 5.00003, "duration": 5.00003}
    assert silences[2] == {"start": 62.0, "end": 82.0, "duration": 20.0}


def test_parse_silencedetect_handles_dangling_start_without_end():
    # A silence_start with no matching end (stream ended in silence) must not
    # leak into the output as a malformed entry — silences need both bounds.
    text = "[silencedetect @ 0x1] silence_start: 10\n"
    assert parse_silencedetect(text) == []


def test_parse_silencedetect_ignores_unrelated_lines():
    assert parse_silencedetect("nothing to see here\nffmpeg version 6.x\n") == []


# ── parse_astats ─────────────────────────────────────────────────────────
ASTATS_FIXTURE = """\
[Parsed_astats_0 @ 0x1] Channel: 1
[Parsed_astats_0 @ 0x1] Peak level dB: -8.070177
[Parsed_astats_0 @ 0x1] RMS trough dB: -42.0
[Parsed_astats_0 @ 0x1] Channel: 2
[Parsed_astats_0 @ 0x1] Peak level dB: -10.5
[Parsed_astats_0 @ 0x1] RMS trough dB: -38.0
[Parsed_astats_0 @ 0x1] Peak level dB: -8.070177
[Parsed_astats_0 @ 0x1] RMS trough dB: -38.0
"""


def test_parse_astats_picks_loudest_peak_and_lowest_rms_trough():
    # Peak: max across channels (loudest moment). Noise floor: min RMS trough
    # (quietest sustained section). Asymmetric on purpose — we want the
    # widest possible dynamic-range estimate.
    stats = parse_astats(ASTATS_FIXTURE)
    assert stats == {"peak_db": -8.070177, "noise_floor_db": -42.0}


def test_parse_astats_returns_none_when_silent():
    # ffmpeg reports "-inf" for fully silent input; parser should yield None.
    text = (
        "[astats] Peak level dB: -inf\n"
        "[astats] RMS trough dB: -inf\n"
    )
    assert parse_astats(text) == {"peak_db": None, "noise_floor_db": None}


def test_parse_astats_empty_input():
    assert parse_astats("") == {"peak_db": None, "noise_floor_db": None}
