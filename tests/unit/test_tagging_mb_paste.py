"""Static guards for the "paste a MusicBrainz release link" path.

The tag panel already accepted a pasted Discogs link; this adds the
MusicBrainz equivalent (paste a `musicbrainz.org/release/<id>` URL or a
bare MBID to load that exact pressing, bypassing search). The parser +
wiring live in `tagging.js`, an ES module the node sandbox can't import
(it pulls in 8 sibling modules at load), so — like the Discogs sibling —
the behaviour is exercised end-to-end by Playwright. These substring
checks are the cheap regression net for the wiring itself.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TAGGING = REPO_ROOT / "app" / "static" / "modules" / "tagging.js"


@pytest.fixture(scope="module")
def js() -> str:
    return TAGGING.read_text(encoding="utf-8")


def test_mbid_parser_defined(js):
    assert "function _parseMbReleaseMbid" in js


def test_parser_requires_release_path_not_release_group(js):
    # The URL regex must anchor on `/release/` so a pasted release-GROUP /
    # recording / artist link (same UUID shape) isn't fed to
    # /api/release/{mbid} as if it were a release. The pattern is built in a
    # `new RegExp(`…`)` template literal, so the source carries a doubled
    # backslash before the dot.
    assert r"musicbrainz\\.org/release/" in js
    # A full 8-4-4-4-12 UUID shape is matched (not a loose hex run).
    assert "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}" in js


def test_findmode_routes_pasted_mbid(js):
    # _findMode must consult the parser and emit the new mode kind.
    assert "_parseMbReleaseMbid(t)" in js
    assert "'mb-release'" in js


def test_enter_dispatches_to_mb_fetch(js):
    assert "_fetchMbReleaseByMbid(mode.mbid)" in js
    assert "function _fetchMbReleaseByMbid" in js


def test_shared_loader_used_by_both_paths(js):
    # The candidate-pick and paste paths funnel through one loader so the
    # field-population / cover / status logic can't drift between them.
    assert "function _loadMbRelease" in js
    assert "_loadMbRelease(c.mbid, 'candidate')" in js
    assert "_loadMbRelease(mbid, 'paste')" in js
    # The paste status reads differently so the user knows it came from a paste.
    assert "from MusicBrainz paste" in js


def test_empty_state_copy_mentions_musicbrainz_paste(js):
    # The hint that teaches the feature should name both link types now.
    assert "paste a Discogs or MusicBrainz link" in js
