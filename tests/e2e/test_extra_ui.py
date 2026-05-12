"""Two cheap UI checks that piggy-back on the running compose stack:

1. The album table renders `mixed` in the format cell when sides have
   differing bit depth / sample rate. Pins the per-side `source_format`
   plumbing through the album payload + `fmtSourceFormat` walking
   `sides[]`.

2. The pi-deploy modal opens from the header menu, validates required
   fields client-side, and closes via the cancel button. No SSH happens
   because the validation rejects the empty-password submit before any
   `/api/pi/deploy` POST goes out — the test asserts that absence too.

Both reuse the seeded raw/ FLAC pattern (one-shot ffmpeg in the recorder
container) and add ~5-10 s to the e2e wall time.
"""
import subprocess
from pathlib import Path

import pytest

try:
    from playwright.sync_api import expect  # noqa: F401
except ImportError:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e


def _seed_side(raw_dir: Path, name: str, *, sample_rate: int) -> Path:
    """Drop a 1-second 16-bit FLAC into raw/ at the requested sample rate
    via the recorder container's ffmpeg. Sample rate is enough to make the
    sides disagree in `sample_rate_khz` and trigger the `mixed` label."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    p = raw_dir / name
    rel = p.relative_to(raw_dir.parent)
    container_path = f"/output/{rel}"
    subprocess.run(
        ["docker", "exec", "vinyl-recorder", "ffmpeg",
         "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=f=440:duration=1,volume=0.5",
         "-ar", str(sample_rate), "-ac", "2",
         "-sample_fmt", "s16",
         "-c:a", "flac", "-y", container_path],
        check=True, capture_output=True, text=True,
    )
    return p


def test_album_format_cell_says_mixed_for_differing_sides(stack, page):
    """Combine two sides that disagree on sample rate (96 kHz vs 44.1 kHz);
    the album row's `[data-col="fmt"]` cell should render `mixed` rather
    than the first side's format string."""
    raw = stack["raw"]
    a = _seed_side(raw, "mix_a.flac", sample_rate=96000)
    b = _seed_side(raw, "mix_b.flac", sample_rate=44100)
    seeded = [a, b]
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_function(
            "(names) => names.every(n => document.querySelector("
            "  `input.row-check[data-fname=\"${n}\"]`))",
            arg=[s.name for s in seeded],
            timeout=10_000,
        )
        page.evaluate(
            "(names) => { for (const n of names) {"
            "  const cb = document.querySelector("
            "    `input.row-check[data-fname=\"${n}\"]`);"
            "  if (!cb.checked) cb.click();"
            "} }",
            [s.name for s in seeded],
        )
        page.wait_for_selector('#combine-btn:not([disabled])', timeout=5_000)
        page.click('#combine-btn')
        page.wait_for_selector('#tag-modal:not([hidden])')
        page.fill('#t-artist', 'MixedFmtArtist')
        page.fill('#t-album',  'MixedFmtAlbum')
        page.fill('#t-year',   '2026')
        page.click('#tag-apply-btn')
        page.wait_for_function(
            "() => document.getElementById('tag-modal').hasAttribute('hidden')",
            timeout=20_000,
        )
        page.wait_for_function(
            "(album) => Array.from(document.querySelectorAll('tr[data-album-id]'))"
            ".some(r => r.textContent.includes(album))",
            arg='MixedFmtAlbum',
            timeout=10_000,
        )
        fmt_text = page.evaluate(
            "(album) => Array.from(document.querySelectorAll('tr[data-album-id]'))"
            ".find(r => r.textContent.includes(album))"
            ".querySelector('[data-col=\"fmt\"]').textContent.trim()",
            'MixedFmtAlbum',
        )
        assert fmt_text == 'mixed', \
            f"format cell should say 'mixed' for differing sides, got {fmt_text!r}"
    finally:
        for p in seeded:
            try: p.unlink(missing_ok=True)
            except Exception: pass


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
