"""E2E check that the album table renders `mixed` in the format cell when
sides disagree on bit depth / sample rate.

Pins the per-side `source_format` plumbing through the album payload +
`fmtSourceFormat` walking `sides[]`. Reuses the seeded raw/ FLAC pattern
(one-shot ffmpeg in the recorder container); adds ~5 s on the running
compose stack.
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
