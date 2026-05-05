"""Playwright smoke tests for the recorder UI.

Big picture: `main.js` (~56 kB) and `wave-editor.js` (~39 kB) are by far
the largest untested surface in this project. These tests aren't trying
to replace manual QA — they cover the boring "did the wires fall off"
regressions that are easy to introduce when refactoring the WebSocket
handler or the record button state machine.

Runs against the test-streams compose stack (same `stack` fixture as
the rest of e2e). Headless chromium; pytest-playwright supplies the
`page` fixture.
"""
import re

import pytest

try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover — only in environments without playwright
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e


# Generous timeouts because every "interaction" goes through the
# WebSocket round-trip and a real ffmpeg pipeline.
WS_SETTLE_MS = 10_000


def test_page_loads_with_main_controls(stack, page):
    """Bare-minimum smoke: page renders and the controls the user touches
    first are all visible and labelled."""
    page.goto(RECORDER_URL)
    expect(page).to_have_title(re.compile(r"vinyl recorder", re.IGNORECASE))
    expect(page.locator("#connect-btn")).to_be_visible()
    expect(page.locator("#recbtn")).to_be_visible()
    expect(page.locator("#stream-url")).to_be_visible()
    expect(page.locator("#log")).to_be_visible()


def test_auto_connect_lights_status_and_vu(stack, page):
    """AUTO_CONNECT=true (set by docker-compose.test.yml) should leave the
    page in the "connected, idle" state once the WebSocket replays state.
    The connect button flips to "disconnect" and the status text reads
    "connected"."""
    page.goto(RECORDER_URL)
    expect(page.locator("#connect-btn")).to_have_text(
        "disconnect", timeout=WS_SETTLE_MS,
    )
    expect(page.locator("#stext")).to_have_text(
        "connected", timeout=WS_SETTLE_MS,
    )

    # The /loop test stream is at -8 dBFS, so the VU mask (which shows
    # `100% - level%`) should drop well below 100 once a few WS frames
    # arrive. Poll rather than sleep-then-check: a fixed 1.5 s wait
    # flakes when the runner is slow to bring up ffmpeg + WS.
    page.wait_for_function(
        "() => { const w = document.querySelector('#mask-L').style.width;"
        "        return w && parseFloat(w) < 99; }",
        timeout=WS_SETTLE_MS,
    )


def test_record_then_stop_creates_library_row(stack, page):
    """The smoke test for the lifecycle path. Clicking record starts a
    timer, clicking stop drops a new row into the library — same as the
    recordings.py e2e but driven through the UI rather than the API."""
    page.goto(RECORDER_URL)
    expect(page.locator("#connect-btn")).to_have_text(
        "disconnect", timeout=WS_SETTLE_MS,
    )

    # Library may already have rows from earlier tests in the session;
    # use a count delta rather than an absolute number.
    initial_rows = page.locator("#lib-tbody tr").count()

    page.locator("#recbtn").click()

    # Recording active: status text flips and the timer leaves zeros.
    expect(page.locator("#stext")).to_have_text("recording", timeout=5_000)
    expect(page.locator("#timer")).not_to_have_text("00:00:00", timeout=5_000)

    # Three seconds of audio is plenty for a smoke test.
    page.wait_for_timeout(3_000)

    page.locator("#recbtn").click()

    # The library refresh is async; allow a few seconds for the WS event
    # + GET /api/recordings round-trip.
    expect(page.locator("#lib-tbody tr")).to_have_count(
        initial_rows + 1, timeout=15_000,
    )
    expect(page.locator("#stext")).to_have_text("connected", timeout=5_000)
