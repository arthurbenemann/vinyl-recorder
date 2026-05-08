"""End-to-end checks for the post-#71 raw-row UI cleanup.

Pins the visible surface area this PR removed (the Status column, the
`.row-tagged` / `.badge.tagged` / `.badge.raw` rules, the `f.tagged`
gating on inline rename) so a future regression that resurrects any of
them fails at the door instead of slipping past code review.

Drops a single synthetic FLAC into `output/raw/` via the recorder
container's ffmpeg — same seeding pattern other e2e files already use
to avoid a host-side ffmpeg dependency.
"""
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


def _seed_raw_flac(raw_dir: Path, name: str = "ui_smoke_side.flac") -> Path:
    """Drop a 3-second 96 kHz/16-bit stereo FLAC into raw/ via the
    recorder container's ffmpeg."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    p = raw_dir / name
    rel = p.relative_to(raw_dir.parent)
    container_path = f"/output/{rel}"
    subprocess.run(
        ["docker", "exec", "vinyl-recorder", "ffmpeg",
         "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=f=440:duration=3,volume=0.5",
         "-ar", "96000", "-ac", "2", "-c:a", "flac", "-y",
         container_path],
        check=True, capture_output=True, text=True,
    )
    return p


def test_raw_section_has_no_legacy_tagged_chrome(stack, page):
    """The Status column header, `.row-tagged` rows, and `.badge.tagged`
    / `.badge.raw` chips were dropped along with the dead `f.tagged`
    field. The amber `.row-untagged` accent on the title cell stays —
    raw rows are now unconditionally untagged, that class drives the
    accent bar in style.css."""
    raw = stack["raw"]
    seeded = _seed_raw_flac(raw)
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        # Wait for the seeded row to render before reading counts.
        page.wait_for_function(
            f"() => document.querySelectorAll("
            f"  '#raw-section tbody tr input.row-check[data-fname=\"{seeded.name}\"]'"
            f").length === 1",
            timeout=10_000,
        )

        counts = page.evaluate(
            """
            () => ({
                rowTagged: document.querySelectorAll('.row-tagged').length,
                badgeTagged: document.querySelectorAll('.badge.tagged').length,
                badgeRaw: document.querySelectorAll('.badge.raw').length,
                statusTh: document.querySelectorAll(
                    '#raw-section thead th[data-sort="status"]').length,
                rowUntagged: document.querySelectorAll(
                    '#raw-section tbody tr.row-untagged').length,
            })
            """
        )
        assert counts["rowTagged"]   == 0, f".row-tagged present: {counts}"
        assert counts["badgeTagged"] == 0, f".badge.tagged present: {counts}"
        assert counts["badgeRaw"]    == 0, f".badge.raw present: {counts}"
        assert counts["statusTh"]    == 0, f"raw Status column header present: {counts}"
        assert counts["rowUntagged"] >= 1, \
            f".row-untagged accent missing: {counts}"
    finally:
        try: seeded.unlink(missing_ok=True)
        except Exception: pass


def test_raw_row_title_inline_rename(stack, page):
    """Double-click the title cell of a raw row → input swap → Enter
    saves → the FLAC is renamed on disk (verified via /api/recordings).

    Pre-#80 the rename was gated on `!f.tagged`, evaluating against an
    `undefined` field. Now raw rows are unconditionally rename-able and
    the gate is gone; this test pins the new behaviour."""
    raw = stack["raw"]
    seeded = _seed_raw_flac(raw, name=f"rename_smoke_{int(time.time())}.flac")
    new_stem = f"renamed_smoke_{int(time.time())}"
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")

        # Locate the seeded row's title cell. The cell carries `data-fname`
        # and an `ondblclick="startInlineRename(...)"` handler.
        cell_sel = (
            f"#raw-section tbody td[data-fname=\"{seeded.name}\"]"
            f"[ondblclick^=\"startInlineRename\"]"
        )
        page.wait_for_selector(cell_sel, timeout=10_000)
        page.dblclick(cell_sel)
        # Inline rename inserts an `input.inline-rename` next to the title.
        page.wait_for_selector(f"{cell_sel} + input.inline-rename", timeout=4_000)
        page.fill(f"{cell_sel} + input.inline-rename", new_stem)
        page.press(f"{cell_sel} + input.inline-rename", "Enter")

        # The input vanishes once the POST resolves and the row re-renders
        # under the new filename.
        page.wait_for_function(
            "() => document.querySelector('input.inline-rename') === null",
            timeout=5_000,
        )
        page.wait_for_function(
            f"() => document.querySelectorAll("
            f"  '#raw-section tbody input.row-check[data-fname=\"{new_stem}.flac\"]'"
            f").length === 1",
            timeout=5_000,
        )

        # Belt-and-braces: confirm the rename actually moved the file
        # rather than just relabelling the row in the DOM.
        names = page.evaluate(
            """
            async () => {
                const r = await fetch('/api/recordings');
                const d = await r.json();
                return d.files.map(f => f.filename);
            }
            """
        )
        assert f"{new_stem}.flac" in names, \
            f"renamed file missing from /api/recordings: {names}"
        assert seeded.name not in names, \
            f"original filename still present after rename: {names}"
    finally:
        # Clean up under whichever name survived.
        for cand in (seeded, raw / f"{new_stem}.flac"):
            try: cand.unlink(missing_ok=True)
            except Exception: pass
