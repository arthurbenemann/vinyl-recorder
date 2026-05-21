"""E2E for the "recording but no audio detected" warning.

The server's watcher emits `record:no_signal` when a take runs without the
upstream peak ever clearing the floor (turntable off / wrong input) — a
case auto-stop-on-silence can't catch because it never arms. The frontend
surfaces it as a persistent toast that auto-clears when signal returns.

Driven synthetically (dispatch the WS event onto the live socket) so the
exact wiring — handler -> toast, vu-signal -> dismiss — is pinned without
needing a genuinely-silent upstream in the compose stack.
"""
import json

import pytest

try:
    from playwright.sync_api import expect  # noqa: F401
except ImportError:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e

PROMPT_SELECTOR = '[data-toast-id="rec-no-signal"]'

_WS_CAPTURE_INIT = """
(() => {
  const RealWS = window.WebSocket;
  window.__vrSockets = [];
  const Wrapped = function(...a) {
    const s = new RealWS(...a);
    window.__vrSockets.push(s);
    return s;
  };
  Wrapped.prototype = RealWS.prototype;
  window.WebSocket = Wrapped;
})();
"""


def _dispatch_ws(page, payload: dict) -> None:
    page.evaluate(
        """(data) => {
          const s = window.__vrSockets[window.__vrSockets.length - 1];
          s.dispatchEvent(new MessageEvent('message', { data }));
        }""",
        json.dumps(payload),
    )


def test_no_signal_event_surfaces_and_clears(stack, page):
    page.add_init_script(_WS_CAPTURE_INIT)
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => (window.__vrSockets || []).length > 0",
                           timeout=10_000)

    # No warning until the server says so.
    expect(page.locator(PROMPT_SELECTOR)).to_have_count(0)

    _dispatch_ws(page, {"type": "record", "event": "no_signal",
                        "session_id": "sig-test"})
    prompt = page.locator(PROMPT_SELECTOR)
    expect(prompt).to_be_visible(timeout=5_000)
    expect(prompt).to_contain_text("no audio detected")

    # A vu frame above the floor (signal returned) dismisses it.
    _dispatch_ws(page, {"type": "vu", "peak_l": 0.5, "peak_r": 0.5,
                        "clipped_l": False, "clipped_r": False})
    expect(page.locator(PROMPT_SELECTOR)).to_have_count(0, timeout=3_000)


def test_no_signal_warning_dismissed_on_record_stop(stack, page):
    page.add_init_script(_WS_CAPTURE_INIT)
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => (window.__vrSockets || []).length > 0",
                           timeout=10_000)

    _dispatch_ws(page, {"type": "record", "event": "no_signal",
                        "session_id": "sig-test"})
    expect(page.locator(PROMPT_SELECTOR)).to_be_visible(timeout=5_000)

    # Recording ends → warning clears.
    _dispatch_ws(page, {"type": "record", "event": "stop",
                        "session_id": "sig-test", "reason": "user",
                        "filename": "x.flac"})
    expect(page.locator(PROMPT_SELECTOR)).to_have_count(0, timeout=3_000)
