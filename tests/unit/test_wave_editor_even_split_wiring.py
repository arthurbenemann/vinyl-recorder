"""Static guards for the "split evenly" (equal-interval) cut seeder.

The pure helper (`_weEvenCuts`) has a node unit test; the full flow has a
Playwright e2e. These substring checks are the cheap local net for the
wiring across timeline-state.js / wave-editor.js / index.html — a refactor
that drops the button, the popover, or the helper call fails here.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMELINE = REPO_ROOT / "app" / "static" / "modules" / "timeline-state.js"
WAVE = REPO_ROOT / "app" / "static" / "wave-editor.js"
INDEX = REPO_ROOT / "app" / "static" / "index.html"


@pytest.fixture(scope="module")
def timeline() -> str:
    return TIMELINE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wave() -> str:
    return WAVE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_helper_defined_and_exported(timeline):
    assert "function _weEvenCuts" in timeline
    assert "window._weEvenCuts" in timeline


def test_split_evenly_uses_helper_and_persists(wave):
    assert "function weSplitEvenly" in wave
    assert "_weEvenCuts(we.total" in wave
    # Same persistence shape as weClearCuts — flips dirty + re-renders so the
    # draft saves through renderTracks() → _persistDraft().
    assert "we.dirty = true" in wave
    assert "renderTracks();" in wave


def test_popover_toggle_handles_even(wave):
    # Both suggest popovers route through the one map; opening one closes the
    # other. The 'even' kind must be registered.
    assert "we-pop-even" in wave
    assert "even: 'we-pop-even'" in wave
    # Esc dismisses the even popover before closing the whole editor.
    assert "getElementById('we-pop-even')" in wave


def test_html_exposes_button_input_and_popover(html):
    assert "weToggleSuggest('even')" in html
    assert 'id="we-pop-even"' in html
    assert 'id="we-even-n"' in html
    assert "weSplitEvenly()" in html
