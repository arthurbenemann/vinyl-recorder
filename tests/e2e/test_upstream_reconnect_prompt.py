"""E2E for the upstream-drop reconnect prompt.

The bug being closed: when the upstream stream dies mid-recording the
recorder cleanly finalises the session (reason="crash") but does not
auto-respawn — the user has to click Connect again. The lightweight
frontend fix is to surface a toast with a Reconnect button so the path
back to a working recorder is one click away.

Two flavours are exercised here:

  1. Synthetic — dispatch a `record:stop reason='crash'` MessageEvent
     onto the page's live WebSocket. Asserts the toast renders, the
     button POSTs to /api/connect with the current stream-url, and a
     follow-up synthetic `upstream connected:true` event dismisses the
     toast (the multi-tab idempotency story).
  2. Realistic — drop the test-streams container while a recording is
     live, wait for the watcher to finalise + emit the crash event,
     assert the toast renders and clicking it brings the upstream back.

The synthetic tests are the load-bearing ones: they pin the exact
wiring (handler -> toast -> POST) regardless of the compose stack's
reaction time. The realistic test is a thin smoke covering the
end-to-end path so a regression in event plumbing — e.g. someone
renames `reason` — also fails the e2e. They share the compose `stack`
fixture so the cost of bringing the stack up is paid once.
"""
import subprocess
import time

import pytest

try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover — only on hosts without playwright
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import (
    RECORDER_URL,
    STREAM_URL,
    compose,
    http_json,
    wait_for_upstream_configured,
)

pytestmark = pytest.mark.e2e


# Selector for the prompt; `data-toast-id` is set by `toastAction` in
# log.js and is the API contract between the JS and these tests.
PROMPT_SELECTOR = '[data-toast-id="upstream-reconnect"]'


# Init script that captures every WebSocket constructed by the page so
# tests can dispatch synthetic `message` events to drive `handleWsEvent`
# without mocking the socket. The script is installed before any page
# script runs, so the WS opened by ws.js on page-load lands in
# `window.__vrSockets[0]`.
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


def _ensure_stack_healthy() -> None:
    """Make sure the upstream is up before driving the page. Earlier tests
    in the e2e session (e.g. test_crash_recovery) kill test-streams as part
    of their scenarios — we restart it here so each test in this file has
    a known-good starting state. Defensive: if the stack is already healthy
    this is effectively a no-op."""
    try:
        wait_for_upstream_configured(timeout=10)
        return
    except RuntimeError:
        pass
    compose("start", "test-streams", timeout=30)
    wait_for_upstream_configured(timeout=30)


def _wait_idle(page):
    """Wait for the WS hello to land and put the page in connected/idle."""
    expect(page.locator("#connect-btn")).to_have_text("disconnect", timeout=10_000)


def _dispatch_ws(page, payload: dict) -> None:
    """Fire a synthetic `message` on the most recently-opened WS so
    ws.js' onmessage routes it through handleWsEvent."""
    import json
    page.evaluate(
        """(data) => {
          const s = window.__vrSockets[window.__vrSockets.length - 1];
          s.dispatchEvent(new MessageEvent('message', { data }));
        }""",
        json.dumps(payload),
    )


def test_synthetic_crash_event_surfaces_reconnect_prompt(stack, page):
    """Fire a fake `record:stop reason='crash'` event through the WS,
    assert the toast appears with a Reconnect button, and that clicking
    it POSTs /api/connect with the current stream-url."""
    _ensure_stack_healthy()
    page.add_init_script(_WS_CAPTURE_INIT)

    # Record /api/connect POSTs so we can assert exactly what the toast
    # button does. Listening on `request` (not `response`) means we see
    # the call even if the server happens to be slow to acknowledge.
    connect_posts: list[tuple[str, str]] = []
    page.on(
        "request",
        lambda r: connect_posts.append((r.url, r.post_data or ""))
        if r.method == "POST" and r.url.endswith("/api/connect") else None,
    )

    page.goto(RECORDER_URL)
    _wait_idle(page)
    page.wait_for_function("() => (window.__vrSockets || []).length > 0",
                           timeout=10_000)

    _dispatch_ws(page, {
        "type": "record", "event": "stop", "reason": "crash",
        "session_id": "e2e-fake-sid", "filename": "e2e-fake.flac",
        "elapsed": 0, "size_mb": 0,
    })

    # Prompt should render with the message and a Reconnect button.
    prompt = page.locator(PROMPT_SELECTOR)
    expect(prompt).to_be_visible(timeout=5_000)
    expect(prompt).to_contain_text("Stream connection lost")
    reconnect_btn = prompt.locator(".btn-tiny")
    expect(reconnect_btn).to_have_text("Reconnect")

    # The toast must NOT auto-dismiss — wait a beat and confirm it's
    # still there. Regular toasts fade after 3.5 s; this one should not.
    page.wait_for_timeout(800)
    expect(prompt).to_be_visible()

    # Click — should POST /api/connect with the input's current value.
    initial_count = len(connect_posts)
    reconnect_btn.click()
    # Give the click handler a moment to fire its fetch.
    page.wait_for_timeout(1_000)
    assert len(connect_posts) > initial_count, (
        f"Reconnect button click did not POST /api/connect: {connect_posts!r}"
    )
    url, body = connect_posts[-1]
    assert "/api/connect" in url
    # Body must carry the stream_url the input is currently showing.
    assert STREAM_URL in body, f"unexpected POST body: {body!r}"

    # Toast removes itself on click.
    expect(prompt).to_have_count(0, timeout=3_000)


def test_synthetic_upstream_recovery_dismisses_prompt(stack, page):
    """A sibling tab that received the prompt but did *not* click should
    have its toast dismissed when any tab brings the upstream back —
    drives the multi-tab idempotency story without needing two browsers.
    """
    _ensure_stack_healthy()
    page.add_init_script(_WS_CAPTURE_INIT)
    page.goto(RECORDER_URL)
    _wait_idle(page)
    page.wait_for_function("() => (window.__vrSockets || []).length > 0",
                           timeout=10_000)

    _dispatch_ws(page, {
        "type": "record", "event": "stop", "reason": "crash",
        "session_id": "e2e-fake-sid", "filename": "e2e-fake.flac",
        "elapsed": 0, "size_mb": 0,
    })
    expect(page.locator(PROMPT_SELECTOR)).to_be_visible(timeout=5_000)

    # Upstream comes back (as if a sibling tab clicked).
    _dispatch_ws(page, {
        "type": "upstream", "connected": True, "configured": True,
        "url": STREAM_URL,
        "format": {"sample_rate": 44100, "channels": 2},
    })
    expect(page.locator(PROMPT_SELECTOR)).to_have_count(0, timeout=3_000)


def test_normal_stop_does_not_show_reconnect_prompt(stack, page):
    """Regression guard: record:stop with reason='user' or 'auto' must
    NOT surface the reconnect prompt. The bug fix is narrowly scoped to
    upstream-drop crashes; widening it would spam every stop with a
    spurious 'reconnect?' affordance.
    """
    _ensure_stack_healthy()
    page.add_init_script(_WS_CAPTURE_INIT)
    page.goto(RECORDER_URL)
    _wait_idle(page)
    page.wait_for_function("() => (window.__vrSockets || []).length > 0",
                           timeout=10_000)

    for reason in ("user", "auto"):
        _dispatch_ws(page, {
            "type": "record", "event": "stop", "reason": reason,
            "session_id": "e2e-fake-sid", "filename": "e2e-fake.flac",
            "elapsed": 5, "size_mb": 0.5,
        })
    page.wait_for_timeout(500)
    expect(page.locator(PROMPT_SELECTOR)).to_have_count(0)


def test_real_upstream_drop_then_click_restores_stream(stack, page):
    """End-to-end smoke that exercises the live event wiring with a real
    upstream drop. Starts a recording, kills test-streams (mirrors the
    network-blip / Pi-reboot scenario), waits for the watcher to finalise
    + emit the crash event, asserts the toast renders, clicks Reconnect,
    and (after the test-streams container is brought back up) checks
    that /api/status reports the upstream as connected again.
    """
    raw = stack["raw"]

    # Start from a healthy stack — defensive guard in case an earlier test
    # left the stream stopped.
    try:
        wait_for_upstream_configured(timeout=15)
    except RuntimeError:
        compose("start", "test-streams", timeout=30)
        wait_for_upstream_configured(timeout=30)

    page.goto(RECORDER_URL)
    _wait_idle(page)

    started = http_json(
        f"{RECORDER_URL}/api/record/start", method="POST",
        body={
            "stream_url": STREAM_URL,
            "artist": "e2e", "album": "drop-prompt", "year": "2026",
            "duration": 0,
        },
    )
    sid = started["session_id"]

    expect(page.locator("#stext")).to_have_text("recording", timeout=10_000)
    time.sleep(2)

    try:
        # Kill the upstream — same primitive test_crash_recovery uses.
        r = compose("kill", "test-streams", timeout=30)
        assert r.returncode == 0, f"compose kill failed: {r.stderr}"

        # Watcher finalises within ~30 s; the WS event flows straight
        # from _finalize_session so the prompt should appear shortly
        # after. Widen the timeout to absorb GHA jitter.
        expect(page.locator(PROMPT_SELECTOR)).to_be_visible(timeout=45_000)
        # Recording UI flips back to "not recording".
        expect(page.locator("#stext")).not_to_have_text("recording", timeout=15_000)

        # Bring the upstream back BEFORE the user clicks so the click
        # actually has something to reconnect to.
        compose("start", "test-streams", timeout=30)
        deadline = time.time() + 30
        while time.time() < deadline:
            inspect = subprocess.run(
                ["docker", "inspect",
                 "--format={{.State.Health.Status}}",
                 "vinyl-test-streams"],
                capture_output=True, text=True,
            )
            if inspect.stdout.strip() == "healthy":
                break
            time.sleep(1)

        # Click the prompt's Reconnect button.
        page.locator(f"{PROMPT_SELECTOR} .btn-tiny").click()

        # The server-side reconnect + WS `upstream connected:true` lands
        # within a few seconds; the toast self-dismisses on that event.
        expect(page.locator(PROMPT_SELECTOR)).to_have_count(0, timeout=15_000)

        # Steady state: status reports a connected upstream again.
        wait_for_upstream_configured(timeout=15)
        post = http_json(f"{RECORDER_URL}/api/status")
        assert post["upstream"]["configured"] is True
        # `connected` flips true while a holder is active; the page's
        # visible WS counts, so it should be back up.
        assert post["upstream"]["connected"] is True, (
            f"upstream did not reconnect after the prompt click: {post['upstream']!r}"
        )

    finally:
        # Defensive cleanup so subsequent tests see a healthy stack.
        compose("start", "test-streams", timeout=30)
        try:
            http_json(
                f"{RECORDER_URL}/api/connect", method="POST",
                body={"stream_url": STREAM_URL},
            )
        except Exception:
            pass
        try:
            wait_for_upstream_configured(timeout=30)
        except RuntimeError:
            pass
        # If the recording session is still listed (shouldn't be after
        # the watcher reaped it), force-stop so it doesn't leak.
        try:
            http_json(f"{RECORDER_URL}/api/record/stop/{sid}", method="POST")
        except Exception:
            pass
        for f in raw.glob("*drop-prompt*"):
            try: f.unlink()
            except Exception: pass
