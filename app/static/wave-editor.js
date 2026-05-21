// Unified album-split editor.
//
// See Architecture.md § Wave editor for design rationale.

'use strict';

const we = {
  albumId:     null,         // album_id slug from `albumsByName`
  total:       0,            // album duration in seconds
  viewStart:   0,            // visible window in seconds
  viewEnd:     0,
  cuts:        [],           // sorted seconds
  titles:      ['Track 1'],  // length === cuts.length + 1
  skipped:     [false],      // length === cuts.length + 1; true = drop region from output
  positions:   [''],         // length === titles; per-region Discogs position
                             //   (e.g. "A1", "B2", "1-01"). Empty when no
                             //   tracklist has been applied, or for regions
                             //   the user added by hand.
  silences:    [],           // {start, end, duration} from /detect-silences
  candidates:  [],           // last MB+Discogs search results
  hoverX:      null,         // last mouse x in main waveform px (for + add cut at playhead)
  dragging:    null,         // { kind: 'cut'|'mini', i?, lastX? }
  isPlaying:   false,
  playingTrack: null,        // index of the track currently being played, or null for free play
  playingEnd:  null,         // time at which playback should auto-pause
  measured:    null,         // last /api/album/measure result, or null while stale
  approxPeakDb: null,        // peak read from .peaks.dat (instant, ±0.05 dB at vinyl peaks). Replaced by exact value on measure.
  peaks:       null,         // parsed .peaks.dat, fed to drawPeaks() on every redraw
  targetPeakDb: -1.0,        // overwritten from /api/config (default_split_target_peak_db)
  // True iff the user has actually edited cuts/titles/skip/etc since the
  // last save or open. _savePlanNow gates on this so a no-edit open-and-
  // close sequence doesn't write a default-state plan to album.json. The
  // user-edit call sites (weAddCutAtTime, weDeleteCut, weToggleSkip,
  // weSetTitle, weSetCutAt, weClearCuts) flip it to true; _savePlanNow
  // resets it after a successful POST.
  dirty:       false,
  // Optimistic-concurrency token. Seeded from the manifest's current
  // plan_version on editor open (via weLoadExistingSplit) and bumped on
  // every successful save. Sent as `expected_version` in the next save
  // so the server can detect that another tab wrote in between.
  planVersion: 0,
  // Latch flipped true after a 409 to keep _persistDraft from
  // re-arming the debounce in a tight loop. Cleared when the user
  // resolves the conflict (reload-from-server or modal close).
  planConflict: false,
};

// Slider-readout helper: convert 1..127 to dB. Mid-bin reconstruction:
// amp = (v*256 + 127.5) / 32768; dB = 20*log10(amp). Slider notches match
// the 8-bit quantisation in .peaks.dat so each step is one detectable
// amplitude level.
function weNoiseSliderDb(v) {
  const n = Math.max(1, Math.min(127, parseInt(v, 10) || 1));
  const amp = (n * 256 + 127.5) / 32768;
  return (20 * Math.log10(Math.min(1, amp))).toFixed(1);
}

function fmtMMSS(sec) {
  if (sec == null || isNaN(sec)) return '';
  const cs = Math.max(0, Math.round(sec * 100));
  const m = Math.floor(cs / 6000);
  const s = Math.floor((cs % 6000) / 100);
  const c = cs % 100;
  return m + ':' + String(s).padStart(2, '0') + '.' + String(c).padStart(2, '0');
}
function parseMMSS(str) {
  if (!str) return 0;
  const parts = String(str).trim().split(':').map(p => parseFloat(p) || 0);
  return parts.length === 2 ? parts[0] * 60 + parts[1]
       : parts.length === 3 ? parts[0] * 3600 + parts[1] * 60 + parts[2]
       : parts[0] || 0;
}

// Pixel ↔ time mapping for the *main* waveform (which shows [viewStart, viewEnd]).
function _wrap()    { return document.getElementById('we-wrap'); }
function _wrapW()   { return _wrap().getBoundingClientRect().width; }
function _viewLen() { return Math.max(0.01, we.viewEnd - we.viewStart); }
function _xToTime(x) {
  return Math.max(0, Math.min(we.total, we.viewStart + (x / _wrapW()) * _viewLen()));
}
function _timeToPctView(t) {
  // % within the current viewport (clipped). Returns null if out of view.
  if (t < we.viewStart || t > we.viewEnd) return null;
  return ((t - we.viewStart) / _viewLen()) * 100;
}
function _timeToPctFull(t) {
  if (!we.total) return 0;
  return Math.max(0, Math.min(100, (t / we.total) * 100));
}

// ── Draft persistence ─────────────────────────────────────────────────────
// Cuts, titles, and skip flags are persisted to album.json.plan on the
// server (debounced). If the user closes the modal mid-edit, reopens the
// album from another browser, or even hands off to someone else on the
// same recorder install, the in-progress edit is right where they left
// it. The plan field is the same one /api/album/split writes after a
// successful run — the only difference is `manifest.music_relpath`, which
// stays null until tracks have actually been emitted to music/.
let _planSaveTimer = null;
let _planSaveInFlight = null;

function _persistDraft() {
  // Called from every interaction (cut drag, title input, skip toggle).
  // Debounce ~500 ms so a flurry of micro-edits collapses into one POST.
  if (!we.albumId) return;
  if (_planSaveTimer) clearTimeout(_planSaveTimer);
  _planSaveTimer = setTimeout(_savePlanNow, 500);
}

async function _savePlanNow() {
  _planSaveTimer = null;
  if (!we.albumId) return;
  // In-flight coalesce. The 500 ms debounce in _persistDraft already
  // collapses bursts of edits, but a slow POST can outlive its own
  // debounce window — a follow-up flurry would fire a second fetch
  // while the first is still in transit, and two concurrent
  // write_manifest calls on the server race with no file lock. Reschedule
  // instead; _buildPlan reads from we.* at call time, so the retry picks
  // up the latest edits including anything that arrived during the
  // outstanding POST. Mirrors the we.loaded race-guard pattern below.
  if (_planSaveInFlight) {
    _planSaveTimer = setTimeout(_savePlanNow, 200);
    return;
  }
  // Open-time race guard. openWaveEditor seeds the editor with empty
  // defaults and then kicks off weLoadExistingSplit() to fill them in
  // from album.json. drawAll() runs in between, which calls _persistDraft()
  // via renderTracks(). If the debounce fires before the load completes
  // we'd POST the empty default state and clobber the user's saved plan.
  // we.loaded flips true the moment the load resolves (with or without
  // a plan); until then we just re-arm the timer.
  if (!we.loaded) {
    _planSaveTimer = setTimeout(_savePlanNow, 200);
    return;
  }
  // Ghost-plan guard. renderTracks() also fires _persistDraft (so newly-
  // loaded plan rows trigger a fresh re-save), and drawAll() runs once at
  // open time. Without this gate, every editor open writes a default
  // single-track plan even if the user never touched anything — pollutes
  // album.json.has_draft for every album the user merely peeked at.
  if (!we.dirty) return;
  // Conflict latch: once we've seen a 409, stop auto-saving until the
  // user resolves it (reload-from-server clears the latch). Otherwise
  // every keystroke would re-collide with the server's newer state.
  if (we.planConflict) return;
  // Snapshot the current editor state into the plan shape the server
  // already understands (see SplitRequest / PlanUpdateRequest).
  const tracks = _regions().map(r => ({
    title: (r.title || '').trim(),
    duration_seconds: Math.max(0, r.end - r.start),
    skip: !!r.skip,
  }));
  const albumId = we.albumId;
  // Persist the format / bit-depth / sample-rate selectors alongside the
  // tracks. PlanUpdateRequest treats null fields as no-op, so reading the
  // <select> values once each save is fine even if the user never touched
  // them — we just write the same default back. Re-edit reload then sees
  // these and rehydrates the selectors.
  const outputFormat = document.getElementById('we-format')?.value || 'flac';
  const bitDepth     = parseInt(document.getElementById('we-bitdepth')?.value, 10);
  const sampleRate   = parseInt(document.getElementById('we-sample-rate')?.value, 10);
  const planBody = { tracks, expected_version: we.planVersion };
  if (!Number.isNaN(bitDepth))   planBody.bit_depth     = bitDepth;
  if (!Number.isNaN(sampleRate)) planBody.sample_rate   = sampleRate;
  if (outputFormat)              planBody.output_format = outputFormat;
  // Clear dirty BEFORE awaiting the fetch. The body we're about to POST
  // is already a snapshot of we.* at this point, so we've "consumed" the
  // current dirt. Any edit that lands while the fetch is in flight will
  // (re-)set we.dirty = true via its callsite, and the coalesce guard at
  // the top reschedules the next save instead of starting a second POST
  // — so this never loses an edit. On a network failure we restore the
  // flag in catch so the next interaction still retries.
  we.dirty = false;
  _showSavingIndicator();
  try {
    _planSaveInFlight = fetch(`/api/album/${albumId}/plan`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(planBody),
    });
    const r = await _planSaveInFlight;
    if (we.albumId !== albumId) return;  // editor moved on
    if (r && r.status === 409) {
      // Another tab wrote a newer plan_version. Don't overwrite local
      // edits — surface a toast with a Reload action so the user can
      // pull the latest server state when they're ready.
      we.planConflict = true;
      // PR-31 cleared `we.dirty` pre-fetch (so concurrent edits during
      // the in-flight save flip it back to true). On a 409 the server
      // rejected our snapshot, so re-arm dirty: a future user gesture
      // (e.g. the Reload button) clears the conflict latch and the
      // next save retries with the rehydrated server state.
      we.dirty = true;
      _showPlanConflictToast(albumId);
      return;
    }
    if (r && r.ok) {
      // we.dirty was already cleared pre-fetch (see comment above the
      // `try` block); read the new plan_version from the response and
      // hand off to the saved-pill ticker.
      try {
        const body = await r.json();
        if (body && typeof body.plan_version === 'number') {
          we.planVersion = body.plan_version;
        }
      } catch (e) { /* legacy server without version — leave planVersion */ }
      _markSavedNow();
    } else {
      // Non-OK response (server rejected the plan). Mark dirty again so
      // the next interaction re-attempts and the user isn't stranded on
      // a stale view of the saved state.
      we.dirty = true;
    }
  } catch (e) {
    // Network blip — re-arm dirty so the next change retries. Silent on
    // purpose; a toast for every transient save failure would spam.
    if (we.albumId === albumId) we.dirty = true;
  } finally {
    _planSaveInFlight = null;
    _hideSavingIndicator();
  }
}

// "Saving…" indicator — shown while a POST /api/album/{id}/plan is in
// flight. Pairs with the persistent "saved Xs ago" pill (#we-saved):
// during a save the saving indicator is visible and the saved pill is
// hidden; on success _markSavedNow() shows the saved pill again. Lets
// the user tell "edit in flight" from "edit confirmed" at a glance.
function _showSavingIndicator() {
  const el = document.getElementById('we-saving-indicator');
  const saved = document.getElementById('we-saved');
  if (el) el.hidden = false;
  // Hide the saved pill while a save is pending so the two indicators
  // don't read as conflicting state. _renderSavedLabel will un-hide it
  // when the save lands.
  if (saved) saved.hidden = true;
}

function _hideSavingIndicator() {
  const el = document.getElementById('we-saving-indicator');
  if (el) el.hidden = true;
}

// 409 UX: surface a toast announcing the conflict and offer a Reload
// action that re-runs weLoadExistingSplit (which resets planVersion +
// rehydrates cuts/titles/skipped from the server's newer plan). Until
// the user clicks Reload OR closes + reopens the editor, the conflict
// latch keeps auto-save paused so we don't spam the server with
// guaranteed-409 writes.
function _showPlanConflictToast(albumId) {
  // Prefer the toast helper if it's available (it's exported on
  // `window.toast` from app/static/main.js). Fall back to console so the
  // unit test (which stubs the DOM out) can still observe the call.
  const msg = 'Another tab saved newer edits — reload?';
  if (typeof window !== 'undefined' && typeof window.toast === 'function') {
    window.toast(msg, 'err');
  }
  // Surface a Reload action next to the saved-Xs-ago label so the user
  // doesn't have to close-reopen the modal. The button restores the
  // server's plan and clears the conflict latch.
  const host = document.getElementById('we-saved');
  if (host) {
    host.hidden = false;
    host.textContent = msg + ' ';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'we-reload-plan';
    btn.textContent = 'Reload';
    btn.onclick = () => {
      if (we.albumId !== albumId) return;
      we.planConflict = false;
      we.dirty = false;
      // weLoadExistingSplit reseeds we.planVersion + repopulates cuts/
      // titles/skipped from the server's newer plan.
      try { weLoadExistingSplit(albumId); } catch (e) { /* logged in fn */ }
    };
    host.appendChild(btn);
  }
}

// Persistent confirmation that the debounced auto-save landed. The
// indicator stays hidden until the first successful save, then shows a
// quiet "saved Xs ago" pill that ticks once every few seconds while the
// modal is open. A previous version of this flashed for ~1 s and hid
// again; if the user closed the modal between flashes they couldn't tell
// whether their work persisted.
let _savedAt = null;             // ms epoch of last successful save
let _savedTickTimer = null;      // shared interval that re-renders the label

function _renderSavedLabel() {
  const el = document.getElementById('we-saved');
  if (!el) return;
  if (_savedAt == null) { el.hidden = true; return; }
  const ageMs = Date.now() - _savedAt;
  const ageS  = Math.max(0, Math.floor(ageMs / 1000));
  let text;
  if (ageS < 5)         text = 'saved just now';
  else if (ageS < 10)   text = 'saved <10s ago';
  else if (ageS < 60)   text = `saved ${ageS}s ago`;
  else if (ageS < 3600) text = `saved ${Math.floor(ageS / 60)}m ago`;
  else                  text = `saved ${Math.floor(ageS / 3600)}h ago`;
  el.textContent = text;
  el.hidden = false;
}

function _markSavedNow() {
  _savedAt = Date.now();
  _renderSavedLabel();
  // Single shared interval — start it the first time we have a save to
  // display, and let _stopSavedTicker (called on modal close) clear it.
  if (_savedTickTimer == null) {
    _savedTickTimer = setInterval(_renderSavedLabel, 5000);
  }
}

function _stopSavedTicker() {
  if (_savedTickTimer != null) {
    clearInterval(_savedTickTimer);
    _savedTickTimer = null;
  }
  _savedAt = null;
  const el = document.getElementById('we-saved');
  if (el) { el.hidden = true; el.textContent = ''; }
  // Make sure the "Saving…" indicator isn't left hanging on close —
  // closing the modal mid-save could otherwise leave it visible the
  // next time the modal reopens (the DOM persists across opens).
  _hideSavingIndicator();
}

// Public hook: called on modal close so the final state is flushed even
// if the debounce timer hadn't fired yet.
async function _flushPlanSave() {
  if (_planSaveTimer) {
    clearTimeout(_planSaveTimer);
    _planSaveTimer = null;
    // If a save is still in flight, wait it out first so the coalesce
    // guard in _savePlanNow doesn't bounce us back onto setTimeout — we
    // need the flush to actually post the latest state before the modal
    // tears down.
    if (_planSaveInFlight) {
      try { await _planSaveInFlight; } catch (e) {}
    }
    await _savePlanNow();
  } else if (_planSaveInFlight) {
    try { await _planSaveInFlight; } catch (e) {}
  }
}

// ── Per-side audio playback ───────────────────────────────────────────────
// `weAudio` (multi-side `<audio>` wrapper) lives in
// `modules/audio-manager.js`, loaded as a classic script before this one
// so the binding is in script-scope. See that file for the rationale.

// ── Open / close ──────────────────────────────────────────────────────────
function openWaveEditor(fname) {
  const a = albumsByName[fname];
  if (!a) return;
  const total = a.duration_seconds || 0;
  // Initial state is the empty editor with one default track. Any prior
  // draft / completed split is fetched server-side via weLoadExistingSplit
  // (kicked off below) and patched in once the response arrives.
  Object.assign(we, {
    albumId:     fname,
    total:       total,
    viewStart:   0,
    viewEnd:     total,
    cuts:        [],
    titles:      ['Track 1'],
    skipped:     [false],
    positions:   [''],
    silences:    [],   // re-detected on demand via the suggest panel
    candidates:  [],
    hoverX:      null,
    isPlaying:   false,
    playingTrack: null,
    playingEnd:  null,
    measured:    null,
    approxPeakDb: null,
    peaks:       null,
    sides:       [],   // populated below from the album manifest
    // Flips true once weLoadExistingSplit resolves. _savePlanNow gates on
    // this so the empty default state never races ahead of the load.
    loaded:      false,
    // Reset on every open. weLoadExistingSplit re-populates cuts/titles/
    // etc. from the manifest without going through the user-edit call
    // sites, so it must NOT flip dirty. Only actual user input does.
    dirty:       false,
    // Reset the optimistic-concurrency state per-open. weLoadExistingSplit
    // seeds planVersion from the server; until it resolves we use 0,
    // which only matches a brand-new album anyway.
    planVersion: 0,
    planConflict: false,
  });
  resetMeasureUI();
  // Reset the encoder selectors to defaults on every open. weLoadExistingSplit
  // overrides these from the saved plan if one exists; otherwise the user
  // gets a clean FLAC/keep-source/keep-source slate without state bleeding
  // across albums.
  const fmtSelReset = document.getElementById('we-format');
  if (fmtSelReset) fmtSelReset.value = 'flac';
  const bdSelReset  = document.getElementById('we-bitdepth');
  if (bdSelReset)  bdSelReset.value = '0';
  const srSelReset  = document.getElementById('we-sample-rate');
  if (srSelReset)  srSelReset.value = '0';
  _weApplyFormatUI();
  // Reset the auto-save indicator. Each open starts hidden; the first
  // successful debounced save flips it to "saved just now".
  _stopSavedTicker();
  // Kick off peaks fetch in parallel with audio loading. When peaks land
  // we redraw the canvas + minimap and surface the approximate peak as
  // the album-stats readout — instant feedback while astats stays
  // expensive and on-demand.
  _loadAndDrawPeaks(fname);
  // The "filename" field on the editor used to literally be a FLAC name; in
  // the album-folder model `fname` is the opaque album_id. Show the album's
  // human label in the title bar instead.
  const headerLabel = [a.artist, a.album].filter(Boolean).join(' — ') || fname;
  document.getElementById('we-filename').textContent = headerLabel;
  document.getElementById('we-duration').textContent = fmtMMSS(we.total);
  document.getElementById('we-mini-end').textContent = fmtMMSS(we.total);
  document.getElementById('we-pop-silence').hidden = true;
  document.getElementById('we-search-status').textContent = '';
  document.getElementById('we-silence-status').textContent = '';

  // Per-side audio source: weAudio wraps `<audio>` and swaps `src` at side
  // boundaries while exposing album-time to the rest of the editor. Side
  // durations come from the album row's `sides[]` (populated server-side
  // in _summarize_album); fall back to a single-side stub if a stale
  // /api/albums response is missing it, so the editor still loads.
  const manifestSides = Array.isArray(a.sides) && a.sides.length
    ? a.sides
    : [{ filename: '', duration_seconds: total }];
  // Snapshot for the sides-reorder remap math. Each side row carries
  // {filename, duration_seconds}; we compute album-time offsets on the
  // fly when we need them.
  we.sides = manifestSides.map(s => ({
    filename:         s.filename,
    duration_seconds: Number(s.duration_seconds) || 0,
  }));
  weAudio.onTimeUpdate = onAudioTimeUpdate;
  weAudio.onEnded      = () => stopPlayback();
  weAudio.init(fname, manifestSides);
  if (!we.total && weAudio.totalDuration() > 0) {
    we.total   = weAudio.totalDuration();
    we.viewEnd = we.total;
    document.getElementById('we-duration').textContent = fmtMMSS(we.total);
    document.getElementById('we-mini-end').textContent = fmtMMSS(we.total);
  }

  weRenderSides();
  drawAll();
  // Remember the activator so close can restore focus to it. See the same
  // pattern in main.js's openTag/closeTag.
  _weFocusReturn = document.activeElement;
  document.getElementById('we-modal').hidden = false;
  document.addEventListener('keydown', weKeyDown);
  // Move focus into the modal so screen readers announce its content.
  const firstFocusable = document.querySelector('#we-modal button, #we-modal input, #we-modal select');
  if (firstFocusable) firstFocusable.focus();
  // Repopulate the editor from the saved plan in album.json (if any). When
  // no plan exists yet, fall back to the album's saved Discogs / MB id so a
  // first-time open of an MB-tagged album auto-suggests a tracklist instead
  // of forcing the user to run the search by hand.
  weLoadExistingSplit(fname).then(() => _weAutoLoadFromIds(a));
}

// Repopulate cuts, titles, and skip flags from album.json.plan. Covers
// both an in-progress draft (saved by _savePlanNow) and a completed
// split (saved by /api/album/split). Leaves the empty default state in
// place when there's nothing in the manifest.
async function weLoadExistingSplit(fname) {
  try {
    const r = await fetch(`/api/album/${encodeURIComponent(fname)}/tracks`);
    if (!r.ok) return;
    const d = await r.json();
    if (we.albumId !== fname) return;  // editor moved on while we were waiting
    // Seed the optimistic-concurrency token from the server's current
    // plan_version. Set before the early-return below so even an
    // album with no plan tracks yet starts with the right baseline
    // version (legacy manifests report 0).
    if (typeof d.plan_version === 'number') {
      we.planVersion = d.plan_version;
    }
    // Clear any prior conflict latch — a fresh load means we're back in
    // sync with the server and the user's saved-Xs-ago indicator can
    // resume normal display.
    we.planConflict = false;
    const plan = d.plan;
    const ptracks = (plan && Array.isArray(plan.tracks)) ? plan.tracks : null;
    if (!ptracks || !ptracks.length) return;
    // N tracks → N-1 cuts (between regions). A 1-track plan is valid —
    // it's the editor's "whole album as one track" state with a chosen
    // title or skip flag. We still need to apply titles/skipped so a
    // single skip-marked track survives reopen.
    const cuts = [];
    let cursor = 0;
    for (let j = 0; j < ptracks.length - 1; j++) {
      cursor += ptracks[j].duration_seconds || 0;
      if (cursor > 0 && cursor < we.total) cuts.push(cursor);
    }
    we.cuts      = cuts;
    we.titles    = ptracks.map(t => t.title || '');
    we.skipped   = ptracks.map(t => !!t.skip);
    // Saved plans don't carry the original Discogs `position` — the editor
    // can re-apply a tracklist from the Discogs popover to repopulate them
    // when needed. Until then, no per-region sleeve labels.
    we.positions = ptracks.map(() => '');
    // Rehydrate the encoder selectors from the saved plan. Default `flac`
    // when an older plan predates the format selector, so the first reopen
    // of a legacy draft picks up "lossless" without changing anything.
    const fmtSel = document.getElementById('we-format');
    if (fmtSel) {
      fmtSel.value = plan.output_format || 'flac';
      // Reapply the disabled state on the bit-depth select. Use the pure-UI
      // helper so the load path doesn't flip we.dirty (which would trigger
      // a redundant plan-save echoing back what we just read).
      _weApplyFormatUI();
    }
    const bdSel = document.getElementById('we-bitdepth');
    if (bdSel && plan.bit_depth != null) bdSel.value = String(plan.bit_depth);
    const srSel = document.getElementById('we-sample-rate');
    if (srSel && plan.sample_rate != null) srSel.value = String(plan.sample_rate);
    drawAll();
  } catch (e) { /* nothing existing — leave the empty state */ }
  finally {
    // Always flip loaded — _savePlanNow's race guard releases either way.
    if (we.albumId === fname) we.loaded = true;
  }
}

let _weFocusReturn = null;

function closeWaveEditor() {
  stopPlayback();
  weAudio.release();
  document.getElementById('we-modal').hidden = true;
  document.removeEventListener('keydown', weKeyDown);
  _hidePeaksOverlay();
  // Stop the shared "saved Xs ago" interval and hide the pill so the next
  // open starts fresh — no stale "saved 12m ago" carrying over from a
  // different album.
  _stopSavedTicker();
  // Flush any debounced plan-save in flight so a fast-close doesn't lose
  // the user's last edit. Runs in the background; the modal is already
  // hidden so the user isn't waiting on the network.
  _flushPlanSave();
  if (_weFocusReturn && typeof _weFocusReturn.focus === 'function') {
    try { _weFocusReturn.focus(); } catch (e) { /* element gone */ }
  }
  _weFocusReturn = null;
}

// Re-render everything that depends on viewStart/viewEnd or cuts.
function drawAll() {
  redrawWaveform();
  renderWaveformOverlay();
  renderMinimapOverlay();
  renderTracks();
  document.getElementById('we-t0').textContent = fmtMMSS(we.viewStart);
  document.getElementById('we-t1').textContent = fmtMMSS(we.viewEnd);
}

// ── Waveform canvas ───────────────────────────────────────────────────────
// Both the main waveform and the minimap render from the parsed .peaks.dat
// (loaded once per album open). Zoom/pan is local — sub-millisecond canvas
// redraw, no network round-trip.
function redrawWaveform() {
  const canvas = document.getElementById('we-canvas');
  const mini   = document.getElementById('we-minimap-canvas');
  if (canvas) drawPeaks(canvas, we.peaks, we.viewStart, we.viewEnd, '#6db3ff');
  if (mini)   drawPeaks(mini,   we.peaks, 0,             we.total,   '#6db3ff');
}

// Show the loading overlay during the (occasional) cold-fetch of /peaks
// when the album's concat cache + dat aren't yet built. Cached fetches
// return in milliseconds and the overlay never appears.
let _peaksOverlayTimer = null;
function _showPeaksOverlay() {
  const ov = document.getElementById('we-wf-overlay');
  if (!ov) return;
  if (_peaksOverlayTimer) clearTimeout(_peaksOverlayTimer);
  _peaksOverlayTimer = setTimeout(() => { ov.hidden = false; }, 200);
}
function _hidePeaksOverlay() {
  if (_peaksOverlayTimer) { clearTimeout(_peaksOverlayTimer); _peaksOverlayTimer = null; }
  const ov = document.getElementById('we-wf-overlay');
  if (ov) ov.hidden = true;
}

async function _loadAndDrawPeaks(albumId) {
  _showPeaksOverlay();
  const a = albumsByName[albumId];
  const manifestSides = a && Array.isArray(a.sides) && a.sides.length
    ? a.sides
    : null;
  if (!manifestSides) {
    _hidePeaksOverlay();
    const text = document.getElementById('we-stats-text');
    if (text) text.textContent = 'waveform unavailable: album manifest missing sides';
    return;
  }
  try {
    const peaks = await loadAlbumPeaks(albumId, manifestSides);
    if (we.albumId !== albumId) return;  // editor moved on while we were waiting
    we.peaks = peaks;
    if (!we.total && peaks.total > 0) {
      we.total   = peaks.total;
      we.viewEnd = peaks.total;
      document.getElementById('we-duration').textContent = fmtMMSS(we.total);
      document.getElementById('we-mini-end').textContent = fmtMMSS(we.total);
    }
    we.approxPeakDb = approxPeakDbFromPeaks(peaks);
    _renderApproxStats();
    drawAll();
  } catch (e) {
    const text = document.getElementById('we-stats-text');
    if (text) text.textContent = 'waveform unavailable: ' + e.message;
  } finally {
    _hidePeaksOverlay();
  }
}

// Stats readout: fall back to the .dat-derived peak (with `~` prefix to
// flag it as approximate) until astats has run. After /measure returns
// the exact number, formatMeasured replaces the ~-prefixed line.
function _renderApproxStats() {
  if (we.measured && we.measured.peak_db != null) return;  // measured wins
  const text = document.getElementById('we-stats-text');
  if (!text) return;
  if (we.approxPeakDb == null) {
    text.textContent = _sourceFormatPrefix() + 'click measure to compute peak + noise floor';
    return;
  }
  text.textContent = _sourceFormatPrefix()
    + `peak ~${we.approxPeakDb.toFixed(1)} dB · click measure for noise floor`;
}

// ── Sides reorder ─────────────────────────────────────────────────────────
// Most albums are 2-side; a handful go to 4-6 (double LP, etc.). Render a
// compact pill list above the minimap so reordering is one click away
// without crowding the canvas. Single-side albums get an empty hidden
// container — no point showing "drag to reorder" with one row.
function weRenderSides() {
  const host = document.getElementById('we-sides');
  if (!host) return;
  const sides = we.sides || [];
  if (sides.length < 2) { host.hidden = true; host.innerHTML = ''; return; }
  host.hidden = false;
  const rows = sides.map((s, i) => {
    const isFirst = i === 0, isLast = i === sides.length - 1;
    const dur = fmtMMSS(s.duration_seconds || 0);
    return `
      <div class="we-side-row" draggable="true" data-i="${i}"
           ondragstart="weSidesDragStart(event, ${i})"
           ondragover="weSidesDragOver(event, ${i})"
           ondragleave="weSidesDragLeave(event)"
           ondrop="weSidesDrop(event, ${i})"
           ondragend="weSidesDragEnd(event)">
        <span class="drag-handle" title="Drag to reorder">≡</span>
        <span class="num">${i + 1}.</span>
        <span class="name" title="${htmlEscape(s.filename)}">${htmlEscape(s.filename)}</span>
        <span class="dur">${dur}</span>
        <span class="arrows">
          <button class="arrow-btn" onclick="weMoveSide(${i}, -1)" ${isFirst ? 'disabled' : ''} title="Move up">▲</button>
          <button class="arrow-btn" onclick="weMoveSide(${i},  1)" ${isLast  ? 'disabled' : ''} title="Move down">▼</button>
        </span>
      </div>`;
  }).join('');
  host.innerHTML = `<span class="we-sides-label">Sides</span>` + rows;
}

let _weSidesDragFrom = null;
function weSidesDragStart(e, i) {
  _weSidesDragFrom = i;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(i));
  e.currentTarget.classList.add('dragging');
}
function weSidesDragOver(e, i) {
  if (_weSidesDragFrom == null || _weSidesDragFrom === i) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drag-over');
}
function weSidesDragLeave(e) { e.currentTarget.classList.remove('drag-over'); }
function weSidesDrop(e, j) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  const i = _weSidesDragFrom;
  _weSidesDragFrom = null;
  if (i == null || i === j) return;
  // Build the permutation: move sides[i] into the j slot, shifting others.
  const order = we.sides.map((_, idx) => idx);
  const moved = order.splice(i, 1)[0];
  order.splice(j, 0, moved);
  weReorderSides(order);
}
function weSidesDragEnd(e) {
  _weSidesDragFrom = null;
  e.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.we-side-row.drag-over').forEach(r =>
    r.classList.remove('drag-over'));
}

// Arrow-button swap: shorthand for "swap with my immediate neighbor".
function weMoveSide(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= we.sides.length) return;
  const order = we.sides.map((_, idx) => idx);
  [order[i], order[j]] = [order[j], order[i]];
  weReorderSides(order);
}

// Apply a permutation `newOrder[k] = oldIndex` to the editor state:
//   1. Remap album-time cuts so each one keeps its position WITHIN its
//      original side (track A's silences stay attached to track A).
//   2. POST /sides/reorder so the manifest is durable.
//   3. Refetch peaks (per-side dats are already cached server-side; the
//      `loadAlbumPeaks` call goes by index so it picks up the new order).
//   4. Re-init weAudio so playback follows the new order, redraw, and
//      flush the plan to album.json.
async function weReorderSides(newOrder) {
  const albumId = we.albumId;
  if (!albumId) return;
  // Identity permutation = no-op. Skip the round-trip.
  const isIdentity = newOrder.every((v, i) => v === i);
  if (isIdentity) return;
  const oldSides = we.sides.slice();
  const newSides = newOrder.map(idx => oldSides[idx]);
  const remapped = _weRemapForSides(oldSides, newSides, {
    cuts: we.cuts, titles: we.titles, skipped: we.skipped, total: we.total,
  });
  // Optimistic: apply locally first so the UI feels instant; on POST
  // failure we revert. The server is the source of truth for the manifest.
  we.sides   = newSides;
  we.cuts    = remapped.cuts;
  we.titles  = remapped.titles;
  we.skipped = remapped.skipped;
  we.dirty   = true;
  // Stop any in-flight playback so it doesn't keep playing the old
  // virtual-album-time position into the new layout's audio.
  stopPlayback();
  // Update the global album cache so a re-open uses the new order too.
  const cached = albumsByName[albumId];
  if (cached) cached.sides = newSides.map(s => ({ ...s }));
  // Repaint immediately so the user sees the new order.
  weRenderSides();
  invalidateMeasure();
  drawAll();
  try {
    const r = await fetch(`/api/album/${encodeURIComponent(albumId)}/sides/reorder`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ sides: newSides.map(s => s.filename) }),
    });
    if (!r.ok) throw new Error(await parseError(r));
  } catch (e) {
    // Revert local state.
    we.sides   = oldSides;
    we.cuts    = remapped.oldCuts;
    we.titles  = remapped.oldTitles;
    we.skipped = remapped.oldSkipped;
    if (cached) cached.sides = oldSides.map(s => ({ ...s }));
    weRenderSides();
    drawAll();
    toast('✗ reorder failed: ' + e.message, 'err');
    return;
  }
  // Refetch peaks for the new order. Per-side .peaks.dat is keyed by
  // filename stem on the server, so the data lands fast (cache hit) and
  // loadAlbumPeaks just re-stitches the album-time view.
  try {
    const peaks = await loadAlbumPeaks(albumId, newSides);
    if (we.albumId === albumId) {
      we.peaks = peaks;
      we.approxPeakDb = approxPeakDbFromPeaks(peaks);
      _renderApproxStats();
      drawAll();
    }
  } catch (e) { /* leave the existing render in place */ }
  // Re-init audio against the new side order so playback boundaries
  // match the new manifest.
  weAudio.init(albumId, newSides);
  _persistDraft();
}

// `_weRemapForSides` lives in `modules/timeline-state.js`; the side-
// reorder caller above resolves it via the script-scope binding that the
// module exposes on `window`.

// ── Per-region sleeve-style labels (A1, A2, B1, …) ───────────────────────
// `_weDerivedPositions`, `_weEffectivePositions`, `_weSideBounds`, and
// `_weCutGroupSpan` are extracted to `modules/timeline-state.js` (loaded
// as a classic script before this one). They read `we` through
// `window.we`, which is set by the inline bridge in `index.html` right
// after this file loads.

// ── Minimap viewport rect + cut markers ───────────────────────────────────
function renderMinimapOverlay() {
  const wrap = document.getElementById('we-minimap-wrap');
  const W = wrap.getBoundingClientRect().width;
  const vp = document.getElementById('we-minimap-vp');
  const lo = _timeToPctFull(we.viewStart);
  const hi = _timeToPctFull(we.viewEnd);
  vp.style.left  = lo + '%';
  vp.style.width = (hi - lo) + '%';

  const host = document.getElementById('we-minimap-cuts');
  const boundaries = [0, ...we.cuts, we.total];
  const skipBands = boundaries.slice(0, -1).map((start, i) => {
    if (!we.skipped[i]) return '';
    const w = ((boundaries[i + 1] - start) / Math.max(0.0001, we.total)) * 100;
    return `<div class="wave-skip" style="left:${(start / we.total) * 100}%;width:${w}%"></div>`;
  }).join('');
  // Recording-based side-switch ticks (dashed blue). Match the main-waveform
  // markers so the minimap and waveform agree at a glance.
  const sides = we.sides || [];
  let sacc = 0;
  const sideMarks = sides.length >= 2
    ? sides.slice(0, -1).map((s, i) => {
        sacc += Number(s.duration_seconds) || 0;
        return `<div class="ms" style="left:${_timeToPctFull(sacc)}%" title="Side ${i + 2}"></div>`;
      }).join('')
    : '';
  const cutMarks = we.cuts.map(t =>
    `<div class="mc" style="left:${_timeToPctFull(t)}%"></div>`
  ).join('');
  host.innerHTML = skipBands + sideMarks + cutMarks;
}

function weMinimapDown(e) {
  const wrap = document.getElementById('we-minimap-wrap');
  const r = wrap.getBoundingClientRect();
  const isOnVp = e.target.classList.contains('we-minimap-vp');
  const startX = e.clientX;
  const startView = { s: we.viewStart, e: we.viewEnd };
  if (!isOnVp) {
    // Click on bare minimap = recenter view on click.
    const t = ((e.clientX - r.left) / r.width) * we.total;
    const halfLen = (we.viewEnd - we.viewStart) / 2;
    we.viewStart = Math.max(0, t - halfLen);
    we.viewEnd   = Math.min(we.total, we.viewStart + halfLen * 2);
    we.viewStart = Math.max(0, we.viewEnd - halfLen * 2);
    drawAll();
    return;
  }
  // Drag the viewport rectangle = pan.
  e.preventDefault();
  const len = startView.e - startView.s;
  const onMove = ev => {
    const dx = ev.clientX - startX;
    const dt = (dx / r.width) * we.total;
    we.viewStart = Math.max(0, Math.min(we.total - len, startView.s + dt));
    we.viewEnd   = we.viewStart + len;
    drawAll();
  };
  const onUp = () => {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup',   onUp);
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup',   onUp);
}

// ── Zoom (wheel) ──────────────────────────────────────────────────────────
function weWheel(e) {
  e.preventDefault();
  const r = _wrap().getBoundingClientRect();
  const x = e.clientX - r.left;
  const tCenter = _xToTime(x);
  const factor = e.deltaY > 0 ? 1.25 : 0.8;        // wheel down = zoom out
  const len    = _viewLen() * factor;
  // Each .peaks.dat bucket is 256 samples ≈ 2.7 ms at 96 kHz. A 0.5 s
  // window across 2400 px gives ~13 px/bucket — visible stair-stepping
  // but cut placement (millisecond precision) is unaffected.
  const minLen = 0.5;
  const newLen = Math.max(minLen, Math.min(we.total, len));
  // Keep the time under the cursor pinned to the same x as we zoom.
  const frac = x / r.width;
  let s = tCenter - frac * newLen;
  let e2 = s + newLen;
  if (s < 0)        { s = 0; e2 = newLen; }
  if (e2 > we.total){ e2 = we.total; s = we.total - newLen; }
  we.viewStart = Math.max(0, s);
  we.viewEnd   = Math.min(we.total, e2);
  drawAll();
}

// ── Cut handles + hover/click on main waveform ────────────────────────────
function weHoverMove(e) {
  const r = _wrap().getBoundingClientRect();
  we.hoverX = e.clientX - r.left;
  if (we.dragging?.kind === 'cut') {
    // Clamp to the (lo, hi) window snapshotted at drag start. Because the
    // cut can't cross a neighbour, we.cuts stays sorted and stays aligned
    // with we.titles / we.skipped / we.positions — no re-sort needed.
    const { i, lo, hi } = we.dragging;
    we.cuts[i] = Math.max(lo, Math.min(hi, _snapToSilence(_xToTime(we.hoverX))));
    we.dirty = true;
    renderWaveformOverlay();
    renderMinimapOverlay();
    renderTracks();
    return;
  }
  if (we.dragging?.kind === 'cutGroup') {
    // Rigid translation of the grabbed cut and the same-side cuts after it
    // (indices i..last). Snap the leading cut to silence, then translate
    // the group by that same delta so relative spacing stays exact. Clamp
    // at both ends so the lead can't cross the cut before it or drop below
    // the side start, and the trailing cut of the group can't pass the
    // side end — the group stays within its raw recording region and
    // we.cuts stays sorted automatically.
    const { i, last, sideLo, sideHi, orig } = we.dragging;
    const tLead = _snapToSilence(_xToTime(we.hoverX));
    const minLead = Math.max(sideLo, i > 0 ? orig[i - 1] : 0);
    const maxLead = orig[i] + (sideHi - orig[last]);
    const newLead = Math.max(minLead, Math.min(maxLead, tLead));
    const delta = newLead - orig[i];
    for (let j = i; j <= last; j++) we.cuts[j] = orig[j] + delta;
    we.dirty = true;
    renderWaveformOverlay();
    renderMinimapOverlay();
    renderTracks();
    return;
  }
  document.getElementById('we-readout').textContent = fmtMMSS(_xToTime(we.hoverX));
}

// Snap a dragged cut to a nearby detected silence within 1s. Long silences
// (album side flips, fade-outs followed by long lead-in) need a cut at the
// start or the end, not the middle, so each silence contributes three snap
// candidates and the closest one wins.
function _snapToSilence(t) {
  if (!we.silences.length) return t;
  const SNAP = 1.0;
  let best = null, bestDist = SNAP;
  for (const s of we.silences) {
    for (const cand of [s.start, (s.start + s.end) / 2, s.end]) {
      const d = Math.abs(t - cand);
      if (d < bestDist) { best = cand; bestDist = d; }
    }
  }
  return best == null ? t : best;
}

function weHoverLeave() {
  we.hoverX = null;
  document.getElementById('we-readout').textContent = '';
}

// Click on bare waveform = seek playhead. Use the "+ add cut at playhead"
// button (or shift-click) for adding cuts. This separation matches Audacity.
function weAddCutAtClick(e) {
  if (we.dragging) return;                         // drop on drag-end, not a click
  const r = _wrap().getBoundingClientRect();
  const t = _xToTime(e.clientX - r.left);
  if (e.shiftKey) {
    weAddCutAtTime(t);
    return;
  }
  // Plain click = seek the audio.
  weAudio.seek(t);
  renderWaveformOverlay();
}

function weAddCutAtTime(t) {
  // Reject only literal duplicates. Tracks shorter than 0.5s are flagged as
  // "doesn't fit" by renderTracks(); the drag handler has no minimum-gap
  // check at all. A 0.5s deadband here silently blocked every insert in the
  // deepest-zoom window (which floors at 0.5s) — see plan for full context.
  if (we.cuts.some(c => Math.abs(c - t) < 0.001)) return;
  we.cuts.push(t);
  we.cuts.sort((a, b) => a - b);
  // Keep titles + skipped aligned: insert at the right slot. New regions
  // inherit the skip flag of the region they were carved from so splitting a
  // skip in two doesn't surprise-export it.
  const idx = we.cuts.indexOf(t) + 1;
  const inheritSkip = !!we.skipped[idx - 1];
  we.titles.splice(idx, 0, `Track ${we.titles.length + 1}`);
  we.skipped.splice(idx, 0, inheritSkip);
  // Position labels come from Discogs metadata and don't apply to manual
  // splits — leave the new region's slot blank so the handle/list don't show
  // a misleading sleeve label.
  we.positions.splice(idx, 0, '');
  we.dirty = true;
  invalidateMeasure();
  renderWaveformOverlay();
  renderMinimapOverlay();
  renderTracks();
}

function weAddCutAtPlayhead() {
  const t = weAudio.currentTime || _xToTime(we.hoverX ?? _wrapW() / 2);
  weAddCutAtTime(t);
}

function weClearCuts() {
  if (!we.cuts.length) return;
  if (!confirm(`Clear all ${we.cuts.length} cuts?`)) return;
  we.cuts = [];
  we.titles = ['Track 1'];
  we.skipped = [false];
  we.positions = [''];
  we.dirty = true;
  invalidateMeasure();
  renderWaveformOverlay();
  renderMinimapOverlay();
  renderTracks();
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────
// Active only while the wave editor modal is open. Skips when typing into a
// track-list input so titles + start times remain editable.
function _nearestCutIndex(t) {
  if (!we.cuts.length) return -1;
  let best = 0, dist = Math.abs(we.cuts[0] - t);
  for (let i = 1; i < we.cuts.length; i++) {
    const d = Math.abs(we.cuts[i] - t);
    if (d < dist) { best = i; dist = d; }
  }
  return best;
}

function weKeyDown(e) {
  // Tab cycles inside the open modal so AT / keyboard users can't lose
  // context to the underlying library page. trapModalFocus is defined in
  // main.js and shared with the tag/pi-deploy modals.
  if (e.key === 'Tab' && typeof trapModalFocus === 'function') {
    const m = document.getElementById('we-modal');
    if (m && !m.hidden) trapModalFocus(m, e);
    // Tab inside text inputs is otherwise unaffected; the trap only
    // intervenes at wrap-around boundaries.
  }
  const tag = (e.target.tagName || '').toUpperCase();
  if (tag === 'INPUT' || tag === 'TEXTAREA') {
    if (e.key === 'Escape') { e.target.blur(); }
    return;
  }
  const t = weAudio.currentTime || 0;
  switch (e.key) {
    case ' ':
      e.preventDefault();
      weTogglePlay();
      return;
    case 'Escape': {
      e.preventDefault();
      // Dismiss an open suggest popover first; only close the whole editor
      // once nothing is layered on top.
      const popSilence = document.getElementById('we-pop-silence');
      if (popSilence && !popSilence.hidden) {
        popSilence.hidden = true;
        return;
      }
      closeWaveEditor();
      return;
    }
    case 'ArrowLeft':
    case 'ArrowRight': {
      if (!weAudio.hasSrc) return;
      e.preventDefault();
      const step = (e.shiftKey ? 1.0 : 0.1) * (e.key === 'ArrowLeft' ? -1 : 1);
      weAudio.seek(Math.max(0, Math.min(we.total, t + step)));
      renderWaveformOverlay();
      return;
    }
    case 'j':
    case 'k': {
      if (!we.cuts.length || !weAudio.hasSrc) return;
      e.preventDefault();
      const sorted = we.cuts.slice().sort((a, b) => a - b);
      const target = e.key === 'j'
        ? [...sorted].reverse().find(c => c < t - 0.05)
        : sorted.find(c => c > t + 0.05);
      if (target != null) {
        weAudio.seek(target);
        renderWaveformOverlay();
      }
      return;
    }
    case 'Delete':
    case 'Backspace': {
      if (!we.cuts.length) return;
      e.preventDefault();
      const i = _nearestCutIndex(t);
      weDeleteCut(i);
      return;
    }
    case 'p':
    case 'P': {
      // Audition the nearest cut (play around the boundary, then auto-stop).
      e.preventDefault();
      wePreviewCut();
      return;
    }
    case 's':
    case 'S': {
      // Toggle skip on the region containing the playhead.
      e.preventDefault();
      const boundaries = [0, ...we.cuts, we.total];
      let region = 0;
      for (let i = 0; i < boundaries.length - 1; i++) {
        if (t >= boundaries[i] && t < boundaries[i + 1]) { region = i; break; }
      }
      weToggleSkip(region);
      return;
    }
  }
}

function weStartDrag(i, e) {
  e.preventDefault();
  e.stopPropagation();
  // Shift+drag rigidly translates this cut and every cut after it. Useful
  // when one early cut is off by a constant and the rest of the album has
  // shifted in lockstep — common after a re-detect or a side-flip nudge.
  // Snapshot the original cut positions so each mousemove computes the
  // delta from the start, not from the previous frame (otherwise snap
  // jitter on the lead would compound into drift on the tail).
  if (e.shiftKey) {
    // Shift+drag rigidly translates this cut and the cuts after it — but
    // only the ones sharing its raw recording side (see _weCutGroupSpan).
    // Cuts on later sides stay put, and weHoverMove clamps the group so it
    // can't be pushed across the side boundary.
    we.dragging = { kind: 'cutGroup', ..._weCutGroupSpan(i), orig: we.cuts.slice() };
  } else {
    // Constrain a single-cut drag to the raw recording side it sits in and
    // to the gap between its neighbours — so a marker can't be dragged
    // across a side boundary, and can't cross a sibling cut (which would
    // desync the parallel title/skip/position arrays from we.cuts). The
    // neighbours and side bounds are stable for the whole drag because the
    // clamp guarantees this cut never leaves the (lo, hi) window.
    const [sideLo, sideHi] = _weSideBounds(we.cuts[i]);
    const lo = Math.max(sideLo, i > 0 ? we.cuts[i - 1] : 0);
    const hi = Math.min(sideHi, i < we.cuts.length - 1 ? we.cuts[i + 1] : we.total);
    we.dragging = { kind: 'cut', i, lo, hi };
  }
  const onMove = ev => weHoverMove(ev);
  const onUp   = () => {
    we.dragging = null;
    invalidateMeasure();   // cut moved → ranges changed → re-measure before normalize
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup',   onUp);
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup',   onUp);
}

function weDeleteCut(i, e) {
  if (e) { e.preventDefault(); e.stopPropagation(); }
  we.cuts.splice(i, 1);
  // Drop the title + skip flag + position for the boundary that just
  // disappeared. The surviving region keeps its own label; the trailing
  // region's metadata is the one that goes.
  we.titles.splice(i + 1, 1);
  we.skipped.splice(i + 1, 1);
  we.positions.splice(i + 1, 1);
  we.dirty = true;
  invalidateMeasure();
  renderWaveformOverlay();
  renderMinimapOverlay();
  renderTracks();
}

function renderWaveformOverlay() {
  const overlay = document.getElementById('we-overlay');
  overlay.querySelectorAll('.wave-cut, .wave-silence, .wave-skip, .wave-side-switch, .wave-playhead').forEach(el => el.remove());

  // Silence highlights (amber bands) from the last detection.
  for (const s of we.silences) {
    if (s.end <= we.viewStart || s.start >= we.viewEnd) continue;
    const a = ((Math.max(s.start, we.viewStart) - we.viewStart) / _viewLen()) * 100;
    const b = ((Math.min(s.end,   we.viewEnd)   - we.viewStart) / _viewLen()) * 100;
    const el = document.createElement('div');
    el.className = 'wave-silence';
    el.style.left  = a + '%';
    el.style.width = (b - a) + '%';
    overlay.appendChild(el);
  }

  // Skipped regions (grey diagonal hatch). Drawn on top of silence bands so
  // a committed skip dominates a merely-detected silence visually.
  const boundaries = [0, ...we.cuts, we.total];
  boundaries.slice(0, -1).forEach((start, i) => {
    if (!we.skipped[i]) return;
    const end = boundaries[i + 1];
    if (end <= we.viewStart || start >= we.viewEnd) return;
    const a = ((Math.max(start, we.viewStart) - we.viewStart) / _viewLen()) * 100;
    const b = ((Math.min(end,   we.viewEnd)   - we.viewStart) / _viewLen()) * 100;
    const el = document.createElement('div');
    el.className = 'wave-skip';
    el.style.left  = a + '%';
    el.style.width = (b - a) + '%';
    overlay.appendChild(el);
  });

  // Side-switch markers — dashed verticals at album-time side boundaries
  // inferred from the per-side recording durations. Vinyl flip gaps are
  // natural cut locations, so surfacing the boundary helps the user place a
  // split there.
  const sides = we.sides || [];
  if (sides.length >= 2) {
    let acc = 0;
    for (let i = 0; i < sides.length - 1; i++) {
      acc += Number(sides[i].duration_seconds) || 0;
      const pct = _timeToPctView(acc);
      if (pct == null) continue;
      const el = document.createElement('div');
      el.className = 'wave-side-switch';
      el.style.left = pct + '%';
      el.title = `Side ${i + 1} → Side ${i + 2} at ${fmtMMSS(acc)}`;
      const badge = document.createElement('div');
      badge.className = 'wave-side-badge';
      badge.textContent = `Side ${i + 2}`;
      el.appendChild(badge);
      overlay.appendChild(el);
    }
  }

  // Cut handles. The handle just before region i+1 gets that region's
  // sleeve-style position (A1, B2, …) as a small badge — either from a
  // Discogs tracklist when one is applied, or auto-derived from the side
  // layout + cut order on a manual split. The same string appears in the
  // track list, so the link between sleeve, waveform and list is unbroken.
  const effPositions = _weEffectivePositions();
  we.cuts.forEach((t, i) => {
    const pct = _timeToPctView(t);
    if (pct == null) return;
    const el = document.createElement('div');
    el.className = 'wave-cut';
    el.style.left = pct + '%';
    const pos = effPositions[i + 1] || '';
    el.title = pos
      ? `Cut at ${fmtMMSS(t)} — ${pos} starts here. Drag to nudge, shift-drag to also shift later cuts, right-click to delete.`
      : `Cut at ${fmtMMSS(t)} — drag to nudge, shift-drag to also shift later cuts, right-click to delete`;
    el.addEventListener('mousedown',   ev => weStartDrag(i, ev));
    el.addEventListener('contextmenu', ev => weDeleteCut(i, ev));
    // The grip doubles as the position chip: when the cut carries a
    // sleeve position the label sits inside the red handle square itself
    // (.grip-labeled), otherwise it's the plain small square.
    const grip = document.createElement('div');
    grip.className = pos ? 'grip grip-labeled' : 'grip';
    if (pos) grip.textContent = pos;
    el.appendChild(grip);
    overlay.appendChild(el);
  });

  // Playhead.
  if (weAudio.hasSrc) {
    const t = weAudio.currentTime;
    if (!isNaN(t)) {
      const pct = _timeToPctView(t);
      if (pct != null) {
        const ph = document.createElement('div');
        ph.className = 'wave-playhead';
        ph.style.left = pct + '%';
        overlay.appendChild(ph);
      }
    }
  }
}

// ── Track list ────────────────────────────────────────────────────────────
function renderTracks() {
  const host = document.getElementById('we-tracks');
  const boundaries = [0, ...we.cuts, we.total];
  const need = Math.max(1, boundaries.length - 1);
  while (we.titles.length  < need) we.titles.push(`Track ${we.titles.length + 1}`);
  while (we.skipped.length < need) we.skipped.push(false);
  we.titles.length  = need;
  we.skipped.length = need;

  // Re-flow output numbers over non-skipped regions so what the user sees
  // matches the TRACKNUMBER tag the backend will write. Zero-length regions
  // are Discogs tracks that didn't fit the recording — they're displayed for
  // visibility but excluded from the output numbering and not exported.
  let outNum = 0;
  let exportable = 0;
  // The `pn` column shows the region's sleeve-style position (A1, B2, …)
  // when available — either from a Discogs tracklist or auto-derived from
  // the side layout + cut order on a manual split. Sequential numbers
  // remain the fallback for single-side records and the no-cuts case.
  const effPositions  = _weEffectivePositions();
  const havePositions = effPositions.some(p => p);
  host.innerHTML = boundaries.slice(0, -1).map((start, i) => {
    const end = boundaries[i + 1];
    const isFirst = i === 0;
    const skipped = !!we.skipped[i];
    const unfit   = (end - start) < 0.5;
    if (!skipped && !unfit) { outNum += 1; exportable += 1; }
    const playing = we.playingTrack === i ? 'playing' : '';
    const pos = effPositions[i] || '';
    const num = (skipped || unfit)
      ? '—'
      : (havePositions && pos ? pos : `${outNum}.`);
    const titleVal = skipped ? 'skip — not exported' : (we.titles[i] || '');
    const titleAttrs = (skipped || unfit)
      ? 'disabled'
      : `oninput="weSetTitle(${i}, this.value)"`;
    const rangeText = unfit ? "doesn't fit" : fmtMMSS(end - start);
    const rangeTitle = unfit ? 'Track from Discogs is longer than the recording — not exported' : '';
    const rowClass = ['wave-track'];
    if (skipped) rowClass.push('skip');
    if (unfit)   rowClass.push('unfit');
    // Per-region context for AT — include the track number so the
    // announcement makes sense out of order ("Play track 3" vs "Play").
    const ctx = (we.titles[i] && we.titles[i].trim()) ? `${num}: ${we.titles[i]}` : `${num}`;
    return `
      <div class="${rowClass.join(' ')}">
        <span class="pn">${num}</span>
        <button class="play-track ${playing}" onclick="wePlayTrack(${i})" title="Play this region" aria-label="${htmlEscape('Play track ' + ctx)}" ${unfit ? 'disabled' : ''}>▶</button>
        <input type="text" value="${htmlEscape(titleVal)}" ${titleAttrs} aria-label="${htmlEscape('Title for track ' + num)}">
        <input type="text" class="start-input" value="${fmtMMSS(start)}" placeholder="m:ss.ss"
               ${isFirst || unfit ? 'disabled' : ''}
               aria-label="${htmlEscape('Start time of track ' + num)}"
               onchange="weSetCutAt(${i}, parseMMSS(this.value))">
        <span class="range" title="${rangeTitle}">${rangeText}</span>
        <button class="skip-btn ${skipped ? 'on' : ''}"
                title="${skipped ? 'Restore region as a track (S toggles the region at the playhead)' : 'Skip — drop region from output and measurement (S toggles the region at the playhead)'}"
                aria-label="${htmlEscape((skipped ? 'Restore track ' : 'Skip track ') + ctx)}"
                onclick="weToggleSkip(${i})" ${unfit ? 'disabled' : ''}>⊘</button>
        <button class="del" title="Remove cut (Del removes the cut nearest the playhead)" aria-label="${htmlEscape('Remove cut before track ' + ctx)}" ${isFirst ? 'disabled' : ''}
                onclick="weDeleteCut(${i - 1})">✕</button>
      </div>`;
  }).join('');
  document.getElementById('we-go').disabled = exportable === 0;
  _persistDraft();
}

function weSetTitle(i, v) {
  we.titles[i] = v;
  we.dirty = true;
  _persistDraft();
}

function weToggleSkip(i) {
  if (i < 0 || i >= we.skipped.length) return;
  we.skipped[i] = !we.skipped[i];
  we.dirty = true;
  invalidateMeasure();
  renderWaveformOverlay();
  renderMinimapOverlay();
  renderTracks();
}

function weSetCutAt(i, seconds) {
  if (i <= 0) return;
  const cutIdx = i - 1;
  if (cutIdx < 0 || cutIdx >= we.cuts.length) return;
  we.cuts[cutIdx] = Math.max(0, Math.min(we.total, seconds));
  we.cuts.sort((a, b) => a - b);
  we.dirty = true;
  invalidateMeasure();
  renderWaveformOverlay();
  renderMinimapOverlay();
  renderTracks();
}

// ── Audio playback ────────────────────────────────────────────────────────
function weTogglePlay() {
  if (!weAudio.hasSrc) return;
  if (weAudio.paused) {
    we.playingTrack = null;
    we.playingEnd   = null;
    if (!_jumpOverSkippedFromHere()) return;
    weAudio.play();
    document.getElementById('we-play').textContent = '⏸';
    we.isPlaying = true;
  } else {
    weAudio.pause();
    document.getElementById('we-play').textContent = '▶';
    we.isPlaying = false;
  }
}

// During free play, if the playhead is inside a skipped region, jump to the
// start of the next non-skipped region. Returns false (and stops playback)
// when no non-skipped region remains; true otherwise.
function _jumpOverSkippedFromHere() {
  if (!weAudio.hasSrc || we.playingEnd != null) return true;
  const boundaries = [0, ...we.cuts, we.total];
  const t = weAudio.currentTime;
  let idx = -1;
  for (let i = 0; i < boundaries.length - 1; i++) {
    if (t >= boundaries[i] && t < boundaries[i + 1]) { idx = i; break; }
  }
  if (idx < 0 || !we.skipped[idx]) return true;
  let next = idx + 1;
  while (next < we.skipped.length && we.skipped[next]) next++;
  if (next < we.skipped.length) {
    weAudio.seek(boundaries[next]);
    return true;
  }
  stopPlayback();
  return false;
}

function wePlayTrack(i) {
  if (!weAudio.hasSrc) return;
  const boundaries = [0, ...we.cuts, we.total];
  const start = boundaries[i];
  const end   = boundaries[i + 1];
  if (end == null || end <= start) return;
  weAudio.seek(start);
  we.playingTrack = i;
  we.playingEnd   = end;
  weAudio.play();
  document.getElementById('we-play').textContent = '⏸';
  we.isPlaying = true;
  renderTracks();
}

// Audition the cut nearest the playhead: play a couple seconds before to a
// couple after, then auto-stop. Reuses the `playingEnd` watcher (same
// mechanism as wePlayTrack), so no extra timer — and because playingEnd is
// set, _jumpOverSkippedFromHere is bypassed, so the boundary plays through
// even when an adjacent region is marked skip (which is what you want when
// checking the cut).
function wePreviewCut() {
  if (!we.cuts.length || !weAudio.hasSrc) return;
  const i = _nearestCutIndex(weAudio.currentTime || 0);
  const { start, end } = window._wePreviewWindow(we.cuts[i], we.total, 2, 2);
  if (end <= start) return;
  weAudio.seek(start);
  we.playingTrack = null;
  we.playingEnd   = end;
  weAudio.play();
  document.getElementById('we-play').textContent = '⏸';
  we.isPlaying = true;
  renderWaveformOverlay();
}

function stopPlayback() {
  weAudio.pause();
  we.isPlaying    = false;
  we.playingTrack = null;
  we.playingEnd   = null;
  const btn = document.getElementById('we-play');
  if (btn) btn.textContent = '▶';
  renderTracks();
}

function onAudioTimeUpdate() {
  const t = weAudio.currentTime;
  if (we.playingEnd != null && t >= we.playingEnd) {
    stopPlayback();
    return;
  }
  _jumpOverSkippedFromHere();
  if (we.isPlaying === false) return;
  document.getElementById('we-time').textContent = fmtMMSS(t);
  // Ensure the playhead stays inside the current view; auto-scroll if it leaves.
  if (t < we.viewStart || t > we.viewEnd) {
    const len = _viewLen();
    we.viewStart = Math.max(0, t - len * 0.1);
    we.viewEnd   = Math.min(we.total, we.viewStart + len);
    drawAll();
  } else {
    renderWaveformOverlay();   // playhead position only
  }
}

// ── Suggest popovers ──────────────────────────────────────────────────────
// The Discogs/MB tracklist search popover was retired in favor of the
// "↻ load tracklist" button (which pulls from the album's already-saved
// Discogs / MusicBrainz id). Only the silence popover is left.
function weToggleSuggest(which) {
  const b = document.getElementById('we-pop-silence');
  if (which === 'silence') b.hidden = !b.hidden;
}

// Manual re-trigger for the album's saved-id tracklist fetch. _weAutoLoadFromIds
// runs once on open; this button lets the user re-run it after re-tagging,
// or get a clear "no ids on this album" message when the album isn't tagged.
async function weLoadTracklistFromTags() {
  const status = document.getElementById('we-search-status');
  const a = (typeof albumsByName !== 'undefined') ? albumsByName[we.albumId] : null;
  if (!a) { status.textContent = 'No album loaded.'; return; }
  const hasDiscogs = !!a.discogs_release_id;
  const hasMbid    = !!a.musicbrainz_albumid;
  if (!hasDiscogs && !hasMbid) {
    status.textContent =
      'No Discogs / MusicBrainz id saved on this album — tag it first via "edit tags" in the library.';
    return;
  }
  // Temporarily clear cuts gate so _weAutoLoadFromIds re-runs. The function
  // bails early when we.cuts.length > 0, which is the right thing on open
  // but wrong when the user explicitly asked to reload. Snapshot + restore.
  const prevCuts = we.cuts.slice();
  we.cuts = [];
  status.textContent = hasDiscogs
    ? 'loading tracklist from saved Discogs id…'
    : 'loading tracklist from saved MusicBrainz id…';
  try {
    await _weAutoLoadFromIds(a);
  } finally {
    // If auto-load applied a tracklist, we.cuts is now populated with new
    // values and the snapshot is stale — keep the new cuts. Otherwise put
    // the prior cuts back so we don't blank the editor on a failure.
    if (!we.cuts.length) we.cuts = prevCuts;
  }
}

// The wave-editor used to host its own MB/Discogs search popover. Removed
// in favor of the "↻ load tracklist" button + the album's saved Discogs /
// MB id — see weLoadTracklistFromTags above. To re-tag an album mid-split,
// close the editor and use the library's "edit tags" action.

// Apply cumulative track durations as cut positions. `_wePosLetter` and
// `_weCutsFromTracklist` live in `modules/timeline-state.js`; the
// algorithm rationale (per-side anchoring, end-of-side gap region,
// overflow handling, …) lives next to those functions there.

// Window exports introduced by the plan_version conflict-detection work
// (#33). `_wePosLetter` and `_weCutsFromTracklist` are no longer re-exported
// here — they live (and re-export themselves) inside
// `modules/timeline-state.js` after the extraction.
if (typeof window !== 'undefined') {
  // Exposed for the Node-sandbox test that drives the 409 path.
  window._savePlanNow        = _savePlanNow;
  window._weEditorState      = we;
}

function _weApplyTracklist(track_details, sourceLabel) {
  const td = (track_details || []).filter(t => t && t.title);
  if (!td.length) {
    document.getElementById('we-search-status').textContent = 'no tracklist on this candidate';
    return false;
  }
  const r = _weCutsFromTracklist(td, we.sides || [], we.total);
  we.cuts      = r.cuts;
  we.titles    = r.titles;
  we.skipped   = r.skipped;
  we.positions = r.positions;
  invalidateMeasure();
  document.getElementById('we-search-status').textContent = r.overflow
    ? `${td.length} tracks · ${sourceLabel} · ${r.overflow} don't fit recording`
    : `${td.length} tracks · ${sourceLabel}`;
  drawAll();
  return true;
}

// wePickCandidate / wePickCollectionCandidate were the click handlers for
// the retired search popover's candidate rows. The auto-load path below
// (and weLoadTracklistFromTags) now covers every tracklist-fetch flow.

// Auto-load the tracklist from a Discogs release id or MBID stored on the
// file. Only called when the editor has no existing cuts (no draft, no
// previous split). The user can still run a manual search to override.
async function _weAutoLoadFromIds(a) {
  if (we.albumId !== a.album_id) return;     // editor moved on
  if (we.cuts.length) return;                  // draft / existing split won
  if (a.discogs_release_id) {
    try {
      const r = await fetch(`/api/release/discogs/${a.discogs_release_id}`);
      if (!r.ok) throw new Error(await parseError(r));
      const d = await r.json();
      if (we.albumId === a.album_id && !we.cuts.length) {
        _weApplyTracklist(d.track_details, 'auto-loaded from saved Discogs id');
      }
      return;
    } catch (e) { /* fall through to MB */ }
  }
  if (a.musicbrainz_albumid) {
    try {
      const r = await fetch(`/api/release/${a.musicbrainz_albumid}`);
      if (!r.ok) return;
      const d = await r.json();
      if (we.albumId === a.album_id && !we.cuts.length) {
        _weApplyTracklist(d.track_details, 'auto-loaded from saved MBID');
      }
    } catch (e) { /* nothing more to try */ }
  }
}

async function weDetectAndApply() {
  await weDetectInternal({ replace: true });
}
async function weDetectShowOnly() {
  await weDetectInternal({ replace: false });
}

async function weDetectInternal({ replace }) {
  // The slider holds an int8 threshold (1..127) matching .peaks.dat
  // resolution; the dB equivalent is computed for display only.
  const thresholdInt8 = parseInt(document.getElementById('we-noise').value, 10) || 8;
  const mindur  = parseFloat(document.getElementById('we-mindur').value)   || 1.5;
  const skipMin = parseFloat(document.getElementById('we-skiplong').value) || 15;
  const status  = document.getElementById('we-silence-status');
  const bar     = document.getElementById('we-silence-bar');
  status.textContent = 'detecting silences…';
  showBar(bar, 'scanning');
  try {
    const d = await withJobProgress(bar, async (jobId) => {
      const r = await fetch('/api/album/detect-silences', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          album_id: we.albumId, threshold_int8: thresholdInt8,
          min_silence: mindur, job_id: jobId,
        }),
      });
      if (!r.ok) throw new Error(await parseError(r));
      return r.json();
    });
    we.silences = (d.silences || []).slice().sort((a, b) => a.start - b.start);
    if (replace) {
      // Two cut placement rules:
      //   * Short silences (track gaps, 2–5s typical): single cut at the
      //     midpoint, becomes a track boundary.
      //   * Long silences (side flips, lead-in/out): pair of cuts at start
      //     and end, and the in-between region is auto-marked skip so
      //     needle-drop pops don't sneak into the output or measurement.
      const CUTOFF = 2.0;
      const skipRanges = [];   // [start, end] seconds for regions to skip
      const cuts = [];
      for (const s of we.silences) {
        if (s.duration >= skipMin) {
          cuts.push(s.start, s.end);
          skipRanges.push([s.start, s.end]);
        } else if (s.duration >= CUTOFF) {
          cuts.push((s.start + s.end) / 2);
        }
      }
      // Dedupe (silenceremove can report start at exactly 0) + clip to bounds.
      const unique = [...new Set(cuts.map(t => Math.round(t * 1000) / 1000))]
        .filter(t => t > 0.01 && t < we.total - 0.01)
        .sort((a, b) => a - b);
      we.cuts   = unique;
      we.titles = unique.map((_, i) => `Track ${i + 1}`).concat([`Track ${unique.length + 1}`]);
      const boundaries = [0, ...we.cuts, we.total];
      we.skipped = boundaries.slice(0, -1).map((start, i) => {
        const end = boundaries[i + 1];
        return skipRanges.some(([a, b]) =>
          Math.abs(a - start) < 0.5 && Math.abs(b - end) < 0.5);
      });
      invalidateMeasure();
      const skipped = we.skipped.filter(Boolean).length;
      status.textContent = `${we.silences.length} silences · ${we.cuts.length} cuts`
        + (skipped ? ` · ${skipped} auto-skipped (≥${skipMin}s)` : '');
    } else {
      status.textContent = `${we.silences.length} silences · highlighted (no cuts changed)`;
    }
    drawAll();
    if (typeof noteAlbumSuccess === 'function') noteAlbumSuccess(we.albumId);
  } catch (e) {
    status.textContent = 'detection failed: ' + e.message;
    if (typeof recordAlbumFailure === 'function') {
      recordAlbumFailure(we.albumId, 'silence-detect', e.message);
    }
  } finally {
    hideBar(bar);
  }
}

// Boundaries (start/end seconds) of every region, paired with its skip flag.
function _regions() {
  const b = [0, ...we.cuts, we.total];
  return b.slice(0, -1).map((start, i) => ({
    start, end: b[i + 1], skip: !!we.skipped[i], title: we.titles[i] || `Track ${i + 1}`,
  }));
}

// ── Output format change ─────────────────────────────────────────────────

// Disable the bit-depth select for lossy formats — encoders use their own
// internal precision; the value would be silently ignored. Greyed out so
// users can still see what they previously had selected. Pure UI mutation
// — no dirty/save side effects so the load path can call this safely.
function _weApplyFormatUI() {
  const fmt = document.getElementById('we-format')?.value || 'flac';
  const bd  = document.getElementById('we-bitdepth');
  const losslessFormats = ['flac', 'wav', 'm4a-alac'];
  const supportsBits = losslessFormats.includes(fmt);
  if (bd) {
    bd.disabled = !supportsBits;
    bd.title = supportsBits
      ? ''
      : 'Bit depth applies to lossless formats only (FLAC / WAV / ALAC).';
  }
}

// onchange handler wired up from the <select> in index.html. Updates the UI
// AND marks the editor dirty so the debounced plan-save fires.
function weOnFormatChange() {
  _weApplyFormatUI();
  we.dirty = true;
  _persistDraft();
}

// ── Apply ─────────────────────────────────────────────────────────────────
async function weApplySplit() {
  if (!we.albumId) return;
  // Drop zero-length regions — those are Discogs tracks that didn't fit the
  // recording, surfaced in the list as informational only. Sending them would
  // create empty FLACs and inflate the track count.
  const tracks = _regions()
    .filter(r => r.end - r.start >= 0.5)
    .map(r => ({
      title: r.title.trim(),
      duration_seconds: r.end - r.start,
      skip: r.skip,
    }));
  if (!tracks.length || tracks.every(t => t.skip)) return;
  const normalize = !!document.getElementById('we-normalize').checked;
  const bitDepth = parseInt(document.getElementById('we-bitdepth').value, 10) || 0;
  const sampleRate = parseInt(document.getElementById('we-sample-rate').value, 10) || 0;
  const outputFormat = document.getElementById('we-format')?.value || 'flac';
  if (normalize && (we.measured == null || we.measured.peak_db == null)) {
    // Either nothing measured yet, or skipping/cut changes invalidated it.
    await weMeasure();
    if (we.measured == null || we.measured.peak_db == null) {
      toast('✗ measurement failed — cannot normalize', 'err');
      return;
    }
  }
  const btn = document.getElementById('we-go');
  const bar = document.getElementById('we-split-bar');
  btn.disabled = true; btn.textContent = 'splitting…';
  showBar(bar, 'encoding tracks');
  try {
    const d = await withJobProgress(bar, async (jobId) => {
      const body = { album_id: we.albumId, tracks, bit_depth: bitDepth, sample_rate: sampleRate, output_format: outputFormat, job_id: jobId };
      if (normalize) {
        body.normalize         = true;
        body.target_peak_db    = we.targetPeakDb;
        body.measured_peak_db  = we.measured.peak_db;
      }
      const r = await fetch('/api/album/split', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await parseError(r));
      return r.json();
    });
    toast(`✓ Split into ${d.tracks.length} tracks`, 'ok');
    // Clear any prior session-failure pill on this album — the rerun worked.
    if (typeof noteAlbumSuccess === 'function') noteAlbumSuccess(we.albumId);
    // No draft to clear — the split route already wrote the same plan to
    // album.json (alongside `music_relpath`). Closing the editor leaves
    // the plan in place so re-edit picks up where this run left off.
    closeWaveEditor();
    refreshAlbums();
  } catch (e) {
    toast('✗ split failed: ' + e.message, 'err');
    // Persist the failure beyond the toast so the user can see at a glance
    // which album had a problem when scanning the library.
    if (typeof recordAlbumFailure === 'function') {
      recordAlbumFailure(we.albumId, 'split', e.message);
    }
  } finally {
    btn.disabled = false; btn.textContent = 'apply split';
    hideBar(bar);
  }
}

// ── Measure (peak / noise floor / dynamic range) ──────────────────────────
function resetMeasureUI() {
  we.measured = null;
  const el = document.getElementById('we-stats-text');
  if (el) el.textContent = 'loading waveform…';
  const btn = document.getElementById('we-measure-btn');
  if (btn) btn.disabled = false;  // measure is on-demand now (no auto-run on open)
}

// Mark cached measurement stale. We keep the approximate (.dat-derived)
// peak visible — accurate to ±0.05 dB at vinyl peaks — and the leading
// `~` flags it as a guess so the readout never lies to the user.
function invalidateMeasure() {
  if (we.measured == null) return;
  we.measured = null;
  const text = document.getElementById('we-stats-text');
  if (text && we.approxPeakDb != null) {
    text.textContent = _sourceFormatPrefix()
      + `peak ~${we.approxPeakDb.toFixed(1)} dB · cuts changed — re-measure`;
  } else if (text) {
    text.textContent = _sourceFormatPrefix() + 'cuts changed — re-measure';
  }
}

async function weMeasure() {
  if (!we.albumId) return;
  const text = document.getElementById('we-stats-text');
  const btn  = document.getElementById('we-measure-btn');
  const bar  = document.getElementById('we-measure-bar');
  if (text) text.textContent = 'measuring…';
  if (btn)  btn.disabled = true;
  showBar(bar, 'analysing');
  try {
    // Only measure regions that will actually be exported.
    const included = _regions()
      .filter(r => !r.skip && r.end > r.start)
      .map(r => [r.start, r.end]);
    // Treat a full-album include set the same as no filter — saves the
    // server building an atrim+concat chain for nothing.
    const allIncluded = included.length === 1
      && included[0][0] <= 0.01 && included[0][1] >= we.total - 0.5;
    const d = await withJobProgress(bar, async (jobId) => {
      const body = { album_id: we.albumId, job_id: jobId };
      if (!allIncluded && included.length) body.included_ranges = included;
      const r = await fetch('/api/album/measure', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await parseError(r));
      return r.json();
    });
    we.measured = d;
    if (text) text.textContent = formatMeasured(d);
    if (typeof noteAlbumSuccess === 'function') noteAlbumSuccess(we.albumId);
  } catch (e) {
    we.measured = null;
    if (text) text.textContent = 'measurement failed: ' + e.message;
    if (typeof recordAlbumFailure === 'function') {
      recordAlbumFailure(we.albumId, 'measure', e.message);
    }
  } finally {
    if (btn) btn.disabled = false;
    hideBar(bar);
  }
}

// Album-stats prefix: `source: 24b / 96 ksps · ` if format is known, else ''.
// Resolves to `source: mixed · ` when sides differ in bit depth or sample rate
// (handled by `fmtSourceFormat` walking `sides[]`).
function _sourceFormatPrefix() {
  if (!we.albumId) return '';
  const a = (typeof albumsByName !== 'undefined') ? albumsByName[we.albumId] : null;
  if (!a) return '';
  const s = (typeof fmtSourceFormat === 'function') ? fmtSourceFormat(a) : '';
  if (!s || s === '—') return '';
  return `source: ${s} · `;
}

function formatMeasured(d) {
  const fmt = v => (v == null || isNaN(v)) ? '—' : v.toFixed(1);
  const bits = d.effective_bits == null ? '—' : d.effective_bits.toFixed(1);
  return _sourceFormatPrefix()
       + `peak ${fmt(d.peak_db)} dB · noise floor ${fmt(d.noise_floor_db)} dB`
       + ` · DR ${fmt(d.dynamic_range_db)} dB (≈ ${bits} effective bits)`;
}

// ── Library row inline track expansion ────────────────────────────────────
async function toggleTracks(fname) {
  const row = document.querySelector(`tr[data-album-id='${fname}']`);
  if (!row) return;
  const next = row.nextElementSibling;
  if (next && next.classList.contains('tracks-sub')) { next.remove(); return; }
  try {
    const r = await fetch(`/api/album/${encodeURIComponent(fname)}/tracks`);
    const d = await r.json();
    const tracks = d.tracks || [];
    if (!tracks.length) return;
    const sub = document.createElement('tr');
    sub.className = 'tracks-sub';
    sub.innerHTML = `<td colspan="10">${tracks.map(t => {
      const key = `${fname}|${t.filename}`;
      const isPlaying = previewIs(key, 'track');
      const playClass = isPlaying ? 'playing' : '';
      const playGlyph = isPlaying ? '⏸' : '▶';
      // Carry the track title into the per-button aria-label so screen
      // readers announce something meaningful when navigated out of order.
      const trackTitle = t.title || t.filename;
      const previewAria = htmlEscape('Preview track ' + trackTitle);
      const downloadAria = htmlEscape('Download track ' + trackTitle);
      return `
      <div class="track-row" title="${htmlEscape(t.filename)}">
        <span class="tnum">${t.track_number || '—'}</span>
        <span class="ttitle">${htmlEscape(t.title || t.filename)}</span>
        <span class="tdur">${fmtDuration(t.duration_seconds)}</span>
        <span class="tsize">${t.size_mb} MB</span>
        <button class="icon-btn preview-btn ${playClass}" data-album-id="${htmlEscape(fname)}" data-track="${htmlEscape(t.filename)}" data-kind="track" title="Preview" aria-label="${previewAria}" onclick="togglePreviewTrack(this.dataset.albumId, this.dataset.track)">${playGlyph}</button>
        <a class="icon-btn" href="/api/album/${encodeURIComponent(fname)}/track/${encodeURIComponent(t.filename)}" download title="Download" aria-label="${downloadAria}">↓</a>
      </div>`;
    }).join('')}</td>`;
    row.insertAdjacentElement('afterend', sub);
  } catch (e) { console.error(e); }
}
