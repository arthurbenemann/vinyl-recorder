"""End-to-end smoke for uploading a custom cover in the tag panel.

Covers the gap where MusicBrainz / CAA and Discogs have no art for an
obscure pressing: the user picks a local image, sees it preview, and on
apply it's re-encoded server-side into the album's cover.jpg. Runs against
the live stack, so the assertion is the real thing — after apply, the
album actually serves the uploaded cover.
"""
import io
import subprocess
import time
from pathlib import Path

import pytest

try:
    from playwright.sync_api import expect  # noqa: F401
except ImportError:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e


def _seed_raw_flac(raw_dir: Path, name: str) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    p = raw_dir / name
    rel = p.relative_to(raw_dir.parent)
    subprocess.run(
        ["docker", "exec", "vinyl-recorder", "ffmpeg",
         "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=f=440:duration=3,volume=0.5",
         "-ar", "96000", "-ac", "2", "-c:a", "flac", "-y",
         f"/output/{rel}"],
        check=True, capture_output=True, text=True,
    )
    return p


def _png_bytes(color=(40, 160, 80), size=(64, 64)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_upload_custom_cover_lands_on_album(stack, page):
    raw = stack["raw"]
    stamp = int(time.time())
    fname = f"coverup_{stamp}.flac"
    seeded = _seed_raw_flac(raw, fname)
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(
            f'#raw-section tbody input.row-check[data-fname="{fname}"]',
            timeout=10_000,
        )
        # Open the tag panel directly on the seeded side (promote flow).
        page.evaluate("(f) => openTag(f)", fname)
        page.wait_for_selector("#tag-modal:not([hidden])", timeout=5_000)
        page.fill("#t-artist", f"CoverArtist{stamp}")
        page.fill("#t-album", f"CoverAlbum{stamp}")

        # Pick a local image; the hidden input accepts an in-memory buffer.
        page.set_input_files("#t-cover-file", files=[{
            "name": "art.png", "mimeType": "image/png", "buffer": _png_bytes(),
        }])
        # The preview renders the picked image locally (no server round-trip).
        page.wait_for_selector("#cover-preview img", timeout=5_000)

        # Apply: promote the side into a new album, then upload the cover.
        page.click("#tag-apply-btn")
        page.wait_for_function(
            "() => document.getElementById('tag-modal').hasAttribute('hidden')",
            timeout=20_000,
        )
        page.wait_for_function(
            f"() => Array.from(document.querySelectorAll('tr[data-album-id]'))"
            f"  .some(t => t.textContent.includes('CoverAlbum{stamp}'))",
            timeout=10_000,
        )
        album_id = page.evaluate(
            f"() => Array.from(document.querySelectorAll('tr[data-album-id]'))"
            f"  .find(t => t.textContent.includes('CoverAlbum{stamp}'))"
            f"  .getAttribute('data-album-id')",
        )
        assert album_id, "apply produced no album row"

        # The custom cover is now served for the album (re-encoded to JPEG).
        resp = page.request.get(f"{RECORDER_URL}/api/file-cover/{album_id}")
        assert resp.ok, f"cover not served: HTTP {resp.status}"
        assert "image/jpeg" in (resp.headers.get("content-type") or ""), resp.headers
        body = resp.body()
        assert body[:3] == b"\xff\xd8\xff", "served cover is not a JPEG"
    finally:
        try:
            seeded.unlink(missing_ok=True)
        except Exception:
            pass
