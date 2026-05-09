#!/usr/bin/env python3
"""Playwright driver for the README screenshots.

Drives a running vinyl-recorder instance with a headless Chromium and
captures three documentation screenshots:

    library.png       -- main page with all three library sections populated
    album-combine.png -- the "Combine into album" modal opened from raw rows
    split-editor.png  -- the wave-editor split modal for an in-progress album

Pinned viewport: 1440 x 900 (matches the size embedded in the README so
diffs between regenerations are dominated by real UI changes, not viewport
churn).

CI runs this on every PR that touches UI files. The script:

  * Assumes the app is already up and serving at `--url`. Seeding
    `OUTPUT_DIR` with realistic-looking data is *not* this script's job —
    `tools/seed_demo_data.py` (or the docker-compose test stack) handles
    that step. We just drive the browser.
  * Is idempotent: re-running overwrites the PNGs in place.
  * Fails loudly on any missing selector / blank canvas — silent passes are
    worse than red CI.

Usage:
    python tools/screenshots.py [--url URL] [--output-dir DIR]

Defaults: URL=http://127.0.0.1:8080, output=images/.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Pinned for layout stability — see module docstring.
VIEWPORT = {"width": 1440, "height": 900}
DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "images"

# Filenames written under --output-dir. Keep these stable; the README links
# to them by name.
OUT_LIBRARY = "library.png"
OUT_COMBINE = "album-combine.png"
OUT_EDITOR  = "split-editor.png"


def _wait_for_app(page, url: str, timeout_s: float = 20.0) -> None:
    """Poll /health until 200 — the app reads env at import time, and
    uvicorn briefly returns 502 while the asyncio loop spins up. Browsers
    cache failed initial fetches, so we hit the JSON endpoint directly."""
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            r = page.request.get(url.rstrip("/") + "/health")
            if r.ok:
                return
            last_err = f"status={r.status}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(0.5)
    raise RuntimeError(f"app at {url} never returned 200 on /health: {last_err}")


def _await_library_populated(page) -> None:
    """Block until all three library sections have rendered at least one
    row. The frontend hits `/api/recordings` and `/api/albums` in parallel,
    each tbody is filled separately, so we poll for tbody children present
    on each one rather than racing the network. Times out at 15 s with a
    descriptive failure so a missing seed crashes loudly."""
    page.wait_for_function(
        """() => {
            const t = id => document.querySelectorAll('#' + id + ' tr');
            // Count any non-empty tbody row, including the .empty-lib
            // placeholder, so a deliberate "Music: empty" seed doesn't
            // hang the test. Still requires raw + albums to actually
            // populate, since those are the load-bearing screenshots.
            const raw = t('lib-tbody').length;
            const alb = t('albums-tbody').length;
            const mus = t('music-tbody').length;
            return raw >= 1 && alb >= 1 && mus >= 1;
        }""",
        timeout=15_000,
    )


def _hide_volatile_chrome(page) -> None:
    """Mask UI bits that change between runs (timestamps, free-disk, level
    indicators) so the screenshot stays diff-stable across regenerations.
    Uses CSS visibility:hidden so layout doesn't shift."""
    page.add_style_tag(content="""
        /* Disk-free readout flickers as the temp dir fills/empties. */
        #disk-free { visibility: hidden; }
        /* Header version chip is `<git-describe>-dirty` — changes on every
           commit and would otherwise force the perceptual-diff to flag the
           screenshots as changed on every PR even when the UI is identical. */
        #version-tag { visibility: hidden !important; }
        /* Health dot color depends on whether AUTO_CONNECT happened to
           catch SomaFM mid-buffer. Hide the colored dot, keep the chip's
           layout slot. */
        .status-indicator .dot { visibility: hidden; }
        /* Suppress any open toast — they flash in/out at 3s and would land
           in the screenshot at random. */
        #toast-container { display: none !important; }
    """)


def _shot(page, output_dir: Path, name: str, full_page: bool = False) -> Path:
    """Write a screenshot atomically under `output_dir`, return the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / name
    page.screenshot(path=str(out), full_page=full_page)
    if not out.exists() or out.stat().st_size < 1024:
        raise RuntimeError(f"screenshot {name} was empty or unwritten")
    return out


# ── library.png ───────────────────────────────────────────────────────────

def _capture_library(page, output_dir: Path) -> Path:
    page.evaluate("() => { document.activeElement?.blur?.(); }")
    return _shot(page, output_dir, OUT_LIBRARY, full_page=True)


# ── album-combine.png ─────────────────────────────────────────────────────

def _capture_combine(page, output_dir: Path) -> Path:
    """Tick the first three raw checkboxes and click Combine. The combine
    modal reuses the tag-panel modal (`#tag-modal`) with the sides reorder
    section unhidden — see openCombine in main.js."""
    # Click the first three .row-check inputs inside the raw section so
    # the bulk bar lights up and the combine button enables.
    checks = page.locator("#raw-section input.row-check")
    n = checks.count()
    if n < 2:
        raise RuntimeError(f"need at least 2 raw rows to demo combine, got {n}")
    pick = min(3, n)
    for i in range(pick):
        # Use force-click on the underlying checkbox; the input lives
        # inside a .col-check cell with its own click handler that toggles
        # the row.
        checks.nth(i).check(force=True)
    # combine-btn is disabled until selected.size >= 1; wait for the
    # transition to be observable rather than relying on the synchronous
    # check() above.
    page.wait_for_selector("#combine-btn:not([disabled])", timeout=5_000)
    page.locator("#combine-btn").click()
    # Modal is `#tag-modal` with combine-sides-section unhidden.
    page.wait_for_selector("#tag-modal:not([hidden])", timeout=5_000)
    page.wait_for_selector("#combine-sides-section:not([hidden])", timeout=5_000)
    # Let the sides list render its draggable cards; bail loudly if it
    # never does (a regression in renderCombineSides would be invisible
    # in a noisy log otherwise).
    page.wait_for_function(
        f"""() => document.querySelectorAll('#combine-sides .side-row').length >= {pick}""",
        timeout=5_000,
    )
    # Click into a neutral spot so no input is focused (focus rings vary
    # between Chromium builds and would diff the PNG).
    page.evaluate("() => { document.activeElement?.blur?.(); }")
    return _shot(page, output_dir, OUT_COMBINE, full_page=False)


# ── split-editor.png ──────────────────────────────────────────────────────

def _capture_split_editor(page, output_dir: Path) -> Path:
    """Open the wave editor for the first in-progress album. Waits for
    the waveform canvas to render non-empty pixels before snapping."""
    # Close the combine modal if still open from the previous step. The
    # `state="hidden"` form waits for the modal-backdrop element to either
    # not exist or have its `hidden` attribute set — the bare attribute
    # selector doesn't match because Playwright's "visible" check excludes
    # hidden-attr nodes by default.
    page.evaluate("() => { if (typeof closeTag === 'function') closeTag(); }")
    page.wait_for_selector("#tag-modal", state="hidden", timeout=5_000)
    # Find the first album row's split (⌇) button. The handler is
    # openWaveEditor(album_id), wired through `data-fname`.
    split_btns = page.locator("#albums-tbody button[onclick^='openWaveEditor']")
    n = split_btns.count()
    if n < 1:
        raise RuntimeError("no in-progress albums found — seed data missing")
    split_btns.first.click()
    page.wait_for_selector("#we-modal:not([hidden])", timeout=5_000)
    # The waveform fetches `.peaks.dat` over the network, then drawPeaks
    # paints the canvas. Probe centerline pixels for the WAVEFORM colour
    # (`#6db3ff` — blue dominates) rather than any non-blank pixel: the
    # cut markers (red) and silence-region hatch land on the canvas
    # before the peaks do, so a colour-blind probe would let us snap a
    # screenshot of cuts-on-blank before the audio is rendered.
    page.wait_for_function(
        """() => {
            const c = document.getElementById('we-canvas');
            if (!c) return false;
            const ctx = c.getContext('2d');
            const data = ctx.getImageData(0, 70, c.width, 1).data;
            for (let i = 0; i < data.length; i += 4) {
                const r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
                // Waveform stroke is `#6db3ff` — blue dominates red.
                // Cut markers (`#ff…` red) fail the `b > r` check, the
                // silence-hatch grey fails the `b > 100` check.
                if (a > 0 && b > r && b > 100) return true;
            }
            return false;
        }""",
        timeout=15_000,
    )
    # Track list is rendered from the saved plan; wait for at least one
    # row so the bottom half of the modal isn't empty in the screenshot.
    # Class name is `.wave-track` — see renderTracks() in wave-editor.js.
    page.wait_for_function(
        "() => document.querySelectorAll('#we-tracks .wave-track').length >= 1",
        timeout=5_000,
    )
    page.evaluate("() => { document.activeElement?.blur?.(); }")
    # Editor modal is fixed-width (1100px) and taller than the viewport.
    # Use full_page=False so the screenshot crops to the visible area —
    # README target is "the editor as the user sees it".
    return _shot(page, output_dir, OUT_EDITOR, full_page=True)


# ── entry point ───────────────────────────────────────────────────────────

def run(url: str, output_dir: Path) -> list[Path]:
    """Drive the browser through all three captures. Returns the list of
    written paths so callers can log them."""
    # Lazy import so `--help` doesn't pay the playwright import tax.
    from playwright.sync_api import sync_playwright

    written: list[Path] = []
    with sync_playwright() as pw:
        # Lock language / timezone so date-formatting in the UI doesn't
        # drift between dev machines and CI.
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport=VIEWPORT,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            reduced_motion="reduce",
        )
        page = ctx.new_page()
        # Surface JS console errors so a load failure doesn't hide behind
        # a screenshot-of-a-blank-page.
        page.on("pageerror", lambda exc: print(f"  [pageerror] {exc}", file=sys.stderr))
        try:
            _wait_for_app(page, url)
            page.goto(url.rstrip("/") + "/", wait_until="networkidle")
            _await_library_populated(page)
            _hide_volatile_chrome(page)

            written.append(_capture_library(page, output_dir))
            written.append(_capture_combine(page, output_dir))
            written.append(_capture_split_editor(page, output_dir))
        finally:
            ctx.close()
            browser.close()
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--url", default=DEFAULT_URL,
        help=f"URL of the running app (default: {DEFAULT_URL}).",
    )
    ap.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory to write PNGs into (default: {DEFAULT_OUTPUT_DIR}).",
    )
    args = ap.parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    written = run(args.url, output_dir)
    for p in written:
        size = p.stat().st_size
        print(f"  wrote {p}  ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
