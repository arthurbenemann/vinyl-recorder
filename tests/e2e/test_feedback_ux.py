"""End-to-end checks for the feedback / undo UX track.

Covers the visible behaviours added in PR-K:
  * The library "Delete" button shows a toast with an inline Undo,
    not a `confirm()` dialog.
  * Clicking Undo restores the deleted FLAC.
  * The bulk-delete progress bar reads "Deleting N of M" rather than
    a flat "deleting…".
  * The wave editor surfaces a "Saving…" indicator while the auto-save
    POST is in flight.

These tests share the same compose stack as the rest of the e2e suite
(see conftest.py); a single synthetic FLAC is seeded into raw/ for the
library checks via the recorder container's ffmpeg (mirroring the
existing test_recordings_ui pattern so we don't have to teach the host
about ffmpeg).
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


def _seed_raw_flac(raw_dir: Path, name: str = "feedback_smoke.flac") -> Path:
    """Drop a 3-second 96 kHz/16-bit stereo FLAC into raw/. Mirrors the
    helper in test_recordings_ui.py — kept inline so the two test files
    are independent and the seeding contract can drift if needed."""
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


def test_library_delete_shows_undo_toast(stack, page):
    """Click the row's ✕ button → a toast with class .toast-with-undo
    appears in #toast-container, the file is gone from /api/recordings,
    and no confirm() dialog ever fired.

    The toast carries an "Undo" <button> that, when clicked, restores
    the file via POST /api/recordings/restore. The row re-appears in
    the table within a few seconds (refreshLib is fired by the undo
    callback)."""
    raw = stack["raw"]
    seeded = _seed_raw_flac(raw, name=f"undo_smoke_{int(time.time())}.flac")
    try:
        # Pre-emptively reject any confirm() dialog — if the old behaviour
        # ever resurfaces we'll see a hang and the rejected event will
        # leave a discoverable trace.
        confirms_seen: list[str] = []

        def _on_dialog(dialog):
            confirms_seen.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", _on_dialog)

        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")

        # Wait for the seeded row to render.
        row_check = (
            f"#raw-section tbody input.row-check[data-fname=\"{seeded.name}\"]"
        )
        page.wait_for_selector(row_check, timeout=10_000)

        # The per-row delete button is an action button — find by data-fname
        # + the "✕" glyph. dom-helpers.actionBtn wires `data-fname`.
        del_btn_sel = (
            f"#raw-section tbody tr "
            f"button[data-fname=\"{seeded.name}\"][title=\"Delete\"]"
        )
        page.click(del_btn_sel)

        # Toast-with-undo should land in #toast-container.
        toast_sel = "#toast-container .toast.toast-with-undo"
        page.wait_for_selector(toast_sel, timeout=4_000)

        # The row vanishes from the library.
        page.wait_for_function(
            f"() => document.querySelectorAll("
            f"  '#raw-section tbody input.row-check[data-fname=\"{seeded.name}\"]'"
            f").length === 0",
            timeout=5_000,
        )

        # Sanity: /api/recordings really doesn't carry the name anymore.
        listing = page.evaluate(
            "async () => (await (await fetch('/api/recordings')).json()).files.map(f => f.filename)"
        )
        assert seeded.name not in listing, \
            f"file still in /api/recordings after delete: {listing}"

        # No confirm() dialog ever fired.
        assert confirms_seen == [], \
            f"unexpected confirm() dialog(s): {confirms_seen}"

        # Click Undo.
        page.click(f"{toast_sel} button.toast-undo")

        # The row should reappear once the restore POST resolves.
        page.wait_for_function(
            f"() => document.querySelectorAll("
            f"  '#raw-section tbody input.row-check[data-fname=\"{seeded.name}\"]'"
            f").length === 1",
            timeout=8_000,
        )
        restored = page.evaluate(
            "async () => (await (await fetch('/api/recordings')).json()).files.map(f => f.filename)"
        )
        assert seeded.name in restored, \
            f"file missing from /api/recordings after Undo: {restored}"
    finally:
        # Belt-and-braces cleanup — whichever name survived (raw/ or
        # .trash/) gets unlinked so the next test starts clean.
        try: seeded.unlink(missing_ok=True)
        except Exception: pass
        try:
            for entry in (raw.parent / ".trash").glob(f"{seeded.stem}__trash_*.flac"):
                entry.unlink(missing_ok=True)
        except Exception: pass


def test_library_delete_toast_expires_without_undo(stack, page):
    """If the user lets the toast time out without clicking Undo, the
    soft-deleted file is purged on the next sweep (no orphan stays
    around forever). We don't wait the full TRASH_TTL_SECONDS (300 s)
    in the test — instead we just verify the toast disappears on its
    own and the file remains absent from /api/recordings."""
    raw = stack["raw"]
    seeded = _seed_raw_flac(raw, name=f"expire_smoke_{int(time.time())}.flac")
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(
            f"#raw-section tbody input.row-check[data-fname=\"{seeded.name}\"]",
            timeout=10_000,
        )
        page.click(
            f"#raw-section tbody tr "
            f"button[data-fname=\"{seeded.name}\"][title=\"Delete\"]"
        )
        toast_sel = "#toast-container .toast.toast-with-undo"
        page.wait_for_selector(toast_sel, timeout=4_000)
        # The toast auto-dismisses after ~5 s — verify it vanishes
        # without us clicking Undo.
        page.wait_for_function(
            "() => document.querySelectorAll('#toast-container .toast.toast-with-undo').length === 0",
            timeout=8_000,
        )
        # File remains gone.
        listing = page.evaluate(
            "async () => (await (await fetch('/api/recordings')).json()).files.map(f => f.filename)"
        )
        assert seeded.name not in listing, listing
    finally:
        try: seeded.unlink(missing_ok=True)
        except Exception: pass
        try:
            for entry in (raw.parent / ".trash").glob(f"{seeded.stem}__trash_*.flac"):
                entry.unlink(missing_ok=True)
        except Exception: pass


def test_bulk_delete_progress_counts(stack, page):
    """Bulk delete from the raw library shows per-file progress.
    Previously the phase text was a static "deleting…"; now it reads
    "Deleting N of M" and the percentage climbs as each file's DELETE
    resolves.

    Asserts the phase text matches the new format at least once; we
    don't pin specific intermediate counts because the per-file
    request resolves so fast for tiny FLACs that the test would race
    the UI updates."""
    raw = stack["raw"]
    seeded: list[Path] = []
    stamp = int(time.time())
    try:
        for i in range(3):
            seeded.append(_seed_raw_flac(raw, name=f"bulk_smoke_{stamp}_{i}.flac"))
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        # Wait for all 3 rows to render.
        for s in seeded:
            page.wait_for_selector(
                f"#raw-section tbody input.row-check[data-fname=\"{s.name}\"]",
                timeout=10_000,
            )
        # Check each of the 3 row checkboxes.
        for s in seeded:
            page.click(
                f"#raw-section tbody input.row-check[data-fname=\"{s.name}\"]"
            )
        # The bulk action bar's Delete button fires bulkDelete().
        page.click("#bulk-bar button[onclick='bulkDelete()']")
        # Phase text appears with the new "Deleting N of M" shape.
        page.wait_for_function(
            "() => /Deleting \\d+ of \\d+/.test("
            "  document.getElementById('bulk-action-phase').textContent)",
            timeout=5_000,
        )
        # All 3 files vanish from the library.
        for s in seeded:
            page.wait_for_function(
                f"() => document.querySelectorAll("
                f"  '#raw-section tbody input.row-check[data-fname=\"{s.name}\"]'"
                f").length === 0",
                timeout=5_000,
            )
    finally:
        for s in seeded:
            try: s.unlink(missing_ok=True)
            except Exception: pass
        try:
            for entry in (raw.parent / ".trash").glob(f"bulk_smoke_{stamp}_*.flac"):
                entry.unlink(missing_ok=True)
        except Exception: pass


def test_wave_editor_saving_indicator_visible_in_flight(stack, page):
    """Editing in the wave editor fires a debounced POST /api/album/.../plan.
    While the request is in flight, the modal header should show a "Saving…"
    pill (#we-saving-indicator); once the response lands, the existing
    "saved Xs ago" pill (#we-saved) takes over.

    We assert the saving indicator becomes visible at some point during
    the save cycle. The window can be brief on a fast machine (~50ms for
    a localhost POST against an in-memory test container), so we use
    page.wait_for_function to poll the visibility rather than a single
    snapshot. The post-save state is verified by the existing
    test_wave_editor.* suite — no need to duplicate it here."""
    raw = stack["raw"]
    sides: list[Path] = []
    stamp = int(time.time())
    try:
        # Two short sides so we can combine + open the editor cheaply.
        for i in range(2):
            sides.append(_seed_raw_flac(raw, name=f"saving_ind_{stamp}_{i}.flac"))
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        # Reuse the JS surface to combine the two seeded sides into an
        # album, then open the editor — same path the wave_editor e2e
        # uses, just inlined here to keep this test independent.
        for s in sides:
            page.wait_for_selector(
                f"#raw-section tbody input.row-check[data-fname=\"{s.name}\"]",
                timeout=10_000,
            )
            page.click(
                f"#raw-section tbody input.row-check[data-fname=\"{s.name}\"]"
            )
        page.click("#combine-btn")
        # The combine flow reuses the tag-panel modal (sides reorder
        # section unhidden via openCombine); there is no separate
        # `#combine-modal`. Match the pattern in test_wave_editor.py.
        page.wait_for_selector("#tag-modal:not([hidden])", timeout=5_000)
        page.wait_for_selector("#combine-sides-section:not([hidden])", timeout=5_000)
        page.fill("#t-artist", f"FeedbackArtist{stamp}")
        page.fill("#t-album", f"FeedbackAlbum{stamp}")
        page.fill("#t-year", "2026")
        page.click("#tag-apply-btn")
        page.wait_for_function(
            "() => document.getElementById('tag-modal').hasAttribute('hidden')",
            timeout=20_000,
        )
        # Open the editor on the new album.
        page.wait_for_function(
            f"() => Array.from(document.querySelectorAll('tr[data-album-id]'))"
            f"  .some(t => t.querySelector('.row-title-text')"
            f"    ?.textContent === 'FeedbackAlbum{stamp}')",
            timeout=10_000,
        )
        page.click(
            f"tr:has(.row-title-text:has-text('FeedbackAlbum{stamp}')) "
            f"button[title*='plit into tracks']"
        )
        page.wait_for_selector("#we-modal:not([hidden])", timeout=10_000)
        # Wait for BOTH the loaded flag AND a non-zero total — the editor
        # marks itself loaded before peaks/duration land, and we need
        # `we.total > 0` so the cut math below picks a real timestamp.
        # Matches the pattern in test_wave_editor.py.
        page.wait_for_function(
            "() => typeof we !== 'undefined' && we.loaded === true && we.total > 0",
            timeout=20_000,
        )
        # Slow the plan POST so the "Saving…" indicator stays visible long
        # enough for Playwright's ~100ms poll cadence to catch it. Without
        # this, the in-flight window on a localhost POST is ~10-50 ms and
        # the visibility transition is racy.
        def _slow_plan_post(route):
            import time as _t
            _t.sleep(0.6)
            route.continue_()
        page.route("**/api/album/**/plan", _slow_plan_post)

        # Trigger an edit that flips dirty=true and schedules the
        # debounced save. weAddCutAtTime is the same handle the existing
        # wave-editor tests use.
        page.evaluate("() => { weAddCutAtTime((we.total || 0) / 2); }")
        # The "Saving…" indicator becomes visible from when _savePlanNow
        # calls _showSavingIndicator (after the 500ms debounce) until the
        # POST resolves. With the slowdown above, that window is ~600ms,
        # comfortably above the poll cadence.
        page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('we-saving-indicator');"
            "  return el && !el.hidden;"
            "}",
            timeout=5_000,
        )
        # And after the save resolves, the indicator hides again and the
        # persistent "saved Xs ago" pill takes over.
        page.wait_for_function(
            "() => {"
            "  const sv = document.getElementById('we-saving-indicator');"
            "  const sd = document.getElementById('we-saved');"
            "  return sv && sv.hidden && sd && !sd.hidden;"
            "}",
            timeout=5_000,
        )
    finally:
        # Close the editor cleanly to flush any in-flight save.
        try: page.evaluate("closeWaveEditor()")
        except Exception: pass
        for s in sides:
            try: s.unlink(missing_ok=True)
            except Exception: pass


def test_toast_with_undo_has_alert_role(stack, page):
    """ARIA contract: the toast-with-undo carries role="alert" so
    screen readers announce it immediately, and the Undo button has
    an aria-label that includes the action context (since "Undo"
    alone is ambiguous when announced out of context)."""
    raw = stack["raw"]
    seeded = _seed_raw_flac(raw, name=f"aria_smoke_{int(time.time())}.flac")
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(
            f"#raw-section tbody input.row-check[data-fname=\"{seeded.name}\"]",
            timeout=10_000,
        )
        page.click(
            f"#raw-section tbody tr "
            f"button[data-fname=\"{seeded.name}\"][title=\"Delete\"]"
        )
        toast_sel = "#toast-container .toast.toast-with-undo"
        page.wait_for_selector(toast_sel, timeout=4_000)
        aria = page.evaluate(
            "() => {"
            "  const t = document.querySelector('#toast-container .toast.toast-with-undo');"
            "  const b = t && t.querySelector('button.toast-undo');"
            "  return {"
            "    toastRole: t && t.getAttribute('role'),"
            "    btnAria:   b && b.getAttribute('aria-label'),"
            "    btnTag:    b && b.tagName,"
            "  };"
            "}"
        )
        assert aria["toastRole"] == "alert", aria
        assert aria["btnTag"] == "BUTTON", aria
        assert aria["btnAria"] and "Undo:" in aria["btnAria"], aria
    finally:
        try: seeded.unlink(missing_ok=True)
        except Exception: pass
        try:
            for entry in (raw.parent / ".trash").glob(f"{seeded.stem}__trash_*.flac"):
                entry.unlink(missing_ok=True)
        except Exception: pass
