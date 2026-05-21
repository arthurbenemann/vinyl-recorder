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


def test_slash_focuses_library_search(stack, page):
    """The "/" shortcut jumps focus to the library filter (GitHub-style),
    but only when not already typing in a field."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")
    # Drop any default focus, then "/" should land on the library filter.
    page.evaluate(
        "() => document.activeElement && document.activeElement.blur"
        " && document.activeElement.blur()"
    )
    page.keyboard.press("/")
    focused = page.evaluate("() => document.activeElement && document.activeElement.id")
    assert focused == "lib-search", f"expected lib-search focused, got {focused!r}"
    # While focus is already in the field, "/" types literally (the handler
    # bails when the target is an input) — it must not be swallowed.
    page.fill("#lib-search", "")
    page.keyboard.type("a/b")
    assert page.input_value("#lib-search") == "a/b"


def test_page_loads_with_main_controls(stack, page):
    """Bare-minimum smoke: page renders and the controls the user touches
    first are all visible and labelled."""
    page.goto(RECORDER_URL)
    expect(page).to_have_title(re.compile(r"vinyl recorder", re.IGNORECASE))
    expect(page.locator("#connect-btn")).to_be_visible()
    expect(page.locator("#recbtn")).to_be_visible()
    expect(page.locator("#stream-url")).to_be_visible()
    # The log surface is wrapped in a `<details>` (collapsed by default), so
    # the inner `#log` div is `display:none` until the user expands it. The
    # disclosure widget itself is what's visible on first load.
    expect(page.locator("#log-details")).to_be_visible()


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
    # The mask value is written to the `--vu-fill` custom property — the
    # same one drives both the horizontal (expanded) and vertical
    # (collapsed-rail) tracks.
    page.wait_for_function(
        "() => { const v = document.querySelector('#mask-L')"
        "          .style.getPropertyValue('--vu-fill');"
        "        return v && parseFloat(v) < 99; }",
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
    # use a count delta rather than an absolute number. The selector targets
    # real file rows only — `refreshLibRender` paints a single placeholder
    # `<tr>` ("No recordings yet…") when the library is empty, which would
    # otherwise inflate the initial count to 1 and mask the +1 from the new
    # recording (placeholder gets replaced, not added to).
    row_locator = page.locator("#lib-tbody tr.row-untagged")
    initial_rows = row_locator.count()

    page.locator("#recbtn").click()

    # Recording active: status text flips and the timer leaves zeros.
    expect(page.locator("#stext")).to_have_text("recording", timeout=5_000)
    expect(page.locator("#timer")).not_to_have_text("00:00:00", timeout=5_000)

    # Two seconds of audio is plenty for a smoke test.
    page.wait_for_timeout(2_000)

    page.locator("#recbtn").click()

    # The library refresh is async; allow a few seconds for the WS event
    # + GET /api/recordings round-trip.
    expect(row_locator).to_have_count(
        initial_rows + 1, timeout=15_000,
    )
    expect(page.locator("#stext")).to_have_text("connected", timeout=5_000)


def test_paused_recording_keeps_a_live_dot_not_gray(stack, page):
    """A paused recording must NOT paint the status dot the same gray as
    "nothing configured" — ffmpeg is still live, the session just isn't
    writing. Regression guard for the connection-state UX fix: pause should
    flip the dot to `.dot.paused`, and the status text to "paused"."""
    page.goto(RECORDER_URL)
    expect(page.locator("#connect-btn")).to_have_text("disconnect", timeout=WS_SETTLE_MS)

    page.locator("#recbtn").click()
    expect(page.locator("#stext")).to_have_text("recording", timeout=5_000)
    # Recording (not paused): the dot blinks red via `.dot.rec`.
    expect(page.locator("#sdot")).to_have_class(re.compile(r"\brec\b"), timeout=5_000)

    # Pause — the pause button reveals once recording is active.
    page.locator("#pausebtn").click()
    expect(page.locator("#stext")).to_have_text("paused", timeout=5_000)
    # The fix: paused paints `.dot.paused`, NOT a bare `dot` (which would be
    # indistinguishable from the disconnected state).
    sdot = page.locator("#sdot")
    expect(sdot).to_have_class(re.compile(r"\bpaused\b"), timeout=5_000)
    klass = sdot.get_attribute("class") or ""
    assert klass.strip() != "dot", "paused dot fell back to the gray disconnected style"

    # Resume + stop so the session doesn't leak into later tests.
    page.locator("#pausebtn").click()
    expect(page.locator("#stext")).to_have_text("recording", timeout=5_000)
    page.locator("#recbtn").click()
    expect(page.locator("#stext")).not_to_have_text("recording", timeout=10_000)
