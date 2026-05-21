"""Static guards for the custom-cover-upload wiring.

The endpoint itself is exercised by `tests/api/test_tagging_endpoints.py`;
the upload round-trip is driven by Playwright. These substring checks are
the cheap net for the frontend wiring that ties the file input → the held
File → the post-apply upload, spread across index.html / tagging.js /
main.js / style.css.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX = REPO_ROOT / "app" / "static" / "index.html"
TAGGING = REPO_ROOT / "app" / "static" / "modules" / "tagging.js"
MAIN_JS = REPO_ROOT / "app" / "static" / "main.js"
STYLE = REPO_ROOT / "app" / "static" / "style.css"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tagging() -> str:
    return TAGGING.read_text(encoding="utf-8")


def test_file_input_present_and_image_only(html):
    assert 'id="t-cover-file"' in html
    # Restrict the native picker to images; the server re-validates anyway.
    assert 'accept="image/*"' in html
    assert "onCoverFileSelected(" in html
    # The visible affordance triggers the hidden input.
    assert 'id="t-cover-upload-btn"' in html


def test_cover_column_styled(html):
    # The preview + button stack in a fixed-width column; without the wrapper
    # the button would land in the fields grid.
    assert 'class="cover-col"' in html
    assert ".cover-col" in (REPO_ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")


def test_handler_holds_file_and_previews(tagging):
    assert "export function onCoverFileSelected" in tagging
    assert "tagPanelCoverFile = file" in tagging
    # Local preview without a server round-trip.
    assert "readAsDataURL" in tagging


def test_apply_uploads_held_cover(tagging):
    assert "function _uploadHeldCover" in tagging
    assert "_uploadHeldCover(newAlbumId)" in tagging
    # Posts the held File to the cover endpoint as multipart.
    assert "/api/file-cover/" in tagging
    assert "FormData" in tagging


def test_cover_state_reset_on_open(tagging):
    # Opening the panel must drop a stale pick + clear the input so it can't
    # ride along with the next album's apply.
    assert "tagPanelCoverFile = null" in tagging
    assert "coverInput.value = ''" in tagging


def test_handler_bridged_to_window():
    main_js = MAIN_JS.read_text(encoding="utf-8")
    assert "window.onCoverFileSelected = onCoverFileSelected" in main_js
