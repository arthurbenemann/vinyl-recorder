"""E2E check for the pi-deploy modal's client-side validation.

Opens the modal, submits with the password blank, and asserts the guard
fires (no `/api/pi/deploy` POST goes out). Then closes via the cancel
button. Pure DOM — no SSH, no extra container — adds ~1 s on the running
compose stack.
"""
import pytest

try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e


def test_pi_deploy_modal_validates_required_fields(stack, page):
    """The header menu's `deploy to pi…` opens the modal; clicking deploy
    with the password blank fires the client-side guard (a toast) and
    skips the network call entirely. Cancel restores the closed state."""
    deploy_posts: list[str] = []
    page.on("request", lambda r: deploy_posts.append(r.url)
            if r.method == "POST" and "/api/pi/deploy" in r.url else None)

    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    # Open via the function the header button calls — avoids depending on
    # the menu-open animation timing.
    page.evaluate("openPiDeploy()")
    page.wait_for_selector('#pi-deploy-modal:not([hidden])', timeout=5_000)

    # Required fields visible.
    expect(page.locator('#pi-host')).to_be_visible()
    expect(page.locator('#pi-user')).to_be_visible()
    expect(page.locator('#pi-pass')).to_be_visible()
    # Username field is pre-populated with a sensible default; password is empty.
    assert (page.input_value('#pi-user') or '').strip() == 'pi'
    assert page.input_value('#pi-pass') == ''

    # Try to deploy with host + empty password — the client guard should
    # toast an error and *not* hit /api/pi/deploy.
    page.fill('#pi-host', '203.0.113.1')   # TEST-NET-3, never routable
    page.click('#pi-deploy-go')
    # Guard fires synchronously; give the (skipped) fetch a moment to
    # confirm it really didn't go out.
    page.wait_for_timeout(300)
    assert not deploy_posts, \
        f"empty-password submit should be guarded, got POSTs: {deploy_posts}"
    # The deploy button is re-enabled because the guard returned early.
    expect(page.locator('#pi-deploy-go')).to_be_enabled()

    # Cancel re-hides the modal.
    page.click('#pi-deploy-modal button.btn:has-text("cancel")')
    page.wait_for_function(
        "() => document.getElementById('pi-deploy-modal').hasAttribute('hidden')",
        timeout=3_000,
    )
