"""E2E checks for the first-run onboarding overlay.

The overlay teaches the implicit Raw → Album → Music pipeline. It auto-
shows once, gated on the `vr.onboarded` localStorage flag, and is re-
openable from the header ⋮ menu's "how it works" item.

pytest-playwright hands each test a fresh browser context (so
localStorage starts empty), which lets the first-run path be exercised
without any teardown ceremony. Pure DOM — no SSH, no extra container —
adds ~1 s on the running compose stack.
"""
import pytest

try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e

ONBOARDED_KEY = "vr.onboarded"


def _flag(page):
    """Read the onboarding localStorage flag (None when unset)."""
    return page.evaluate(f"() => localStorage.getItem({ONBOARDED_KEY!r})")


def test_onboarding_first_run_shows_and_dismisses(stack, page):
    """Fresh context (empty localStorage): the overlay auto-shows, names
    all three pipeline stages, and "Got it" hides it while setting the
    flag. A reload then does NOT re-show it."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    # Auto-shown on first load, with the flag still unset at that point.
    page.wait_for_selector("#onboarding-modal:not([hidden])", timeout=5_000)
    assert _flag(page) is None, "flag must not be set until the user dismisses"

    # The overlay teaches the Raw → Album → Music model.
    overlay = page.locator("#onboarding-modal")
    expect(overlay).to_contain_text("Raw")
    expect(overlay).to_contain_text("Album")
    expect(overlay).to_contain_text("Music")

    # Primary "Got it" button is focused on open and dismisses the overlay.
    expect(page.locator("#onboarding-got-it")).to_be_focused()
    page.click("#onboarding-got-it")
    page.wait_for_function(
        "() => document.getElementById('onboarding-modal').hasAttribute('hidden')",
        timeout=3_000,
    )
    assert _flag(page), "dismissing must set the localStorage flag"

    # Reload — the flag is set now, so the overlay must stay hidden.
    page.reload()
    page.wait_for_load_state("networkidle")
    # Give the deferred initOnboarding() a beat to (not) fire.
    page.wait_for_timeout(500)
    expect(page.locator("#onboarding-modal")).to_be_hidden()


def test_onboarding_reopen_from_header_menu(stack, page):
    """The header ⋮ menu's "how it works" item re-opens the overlay even
    after it's been dismissed. Pre-seed the flag (via an init script that
    runs before app JS) so the auto-show path is out of the way, then drive
    the menu."""
    page.add_init_script(f"localStorage.setItem({ONBOARDED_KEY!r}, '1')")
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    # With the flag set, no auto-show.
    expect(page.locator("#onboarding-modal")).to_be_hidden()

    # Open the header menu and click "how it works".
    page.click("#header-menu-btn")
    page.wait_for_selector("#header-menu-pop:not([hidden])", timeout=3_000)
    page.click("#how-it-works-btn")

    page.wait_for_selector("#onboarding-modal:not([hidden])", timeout=3_000)
    expect(page.locator("#onboarding-modal")).to_contain_text("Raw")
    expect(page.locator("#onboarding-modal")).to_contain_text("Music")
