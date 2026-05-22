"""Static guards for the album-zip download button.

The endpoint behaviour is covered by API tests in test_album_helpers.py;
this pins the frontend wiring (the link is rendered only for split albums
and points at the download endpoint).
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALBUMS_JS = REPO_ROOT / "app" / "static" / "modules" / "albums.js"
ALBUMS_PY = REPO_ROOT / "app" / "routes" / "albums.py"


def test_albums_js_renders_download_for_split_only():
    js = ALBUMS_JS.read_text(encoding="utf-8")
    # Gated on a.split — only finished albums have music/ tracks to bundle.
    assert "a.split" in js
    assert "/download" in js
    assert "downloadBtn" in js
    # And it's placed in the row's action cell.
    assert "${tagBtn}${downloadBtn}" in js


def test_endpoint_registered():
    py = ALBUMS_PY.read_text(encoding="utf-8")
    assert '@router.get("/api/album/{album_id}/download")' in py
    assert "zipfile.ZipFile" in py
    # Cleanup of the temp zip after the response is sent.
    assert "BackgroundTask(os.unlink" in py
