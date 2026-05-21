"""End-to-end smoke for stream-URL persistence + the recent-URLs datalist.

Drives the seed/render side deterministically via localStorage (the write
side — saving on a successful connect — is covered by the static wiring
guard). Confirms a saved URL pre-fills the input over the env default and
the recents land in the datalist after a real page load.
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


def test_saved_stream_url_seeds_input_and_datalist(stack, page):
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

    # The saved URL wins over the test stack's DEFAULT_STREAM_URL (/loop).
    page.wait_for_function(
        "(u) => document.getElementById('stream-url').value === u",
        arg=_URL1,
        timeout=10_000,
    )
    # The datalist is populated from the MRU list, in order.
    opts = page.evaluate(
        "() => Array.from(document.querySelectorAll('#stream-url-recent option'))"
        "  .map(o => o.value)"
    )
    assert opts == [_URL1, _URL2], opts
