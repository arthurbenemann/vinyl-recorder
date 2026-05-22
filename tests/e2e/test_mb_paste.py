"""End-to-end smoke for pasting a MusicBrainz release link into the tag
panel's find bar.

The panel already loaded a pasted Discogs link; this exercises the
MusicBrainz parity path. `/api/release/{mbid}` is stubbed via Playwright
route interception so the test is deterministic and never touches the live
MB endpoint — what's under test is the frontend parser + wiring (detect a
pasted URL / bare MBID, fetch, populate the left-hand fields), not MB
itself.
"""
import json
import subprocess
import time
from pathlib import Path

import pytest

try:
    from playwright.sync_api import expect  # noqa: F401
except ImportError:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e

# A real MB release UUID shape; the value is arbitrary because the endpoint
# is stubbed. Reused for both the URL and the bare-MBID assertions.
MBID = "3c1c2dab-fcc1-4d1c-9d6f-9ef00bf1f9d7"


def _seed_raw_flac(raw_dir: Path, name: str) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    p = raw_dir / name
    rel = p.relative_to(raw_dir.parent)
    subprocess.run(
        ["docker", "exec", "vinyl-recorder", "ffmpeg",
         "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=f=440:duration=3,volume=0.5",
         "-ar", "96000", "-ac", "2", "-c:a", "flac", "-y",
         f"/output/{rel}"],
        check=True, capture_output=True, text=True,
    )
    return p


def _stub_release_route(page):
    """Intercept the exact MBID fetch and return a canned release so the
    populate path runs without the network."""
    body = json.dumps({
        "mbid": MBID, "title": "Pasted Album", "artist": "Pasted Artist",
        "year": "1977", "genre": "Rock", "label": "Harvest",
        "catalog_number": "SHVL 815", "country": "GB", "format": "Vinyl",
        "composer": "", "conductor": "",
        "tracks": ["Pigs on the Wing 1", "Dogs"],
        "track_details": [], "discogs_id": None, "discogs_url": None,
        "cover_url": None,
    })

    def handler(route):
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route(f"**/api/release/{MBID}", handler)


def test_paste_musicbrainz_release_link_populates_tags(stack, page):
    raw = stack["raw"]
    stamp = int(time.time())
    fname = f"mbpaste_{stamp}.flac"
    seeded = _seed_raw_flac(raw, fname)
    try:
        _stub_release_route(page)
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        # Open the tag panel directly on the seeded raw side.
        page.wait_for_selector(
            f'#raw-section tbody input.row-check[data-fname="{fname}"]',
            timeout=10_000,
        )
        page.evaluate("(f) => openTag(f)", fname)
        page.wait_for_selector("#tag-modal:not([hidden])", timeout=5_000)

        def paste(text):
            page.evaluate(
                "(t) => { const i = document.getElementById('t-search');"
                " i.value = t; onFindInput(t); }",
                text,
            )

        # ── Pasting a full release URL flips the bar into mb-release mode ──
        paste(f"https://musicbrainz.org/release/{MBID}")
        subtitle = page.text_content("#t-find-subtitle") or ""
        assert "fetch MusicBrainz release" in subtitle, subtitle

        # ── Enter fetches (stubbed) + fills the left-hand fields ──────────
        page.evaluate("() => onFindEnter()")
        page.wait_for_function(
            "() => document.getElementById('t-artist').value === 'Pasted Artist'",
            timeout=10_000,
        )
        assert page.input_value("#t-album") == "Pasted Album"
        assert page.input_value("#t-year") == "1977"
        assert page.input_value("#t-label") == "Harvest"
        status = page.text_content("#t-search-status") or ""
        assert "from MusicBrainz paste" in status, status
        # onFindEnter clears the bar after a successful load.
        assert page.input_value("#t-search") == ""

        # ── A bare MBID is also accepted (parser's bare-UUID branch) ──────
        paste(MBID)
        subtitle = page.text_content("#t-find-subtitle") or ""
        assert "fetch MusicBrainz release" in subtitle, subtitle

        # ── A release-GROUP link must NOT be treated as a release ─────────
        # Same UUID shape, different entity — it should fall through to
        # collection-filter mode, never the mb-release fetch hint.
        paste(f"https://musicbrainz.org/release-group/{MBID}")
        subtitle = page.text_content("#t-find-subtitle") or ""
        assert "fetch MusicBrainz release" not in subtitle, subtitle
    finally:
        try:
            seeded.unlink(missing_ok=True)
        except Exception:
            pass
