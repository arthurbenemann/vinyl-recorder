"""Smoke tests that the static HTML carries the ARIA attributes added by
the accessibility pass. Catches a regression where someone trims an
attribute during a refactor — the dynamic JS-side aria-labels are still
verified manually with a screen reader, but these guard the rest.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX = REPO_ROOT / "app" / "static" / "index.html"
STYLE = REPO_ROOT / "app" / "static" / "style.css"
MAIN_JS = REPO_ROOT / "app" / "static" / "main.js"
MODULES_DIR = REPO_ROOT / "app" / "static" / "modules"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return STYLE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    # Phase 8 split main.js into ES modules; concatenate them so substring
    # checks for helpers (e.g. trapModalFocus, _announceLibCount) keep working
    # regardless of which module they ended up in.
    parts = [MAIN_JS.read_text(encoding="utf-8")]
    if MODULES_DIR.is_dir():
        for p in sorted(MODULES_DIR.glob("*.js")):
            parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_wave_editor_add_cut_and_nudge_keys_wired(html):
    """The split editor advertises + handles the `c` (add cut at playhead)
    and ←/→ (nudge nearest cut) keyboard shortcuts."""
    # Help text + canvas aria advertise the `c` key.
    assert "add cut at playhead" in html
    canvas_tag = html.split('id="we-canvas"')[1].split(">")[0]
    assert "c to add a cut" in canvas_tag
    # wave-editor.js handles `c` and routes arrows through the nudge helper.
    we_js = (REPO_ROOT / "app" / "static" / "wave-editor.js").read_text(encoding="utf-8")
    assert "case 'c':" in we_js
    assert "weAddCutAtPlayhead()" in we_js
    assert "weNudgeNearestCut(" in we_js
    assert "_weNudgedCutValue(" in we_js


def test_wave_canvas_is_keyboard_focusable(html):
    # The canvas has rich key handlers — exposing it to keyboard nav requires
    # tabindex + role + aria-label so screen readers announce it.
    assert 'id="we-canvas"' in html
    canvas_tag = html.split('id="we-canvas"')[1].split(">")[0]
    assert 'tabindex="0"' in canvas_tag
    assert 'role="application"' in canvas_tag
    assert "aria-label=" in canvas_tag


def test_lib_search_status_live_region_present(html):
    # Drives the "N results" announcement when the user filters the library.
    assert 'id="lib-search-status"' in html
    # The live region must be polite + atomic so the AT replaces the prior
    # count instead of stacking announcements.
    region = html.split('id="lib-search-status"')[1].split(">")[0]
    assert 'aria-live="polite"' in region
    assert 'aria-atomic="true"' in region


def test_record_button_advertises_keyboard_shortcut(html):
    # The record button shortcut (R) is only useful if AT users hear about it.
    rec_tag = html.split('id="recbtn"')[1].split(">")[0]
    assert "(R)" in rec_tag or "shortcut: R" in rec_tag
    assert "aria-keyshortcuts" in rec_tag


def test_clip_indicators_are_live_regions(html):
    # When the latching CLIP badge appears the AT should announce it.
    for half in ("clip-L", "clip-R"):
        tag = html.split(f'id="{half}"')[1].split(">")[0]
        assert 'aria-live="polite"' in tag
        assert "aria-label=" in tag


def test_sr_only_utility_class_exists(css):
    # The visually-hidden utility used by #lib-search-status. Must clip to
    # the standard 1px corner so the live region stays out of the visual
    # flow but reachable to AT.
    assert ".sr-only" in css
    assert "clip:" in css


def test_canvas_focus_visible_outline(css):
    # Wave canvas is reached via Tab; without a focus ring keyboard users
    # can't see where they are.
    assert "#we-canvas:focus-visible" in css


def test_focus_trap_helper_present(js):
    assert "function trapModalFocus" in js
    # The Esc handler must accept a modal id so it can also wire the trap —
    # PR3's whole point is Tab cycling inside modals.
    assert "makeModalEscHandler(closeTag, 'tag-modal')" in js
    assert "makeModalEscHandler(closePiDeploy, 'pi-deploy-modal')" in js


def test_announce_lib_count_helper_wired(js):
    # The polite live region needs something writing into it; the helper
    # should be called from both the search input handler and the clear.
    assert "_announceLibCount" in js
    # …and it should branch on an empty query (drops the message).
    assert "el.textContent = ''" in js or 'el.textContent = ""' in js
