"""Static guards for the wave-editor keyboard/mouse shortcuts legend.

The editor's shortcuts (Space, arrows, J/K, Del, S) previously lived only in
the canvas aria-label + a couple of button titles — invisible to sighted
users. This popover surfaces them. The full open/Esc/mutual-exclusion flow
has a Playwright e2e; these substring checks are the cheap local net for the
wiring across wave-editor.js / index.html / style.css.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAVE = REPO_ROOT / "app" / "static" / "wave-editor.js"
INDEX = REPO_ROOT / "app" / "static" / "index.html"
CSS = REPO_ROOT / "app" / "static" / "style.css"


@pytest.fixture(scope="module")
def wave() -> str:
    return WAVE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_toggle_defined_and_mutually_exclusive(wave):
    assert "function weToggleShortcuts" in wave
    # Opening keys hides the split popover, and vice-versa.
    assert "we-pop-keys" in wave
    assert "getElementById('we-pop-split')" in wave


def test_esc_dismisses_keys_popover(wave):
    # Esc closes an open keys popover before closing the whole editor.
    assert "getElementById('we-pop-keys')" in wave
    # Reset on open too.
    assert "popKeysReset" in wave


def test_html_exposes_button_and_legend(html):
    assert 'id="we-keys-btn"' in html
    assert "weToggleShortcuts()" in html
    assert 'id="we-pop-keys"' in html
    # A few of the actual shortcuts are documented.
    assert "play / pause" in html
    assert "jump to previous / next cut" in html
    assert "skip / unskip" in html
    # Built from <kbd> chips.
    assert "<kbd>Space</kbd>" in html
    assert "<kbd>Esc</kbd>" in html


def test_css_styles_kbd():
    css = CSS.read_text(encoding="utf-8")
    assert "kbd {" in css
    assert ".we-keys-list" in css
