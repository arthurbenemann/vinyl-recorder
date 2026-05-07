"""Round-trip tests for `safe_path_component` ↔ `music_relpath` ↔ filesystem.

Albums in `in-progress/` carry user-supplied tags (artist / album / year)
that get sanitized into the Jellyfin-shaped `music/{Artist}/{Album} (Year)/`
tree at split time. If the sanitizer ever lets a `<>:"/\\|?*` or control
char slip through, music_relpath corrupts — at best the OS rejects mkdir;
at worst we land tracks under a different path than the manifest records,
and the per-track download endpoint can't find them.

These tests stress-test the chars commonly thrown at us by real release
metadata: AC/DC, Hi:Lo, Mötley Crüe, the unicode em dash, and so on.
"""
import pytest

from services.ffmpeg import safe_path_component


# ── boundary-char sanitization (the big risk) ────────────────────────────

# Windows/macOS hostile set + control chars. None of these may appear in
# the output, regardless of input position.
_FORBIDDEN = '<>:"/\\|?*\x00\x01\x1f'


@pytest.mark.parametrize("inp", [
    'AC/DC',
    'Hi:Lo',
    'Q&A "Live"',
    '<script>',
    'foo|bar',
    'a*b?c',
    'path\\with\\slashes',
    'a\x00b\x1fc',                # control chars
    'normal name',                  # baseline: pass through
    'Mötley Crüe',                  # unicode word chars stay
    'Sigur Rós — Ágætis byrjun',    # em dash + accents
])
def test_safe_path_component_strips_only_forbidden(inp):
    out = safe_path_component(inp)
    for ch in _FORBIDDEN:
        assert ch not in out, f"{ch!r} survived in {out!r}"


def test_safe_path_component_preserves_spaces_and_punctuation():
    # The Jellyfin tree reads better with spaces preserved (unlike
    # safe_name which underscores them). Apostrophes, hyphens, parens
    # and period-in-middle survive too.
    assert safe_path_component("Don't Stop Believin'") == "Don't Stop Believin'"
    assert safe_path_component("Side A - Track 1") == "Side A - Track 1"
    assert safe_path_component("Greatest Hits (Vol. 2)") == "Greatest Hits (Vol. 2)"


def test_safe_path_component_strips_trailing_dots_and_whitespace():
    # Windows refuses paths ending in '.' or ' ', and NTFS silently drops
    # trailing dots — cross-platform safety net regardless of where the
    # output dir is mounted.
    assert safe_path_component("Album.") == "Album"
    assert safe_path_component("Album...") == "Album"
    assert safe_path_component("Album ") == "Album"


def test_safe_path_component_falls_back_to_unknown_when_empty():
    # An entirely-stripped name (e.g. all slashes) must still produce a
    # usable single component — empty path components corrupt the tree.
    assert safe_path_component("") == "Unknown"
    assert safe_path_component("///") == "Unknown"
    assert safe_path_component("...") == "Unknown"


# ── filesystem round-trip ────────────────────────────────────────────────
# Real I/O: assemble a music_relpath from tags and assert that mkdir
# actually works. Catches any OS-specific char that snuck past the regex.

@pytest.mark.parametrize("artist, album, year", [
    ("AC/DC",                 "Hi:Lo",                "1981"),
    ("Sigur Rós",             "Ágætis byrjun",        "1999"),
    ('Q&A "Live"',            "<best of>",            "2003"),
    ("Mötley Crüe",           "Dr. Feelgood",         "1989"),
    ("foo|bar",               "wild?card*name",       ""),    # empty year
])
def test_music_relpath_from_tags_mkdirs_cleanly(tmp_path, artist, album, year):
    """The same `_music_dir_for(tags)` shape the split route uses, expressed
    here in the test for layering reasons (the helper is in routes/albums.py
    not services). Verifies that no character in the input survives to break
    `Path.mkdir(parents=True)`."""
    a = safe_path_component(artist)
    b = safe_path_component(album)
    album_dir = f"{b} ({year})" if year else b
    relpath = f"{a}/{album_dir}"
    target = tmp_path / relpath
    target.mkdir(parents=True, exist_ok=False)  # raises if relpath is fishy
    assert target.is_dir()
    # The dir name on disk must match what the manifest will record — if
    # the OS silently rewrites a char (e.g. NTFS dropping trailing dot) the
    # next read of `music_relpath` would point at a missing dir.
    assert target.name == album_dir


def test_music_relpath_does_not_traverse_out(tmp_path):
    """A pathological input must not let the artist/album escape the music
    root. `safe_path_component` is the only line of defense — there's no
    further chroot."""
    artist = "../../etc"
    album  = "../../passwd"
    rel = f"{safe_path_component(artist)}/{safe_path_component(album)}"
    # Forbidden chars are stripped, leaving "...." and ".." which then get
    # trimmed to nothing → "Unknown". Either way, the resolved path stays
    # inside tmp_path.
    resolved = (tmp_path / rel).resolve()
    assert tmp_path.resolve() in resolved.parents or resolved == tmp_path.resolve()


def test_safe_path_component_matches_metaflac_tag_round_trip():
    """If the user types `AC/DC` into the artist field, the manifest stores
    it verbatim — but the music dir uses `ACDC`. This test pins down the
    asymmetry so a future "preserve the slash by escaping" idea doesn't
    silently break the music tree (FAT-32 is unforgiving about `/`)."""
    raw_tag = "AC/DC"
    sanitized = safe_path_component(raw_tag)
    assert sanitized == "ACDC"
    # The manifest stores the raw form; only music_relpath uses sanitized.
    # This separation is intentional — Vorbis tags have no path semantics.
    assert "/" in raw_tag
    assert "/" not in sanitized
