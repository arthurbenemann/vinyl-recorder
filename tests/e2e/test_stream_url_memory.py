"""End-to-end smoke for stream-URL persistence + the recent-URLs datalist.

Drives the seed/render side deterministically via localStorage (the write
side — saving on a successful connect — is covered by the static wiring
guard). Confirms the saved recents land in the #stream-url-recent datalist
after a real page load. (The saved-URL-over-env-default seeding of the input
is pinned by the unit test; e2e can't observe it under the auto-connect test
stack, where the input reflects the live connected URL.)
"""
import pytest

try:
    from playwright.sync_api import expect  # noqa: F401
except ImportError:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e

_URL1 = "http://test-streams:8090/album"
_URL2 = "http://test-streams:8090/clip"


def test_saved_recent_urls_populate_datalist(stack, page):
    """The recent-URLs MRU persists to the #stream-url-recent datalist across a
    real page load.

    Note we assert the datalist, not the input's value: when the server is
    connected the input reflects the *live* connected URL (ws.js), so under the
    auto-connect test stack it shows /loop rather than a saved URL. The
    saved-URL-beats-env-default seeding precedence (which only governs the
    disconnected case) is pinned by the unit test; the datalist is the part the
    live-URL reflection never touches."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")
    # Simulate what rememberStreamUrl persists after successful connects.
    page.evaluate(
        """([u1, u2]) => {
            localStorage.setItem('vr.streamUrl', u1);
            localStorage.setItem('vr.streamUrlRecent', JSON.stringify([u1, u2]));
        }""",
        [_URL1, _URL2],
    )
    page.reload()
    page.wait_for_load_state("networkidle")

    # The datalist is populated from the MRU list, in order, at boot.
    page.wait_for_function(
        "(urls) => { const opts = Array.from("
        "document.querySelectorAll('#stream-url-recent option')).map(o => o.value);"
        " return opts.length === urls.length && opts.every((v, i) => v === urls[i]); }",
        arg=[_URL1, _URL2],
        timeout=10_000,
    )
