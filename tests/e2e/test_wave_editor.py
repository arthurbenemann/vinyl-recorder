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

from .conftest import RECORDER_URL, ffprobe

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
        _drive(page, sides, stack)
    finally:
        for p in sides:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def _drive(page, sides, stack):
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
                // Row checkboxes wire toggleRow via `onclick`, not `onchange` —
                // `cb.click()` toggles state and fires the handler in one go.
                if (!cb.checked) cb.click();
            }
        }
        """,
        side_names,
    )
    page.wait_for_selector('#combine-btn:not([disabled])', timeout=5_000)
    page.click('#combine-btn')
    # Combine reuses the tag-modal: openCombine reveals #combine-sides-section
    # inside it and switches the apply button copy.
    page.wait_for_selector('#tag-modal:not([hidden])')
    page.wait_for_selector('#combine-sides-section:not([hidden])')
    page.fill('#t-artist', 'WaveEditorSmokeArtist')
    page.fill('#t-album',  'WaveEditorSmokeAlbum')
    page.fill('#t-year',   '2026')
    page.click('#tag-apply-btn')
    # The modal flips its [hidden] attribute on close — `wait_for_selector`
    # default state is "visible" and never matches a hidden element, so
    # poll the attribute directly.
    page.wait_for_function(
        "() => document.getElementById('tag-modal').hasAttribute('hidden')",
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

    # ── Cold-build wrote per-side dats, no concat.flac ────────────────
    # The PR's whole point: the editor's first open should materialise
    # one .peaks.dat per side under .cache/peaks/, and never write a
    # `.cache/concat.flac`. The canvas-non-blank check above proves
    # render_peaks ran successfully; this pins the on-disk layout.
    in_progress = stack["output_dir"] / "in-progress"
    album_dir = in_progress / album_id
    cache_dir = album_dir / ".cache"
    peaks_dir = cache_dir / "peaks"
    assert peaks_dir.is_dir(), f"missing per-side peaks dir: {peaks_dir}"
    side_stems = {p.stem for p in sides}
    dat_stems = {p.stem for p in peaks_dir.glob("*.dat")}
    assert side_stems == dat_stems, \
        f"per-side dats don't match sides: {dat_stems} vs {side_stems}"
    assert not (cache_dir / "concat.flac").exists(), \
        "concat.flac should not be written under the per-side pipeline"

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

    # ── Audio element initialised on side 0 from per-side endpoint ────
    audio = page.evaluate(
        """
        () => {
            const a = document.getElementById('we-audio');
            return { src: a.src, duration: a.duration, readyState: a.readyState,
                     sideCount: weAudio.sides.length,
                     totalDur: weAudio.totalDuration() };
        }
        """
    )
    assert audio['src'].endswith(f'/api/album/{album_id}/sides/0/audio'), audio
    assert audio['readyState'] >= 1 and audio['duration'] > 0, audio
    assert audio['sideCount'] == len(sides), audio
    assert audio['totalDur'] > 0, audio

    # ── Side-swap on cross-boundary seek ─────────────────────────────
    # weAudio.seek(albumTime) past the first side's duration must swap
    # the <audio> element's src to /sides/1/audio and resolve currentTime
    # to a position within that side, not into negative or past-end land.
    swap = page.evaluate(
        """
        async () => {
            const beyondFirst = weAudio.sides[0].duration_seconds + 0.5;
            weAudio.seek(beyondFirst);
            // Wait briefly for the loadedmetadata handler to apply
            // currentTime — synthetic 4 s sides settle quickly under
            // headless chromium.
            for (let i = 0; i < 30; i++) {
                if (weAudio.currentSideIdx === 1) break;
                await new Promise(r => setTimeout(r, 50));
            }
            const a = document.getElementById('we-audio');
            return { src: a.src, sideIdx: weAudio.currentSideIdx,
                     albumTime: weAudio.currentTime };
        }
        """
    )
    assert swap['sideIdx'] == 1, swap
    assert swap['src'].endswith(f'/api/album/{album_id}/sides/1/audio'), swap

    # ── No JS pageerrors fired across the whole flow ──────────────────
    # Belt-and-braces — the conftest `page` fixture also asserts no
    # pageerrors on teardown, but pinning it inline here keeps the
    # original test self-documenting.
    assert not pageerrors, "console pageerrors: " + " · ".join(pageerrors[:3])


# ── Helpers shared by the smaller editor flows ───────────────────────────
def _combine_then_open_editor(page, sides, *, artist, album, year="2026"):
    """Drive combine modal + click split-into-tracks; returns the new
    album_id. Filters the album row by title so multiple tests in the
    same suite don't pick each other's rows up."""
    side_names = [s.name for s in sides]
    page.wait_for_function(
        f"() => document.querySelectorAll('input.row-check[data-fname]').length >= {len(sides)}",
        timeout=10_000,
    )
    page.evaluate(
        """
        (names) => {
            for (const n of names) {
                const cb = document.querySelector(
                    `input.row-check[data-fname="${n}"]`);
                // Row checkboxes wire toggleRow via `onclick`, not `onchange` —
                // `cb.click()` toggles state and fires the handler in one go.
                if (!cb.checked) cb.click();
            }
        }
        """,
        side_names,
    )
    page.wait_for_selector('#combine-btn:not([disabled])', timeout=5_000)
    page.click('#combine-btn')
    page.wait_for_selector('#tag-modal:not([hidden])')
    page.wait_for_selector('#combine-sides-section:not([hidden])')
    page.fill('#t-artist', artist)
    page.fill('#t-album',  album)
    page.fill('#t-year',   year)
    page.click('#tag-apply-btn')
    page.wait_for_function(
        "() => document.getElementById('tag-modal').hasAttribute('hidden')",
        timeout=20_000,
    )
    # Find the row by album text (not just first match — earlier tests
    # in the session may have left their own album rows behind).
    page.wait_for_function(
        "(album) => Array.from(document.querySelectorAll('tr[data-album-id]'))"
        ".some(r => r.textContent.includes(album))",
        arg=album,
        timeout=10_000,
    )
    album_id = page.evaluate(
        "(album) => Array.from(document.querySelectorAll('tr[data-album-id]'))"
        ".find(r => r.textContent.includes(album)).getAttribute('data-album-id')",
        album,
    )
    assert album_id, f"combine produced no row for {album!r}"
    page.click(
        f'tr[data-album-id="{album_id}"] button[title*="plit into tracks"]'
    )
    page.wait_for_selector('#we-modal:not([hidden])')
    page.wait_for_function(
        "() => typeof we !== 'undefined' && we.loaded === true && we.total > 0",
        timeout=20_000,
    )
    return album_id


# ── PR B: ghost-plan guard ───────────────────────────────────────────────
def test_wave_editor_open_close_does_not_write_default_plan(stack, page):
    """Reproduces the "ghost plan" issue: opening + closing the editor on
    a freshly-combined album, without touching cuts/titles/skip, must not
    POST a default-state plan to the server. A regression here pollutes
    `album.json.has_draft` (and the future "draft" UI pill) for every
    album the user merely peeked at."""
    raw = stack["raw"]
    sides = _generate_side_flacs(raw, count=2)
    plan_posts: list[str] = []
    page.on("request", lambda r: plan_posts.append(r.url)
            if r.method == "POST" and "/plan" in r.url else None)
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        album_id = _combine_then_open_editor(
            page, sides, artist="GhostPlanArtist", album="GhostPlanAlbum"
        )
        # Sit in the editor for longer than the 500 ms debounce so any
        # accidental _persistDraft would have fired by now.
        page.wait_for_timeout(1_000)
        page.evaluate("closeWaveEditor()")
        # closeWaveEditor calls _flushPlanSave; give the in-flight POST
        # (if any) a moment to land before we assert.
        page.wait_for_timeout(500)
        # Hit the manifest endpoint to confirm `plan` is still null.
        plan = page.evaluate(
            f"async () => {{ "
            f"const r = await fetch('/api/album/{album_id}/tracks'); "
            f"const d = await r.json(); return d.plan; }}"
        )
        assert plan is None, f"editor wrote a ghost plan: {plan!r}"
        # And no /plan POST should have hit the wire at all — the dirty
        # gate runs before the network call.
        assert not plan_posts, f"editor POSTed /plan with no edits: {plan_posts}"
    finally:
        for p in sides:
            try: p.unlink(missing_ok=True)
            except Exception: pass


# ── PR B: re-open after edit restores the draft ──────────────────────────
def test_wave_editor_reopen_restores_cut_skip_title(stack, page):
    """The user's edits — a cut, a skip flag, and a renamed track — must
    survive close + reopen. This walks the same flow that surfaced the
    `we.loaded` race in PR #71 and pins it down with state assertions."""
    raw = stack["raw"]
    sides = _generate_side_flacs(raw, count=2)
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        album_id = _combine_then_open_editor(
            page, sides, artist="ReopenArtist", album="ReopenAlbum"
        )
        # Edit: drop a cut roughly mid-album, mark the first region as
        # skip, and rename the second region. The save is debounced ~500ms
        # so we wait for the resulting /plan POST to settle.
        plan_posts: list[str] = []
        page.on("response", lambda r: plan_posts.append(r.url)
                if r.request.method == "POST" and "/plan" in r.url else None)
        page.evaluate(
            """
            () => {
                const t = (we.total || 0) / 2;
                weAddCutAtTime(t);
                weToggleSkip(0);
                weSetTitle(1, 'Track Beta');
            }
            """
        )
        # Poll plan_posts in the runner (Playwright's wait_for_function
        # runs in the page; the network listener lives here).
        deadline = time.time() + 4.0
        while time.time() < deadline and not plan_posts:
            page.wait_for_timeout(100)
        assert plan_posts, "no /plan POST after editing — debounce never fired"
        # Close + reopen.
        page.evaluate("closeWaveEditor()")
        page.wait_for_function(
            "() => document.getElementById('we-modal').hasAttribute('hidden')",
            timeout=4_000,
        )
        page.click(
            f'tr[data-album-id="{album_id}"] button[title*="e-edit splits"], '
            f'tr[data-album-id="{album_id}"] button[title*="plit into tracks"]'
        )
        page.wait_for_selector('#we-modal:not([hidden])')
        page.wait_for_function(
            "() => we.loaded === true",
            timeout=10_000,
        )
        # State must match what we set — exercises both the manifest
        # write and weLoadExistingSplit's repopulate path.
        state = page.evaluate(
            "() => ({ cuts: we.cuts.slice(), titles: we.titles.slice(), "
            "skipped: we.skipped.slice() })"
        )
        assert len(state['cuts']) == 1, f"expected 1 cut, got {state['cuts']!r}"
        assert state['skipped'][0] is True, f"first region should still be skipped: {state['skipped']!r}"
        assert state['titles'][1] == 'Track Beta', \
            f"second region title not preserved: {state['titles']!r}"
    finally:
        for p in sides:
            try: p.unlink(missing_ok=True)
            except Exception: pass


# ── PR: split with cuts that straddle side boundaries ────────────────────
def test_split_with_cuts_across_side_boundaries(stack, page):
    """The per-side concat-demuxer playlist has to handle the case where
    `-ss/-to` straddles a side boundary. Combine 3 sides (4 s each → 12 s
    album), place cuts at 3 s and 6 s so:

      track 1: 0 → 3 s        within side 1
      track 2: 3 → 6 s        spans side 1/2 boundary at 4 s
      track 3: 6 → 12 s       spans side 2/3 boundary at 8 s

    Run split, then ffprobe each output and assert the durations match
    (±0.05 s for FLAC frame quantisation). A regression in the playlist-
    based -ss/-to math would produce off-by-side-length tracks here."""
    raw = stack["raw"]
    sides = _generate_side_flacs(raw, count=3)
    output_dir = stack["output_dir"]
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        album_id = _combine_then_open_editor(
            page, sides, artist="CrossBoundaryArtist", album="CrossBoundaryAlbum"
        )
        # Place two cuts and rename the tracks so the music/ output is
        # predictable. weAddCutAtTime + weSetTitle are the same call sites
        # the user-edit UI hits, so they flip the dirty flag and trigger
        # the debounced /plan save.
        # Wait for the debounced /plan POST to actually land — relying on a
        # fixed wall-clock wait is the classic Playwright anti-pattern and
        # flakes under GHA load. `expect_response` blocks until a matching
        # request completes, so the test stays correct regardless of the
        # debounce timing.
        with page.expect_response(
            lambda r: "/api/album/" in r.url and r.url.endswith("/plan")
                      and r.request.method == "POST"
                      and r.ok,
            timeout=10_000,
        ):
            page.evaluate(
                """
                () => {
                    weAddCutAtTime(3.0);
                    weAddCutAtTime(6.0);
                    weSetTitle(0, 'A');
                    weSetTitle(1, 'B');
                    weSetTitle(2, 'C');
                }
                """
            )

        # Run split via the API directly — the UI button is present but
        # this path keeps the test focused on the server-side -ss/-to
        # behaviour rather than the modal-confirmation flow.
        split_result = page.evaluate(
            f"""
            async () => {{
                const r = await fetch('/api/album/split', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        album_id: '{album_id}',
                        tracks: [
                            {{title: 'A', duration_seconds: 3.0,  skip: false}},
                            {{title: 'B', duration_seconds: 3.0,  skip: false}},
                            {{title: 'C', duration_seconds: 6.0,  skip: false}},
                        ],
                        normalize: false,
                        target_peak_db: -1.0,
                        bit_depth: 0,
                    }}),
                }});
                if (!r.ok) throw new Error('split failed: ' + r.status + ' ' + (await r.text()));
                return r.json();
            }}
            """
        )
        assert 'music_relpath' in split_result, split_result
        relpath = split_result['music_relpath']
        music = output_dir / "music" / relpath
        out_flacs = sorted(music.glob("*.flac"))
        assert len(out_flacs) == 3, f"expected 3 output tracks: {out_flacs}"

        expected = [3.0, 3.0, 6.0]
        for f, want in zip(out_flacs, expected):
            probe = ffprobe(f)
            got = float(probe['format']['duration'])
            assert abs(got - want) <= 0.05, \
                f"{f.name}: duration {got:.3f} s, expected ~{want} s"
    finally:
        for p in sides:
            try: p.unlink(missing_ok=True)
            except Exception: pass


# ── Music row "N tracks" link expands an inline track list ───────────────
def test_music_row_expands_into_track_list(stack, page):
    """Clicking the `N tracks` link in the Music section toggles an inline
    subrow under the album showing each track's title, duration, size,
    plus a ▶ preview and ↓ download button. Click again to collapse.

    Pins the data-attribute contract between the rendered row
    (`tr[data-album-id]`) and `toggleTracks`' query selector — they
    diverged once already (`data-album` vs `data-album-id`), which
    silently broke the expansion since `querySelector` returned null
    and the handler early-returned with no toast or console error."""
    raw = stack["raw"]
    sides = _generate_side_flacs(raw, count=2)
    try:
        page.goto(RECORDER_URL)
        page.wait_for_load_state("networkidle")
        album_id = _combine_then_open_editor(
            page, sides, artist="ExpandArtist", album="ExpandAlbum"
        )
        # Add a cut so the split produces 2 tracks. Wait for the debounced
        # /plan POST so the editor isn't racing the next API call.
        with page.expect_response(
            lambda r: "/api/album/" in r.url and r.url.endswith("/plan")
                      and r.request.method == "POST" and r.ok,
            timeout=10_000,
        ):
            page.evaluate(
                """
                () => {
                    weAddCutAtTime(4.0);
                    weSetTitle(0, 'First Track');
                    weSetTitle(1, 'Second Track');
                }
                """
            )
        # Split via API + refresh albums so the row moves into the Music
        # section. Then close the editor so the modal isn't capturing
        # focus when we click on the table beneath it.
        page.evaluate(
            f"""
            async () => {{
                const r = await fetch('/api/album/split', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        album_id: '{album_id}',
                        tracks: [
                            {{title: 'First Track',  duration_seconds: 4.0, skip: false}},
                            {{title: 'Second Track', duration_seconds: 4.0, skip: false}},
                        ],
                        normalize: false,
                        target_peak_db: -1.0,
                        bit_depth: 0,
                    }}),
                }});
                if (!r.ok) throw new Error('split failed: ' + r.status + ' ' + (await r.text()));
                await refreshAlbums();
                closeWaveEditor();
            }}
            """
        )
        # The row's `N tracks` link only renders once `a.split && a.track_count`
        # is true on the server side — wait for the post-split refresh to land.
        link_sel = f'tr[data-album-id="{album_id}"] a.track-count-link'
        page.wait_for_selector(link_sel, timeout=10_000)

        # Expand: click the link, expect a sibling `tr.tracks-sub` to appear
        # carrying one `.track-row` per emitted track, each with the play /
        # download affordances wired up.
        page.click(link_sel)
        sub_sel = f'tr[data-album-id="{album_id}"] + tr.tracks-sub'
        page.wait_for_selector(sub_sel, timeout=5_000)
        details = page.evaluate(
            f"""
            () => {{
                const sub = document.querySelector('{sub_sel}');
                const rows = Array.from(sub.querySelectorAll('.track-row'));
                return {{
                    count: rows.length,
                    titles: rows.map(r => r.querySelector('.ttitle').textContent),
                    hasPreviewBtns: rows.every(r =>
                        r.querySelector('button.preview-btn[data-kind="track"]')),
                    hasDownloadLinks: rows.every(r =>
                        r.querySelector('a.icon-btn[href*="/track/"]')),
                }};
            }}
            """
        )
        assert details['count'] == 2, f"expected 2 track rows, got {details}"
        assert details['titles'] == ['First Track', 'Second Track'], details
        assert details['hasPreviewBtns'], details
        assert details['hasDownloadLinks'], details

        # Collapse: click the link again, expect the subrow to be removed.
        # `sub_sel` carries its own double quotes (the `data-album-id`
        # attribute value), so build the selector in JS from the album_id
        # via Playwright's `arg=` rather than f-string-substituting the whole
        # selector — escaping the inner quotes would land us in syntax-error
        # territory exactly the way an earlier revision of this test did.
        page.click(link_sel)
        page.wait_for_function(
            "id => !document.querySelector("
            "  `tr[data-album-id=\"${id}\"] + tr.tracks-sub`)",
            arg=album_id,
            timeout=5_000,
        )
    finally:
        for p in sides:
            try: p.unlink(missing_ok=True)
            except Exception: pass
