"""Static guards for the "/" → focus-library-search shortcut.

The full behaviour (focus on "/", literal "/" while typing, ignored under a
modal) has a Playwright e2e; these substring checks are the cheap local net
for the wiring in main.js + index.html.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_JS = REPO_ROOT / "app" / "static" / "main.js"
INDEX = REPO_ROOT / "app" / "static" / "index.html"


@pytest.fixture(scope="module")
def main_js() -> str:
    return MAIN_JS.read_text(encoding="utf-8")


def test_handler_covers_slash_and_focuses_search(main_js):
    # The global key handler now recognises "/" alongside R.
    assert "e.key !== 'r' && e.key !== 'R' && e.key !== '/'" in main_js
    assert "getElementById('lib-search')" in main_js
    assert "search.focus()" in main_js


def test_guards_preserved(main_js):
    # Same guards as the R shortcut: modifiers, form fields, open modals.
    assert "e.ctrlKey || e.metaKey || e.altKey" in main_js
    assert "input, textarea, select, [contenteditable=\"true\"]" in main_js
    assert ".modal-backdrop:not([hidden])" in main_js


def test_search_input_advertises_shortcut():
    html = INDEX.read_text(encoding="utf-8")
    assert 'aria-keyshortcuts="/"' in html
    assert "Press / to jump here" in html
