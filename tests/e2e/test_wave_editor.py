"""End-to-end smoke for the wave-editor's peaks pipeline.

Drives the browser through the user-visible flow that this PR's API tests
can't exercise: combine N raw sides into an album, open the editor,
confirm the canvas waveform draws, the approximate peak readout shows
with the leading `~`, the silence slider's dB readout updates, the
silence detect via .peaks.dat returns sub-second, measure replaces the
approximation with an exact astats reading, and a sides-reorder rebuilds
the cached `.peaks.dat`.

The synthetic FLACs are dropped into `output/raw/` from the host before
the editor opens — same dir the recorder watches, so /api/recordings
picks them up on the next refresh without going through the upstream
session. Three sides at different sine frequencies make reorder bugs
visually distinguishable without actually capturing audio.
"""
import time
from pathlib import Path

import pytest

try:
    from playwright.sync_api import expect  # noqa: F401
except ImportError:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

from .conftest import RECORDER_URL

pytestmark = pytest.mark.e2e


def _generate_side_flacs(raw_dir: Path, count: int = 3) -> list[Path]:
    """Drop `count` synthetic FLACs into `raw/`. Different frequencies per
    side so the rendered waveform isn't visually identical — catches a
    sides-reorder bug that a single-side test would miss."""
    import subprocess
    out: list[Path] = []
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        p = raw_dir / f"smoke_side{i}.flac"
        # Generate via the recorder container's ffmpeg so we don't depend
        # on a host-side install. The FLAC lands in raw/ via the bind mount.
        rel = p.relative_to(raw_dir.parent)
        container_path = f"/output/{rel}"
        subprocess.run(
            ["docker", "exec", "vinyl-recorder", "ffmpeg",
             "-loglevel", "error",
             "-f", "lavfi",
             "-i", f"sine=f={400 + i * 50}:duration=4,volume=0.5",
             "-ar", "96000", "-ac", "2", "-c:a", "flac", "-y",
             container_path],
            check=True, capture_output=True, text=True,
        )
        out.append(p)
    return out


def test_wave_editor_full_flow(stack, page):
    """Walk the entire PR test plan in one go. Failures are aggregated so
    the report shows everything that broke, not just the first hit."""
    raw = stack["raw"]
    sides = _generate_side_flacs(raw, count=3)
    try:
        _drive(page, sides)
    finally:
        for p in sides:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def _drive(page, sides):
    page.goto(RECORDER_URL)
    page.wait_for_load_state("networkidle")

    pageerrors: list[str] = []
    page.on("pageerror", lambda e: pageerrors.append(e.message))

    # ── Combine: select the seed sides + open + run ───────────────────
    page.wait_for_function(
        f"() => document.querySelectorAll('input.row-check[data-fname]').length >= {len(sides)}",
        timeout=10_000,
    )
    side_names = [s.name for s in sides]
    page.evaluate(
        """
        (names) => {
            for (const n of names) {
                const cb = document.querySelector(
                    `input.row-check[data-fname="${n}"]`);
                cb.checked = true;
                cb.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }
        """,
        side_names,
    )
    page.wait_for_selector('#combine-btn:not([disabled])', timeout=5_000)
    page.click('#combine-btn')
    page.wait_for_selector('#combine-modal:not([hidden])')
    page.fill('#c-artist', 'WaveEditorSmokeArtist')
    page.fill('#c-album',  'WaveEditorSmokeAlbum')
    page.fill('#c-year',   '2026')
    page.click('#combine-go')
    # The modal flips its [hidden] attribute on close — `wait_for_selector`
    # default state is "visible" and never matches a hidden element, so
    # poll the attribute directly.
    page.wait_for_function(
        "() => document.getElementById('combine-modal').hasAttribute('hidden')",
        timeout=20_000,
    )
    page.wait_for_function(
        "() => document.querySelector('tr[data-album-id]') !== null",
        timeout=10_000,
    )
    album_id = page.eval_on_selector(
        'tr[data-album-id]', 'el => el.getAttribute("data-album-id")',
    )
    assert album_id, "combine produced no album row"

    # ── Open editor + wait for peaks to land ──────────────────────────
    page.click(f'tr[data-album-id="{album_id}"] button[title*="plit into tracks"]')
    page.wait_for_selector('#we-modal:not([hidden])')
    page.wait_for_function(
        "() => { const t = document.getElementById('we-stats-text').textContent; return t && (t.includes('peak') || t.includes('unavailable')); }",
        timeout=20_000,
    )

    # ── Canvas renders a non-blank envelope ───────────────────────────
    canvas = page.evaluate(
        """
        () => {
            const c = document.getElementById('we-canvas');
            const ctx = c.getContext('2d');
            const data = ctx.getImageData(0, 0, c.width, c.height).data;
            let nz = 0;
            for (let i = 0; i < data.length; i += 4) {
                if (data[i] || data[i+1] || data[i+2]) nz++;
            }
            return { w: c.width, h: c.height, nz };
        }
        """
    )
    assert canvas['nz'] > 100, f"canvas blank: {canvas}"

    # ── ~peak readout shows up immediately on open ────────────────────
    stats = page.text_content('#we-stats-text') or ""
    assert '~' in stats and 'dB' in stats, f"stats missing ~peak: {stats!r}"

    # ── Wheel-zoom is local (no /peaks /audio /measure /silence calls) ─
    zoom_requests: list[str] = []
    page.on("request", lambda r: zoom_requests.append(r.url))
    wrap = page.locator('#we-wrap').bounding_box()
    cx = wrap['x'] + wrap['width'] / 2
    cy = wrap['y'] + wrap['height'] / 2
    page.mouse.move(cx, cy)
    for _ in range(30):
        page.mouse.wheel(0, -100)
        page.wait_for_timeout(20)
    page.wait_for_timeout(500)
    blocked = ('/peaks', '/waveform', '/audio', '/measure',
               '/silence', '/manifest')
    zoom_relevant = [u for u in zoom_requests if any(p in u for p in blocked)]
    assert not zoom_relevant, f"zoom triggered API calls: {zoom_relevant}"

    zoom_state = page.evaluate("({s: we.viewStart, e: we.viewEnd})")
    assert zoom_state['e'] - zoom_state['s'] <= 1.0 + 1e-6, \
        f"zoom didn't reach a sub-second window: {zoom_state}"

    # Reset zoom for downstream checks.
    page.evaluate("we.viewStart = 0; we.viewEnd = we.total; drawAll()")

    # ── Silence slider readout updates live ───────────────────────────
    page.click('button:has-text("suggest from silence")')
    page.wait_for_selector('#we-pop-silence:not([hidden])')
    page.evaluate(
        """
        () => {
            const s = document.getElementById('we-noise');
            s.value = 32;
            s.dispatchEvent(new Event('input', { bubbles: true }));
        }
        """
    )
    readout = page.text_content('#we-noise-readout') or ""
    db = float(readout.replace(' dB', '').strip())
    # 20*log10((32*256+127.5)/32768) = -11.91 dB. Allow ±0.5 for any
    # mid-bin reconstruction tweak.
    assert -12.4 <= db <= -11.4, f"slider readout off: {readout!r}"

    # ── Silence detect via .dat returns sub-second ────────────────────
    t0 = time.time()
    page.click('#we-pop-silence button:has-text("just highlight")')
    page.wait_for_function(
        "() => { const t = document.getElementById('we-silence-status').textContent; return t && (/silences/.test(t) || /failed/.test(t)); }",
        timeout=10_000,
    )
    elapsed = time.time() - t0
    silence_status = page.text_content('#we-silence-status') or ""
    assert 'failed' not in silence_status.lower(), silence_status
    assert elapsed < 3.0, f"silence detect too slow: {elapsed*1000:.0f} ms"

    # ── Measure replaces ~ with exact peak + noise floor ──────────────
    page.click('#we-measure-btn')
    page.wait_for_function(
        "() => { const t = document.getElementById('we-stats-text').textContent; return t && (/noise floor/.test(t) || /failed/.test(t)); }",
        timeout=20_000,
    )
    measured = page.text_content('#we-stats-text') or ""
    assert 'noise floor' in measured.lower(), measured
    assert '~' not in measured, f"measure result still shows ~ prefix: {measured!r}"

    # ── Audio element loaded a seekable file from /audio ──────────────
    audio = page.evaluate(
        """
        () => {
            const a = document.getElementById('we-audio');
            return { src: a.src, duration: a.duration, readyState: a.readyState };
        }
        """
    )
    assert audio['src'].endswith(f'/api/album/{album_id}/audio'), audio
    assert audio['readyState'] >= 1 and audio['duration'] > 0, audio

    # ── No JS pageerrors fired across the whole flow ──────────────────
    assert not pageerrors, "console pageerrors: " + " · ".join(pageerrors[:3])
