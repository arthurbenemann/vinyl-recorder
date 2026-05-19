"""Discoverability sweep: tooltip coverage on key UI controls.

This file is intentionally narrow — it doesn't assert exact tooltip
wording (which the PR author may want to tweak), only that the controls
the user is most likely to point at carry a non-empty `title` attribute
so the browser surfaces the explanation on hover. The exact text for
the most load-bearing tooltips (peak meter, health stats, combine
button, wave-editor shortcuts) is pinned where the phrasing carries
semantic weight ("Space", "(Del)", units, etc.).

The wave-editor controls live inside a modal that's hidden by default,
so the second test opens the modal via the same `weOpen()` path the
library uses for the in-progress section.
"""
import pytest

try:
    from playwright.sync_api import expect  # noqa: F401
except ImportError:  # pragma: no cover — only in environments without playwright
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e


WS_SETTLE_MS = 10_000


def _title(page, selector: str) -> str:
    """Read the `title` attribute on the first match of `selector`.
    Returns an empty string if the element isn't there or the attribute
    is missing — the caller asserts on the contents."""
    value = page.locator(selector).first.get_attribute('title')
    return value or ''


def test_health_panel_stats_have_explanatory_tooltips(stack, page):
    """The header health panel exposes numbers like `level`, `bytes/sec`,
    `gaps (5 s)` that mean nothing without context. Each row must carry a
    `title` attribute so a hover surfaces the explanation."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    # Every row id present on first paint (the panel itself is hidden,
    # but the rows are in the DOM — `title` is readable without showing).
    rows = {
        '#hp-level':      ['dbfs', 'level'],
        '#hp-bps':        ['data rate', 'bytes'],
        '#hp-expected':   ['expected'],
        '#hp-gaps':       ['dropouts', '5'],
        '#hp-gap-total':  ['cumulative', 'dropout'],
        '#hp-since':      ['frame', 'elapsed'],
        '#hp-reconnects': ['reconnect'],
    }
    for sel, expected_substrings in rows.items():
        # `title` lives on the surrounding `.health-row`, not the value
        # span — that's what hovers naturally as the user reads.
        row_title = page.eval_on_selector(
            sel, "el => el.closest('.health-row').getAttribute('title')",
        ) or ''
        assert row_title.strip(), f"{sel}: row has no title attribute"
        lowered = row_title.lower()
        # At least one of the expected keywords must be present so a
        # rename of the tooltip can't silently swap to a meaningless
        # placeholder.
        assert any(s in lowered for s in expected_substrings), (
            f"{sel}: title {row_title!r} matches none of {expected_substrings}"
        )


def test_peak_meter_indicators_have_tooltips(stack, page):
    """The peak-hold ticks (#peak-L, #peak-R) and the dB readouts
    (#db-L, #db-R) need a tooltip explaining the hold/decay so the user
    isn't confused by the value lagging the bar."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    for sel in ('#peak-L', '#peak-R'):
        t = _title(page, sel)
        assert t.strip(), f"{sel} missing title"
        assert 'peak' in t.lower(), f"{sel} title {t!r} doesn't mention peak"

    for sel in ('#db-L', '#db-R'):
        t = _title(page, sel)
        assert t.strip(), f"{sel} missing title"
        assert 'db' in t.lower(), f"{sel} title {t!r} doesn't mention dB"


def test_combine_button_label_and_tooltip(stack, page):
    """The bulk-bar's `combine` button was renamed to make its purpose
    obvious. The new copy must mention `album` and the tooltip must
    explain what selecting multiple sides does."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")
    btn = page.locator('#combine-btn')
    text = (btn.text_content() or '').strip().lower()
    assert 'album' in text, f"combine button text drifted: {text!r}"
    # Specifically not the old "combine into album" / "tag as album"
    # short copy — the rename intentionally spells the multi-side
    # behaviour out.
    title = btn.get_attribute('title') or ''
    assert title.strip(), "combine button has no title"
    assert 'album' in title.lower(), f"combine title doesn't mention album: {title!r}"


def test_record_button_advertises_shortcut(stack, page):
    """`R` toggles record/stop globally; the button title already says
    so, but pin it so a future refactor that drops the hint fails
    here."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")
    t = _title(page, '#recbtn')
    assert 'r' in t.lower(), f"#recbtn title doesn't reference R shortcut: {t!r}"


def test_wave_editor_controls_have_tooltips_with_shortcut_hints(stack, page):
    """The wave editor has many keyboard shortcuts documented only in
    the help block at the bottom. The most-clicked controls (#we-play,
    apply split, clear cuts, cancel, …) carry an inline `title` that
    names the shortcut so the user doesn't have to scan the help text.

    The modal is hidden by default, but `title` is readable from a
    hidden DOM subtree — no need to drive the modal open just for this
    smoke. Other wave-editor tests already exercise the opening path."""
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    expected = {
        '#we-play':         'space',      # Play/Pause (Space)
        '#we-go':           'split',      # apply split — describes the action
        '#we-measure-btn':  'peak',       # measure — mentions what it computes
    }
    for sel, must_contain in expected.items():
        t = _title(page, sel)
        assert t.strip(), f"{sel} has no title"
        assert must_contain in t.lower(), (
            f"{sel} title {t!r} doesn't contain expected hint {must_contain!r}"
        )

    # The toolbar buttons in the action-row at the bottom of the editor
    # (cancel / clear cuts / add cut at playhead) are matched by their
    # visible text — there's no stable id. The locator is scoped to the
    # we-modal so it doesn't collide with the library's bottom row.
    for label, must_contain in [
        ('cancel',                'esc'),
        ('clear cuts',            'cut'),
        ('+ add cut at playhead', 'cut'),
    ]:
        loc = page.locator(f'#we-modal button:has-text("{label}")').first
        t = (loc.get_attribute('title') or '').lower()
        assert t, f'wave-editor button "{label}" has no title'
        assert must_contain in t, (
            f'wave-editor button "{label}" title {t!r} lacks {must_contain!r}'
        )
