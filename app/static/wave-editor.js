// Unified album-split editor.
//
// State, viewport math, and waveform/minimap rendering. Cuts, tracks, audio
// playback, and the suggest popovers live in the lower halves of this file
// (kept in one module so the closures share state).

'use strict';

const we = {
  albumId:     null,         // album_id slug from `albumsByName`
  total:       0,            // album duration in seconds
  viewStart:   0,            // visible window in seconds
  viewEnd:     0,
  cuts:        [],           // sorted seconds
  titles:      ['Track 1'],  // length === cuts.length + 1
  skipped:     [false],      // length === cuts.length + 1; true = drop region from output
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
  // Snapshot the current editor state into the plan shape the server
  // already understands (see SplitRequest / PlanUpdateRequest).
  const tracks = _regions().map(r => ({
    title: (r.title || '').trim(),
    duration_seconds: Math.max(0, r.end - r.start),
    skip: !!r.skip,
  }));
  const albumId = we.albumId;
  try {
    _planSaveInFlight = fetch(`/api/album/${albumId}/plan`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ tracks }),
    });
    const r = await _planSaveInFlight;
    if (r && r.ok && we.albumId === albumId) {
      // Clear dirty BEFORE flashing — if the user edits again during the
      // flash, the next debounce cycle finds dirty=true and saves again.
      we.dirty = false;
      _flashSavedIndicator();
    }
  } catch (e) {
    // Network blip — the next change will retry. Silent on purpose; a
    // toast for every transient save failure would spam the user. Leave
    // dirty=true so the next interaction re-attempts.
  } finally {
    _planSaveInFlight = null;
  }
}

// Tiny visual confirmation that the debounced auto-save landed. The
// indicator is hidden by default; we add `.flash` for ~1 s and let CSS
// run the fade. Multiple rapid saves just re-trigger the same animation.
let _savedFlashTimer = null;
function _flashSavedIndicator() {
  const el = document.getElementById('we-saved');
  if (!el) return;
  el.hidden = false;
  // Force a reflow so re-adding the class restarts the CSS transition.
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
  if (_savedFlashTimer) clearTimeout(_savedFlashTimer);
  _savedFlashTimer = setTimeout(() => {
    el.classList.remove('flash');
    el.hidden = true;
    _savedFlashTimer = null;
  }, 1100);  // CSS transition is 1 s; add a small buffer.
}

// Public hook: called on modal close so the final state is flushed even
// if the debounce timer hadn't fired yet.
async function _flushPlanSave() {
  if (_planSaveTimer) {
    clearTimeout(_planSaveTimer);
    _planSaveTimer = null;
    await _savePlanNow();
  } else if (_planSaveInFlight) {
    try { await _planSaveInFlight; } catch (e) {}
  }
}

// ── Per-side audio playback ───────────────────────────────────────────────
// The editor presents the album as a single continuous timeline, but
// physically each side is its own FLAC. weAudio wraps the `<audio>` element
// so the rest of the editor can keep using album-time without caring about
// boundaries: seek() resolves album-time to the right side and swaps src
// when needed, currentTime returns album-time, and end-of-side advances
// to the next side automatically. The brief click at src swap lands inside
// the auto-detected silence bands at side flips, so it's invisible.
const weAudio = {
  albumId:        null,
  sides:          [],     // [{filename, duration_seconds, offset}, ...]
  currentSideIdx: 0,
  // Optional callback the editor wires up so onended can drive its
  // playingTrack / playingEnd state machine.
  onEnded:        null,
  onTimeUpdate:   null,

  _el() { return document.getElementById('we-audio'); },

  init(albumId, manifestSides) {
    // Manifest sides arrive without an `offset`; build the cumulative
    // album-time lookup once so seek() is O(log n) per call.
    let off = 0;
    this.albumId = albumId;
    this.sides = manifestSides.map(s => {
      const entry = {
        filename:         s.filename,
        duration_seconds: Number(s.duration_seconds) || 0,
        offset:           off,
      };
      off += entry.duration_seconds;
      return entry;
    });
    this.currentSideIdx = 0;
    const audio = this._el();
    if (!audio) return;
    audio.ontimeupdate = () => { if (this.onTimeUpdate) this.onTimeUpdate(); };
    audio.onended = () => this._onSideEnded();
    if (this.sides.length) {
      audio.src = this._sideUrl(0);
      audio.load();
    }
  },

  _sideUrl(idx) {
    return `/api/album/${encodeURIComponent(this.albumId)}/sides/${idx}/audio`;
  },

  _findSide(albumTime) {
    // Linear scan; albums have ≤ ~6 sides in practice and seek isn't hot.
    for (let i = 0; i < this.sides.length; i++) {
      const s = this.sides[i];
      if (albumTime < s.offset + s.duration_seconds) return i;
    }
    return Math.max(0, this.sides.length - 1);
  },

  _onSideEnded() {
    const audio = this._el();
    if (!audio) return;
    if (this.currentSideIdx < this.sides.length - 1) {
      // Advance to next side and continue playing without surfacing the
      // boundary to the editor's end-of-playback handler.
      this.currentSideIdx += 1;
      audio.src = this._sideUrl(this.currentSideIdx);
      audio.load();
      audio.play().catch(() => {});
    } else if (this.onEnded) {
      this.onEnded();
    }
  },

  // Album-time seek. Swaps `src` if the target falls outside the current
  // side; sets currentTime to the local position within that side.
  seek(albumTime) {
    const audio = this._el();
    if (!audio || !this.sides.length) return;
    const t = Math.max(0, Math.min(this.totalDuration(), albumTime));
    const idx = this._findSide(t);
    const local = Math.max(0, t - this.sides[idx].offset);
    if (idx !== this.currentSideIdx) {
      const wasPlaying = !audio.paused;
      this.currentSideIdx = idx;
      audio.src = this._sideUrl(idx);
      audio.load();
      // Apply currentTime once the new side reports a duration; until then
      // setting currentTime is a no-op or throws InvalidStateError.
      const apply = () => {
        try { audio.currentTime = local; } catch (e) {}
        if (wasPlaying) audio.play().catch(() => {});
        audio.removeEventListener('loadedmetadata', apply);
      };
      audio.addEventListener('loadedmetadata', apply);
    } else {
      try { audio.currentTime = local; } catch (e) {}
    }
  },

  get currentTime() {
    const audio = this._el();
    if (!audio || !this.sides.length) return 0;
    const side = this.sides[this.currentSideIdx];
    return (side ? side.offset : 0) + (audio.currentTime || 0);
  },

  get paused() {
    const audio = this._el();
    return !audio || audio.paused;
  },

  get hasSrc() { return this.sides.length > 0; },

  totalDuration() {
    if (!this.sides.length) return 0;
    const last = this.sides[this.sides.length - 1];
    return last.offset + last.duration_seconds;
  },

  play()   { const a = this._el(); if (a && a.src) a.play().catch(() => {}); },
  pause()  { const a = this._el(); if (a) { try { a.pause(); } catch (e) {} } },

  release() {
    const audio = this._el();
    if (audio) {
      try { audio.pause(); audio.src = ''; } catch (e) {}
    }
    this.albumId        = null;
    this.sides          = [];
    this.currentSideIdx = 0;
  },
};
window.weAudio = weAudio;

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
    silences:    [],   // re-detected on demand via the suggest panel
    candidates:  [],
    hoverX:      null,
    isPlaying:   false,
    playingTrack: null,
    playingEnd:  null,
    measured:    null,
    approxPeakDb: null,
    peaks:       null,
    // Flips true once weLoadExistingSplit resolves. _savePlanNow gates on
    // this so the empty default state never races ahead of the load.
    loaded:      false,
    // Reset on every open. weLoadExistingSplit re-populates cuts/titles/
    // etc. from the manifest without going through the user-edit call
    // sites, so it must NOT flip dirty. Only actual user input does.
    dirty:       false,
  });
  resetMeasureUI();
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
  // Pre-fill with " - " so weRunSearch can split into artist/album. Strip a
  // trailing "(YYYY)" from the album in case the user typed a year in.
  const albumClean = (a.album || '').replace(/\s*\(\d{4}\)\s*$/, '');
  document.getElementById('we-search-q').value =
    [a.artist, albumClean].filter(Boolean).join(' - ');
  document.getElementById('we-pop-discogs').hidden = true;
  document.getElementById('we-pop-silence').hidden = true;
  document.getElementById('we-candidates').innerHTML =
    '<div class="empty-results" style="padding:14px;font-size:11px">Search to load track durations.</div>';
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
  weAudio.onTimeUpdate = onAudioTimeUpdate;
  weAudio.onEnded      = () => stopPlayback();
  weAudio.init(fname, manifestSides);
  if (!we.total && weAudio.totalDuration() > 0) {
    we.total   = weAudio.totalDuration();
    we.viewEnd = we.total;
    document.getElementById('we-duration').textContent = fmtMMSS(we.total);
    document.getElementById('we-mini-end').textContent = fmtMMSS(we.total);
  }

  drawAll();
  document.getElementById('we-modal').hidden = false;
  document.addEventListener('keydown', weKeyDown);
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
    we.cuts    = cuts;
    we.titles  = ptracks.map(t => t.title || '');
    we.skipped = ptracks.map(t => !!t.skip);
    drawAll();
  } catch (e) { /* nothing existing — leave the empty state */ }
  finally {
    // Always flip loaded — _savePlanNow's race guard releases either way.
    if (we.albumId === fname) we.loaded = true;
  }
}

function closeWaveEditor() {
  stopPlayback();
  weAudio.release();
  document.getElementById('we-modal').hidden = true;
  document.removeEventListener('keydown', weKeyDown);
  _hidePeaksOverlay();
  // Flush any debounced plan-save in flight so a fast-close doesn't lose
  // the user's last edit. Runs in the background; the modal is already
  // hidden so the user isn't waiting on the network.
  _flushPlanSave();
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
    text.textContent = 'click measure to compute peak + noise floor';
    return;
  }
  text.textContent =
    `peak ~${we.approxPeakDb.toFixed(1)} dB · click measure for noise floor`;
}

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
  const cutMarks = we.cuts.map(t =>
    `<div class="mc" style="left:${_timeToPctFull(t)}%"></div>`
  ).join('');
  host.innerHTML = skipBands + cutMarks;
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
    const t = _snapToSilence(_xToTime(we.hoverX));
    we.cuts[we.dragging.i] = t;
    we.cuts.sort((a, b) => a - b);
    we.dragging.i = we.cuts.indexOf(t);
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
      const popDiscogs = document.getElementById('we-pop-discogs');
      const popSilence = document.getElementById('we-pop-silence');
      if (!popDiscogs.hidden || !popSilence.hidden) {
        popDiscogs.hidden = true;
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
  we.dragging = { kind: 'cut', i };
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
  // Drop the title + skip flag for the boundary that just disappeared.
  we.titles.splice(i + 1, 1);
  we.skipped.splice(i + 1, 1);
  we.dirty = true;
  invalidateMeasure();
  renderWaveformOverlay();
  renderMinimapOverlay();
  renderTracks();
}

function renderWaveformOverlay() {
  const overlay = document.getElementById('we-overlay');
  overlay.querySelectorAll('.wave-cut, .wave-silence, .wave-skip, .wave-playhead').forEach(el => el.remove());

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

  // Cut handles.
  we.cuts.forEach((t, i) => {
    const pct = _timeToPctView(t);
    if (pct == null) return;
    const el = document.createElement('div');
    el.className = 'wave-cut';
    el.style.left = pct + '%';
    el.title = `Cut at ${fmtMMSS(t)} — drag to nudge, right-click to delete`;
    el.addEventListener('mousedown',   ev => weStartDrag(i, ev));
    el.addEventListener('contextmenu', ev => weDeleteCut(i, ev));
    const grip = document.createElement('div');
    grip.className = 'grip';
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
  host.innerHTML = boundaries.slice(0, -1).map((start, i) => {
    const end = boundaries[i + 1];
    const isFirst = i === 0;
    const skipped = !!we.skipped[i];
    const unfit   = (end - start) < 0.5;
    if (!skipped && !unfit) { outNum += 1; exportable += 1; }
    const playing = we.playingTrack === i ? 'playing' : '';
    const num = (skipped || unfit) ? '—' : `${outNum}.`;
    const titleVal = skipped ? 'skip — not exported' : (we.titles[i] || '');
    const titleAttrs = (skipped || unfit)
      ? 'disabled'
      : `oninput="weSetTitle(${i}, this.value)"`;
    const rangeText = unfit ? "doesn't fit" : fmtMMSS(end - start);
    const rangeTitle = unfit ? 'Track from Discogs is longer than the recording — not exported' : '';
    const rowClass = ['wave-track'];
    if (skipped) rowClass.push('skip');
    if (unfit)   rowClass.push('unfit');
    return `
      <div class="${rowClass.join(' ')}">
        <span class="pn">${num}</span>
        <button class="play-track ${playing}" onclick="wePlayTrack(${i})" title="Play this region" ${unfit ? 'disabled' : ''}>▶</button>
        <input type="text" value="${htmlEscape(titleVal)}" ${titleAttrs}>
        <input type="text" class="start-input" value="${fmtMMSS(start)}" placeholder="m:ss.ss"
               ${isFirst || unfit ? 'disabled' : ''}
               onchange="weSetCutAt(${i}, parseMMSS(this.value))">
        <span class="range" title="${rangeTitle}">${rangeText}</span>
        <button class="skip-btn ${skipped ? 'on' : ''}"
                title="${skipped ? 'Restore region as a track' : 'Skip — drop region from output and measurement'}"
                onclick="weToggleSkip(${i})" ${unfit ? 'disabled' : ''}>⊘</button>
        <button class="del" title="Remove cut" ${isFirst ? 'disabled' : ''}
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
function weToggleSuggest(which) {
  const a = document.getElementById('we-pop-discogs');
  const b = document.getElementById('we-pop-silence');
  if (which === 'discogs') {
    a.hidden = !a.hidden;
    b.hidden = true;
  } else {
    b.hidden = !b.hidden;
    a.hidden = true;
  }
}

async function weRunSearch() {
  const q = document.getElementById('we-search-q').value.trim();
  if (!q) return;
  // Reuse the tag-panel's parser so " - " / " — " separators and word-count
  // heuristics behave the same in both editors. Strip a trailing "(YYYY)"
  // from the album field — MB rejects it as part of the release title.
  const body = parseQuery(q);
  body.album = (body.album || '').replace(/\s*\(\d{4}\)\s*$/, '').trim();
  const status = document.getElementById('we-search-status');
  const list   = document.getElementById('we-candidates');
  status.textContent = 'searching MusicBrainz + your collection…';
  list.innerHTML = '';
  try {
    const r = await fetch('/api/search', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    we.candidates           = d.candidates || [];
    we.collectionCandidates = d.collection_candidates || [];
    const mbN  = we.candidates.length;
    const colN = we.collectionCandidates.length;
    if (!mbN && !colN) {
      list.innerHTML = '<div class="empty-results" style="padding:14px;font-size:11px">No matches.</div>';
      status.textContent = '';
      return;
    }
    const summary =
      (colN ? `${colN} from your collection` : '') +
      (colN && mbN ? ' · ' : '') +
      (mbN ? `${mbN} from MusicBrainz` : '') +
      ' — click to apply track durations';
    status.textContent = summary;
    let html = '';
    if (colN) {
      html += '<div class="cand-section-header">From your collection</div>';
      html += we.collectionCandidates.map(c => {
        const img = c.cover_url
          ? `<img src="${htmlEscape(c.cover_url)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`
          : '';
        const dUrl = `https://www.discogs.com/release/${c.discogs_release_id}`;
        return `
          <div class="candidate collection-cand" onclick="wePickCollectionCandidate(${c.discogs_release_id})">
            <div class="candidate-thumb">${img}</div>
            <div class="candidate-body">
              <div class="candidate-title">
                <span class="ct-text">${htmlEscape(c.title)}</span>
                ${c.score != null ? `<span class="score">${c.score}%</span>` : ''}
              </div>
              <div class="candidate-sub">
                ${htmlEscape(c.artist)} · ${htmlEscape(c.year || '?')}
                ${c.label ? '· ' + htmlEscape(c.label) : ''}
                ${c.catno ? '<span class="pill">' + htmlEscape(c.catno) + '</span>' : ''}
                ${c.format ? '<span class="pill">' + htmlEscape(c.format) + '</span>' : ''}
                <a class="ext-link" href="${dUrl}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Open on Discogs">↗ Discogs</a>
              </div>
            </div>
          </div>`;
      }).join('');
    }
    if (mbN) {
      if (colN) html += '<div class="cand-section-header">MusicBrainz results</div>';
      html += we.candidates.map((c, i) => `
        <div class="candidate" onclick="wePickCandidate(${i})">
          <div class="candidate-thumb"><img src="/api/cover/${c.mbid}" loading="lazy" onerror="this.remove()"></div>
          <div class="candidate-body">
            <div class="candidate-title">
              <span class="ct-text">${htmlEscape(c.title)}</span>
              ${c.score != null ? `<span class="score">${c.score}%</span>` : ''}
            </div>
            <div class="candidate-sub">
              ${htmlEscape(c.artist)} · ${htmlEscape(c.year || '?')}
              ${c.label ? '· ' + htmlEscape(c.label) : ''}
              ${c.catalog_number ? '<span class="pill">' + htmlEscape(c.catalog_number) + '</span>' : ''}
              ${c.country ? '<span class="pill">' + htmlEscape(c.country) + '</span>' : ''}
              <a class="ext-link" href="https://musicbrainz.org/release/${c.mbid}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Open on MusicBrainz">↗ MB</a>
            </div>
          </div>
        </div>`).join('');
    }
    list.innerHTML = html;
  } catch (e) {
    list.innerHTML = `<div class="empty-results" style="padding:14px;font-size:11px">search failed: ${htmlEscape(e.message)}</div>`;
    status.textContent = '';
  }
}

// Apply cumulative track durations as cut positions. The last fitting track
// absorbs slack. Cuts past the recording's end are clamped to we.total, which
// produces zero-length trailing regions — surfaced in the track list as
// "doesn't fit" so the user can see what was truncated rather than having
// titles silently dropped. weApplySplit filters those out before exporting.
function _weApplyTracklist(track_details, sourceLabel) {
  const td = (track_details || []).filter(t => t && t.title);
  if (!td.length) {
    document.getElementById('we-search-status').textContent = 'no tracklist on this candidate';
    return false;
  }
  const newCuts = [];
  let cursor = 0;
  let overflow = 0;
  for (let j = 0; j < td.length - 1; j++) {
    cursor += (td[j].duration_seconds || 0);
    if (cursor >= we.total) {
      newCuts.push(we.total);
      overflow += 1;
    } else if (cursor > 0) {
      newCuts.push(cursor);
    }
  }
  we.cuts    = newCuts;
  we.titles  = td.map(t => t.title);
  we.skipped = we.titles.map(() => false);
  invalidateMeasure();
  document.getElementById('we-search-status').textContent = overflow
    ? `${td.length} tracks · ${sourceLabel} · ${overflow} don't fit recording`
    : `${td.length} tracks · ${sourceLabel}`;
  drawAll();
  return true;
}

async function wePickCandidate(i) {
  const c = we.candidates[i];
  if (!c) return;
  const status = document.getElementById('we-search-status');
  status.textContent = `loading tracklist for ${c.title}…`;
  try {
    const r = await fetch(`/api/release/${c.mbid}`);
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    _weApplyTracklist(d.track_details, d.discogs_id ? 'enriched from Discogs' : 'MB only');
  } catch (e) {
    status.textContent = 'load failed: ' + e.message;
  }
}

async function wePickCollectionCandidate(releaseId) {
  const status = document.getElementById('we-search-status');
  status.textContent = `loading tracklist from your collection…`;
  try {
    const r = await fetch(`/api/release/discogs/${releaseId}`);
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    _weApplyTracklist(d.track_details, 'from your collection');
  } catch (e) {
    status.textContent = 'load failed: ' + e.message;
  }
}

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
  } catch (e) {
    status.textContent = 'detection failed: ' + e.message;
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
      const body = { album_id: we.albumId, tracks, bit_depth: bitDepth, job_id: jobId };
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
    // No draft to clear — the split route already wrote the same plan to
    // album.json (alongside `music_relpath`). Closing the editor leaves
    // the plan in place so re-edit picks up where this run left off.
    closeWaveEditor();
    refreshAlbums();
  } catch (e) {
    toast('✗ split failed: ' + e.message, 'err');
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
    text.textContent =
      `peak ~${we.approxPeakDb.toFixed(1)} dB · cuts changed — re-measure`;
  } else if (text) {
    text.textContent = 'cuts changed — re-measure';
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
  } catch (e) {
    we.measured = null;
    if (text) text.textContent = 'measurement failed: ' + e.message;
  } finally {
    if (btn) btn.disabled = false;
    hideBar(bar);
  }
}

function formatMeasured(d) {
  const fmt = v => (v == null || isNaN(v)) ? '—' : v.toFixed(1);
  const bits = d.effective_bits == null ? '—' : d.effective_bits.toFixed(1);
  return `peak ${fmt(d.peak_db)} dB · noise floor ${fmt(d.noise_floor_db)} dB`
       + ` · DR ${fmt(d.dynamic_range_db)} dB (≈ ${bits} effective bits)`;
}

// ── Library row inline track expansion ────────────────────────────────────
async function toggleTracks(fname) {
  const row = document.querySelector(`tr[data-album='${fname}']`);
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
      return `
      <div class="track-row" title="${htmlEscape(t.filename)}">
        <span class="tnum">${t.track_number || '—'}</span>
        <span class="ttitle">${htmlEscape(t.title || t.filename)}</span>
        <span class="tdur">${fmtDuration(t.duration_seconds)}</span>
        <span class="tsize">${t.size_mb} MB</span>
        <button class="icon-btn preview-btn ${playClass}" data-fname="${htmlEscape(key)}" data-kind="track" title="Preview" onclick="togglePreviewTrack('${fname}', '${htmlEscape(t.filename)}')">${playGlyph}</button>
        <a class="icon-btn" href="/api/album/${encodeURIComponent(fname)}/track/${encodeURIComponent(t.filename)}" download title="Download">↓</a>
      </div>`;
    }).join('')}</td>`;
    row.insertAdjacentElement('afterend', sub);
  } catch (e) { console.error(e); }
}
