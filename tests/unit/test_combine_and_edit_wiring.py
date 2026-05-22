"""Static guards for the opt-in "combine & edit" flow.

The full flow (combine sides → jump straight into the split editor) has a
Playwright e2e; these substring checks are the cheap local net for the
wiring across tagging.js + index.html.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TAGGING = REPO_ROOT / "app" / "static" / "modules" / "tagging.js"
INDEX = REPO_ROOT / "app" / "static" / "index.html"


@pytest.fixture(scope="module")
def tagging() -> str:
    return TAGGING.read_text(encoding="utf-8")


def test_apply_takes_thenedit_and_opens_editor(tagging):
    assert "export async function applyTagPanel(thenEdit = false)" in tagging
    # Captures the new album id and opens the editor once the album list has
    # refreshed (openWaveEditor reads albumsByName).
    assert "const result = await r.json()" in tagging
    assert "await albumsReady" in tagging
    assert "window.openWaveEditor(result.album_id)" in tagging


def test_apply_edit_button_shown_for_new_albums(tagging):
    # Revealed whenever an album is being CREATED — combine N sides or
    # promote one — and hidden only when retagging an existing album.
    assert "tag-apply-edit-btn" in tagging
    assert "const isNewAlbum = tagPanelTarget.album_id === undefined" in tagging
    assert "applyEditBtn.hidden = !isNewAlbum" in tagging
    # Label tracks the mode: "combine & edit" vs "apply & edit".
    assert "isCombine ? 'combine & edit ▸' : 'apply & edit ▸'" in tagging


def test_html_exposes_combine_and_edit_button():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="tag-apply-edit-btn"' in html
    assert "applyTagPanel(true)" in html
    # Hidden by default — openTag flips it on for combine mode.
    import re
    m = re.search(r'<button[^>]*id="tag-apply-edit-btn"[^>]*>', html)
    assert m and "hidden" in m.group(0)
