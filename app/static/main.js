// ── Shared client state ───────────────────────────────────────────────────
// All connection/recording truth lives on the server now. These mirrors are
// updated from WebSocket frames so every tab stays in lockstep.
let recording = false, sessionId = null, paused = false;
let upstreamConnected = false;
let muted = true;       // local — each tab decides its own playback volume
let audioEl = null;

// ── Stereo VU meter (driven by WS frames) ─────────────────────────────────
// Smoothing + peak-hold are still done locally so the meter looks the same
// even at low frame rates / poor connectivity. Raw peak values arrive at
// ~20 Hz from the server-side reader and feed updateMeter().
const lvl = { L: 0, R: 0 };
const peak = { L: 0, R: 0 };
const peakAge = { L: 0, R: 0 };  // frames since peak was set
const PEAK_HOLD_FRAMES = 30;     // ~1.5 s at 50ms tick
const PEAK_DECAY = 0.015;        // per frame after hold expires

// Latched clip flags now mirror server state. Click-to-clear fires a POST
// so every tab un-latches in sync.
const clipped = { L: false, R: false };

function dbStr(v) {
  if (v <= 0.0005) return '−∞';
  const db = 20 * Math.log10(v);
  return (db >= 0 ? '+' : '') + db.toFixed(1);
}

function setClipBadge(ch, on) {
  clipped[ch] = !!on;
  document.getElementById('clip-' + ch).hidden = !on;
}

function updateMeter(ch, level) {
  // level smoothing — fast attack, slow release
  lvl[ch] = level > lvl[ch] ? level : lvl[ch] * 0.82 + level * 0.18;
  // peak hold
  if (level >= peak[ch]) { peak[ch] = level; peakAge[ch] = 0; }
  else if (++peakAge[ch] > PEAK_HOLD_FRAMES) { peak[ch] = Math.max(0, peak[ch] - PEAK_DECAY); }

  const pct = Math.min(lvl[ch] * 100, 100);
  document.getElementById('mask-' + ch).style.width = (100 - pct) + '%';
  document.getElementById('peak-' + ch).style.left = Math.min(peak[ch] * 100, 99.5) + '%';
  document.getElementById('db-' + ch).textContent = dbStr(peak[ch]);
}

async function clearClip(ch) {
  // Server clears the latch and broadcasts to every tab.
  try {
    await fetch('/api/clip/clear?ch=' + encodeURIComponent(ch || ''),
                { method: 'POST' });
  } catch(e) { /* WS will eventually re-sync */ }
}

// At ~20 Hz of WS VU frames the smoother already looks tight, but a 50 ms
// local tick handles peak-hold decay during idle (no frames coming in).
setInterval(() => {
  if (!upstreamConnected) {
    updateMeter('L', 0);
    updateMeter('R', 0);
  }
}, 50);

// ── Timer ─────────────────────────────────────────────────────────────────
function fmt(s) {
  return [Math.floor(s/3600), Math.floor((s%3600)/60), s%60]
    .map(n => String(n).padStart(2,'0')).join(':');
}

// Library "Recorded" column. Compact for this year, year-bearing for older.
// fmtDateFull is used for the cell tooltip so the full timestamp is always
// one hover away.
function fmtDate(unix) {
  if (!unix) return '—';
  const d = new Date(unix * 1000);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString(undefined, sameYear
    ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { year: 'numeric', month: 'short', day: 'numeric' });
}
function fmtDateFull(unix) {
  if (!unix) return '';
  return new Date(unix * 1000).toLocaleString();
}

// Compact source-format readout for library/album tables: "24/96", "16/44.1".
// Returns "—" when the FLAC didn't expose readable format info.
function fmtSourceFormat(f) {
  if (!f.bit_depth || !f.sample_rate_khz) return '—';
  const sr = Number.isInteger(f.sample_rate_khz)
    ? f.sample_rate_khz
    : f.sample_rate_khz.toFixed(1);
  return `${f.bit_depth}/${sr}`;
}

// ── Log helper ────────────────────────────────────────────────────────────
function log(msg, cls='') {
  const el = document.getElementById('log');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
  // trim old lines
  while (el.children.length > 40) el.removeChild(el.firstChild);
}

// ── Error helper ──────────────────────────────────────────────────────────
// Extract a friendly error message from a non-OK fetch response. FastAPI
// surfaces HTTPException(status, "msg") as {"detail": "msg"}; fall back to
// raw body / status code for anything else (e.g. proxy failures).
async function parseError(resp) {
  let body = '';
  try { body = await resp.text(); } catch (e) {}
  if (body) {
    try {
      const j = JSON.parse(body);
      if (j && typeof j.detail === 'string') return j.detail;
    } catch (e) {}
    return body;
  }
  return 'HTTP ' + resp.status;
}

// ── Job progress bars ─────────────────────────────────────────────────────
// Long ffmpeg ops (combine / split / measure / detect-silences / waveform)
// publish progress under a client-supplied job_id via /api/jobs/<id>. The
// helpers below let a caller spin up a bar, post the request with the job_id
// in either the body or query string, poll while it's in flight, and tear
// the bar down on completion. Polling is done with a 250ms cadence — fast
// enough to feel live, slow enough not to hammer the server.

let _jobIdCounter = 0;
function newJobId() {
  _jobIdCounter++;
  return 'j_' + Date.now().toString(36) + '_'
    + Math.random().toString(36).slice(2, 8) + '_' + _jobIdCounter;
}

function _setBar(barEl, progress, phase) {
  if (!barEl) return;
  const fill = barEl.querySelector('.job-bar-fill, .wpo-fill');
  const pct  = barEl.querySelector('.job-bar-pct');
  const ph   = barEl.querySelector('.job-bar-phase, .wpo-text');
  const w    = Math.max(0, Math.min(100, progress * 100));
  if (fill) fill.style.width = w.toFixed(1) + '%';
  if (pct)  pct.textContent  = Math.round(w) + '%';
  if (ph && phase) ph.textContent = phase;
}

function showBar(barEl, label) {
  if (!barEl) return;
  _setBar(barEl, 0, label || 'working…');
  barEl.hidden = false;
}

function hideBar(barEl) {
  if (!barEl) return;
  barEl.hidden = true;
  _setBar(barEl, 0, '');
}

// Run `fn(jobId)` and poll /api/jobs/<jobId> until either fn resolves or the
// server reports done. Updates `barEl` as progress comes in. Returns whatever
// fn returns. The bar is the caller's responsibility to show/hide — we only
// drive the fill width.
async function withJobProgress(barEl, fn) {
  const jobId = newJobId();
  let stop = false;

  const poll = async () => {
    while (!stop) {
      try {
        const r = await fetch('/api/jobs/' + encodeURIComponent(jobId));
        if (r.ok) {
          const d = await r.json();
          _setBar(barEl, d.progress || 0, d.phase || '');
          if (d.done) break;
        }
        // 404 = job not started yet (server hasn't reached start_job); keep polling.
      } catch (e) { /* network blip — try again */ }
      await new Promise(res => setTimeout(res, 250));
    }
  };
  const pollPromise = poll();

  try {
    return await fn(jobId);
  } finally {
    stop = true;
    try { await pollPromise; } catch (e) {}
  }
}

// ── Toast helper ──────────────────────────────────────────────────────────
// Used for action results (✓ tagged, ✕ delete failed). Also writes to the
// sidebar log so the history is preserved when a toast fades.
function toast(msg, kind='info') {
  log(msg, kind);
  const c = document.getElementById('toast-container');
  if (!c) return;
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.textContent = msg;
  c.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.remove(), 250);
  }, 3500);
}

// ── Server connect / disconnect ───────────────────────────────────────────
// "Connect" now means "tell the server to start pulling the upstream
// stream." The state is global — any tab can press it, every tab sees the
// same dot/button via the WebSocket. VU + recording both depend on this.
async function toggleConnect() {
  if (upstreamConnected) {
    // Server logs success/failure to the shared ring buffer; no client toast.
    try {
      const r = await fetch('/api/disconnect', { method: 'POST' });
      if (!r.ok) await parseError(r);  // surfaced via bus.log
    } catch(e) { /* network drop — WS will resync */ }
  } else {
    const url = document.getElementById('stream-url').value;
    try {
      const r = await fetch('/api/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stream_url: url }),
      });
      if (!r.ok) await parseError(r);  // surfaced via bus.log
      // probeGain stays client-side: each tab probes the Pi /info to render
      // the slider; gain changes do persist on the Pi so the next probe
      // anywhere shows the new value.
      else probeGain(url);
    } catch(e) { /* network drop — WS will resync */ }
  }
}

// ── Local audio playback (just an <audio> element) ───────────────────────
// VU comes from the server over WebSocket, so we don't need WebAudio at
// all — a plain HTMLAudioElement plays the proxy stream directly to the
// system mixer. Mute is the element's `.muted` property (and a pause to
// stop the network). Created muted so the autoplay policy lets us prefetch.
function ensureAudioGraph() {
  if (audioEl) return;
  audioEl = new Audio();
  // No crossOrigin: same-origin proxy, no WebAudio that would need it. The
  // attribute used to require CORS headers on the streaming response, which
  // Chrome can be picky about for chunked endpoints.
  audioEl.muted = true;
  audioEl.preload = 'none';  // wait until the user actually unmutes
  audioEl.addEventListener('error', () => {
    const err = audioEl.error;
    const codeMap = {
      1: 'aborted',
      2: 'network',
      3: 'decode',
      4: 'src not supported',
    };
    const detail = err ? `${codeMap[err.code] || err.code}${err.message ? ': ' + err.message : ''}` : 'unknown';
    log(`✗ playback error — ${detail}`, 'err');
  });
}

function applyMuteState() {
  const btn = document.getElementById('mute-btn');
  if (!btn) return;
  btn.textContent = muted ? '🔈 unmute' : '🔇 mute';
  btn.classList.toggle('active', !muted);
  if (audioEl) audioEl.muted = muted;
}

async function toggleMute() {
  ensureAudioGraph();
  muted = !muted;
  if (!muted && upstreamConnected) {
    // Re-point at the proxy so we recover from a prior 409 (no upstream
    // when the page first loaded) or a disconnect/reconnect cycle that
    // left the element in an error state.
    audioEl.src = '/api/stream-proxy?t=' + Date.now();
    audioEl.muted = false;
    try { await audioEl.play(); } catch(e) {}
  } else if (muted) {
    // Pause + drop the src so the browser actually closes the network
    // connection. Otherwise the proxy stream keeps flowing on the server,
    // its ffmpeg eventually blocks on a full stdout pipe, and the orphan
    // subscriber sits around until the TCP connection finally times out.
    try { audioEl.pause(); } catch(e) {}
    try { audioEl.removeAttribute('src'); audioEl.load(); } catch(e) {}
  }
  applyMuteState();
}

// ── ADC gain (pi-recorder /info + /gain) ──────────────────────────────────
let gainBase = null;
let gainTimer = null;
let pendingDefaultGainDb = null;  // set from /api/config; applied once after first probe

function hideGain() {
  document.getElementById('gain-row').hidden = true;
  gainBase = null;
}

async function probeGain(streamUrl) {
  hideGain();
  let base;
  try { const u = new URL(streamUrl); base = `${u.protocol}//${u.host}`; }
  catch { return; }
  try {
    const r = await fetch(`${base}/info`, { mode: 'cors' });
    if (!r.ok) return;
    const info = await r.json();
    if (typeof info.gain_db !== 'number') return;
    const slider = document.getElementById('gain-slider');
    slider.min  = info.gain_min_db  ?? -12;
    slider.max  = info.gain_max_db  ?? 40;
    slider.step = info.gain_step_db ?? 0.5;
    slider.value = info.gain_db;
    document.getElementById('gain-db').textContent = info.gain_db.toFixed(1) + ' dB';
    document.getElementById('gain-row').hidden = false;
    gainBase = base;
    log(`✔ pi gain ${info.gain_db.toFixed(1)} dB · ${info.left_input}/${info.right_input}`, 'ok');

    // Apply DEFAULT_GAIN_DB from server config, once per page load.
    if (pendingDefaultGainDb !== null && Math.abs(pendingDefaultGainDb - info.gain_db) > 0.01) {
      const desired = pendingDefaultGainDb;
      pendingDefaultGainDb = null;
      try {
        const gr = await fetch(`${base}/gain`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ db: desired })
        });
        if (gr.ok) {
          const gd = await gr.json();
          slider.value = gd.gain_db;
          document.getElementById('gain-db').textContent = gd.gain_db.toFixed(1) + ' dB';
          log(`  applied default gain ${gd.gain_db.toFixed(1)} dB`, 'info');
        }
      } catch (err) { log('✗ default gain set failed: ' + err.message, 'err'); }
    }
  } catch (e) {
    // not a pi-recorder host — fine, slider stays hidden
  }
}

document.getElementById('gain-slider').addEventListener('input', (e) => {
  const db = parseFloat(e.target.value);
  document.getElementById('gain-db').textContent = db.toFixed(1) + ' dB';
  if (!gainBase) return;
  clearTimeout(gainTimer);
  gainTimer = setTimeout(async () => {
    try {
      const r = await fetch(`${gainBase}/gain`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ db })
      });
      if (!r.ok) throw new Error(await parseError(r));
      const d = await r.json();
      document.getElementById('gain-db').textContent = d.gain_db.toFixed(1) + ' dB';
    } catch (err) { log('✗ gain set failed: ' + err.message, 'err'); }
  }, 150);
});

// ── Record / stop ─────────────────────────────────────────────────────────
// Recording state is server-side and broadcast over WS, so any tab can stop
// a session that another tab started. The handlers here only issue the API
// call; visible UI changes happen in applyRecordState() (driven by WS).
async function toggleRec() {
  if (recording) {
    if (!sessionId) {
      // Another tab owns the active session and we don't know its id yet —
      // /api/status returns the list of live sessions; pick the first.
      try {
        const s = await (await fetch('/api/status')).json();
        sessionId = (s.sessions && s.sessions[0] && s.sessions[0].id) || null;
      } catch(e) {}
    }
    if (!sessionId) {
      toast('✗ no active session id available', 'err');
      return;
    }
    try {
      const r = await fetch(`/api/record/stop/${sessionId}`, { method: 'POST' });
      if (!r.ok) throw new Error(await parseError(r));
      // The actual UI flip happens via the WS `record:stop` event.
    } catch(e) { toast('✗ ' + e.message, 'err'); }
  } else {
    if (!upstreamConnected) {
      toast('✗ connect to a stream first', 'err');
      return;
    }
    const url = document.getElementById('stream-url').value;
    const dur = parseInt(document.getElementById('dur-sel').value);
    try {
      const r = await fetch('/api/record/start', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ stream_url: url, duration: dur }),
      });
      if (!r.ok) throw new Error(await parseError(r));
    } catch(e) { toast('✗ ' + e.message, 'err'); }
  }
}

async function togglePause() {
  if (!sessionId) return;
  const path = paused ? 'resume' : 'pause';
  try {
    const r = await fetch(`/api/record/${path}/${sessionId}`, { method: 'POST' });
    if (!r.ok) throw new Error(await parseError(r));
    // UI updates via the WS `record:pause`/`resume` event.
  } catch (e) { toast('✗ ' + e.message, 'err'); }
}

// ── Recording UI driven from WS state ─────────────────────────────────────
// `applyRecordState` is the single place that mutates the visible recording
// UI — called by WS hellos (replay on connect) and live record events.
let recStartTimeMs = 0;        // local clock anchor for the elapsed timer
let recDurationSec = 0;        // 0 = unlimited
let recTimerInterval = null;

function applyRecordState({ active, paused: isPaused, sid, durationSec, elapsedSec }) {
  recording = !!active;
  paused    = !!isPaused;
  sessionId = active ? (sid || null) : null;

  const recBtn  = document.getElementById('recbtn');
  const pauseBtn = document.getElementById('pausebtn');
  const sdot    = document.getElementById('sdot');
  const stext   = document.getElementById('stext');
  const hint    = document.getElementById('timer-hint');
  const prog    = document.getElementById('prog');

  recBtn.classList.toggle('active', recording);
  pauseBtn.hidden = !recording;
  pauseBtn.classList.toggle('paused', paused);
  pauseBtn.textContent = paused ? '▶' : '‖';
  pauseBtn.title       = paused ? 'Resume' : 'Pause';

  if (recording) {
    stext.textContent = paused ? 'paused' : 'recording';
    hint.textContent = paused ? 'paused — click ▶ to resume'
                              : 'click ■ to stop · ‖ to pause';
    recDurationSec = durationSec || 0;
    // Anchor the local clock so the timer survives WS gaps.
    recStartTimeMs = Date.now() - (elapsedSec || 0) * 1000;
    if (!recTimerInterval) {
      recTimerInterval = setInterval(tickRecTimer, 250);
    }
    tickRecTimer();
  } else {
    if (recTimerInterval) { clearInterval(recTimerInterval); recTimerInterval = null; }
    stext.textContent = upstreamConnected ? 'connected' : 'idle';
    hint.textContent = 'click ● to start recording';
    prog.style.width = '0%';
    document.getElementById('timer').textContent = fmt(0);
  }
  updateSdot();
}

function tickRecTimer() {
  if (!recording) return;
  if (paused) return;
  const elapsed = Math.floor((Date.now() - recStartTimeMs) / 1000);
  document.getElementById('timer').textContent = fmt(elapsed);
  if (recDurationSec > 0) {
    document.getElementById('prog').style.width =
      Math.min(elapsed / recDurationSec * 100, 100) + '%';
  }
}

function applyUpstreamState({ connected, fmt: f }) {
  upstreamConnected = !!connected;
  const btn = document.getElementById('connect-btn');
  if (btn) btn.textContent = connected ? 'disconnect' : 'connect';
  // Lock the URL input while connected — changing it has no effect until
  // disconnect anyway, so making it look uneditable matches reality.
  const urlInput = document.getElementById('stream-url');
  if (urlInput) urlInput.disabled = !!connected;
  if (!recording) {
    document.getElementById('stext').textContent = connected ? 'connected' : 'idle';
  }
  // Chevron is the click affordance for the health panel; only show it when
  // there's something to see (connected). The whole .status-indicator is the
  // click target — toggle the .clickable class for cursor + hover.
  const chevron = document.getElementById('health-chevron');
  const ind = document.getElementById('status-indicator');
  if (chevron) chevron.hidden = !connected;
  if (ind) ind.classList.toggle('clickable', !!connected);
  updateSdot();
  if (!connected) {
    // Decay meters; clear gain slider since the Pi probe needs reconnect.
    peak.L = peak.R = 0; lvl.L = lvl.R = 0;
    hideGain();
    applyHealthState(null);
    // Auto-collapse the panel on disconnect so it doesn't linger empty.
    const panel = document.getElementById('health-panel');
    if (panel) panel.hidden = true;
  }
}

// Single source of truth for the connection-dot color. Recording wins (red
// blink) over health; otherwise the dot reflects the latest health level
// while connected, gray when disconnected.
let lastHealthLevel = null; // 'green' | 'yellow' | 'red' | null
function updateSdot() {
  const sdot = document.getElementById('sdot');
  if (!sdot) return;
  if (recording) {
    sdot.className = paused ? 'dot' : 'dot rec';
    return;
  }
  if (!upstreamConnected) {
    sdot.className = 'dot';
    return;
  }
  sdot.className = lastHealthLevel === 'yellow' ? 'dot warn'
                 : lastHealthLevel === 'red'    ? 'dot bad'
                 : 'dot ok';  // 'green' or null (no health tick yet)
}

// ── Stream-health indicator ───────────────────────────────────────────────
// Receives `health` WS events with bytes/sec, gap counts, level, etc.
// Updates a colored dot in the header and a collapsible stats panel.
function fmtBps(bps) {
  if (!bps && bps !== 0) return '—';
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + ' MB/s';
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + ' kB/s';
  return bps + ' B/s';
}

function applyHealthState(h) {
  lastHealthLevel = (h && h.level) || null;
  updateSdot();
  const ind = document.getElementById('status-indicator');
  if (ind) {
    ind.title = h
      ? `stream health: ${h.level || '—'} · ${fmtBps(h.bytes_per_sec)} ` +
        `(expected ${fmtBps(h.expected_bps)}) — click for details`
      : 'Stream health';
  }
  const map = {
    'hp-level':       h ? (h.level || '—') : '—',
    'hp-bps':         h ? fmtBps(h.bytes_per_sec) : '—',
    'hp-expected':    h ? fmtBps(h.expected_bps) : '—',
    'hp-gaps':        h ? String(h.gap_count_recent ?? 0) : '0',
    'hp-gap-total':   h ? String(h.gap_count ?? 0) : '0',
    'hp-since':       h ? `${h.ms_since_last_frame ?? 0} ms` : '—',
    'hp-reconnects':  h ? String(h.reconnect_count ?? 0) : '0',
  };
  for (const [id, v] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (el) el.textContent = v;
  }
}

function toggleHealthPanel() {
  if (!upstreamConnected) return;  // nothing to show until first health tick
  const panel = document.getElementById('health-panel');
  if (!panel) return;
  panel.hidden = !panel.hidden;
}

// ── Library ───────────────────────────────────────────────────────────────
let filesByName = {};
const selected = new Set();

// Sort state — defaults to newest-first, which is what we want when you've
// just stopped a recording. Click a header to switch column; click again to
// flip direction. Persisted across reloads via localStorage.
let sortBy  = localStorage.getItem('lib.sortBy')  || 'date';
let sortDir = localStorage.getItem('lib.sortDir') || 'desc';

// Filter state — restored from localStorage so a freshly-loaded tab keeps
// the filter you had applied. Tracks the visible (post-filter) row set so
// "select all" only checks visible rows and bulk-bar shows accurate counts.
// Single text filter applied to BOTH the library and the albums table.
// Persists across reloads via localStorage.
let libFilterText = localStorage.getItem('lib.filterText') || '';
let libVisibleNames = new Set();

function rowMatches(f) {
  const q = libFilterText.trim().toLowerCase();
  if (!q) return true;
  const hay = [
    f.filename, f.artist, f.album, f.year, f.genre, f.label, f.catalog_number,
  ].filter(Boolean).join(' ').toLowerCase();
  return hay.includes(q);
}

function applyLibFilterControls() {
  const inp = document.getElementById('lib-search');
  if (inp) {
    if (document.activeElement !== inp) inp.value = libFilterText;
    inp.classList.toggle('active', !!libFilterText.trim());
  }
  const clr = document.getElementById('lib-filter-clear');
  if (clr) clr.hidden = !libFilterText.trim();
}

function onLibSearchInput(v) {
  libFilterText = v;
  localStorage.setItem('lib.filterText', v);
  refreshLibRender();
  refreshAlbumsRender();
}

function clearLibFilter() {
  libFilterText = '';
  localStorage.removeItem('lib.filterText');
  refreshLibRender();
  refreshAlbumsRender();
}

const SORT_KEYS = {
  album:  f => (f.album  || f.filename).toLowerCase(),
  artist: f => (f.artist || '').toLowerCase(),
  year:   f => parseInt(f.year, 10) || 0,
  length: f => f.duration_seconds || 0,
  size:   f => f.size_mb || 0,
  // Sort by total information rate (bps × Hz) so 24/96 ranks above 16/44.1.
  fmt:    f => (f.bit_depth || 0) * (f.sample_rate_khz || 0),
  status: f => (f.tagged ? 1 : 0),
  date:   f => f.mtime || 0,
};

function setSort(col) {
  if (sortBy === col) {
    sortDir = sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    sortBy = col;
    // Sensible default direction per column: text → asc, numeric/date → desc.
    sortDir = (col === 'album' || col === 'artist') ? 'asc' : 'desc';
  }
  localStorage.setItem('lib.sortBy',  sortBy);
  localStorage.setItem('lib.sortDir', sortDir);
  refreshLib();
  refreshAlbumsRender();
}

function sortFiles(files) {
  const key = SORT_KEYS[sortBy] || SORT_KEYS.date;
  const dir = sortDir === 'asc' ? 1 : -1;
  // Stable secondary sort by mtime (newest first) so equal keys keep a
  // predictable order between renders.
  return files.slice().sort((a, b) => {
    const ka = key(a), kb = key(b);
    if (ka < kb) return -1 * dir;
    if (ka > kb) return  1 * dir;
    return (b.mtime || 0) - (a.mtime || 0);
  });
}

function updateSortHeaders() {
  const arrow = sortDir === 'asc' ? '▲' : '▼';
  document.querySelectorAll('.lib-table th.sortable').forEach(th => {
    const active = th.dataset.sort === sortBy;
    th.classList.toggle('sorted', active);
    th.querySelector('.sort-arrow').textContent = active ? arrow : '';
  });
}

function htmlEscape(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Build a keydown handler that closes a modal on Escape. If focus is in a
// text input, the first Escape just blurs the input — guards against losing
// half-typed metadata. A second Escape (or any Escape with focus elsewhere)
// closes the modal. Mirrors the wave editor's own ESC handling.
function makeModalEscHandler(closeFn) {
  return function (e) {
    if (e.key !== 'Escape') return;
    const tag = (e.target.tagName || '').toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      e.target.blur();
      return;
    }
    e.preventDefault();
    closeFn();
  };
}

function updateBulkBar() {
  const bar = document.getElementById('bulk-bar');
  document.getElementById('bulk-count').textContent = selected.size;
  bar.classList.toggle('hidden', selected.size === 0);
  // "Check All" reflects the state of the CURRENTLY VISIBLE rows so a
  // filtered list can still be batch-selected predictably. Off-screen
  // (filtered-out) selections are preserved untouched.
  const visible = libVisibleNames.size;
  let visSelected = 0;
  for (const n of libVisibleNames) if (selected.has(n)) visSelected += 1;
  const checkAll = document.getElementById('check-all');
  if (checkAll) checkAll.checked = visible > 0 && visSelected === visible;
  const combineBtn = document.getElementById('combine-btn');
  if (combineBtn) combineBtn.disabled = selected.size < 2;
}

function toggleRow(fname, checked) {
  if (checked) selected.add(fname); else selected.delete(fname);
  updateBulkBar();
}

function toggleAll(checked) {
  // Operate only on currently visible rows so a filter narrows the bulk
  // operation. Items hidden by the filter keep their selection state.
  if (checked) libVisibleNames.forEach(fn => selected.add(fn));
  else         libVisibleNames.forEach(fn => selected.delete(fn));
  document.querySelectorAll('.row-check').forEach(cb => { cb.checked = checked; });
  updateBulkBar();
}

function clearSelection() {
  selected.clear();
  document.querySelectorAll('.row-check').forEach(cb => cb.checked = false);
  const ca = document.getElementById('check-all'); if (ca) ca.checked = false;
  updateBulkBar();
}

async function bulkDelete() {
  if (!selected.size) return;
  const names = [...selected];
  if (!confirm(`Delete ${names.length} recording${names.length===1?'':'s'}? This cannot be undone.`)) return;
  const bar = document.getElementById('bulk-action-bar');
  const fill = document.getElementById('bulk-action-fill');
  document.getElementById('bulk-action-phase').textContent = 'deleting…';
  document.getElementById('bulk-action-pct').textContent = '';
  fill.classList.add('indeterminate');
  bar.hidden = false;
  try {
    const r = await fetch('/api/recordings/bulk-delete', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({filenames: names})
    });
    const d = await r.json();
    toast(`✓ Deleted ${d.deleted.length} file${d.deleted.length===1?'':'s'}`,
          d.missing?.length ? 'err' : 'ok');
    selected.clear();
    refreshLib();
  } catch(e) { toast('✗ ' + e.message, 'err'); }
  finally {
    bar.hidden = true;
    fill.classList.remove('indeterminate');
    fill.style.width = '0%';
  }
}

async function bulkPromote() {
  if (!selected.size) return;
  const names = [...selected];
  if (!confirm(`Promote ${names.length} recording${names.length===1?'':'s'} to albums/ using existing tags?`)) return;

  const bar  = document.getElementById('bulk-action-bar');
  const fill = document.getElementById('bulk-action-fill');
  const pct  = document.getElementById('bulk-action-pct');
  document.getElementById('bulk-action-phase').textContent = 'promoting…';
  bar.hidden = false;

  let done = 0, failed = 0;
  const total = names.length;
  for (const fname of names) {
    const f = filesByName[fname] || {};
    const album = {
      artist: f.artist || '',
      album:  f.album  || '',
      year:   f.year   || '',
      genre:  f.genre  || '',
      label:  f.label  || '',
    };
    try {
      const r = await fetch('/api/promote', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ filename: fname, album }),
      });
      if (!r.ok) throw new Error(await parseError(r));
      done++;
    } catch { failed++; }
    const progress = ((done + failed) / total) * 100;
    fill.style.width = progress + '%';
    pct.textContent  = `${done + failed} / ${total}`;
  }

  bar.hidden = true;
  fill.style.width = '0%';

  if (failed === 0) {
    toast(`✓ Promoted ${done} recording${done===1?'':'s'} to albums`, 'ok');
  } else {
    toast(`Promoted ${done}, failed ${failed}`, 'err');
  }
  selected.clear();
  refreshLib();
  refreshAlbums();
}

async function refreshLib() {
  try {
    const r = await fetch('/api/recordings');
    const d = await r.json();
    updateDiskFree(d.disk_free_gb);
    filesByName = {};
    d.files.forEach(f => filesByName[f.filename] = f);
    // drop selections that no longer exist
    [...selected].forEach(fn => { if (!filesByName[fn]) selected.delete(fn); });
    refreshLibRender();
  } catch(e) { console.error(e); }
}

function refreshLibRender() {
  applyLibFilterControls();
  const tbody = document.getElementById('lib-tbody');
  if (!tbody) return;
  const all = Object.values(filesByName);
  const filtered = all.filter(rowMatches);
  const files = sortFiles(filtered);
  libVisibleNames = new Set(files.map(f => f.filename));
  const total = all.length;
  const shown = files.length;
  const filterActive = !!libFilterText.trim();
  const countEl = document.getElementById('lib-count');
  if (countEl) {
    countEl.textContent = filterActive
      ? `${shown} of ${total} file${total===1?'':'s'}`
      : `${total} file${total===1?'':'s'}`;
  }
  updateSortHeaders();
  if (!files.length) {
    const msg = total === 0
      ? 'No recordings yet. Drop the needle!'
      : 'No matches for current filter.';
    tbody.innerHTML = `<tr><td colspan="10" class="empty-lib">${msg}</td></tr>`;
    updateBulkBar();
    return;
  }
  tbody.innerHTML = files.map(f => {
      const fn = htmlEscape(f.filename);
      const isSel = selected.has(f.filename) ? 'checked' : '';
      const rowClass = f.tagged ? 'row-tagged' : 'row-untagged';
      const playing = previewIs(f.filename, 'lib') ? 'playing' : '';
      const playGlyph = previewIs(f.filename, 'lib') ? '⏸' : '▶';
      const titleText = htmlEscape(f.album || f.filename.replace('.flac',''));
      // Untagged rows allow double-click rename in place (no tag panel needed).
      // Handler lives on the whole <td> so the entire cell (including padding
      // and whitespace to the right of short titles) is a click target —
      // clicks elsewhere on the row still bubble normally.
      const cellAttrs = f.tagged
        ? ''
        : ` ondblclick="startInlineRename(this.dataset.fname, this.querySelector('.row-title-text'))" data-fname="${fn}" title="Double-click to rename"`;
      return `
      <tr class="${rowClass}">
        <td class="col-check"><input type="checkbox" class="row-check" data-fname="${fn}" ${isSel}
            onclick="toggleRow(this.dataset.fname, this.checked)"></td>
        <td style="font-weight:500"${cellAttrs}>
          <div class="row-title">
            <span class="row-thumb"><img src="/api/file-cover/${encodeURIComponent(f.filename)}" loading="lazy" onerror="this.remove()"></span>
            <span class="row-title-text">${titleText}</span>
          </div>
        </td>
        <td style="color:var(--muted)">${htmlEscape(f.artist || '—')}</td>
        <td style="color:var(--muted)">${htmlEscape(f.year || '—')}</td>
        <td style="color:var(--muted);white-space:nowrap" title="${htmlEscape(fmtDateFull(f.mtime))}">${htmlEscape(fmtDate(f.mtime))}</td>
        <td style="color:var(--muted)">${fmtDuration(f.duration_seconds)}</td>
        <td style="color:var(--muted)">${f.size_mb} MB</td>
        <td style="color:var(--muted);font-variant-numeric:tabular-nums" title="bit depth / sample rate (kHz)">${fmtSourceFormat(f)}</td>
        <td>
          <span class="badge ${f.tagged ? 'tagged' : 'raw'}">${f.tagged ? 'tagged' : 'untagged'}</span>
        </td>
        <td style="white-space:nowrap;text-align:right">
          <button class="icon-btn preview-btn ${playing}" data-fname="${fn}" data-kind="lib" title="Preview" onclick="togglePreview(this.dataset.fname, this.dataset.kind)">${playGlyph}</button>
          <button class="icon-btn" title="Tag album" onclick="openTag('${fn}')">✎</button>
          <button class="icon-btn" title="Promote to album" onclick="openPromote('${fn}')">▲</button>
          <a class="icon-btn" href="/api/download/${encodeURIComponent(f.filename)}" download title="Download">↓</a>
          <button class="icon-btn danger" title="Delete" onclick="deleteFile('${fn}')">✕</button>
        </td>
      </tr>`;
  }).join('');
  updateBulkBar();
}

async function deleteFile(fname) {
  if (!confirm(`Delete ${fname}? This cannot be undone.`)) return;
  try {
    const r = await fetch(`/api/recordings/${encodeURIComponent(fname)}`, { method: 'DELETE' });
    if (!r.ok) throw new Error(await parseError(r));
    selected.delete(fname);
    refreshLib();
  } catch (e) { toast('✗ delete failed: ' + e.message, 'err'); }
}

// Inline rename for untagged rows. Double-clicking the title swaps it for an
// input; Enter saves, Esc / blur cancels.
function startInlineRename(fname, el) {
  const f = filesByName[fname];
  if (!f || f.tagged) return;
  const current = (f.album || f.filename.replace(/\.flac$/, '')).trim();
  const input = document.createElement('input');
  input.className = 'inline-rename';
  input.value = current;
  input.size = Math.max(20, Math.min(60, current.length + 4));
  el.style.display = 'none';
  el.parentNode.insertBefore(input, el.nextSibling);
  input.focus();
  input.select();
  let done = false;
  const cancel = () => { if (done) return; done = true; input.remove(); el.style.display = ''; };
  const save = async () => {
    if (done) return;
    const newName = input.value.trim();
    if (!newName || newName === current) return cancel();
    done = true;
    try {
      const r = await fetch(`/api/recordings/${encodeURIComponent(fname)}/rename`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ new_name: newName }),
      });
      if (!r.ok) throw new Error(await parseError(r));
      const d = await r.json();
      toast(`✓ Renamed → ${d.filename}`, 'ok');
      // The file moved — drop it from selection so we don't try to act on a
      // stale name later.
      selected.delete(fname);
      refreshLib();
    } catch (e) {
      toast('✗ rename failed: ' + e.message, 'err');
      input.remove();
      el.style.display = '';
    }
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });
  input.addEventListener('blur', cancel);
}

// ── Albums ────────────────────────────────────────────────────────────────
let albumsByName = {};

function fmtDuration(sec) {
  if (!sec) return '—';
  const s = Math.round(sec);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h > 0
    ? `${h}h ${String(m).padStart(2,'0')}m`
    : `${m}m ${String(ss).padStart(2,'0')}s`;
}

// Tracks selected album filenames for the bulk-action bar (mirrors `selected`).
let albumsSelected = new Set();

function updateAlbumsBulkBar() {
  const bar = document.getElementById('albums-bulk-bar');
  const cnt = document.getElementById('albums-bulk-count');
  if (!bar || !cnt) return;
  cnt.textContent = albumsSelected.size;
  bar.classList.toggle('hidden', albumsSelected.size === 0);
  const total = Object.keys(albumsByName).length;
  const checkAll = document.getElementById('albums-check-all');
  if (checkAll) checkAll.checked = total > 0 && albumsSelected.size === total;
}

function toggleAlbumRow(fname, checked) {
  if (checked) albumsSelected.add(fname); else albumsSelected.delete(fname);
  updateAlbumsBulkBar();
}

function toggleAllAlbums(checked) {
  if (checked) Object.keys(albumsByName).forEach(fn => albumsSelected.add(fn));
  else albumsSelected.clear();
  document.querySelectorAll('.album-row-check').forEach(cb => { cb.checked = checked; });
  updateAlbumsBulkBar();
}

function clearAlbumsSelection() {
  albumsSelected.clear();
  refreshAlbums();
  updateAlbumsBulkBar();
}

async function bulkDeleteAlbums() {
  if (albumsSelected.size === 0) return;
  if (!confirm(`Delete ${albumsSelected.size} album(s)? Sides remain in the library.`)) return;
  const names = [...albumsSelected];
  for (const fn of names) {
    try { await fetch(`/api/albums/${encodeURIComponent(fn)}`, { method: 'DELETE' }); }
    catch (e) { console.error(e); }
  }
  toast(`✓ Deleted ${names.length} album${names.length === 1 ? '' : 's'}`, 'ok');
  albumsSelected.clear();
  refreshAlbums();
}

async function refreshAlbums() {
  try {
    const r = await fetch('/api/albums');
    const d = await r.json();
    albumsByName = {};
    (d.albums || []).forEach(a => albumsByName[a.filename] = a);
    [...albumsSelected].forEach(fn => { if (!albumsByName[fn]) albumsSelected.delete(fn); });
    refreshAlbumsRender();
  } catch (e) { console.error(e); }
}

function refreshAlbumsRender() {
  const all = Object.values(albumsByName);
  const filtered = sortFiles(all.filter(rowMatches));
  const total = all.length;
  const shown = filtered.length;
  const filterActive = !!libFilterText.trim();
  // Hide the section entirely only when there are zero albums to begin with.
  // While filtering, keep the section visible so the user sees the "0 of N"
  // count rather than the section vanishing under them.
  const section = document.getElementById('albums-section');
  if (section) section.hidden = total === 0;
  const countEl = document.getElementById('albums-count');
  if (countEl) {
    countEl.textContent = filterActive
      ? `${shown} of ${total} album${total === 1 ? '' : 's'}`
      : `${total} album${total === 1 ? '' : 's'}`;
  }
  const tbody = document.getElementById('albums-tbody');
  if (!tbody) return;
  if (!filtered.length) {
    const colspan = tbody.parentElement.querySelector('thead tr').children.length;
    const msg = total === 0 ? 'No albums yet.' : 'No matches for current filter.';
    tbody.innerHTML = `<tr><td colspan="${colspan}" class="empty-lib">${msg}</td></tr>`;
    updateAlbumsBulkBar();
    return;
  }
  tbody.innerHTML = filtered.map(a => {
      const fn = htmlEscape(a.filename);
      const isSel = albumsSelected.has(a.filename) ? 'checked' : '';
      const sidesCell = a.track_count
        ? `${a.side_count || '—'} · <a class="track-count-link" onclick="toggleTracks('${fn}')">${a.track_count} tracks</a>`
        : `${a.side_count || '—'}`;
      const splitTitle = a.track_count ? 'Re-split into tracks' : 'Split into tracks';
      const playing = previewIs(a.filename, 'album') ? 'playing' : '';
      const playGlyph = previewIs(a.filename, 'album') ? '⏸' : '▶';
      return `
      <tr data-album="${fn}">
        <td class="col-check"><input type="checkbox" class="album-row-check" data-fname="${fn}" ${isSel}
            onclick="toggleAlbumRow(this.dataset.fname, this.checked)"></td>
        <td style="font-weight:500">
          <div class="row-title">
            <span class="row-thumb"><img src="/api/file-cover/${encodeURIComponent(a.filename)}" loading="lazy" onerror="this.remove()"></span>
            <span class="row-title-text">${htmlEscape(a.album || a.filename.replace('.flac',''))}</span>
          </div>
        </td>
        <td style="color:var(--muted)">${htmlEscape(a.artist || '—')}</td>
        <td style="color:var(--muted)">${htmlEscape(a.year || '—')}</td>
        <td style="color:var(--muted);white-space:nowrap" title="${htmlEscape(fmtDateFull(a.mtime))}">${htmlEscape(fmtDate(a.mtime))}</td>
        <td style="color:var(--muted)">${fmtDuration(a.duration_seconds)}</td>
        <td style="color:var(--muted)">${a.size_mb} MB</td>
        <td style="color:var(--muted);font-variant-numeric:tabular-nums" title="bit depth / sample rate (kHz)">${fmtSourceFormat(a)}</td>
        <td style="color:var(--muted)">${sidesCell}</td>
        <td style="white-space:nowrap;text-align:right">
          <button class="icon-btn preview-btn ${playing}" data-fname="${fn}" data-kind="album" title="Preview" onclick="togglePreview(this.dataset.fname, this.dataset.kind)">${playGlyph}</button>
          <button class="icon-btn" title="Edit tags" onclick="openTagAlbum('${fn}')">✎</button>
          <button class="icon-btn" title="${splitTitle}" onclick="openWaveEditor('${fn}')">⌇</button>
          <a class="icon-btn" href="/api/download/${encodeURIComponent(a.filename)}" download title="Download">↓</a>
          <button class="icon-btn danger" title="Delete album" onclick="deleteAlbum('${fn}')">✕</button>
        </td>
      </tr>`;
  }).join('');
  updateAlbumsBulkBar();
}

function openTagAlbum(fname) {
  // The tag panel is keyed off filesByName; albums live in albumsByName.
  // Mirror the album entry into filesByName so openTag finds it.
  const a = albumsByName[fname];
  if (!a) return;
  filesByName[fname] = a;
  openTag(fname);
}

async function deleteAlbum(fname) {
  if (!confirm(`Delete album ${fname}? Sides remain in the library.`)) return;
  const r = await fetch(`/api/albums/${encodeURIComponent(fname)}`, { method: 'DELETE' });
  if (r.ok) {
    toast(`✓ Album deleted — ${fname}`, 'ok');
    refreshAlbums();
  } else {
    toast('✗ delete failed', 'err');
  }
}

// ── Inline preview (library + album rows) ────────────────────────────────
// Single shared <audio>; clicking ▶ on a different row swaps the source.
// Keyed on (kind, fname) so a side and an album sharing a filename don't
// collide on either the visual badge or the download URL.
const preview = { audio: null, fname: null, kind: null };

function previewIs(fname, kind) { return preview.fname === fname && preview.kind === kind; }

function _refreshPreviewButtons() {
  document.querySelectorAll('.preview-btn').forEach(btn => {
    const on = btn.dataset.fname === preview.fname && btn.dataset.kind === preview.kind;
    btn.classList.toggle('playing', on);
    btn.textContent = on ? '⏸' : '▶';
  });
}

function togglePreview(fname, kind) {
  if (preview.fname === fname && preview.kind === kind) { stopPreview(); return; }
  stopPreview(/*silent=*/true);
  preview.fname = fname;
  preview.kind  = kind;
  if (!preview.audio) {
    preview.audio = new Audio();
    preview.audio.addEventListener('ended', () => stopPreview());
  }
  const qs = kind === 'album' ? '?source=album' : '';
  preview.audio.src = '/api/download/' + encodeURIComponent(fname) + qs;
  preview.audio.play().catch(e => {
    toast('✗ preview failed: ' + e.message, 'err');
    stopPreview();
  });
  _refreshPreviewButtons();
}

function togglePreviewTrack(album, trackname) {
  const key = album + '|' + trackname;
  if (preview.fname === key && preview.kind === 'track') { stopPreview(); return; }
  stopPreview(/*silent=*/true);
  preview.fname = key;
  preview.kind  = 'track';
  if (!preview.audio) {
    preview.audio = new Audio();
    preview.audio.addEventListener('ended', () => stopPreview());
  }
  preview.audio.src = '/api/album/' + encodeURIComponent(album) + '/track/' + encodeURIComponent(trackname);
  preview.audio.play().catch(e => {
    toast('✗ preview failed: ' + e.message, 'err');
    stopPreview();
  });
  _refreshPreviewButtons();
}

function stopPreview(silent) {
  if (preview.audio) {
    try { preview.audio.pause(); preview.audio.src = ''; } catch (e) {}
  }
  preview.fname = null;
  preview.kind  = null;
  if (!silent) _refreshPreviewButtons();
}

// ── Combine modal ─────────────────────────────────────────────────────────
let combineOrder = [];

function openCombine() {
  if (selected.size < 2) return;
  // Default order: oldest recorded first (typical A→B→C→D).
  combineOrder = [...selected].sort((a, b) =>
    (filesByName[a]?.mtime || 0) - (filesByName[b]?.mtime || 0)
  );
  // Pre-fill metadata from the most-tagged side (artist+album wins, then artist).
  const score = f => (f.artist ? 2 : 0) + (f.album ? 1 : 0);
  const seed = combineOrder
    .map(fn => filesByName[fn])
    .filter(Boolean)
    .sort((a, b) => score(b) - score(a))[0] || {};
  document.getElementById('c-album').value  = seed.album  || '';
  document.getElementById('c-artist').value = seed.artist || '';
  document.getElementById('c-year').value   = seed.year   || '';
  document.getElementById('c-genre').value  = seed.genre  || '';
  document.getElementById('c-label').value  = seed.label  || '';
  renderCombineSides();
  document.getElementById('combine-modal').hidden = false;
  document.addEventListener('keydown', combineEscHandler);
}

function closeCombine() {
  document.getElementById('combine-modal').hidden = true;
  document.removeEventListener('keydown', combineEscHandler);
}
const combineEscHandler = makeModalEscHandler(closeCombine);

function renderCombineSides() {
  const host = document.getElementById('combine-sides');
  host.innerHTML = combineOrder.map((fn, i) => {
    const f = filesByName[fn] || {};
    const label = f.album ? `${f.album}${f.artist ? ' · ' + f.artist : ''}` : fn;
    const recorded = f.mtime ? fmtDate(f.mtime) : '—';
    const isFirst = i === 0, isLast = i === combineOrder.length - 1;
    return `
      <div class="side-row" draggable="true" data-i="${i}"
           ondragstart="combineDragStart(event, ${i})"
           ondragover="combineDragOver(event, ${i})"
           ondragleave="combineDragLeave(event)"
           ondrop="combineDrop(event, ${i})"
           ondragend="combineDragEnd(event)">
        <div class="drag-handle" title="Drag to reorder">≡</div>
        <div class="num">${i + 1}.</div>
        <div class="name" title="${htmlEscape(fn)}">${htmlEscape(label)}</div>
        <div class="meta">${htmlEscape(recorded)} · ${f.size_mb || '—'} MB</div>
        <div class="arrows">
          <button class="arrow-btn" onclick="moveSide(${i}, -1)" ${isFirst ? 'disabled' : ''} title="Move up">▲</button>
          <button class="arrow-btn" onclick="moveSide(${i},  1)" ${isLast  ? 'disabled' : ''} title="Move down">▼</button>
        </div>
      </div>`;
  }).join('');
}

function moveSide(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= combineOrder.length) return;
  [combineOrder[i], combineOrder[j]] = [combineOrder[j], combineOrder[i]];
  renderCombineSides();
}

// HTML5 drag-and-drop. The arrow buttons stay as a fallback for keyboard /
// touch users.
let combineDragFrom = null;
function combineDragStart(e, i) {
  combineDragFrom = i;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(i));
  e.currentTarget.classList.add('dragging');
}
function combineDragOver(e, i) {
  if (combineDragFrom == null || combineDragFrom === i) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drag-over');
}
function combineDragLeave(e) { e.currentTarget.classList.remove('drag-over'); }
function combineDrop(e, j) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  const i = combineDragFrom;
  combineDragFrom = null;
  if (i == null || i === j) return;
  const item = combineOrder.splice(i, 1)[0];
  combineOrder.splice(j, 0, item);
  renderCombineSides();
}
function combineDragEnd(e) {
  combineDragFrom = null;
  e.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.side-row.drag-over').forEach(r => r.classList.remove('drag-over'));
}

async function runCombine() {
  const album = {
    artist: document.getElementById('c-artist').value.trim(),
    album:  document.getElementById('c-album').value.trim(),
    year:   document.getElementById('c-year').value.trim(),
    genre:  document.getElementById('c-genre').value.trim(),
    label:  document.getElementById('c-label').value.trim(),
  };
  if (!album.artist || !album.album) {
    toast('✗ Combine needs at least artist + album', 'err');
    return;
  }
  const btn = document.getElementById('combine-go');
  const bar = document.getElementById('combine-bar');
  btn.disabled = true; btn.textContent = 'combining…';
  showBar(bar, 'encoding');
  try {
    const d = await withJobProgress(bar, async (jobId) => {
      const r = await fetch('/api/combine', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ filenames: combineOrder, album, job_id: jobId }),
      });
      if (!r.ok) throw new Error(await parseError(r));
      return r.json();
    });
    toast(`✓ Combined ${combineOrder.length} sides · ${fmtDuration(d.duration_seconds)}`, 'ok');
    closeCombine();
    selected.clear();
    refreshLib();
    refreshAlbums();
  } catch (e) {
    toast('✗ combine failed: ' + e.message, 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'combine';
    hideBar(bar);
  }
}

// ── Promote modal ─────────────────────────────────────────────────────────
let promoteFilename = null;

function openPromote(fname) {
  const f = filesByName[fname];
  if (!f) return;
  promoteFilename = fname;
  document.getElementById('p-album').value  = f.album  || '';
  document.getElementById('p-artist').value = f.artist || '';
  document.getElementById('p-year').value   = f.year   || '';
  document.getElementById('p-genre').value  = f.genre  || '';
  document.getElementById('p-label').value  = f.label  || '';
  const label = f.album ? `${f.album}${f.artist ? ' · ' + f.artist : ''}` : fname;
  const recorded = f.mtime ? fmtDate(f.mtime) : '—';
  document.getElementById('promote-source').innerHTML = `
    <div class="side-row">
      <div class="num">·</div>
      <div class="name" title="${htmlEscape(fname)}">${htmlEscape(label)}</div>
      <div class="meta">${htmlEscape(recorded)} · ${f.size_mb || '—'} MB</div>
    </div>`;
  document.getElementById('promote-modal').hidden = false;
  document.addEventListener('keydown', promoteEscHandler);
}

function closePromote() {
  document.getElementById('promote-modal').hidden = true;
  document.removeEventListener('keydown', promoteEscHandler);
  promoteFilename = null;
}
const promoteEscHandler = makeModalEscHandler(closePromote);

async function runPromote() {
  if (!promoteFilename) return;
  const album = {
    artist: document.getElementById('p-artist').value.trim(),
    album:  document.getElementById('p-album').value.trim(),
    year:   document.getElementById('p-year').value.trim(),
    genre:  document.getElementById('p-genre').value.trim(),
    label:  document.getElementById('p-label').value.trim(),
  };
  if (!album.artist || !album.album) {
    toast('✗ Promote needs at least artist + album', 'err');
    return;
  }
  const btn = document.getElementById('promote-go');
  const bar = document.getElementById('promote-bar');
  btn.disabled = true; btn.textContent = 'promoting…';
  bar.hidden = false;
  try {
    const r = await fetch('/api/promote', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ filename: promoteFilename, album }),
    });
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    toast(`✓ Promoted → ${d.filename} · ${fmtDuration(d.duration_seconds)}`, 'ok');
    selected.delete(promoteFilename);
    closePromote();
    refreshLib();
    refreshAlbums();
  } catch (e) {
    toast('✗ promote failed: ' + e.message, 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'promote';
    bar.hidden = true;
  }
}

// ── Tag panel ─────────────────────────────────────────────────────────────
let tagPanelMbid = null;        // mbid of currently-picked candidate (drives cover embed on apply)
let tagPanelDiscogsId = null;   // Discogs release id — persisted so the wave editor can auto-load tracks later
let tagPanelCandidates = [];

function setLeft(fields) {
  document.getElementById('t-album').value   = fields.album   ?? '';
  document.getElementById('t-artist').value  = fields.artist  ?? '';
  document.getElementById('t-year').value    = fields.year    ?? '';
  document.getElementById('t-genre').value   = fields.genre   ?? '';
  document.getElementById('t-label').value   = fields.label   ?? '';
  document.getElementById('t-catno').value   = fields.catalog_number ?? '';
  document.getElementById('t-country').value = fields.country ?? '';
  document.getElementById('t-format').value  = fields.format  ?? '';
  if (fields.tracks !== undefined) {
    const arr = Array.isArray(fields.tracks) ? fields.tracks
      : String(fields.tracks || '').split(' / ').filter(Boolean);
    document.getElementById('t-tracks').value = arr.join('\n');
  }
}

function setCover(url) {
  const box = document.getElementById('cover-preview');
  if (!url) { box.innerHTML = 'no cover'; return; }
  const img = new Image();
  img.onload = () => { box.innerHTML = ''; box.appendChild(img); };
  img.onerror = () => { box.innerHTML = 'no cover'; };
  img.src = url;
}

function openTag(fname) {
  const f = filesByName[fname];
  if (!f) return;
  tagPanelMbid = null;
  tagPanelDiscogsId = null;
  tagPanelCandidates = [];
  document.getElementById('tag-filename').textContent = fname;
  setLeft({
    album: f.album, artist: f.artist, year: f.year, genre: f.genre,
    label: f.label, catalog_number: f.catalog_number, country: f.country,
    format: '', tracks: f.tracks,
  });
  setCover(null);
  // Pre-fill the search query from existing tags so a single click runs the search.
  const q = [f.artist, f.album].filter(Boolean).join(' ');
  document.getElementById('t-search-q').value = q;
  document.getElementById('t-candidates').innerHTML =
    '<div class="empty-results">Hit search to look up this album on MusicBrainz.</div>';
  document.getElementById('t-search-status').textContent = '';
  document.getElementById('tag-modal').dataset.fname = fname;
  document.getElementById('tag-modal').hidden = false;
  document.addEventListener('keydown', tagEscHandler);
}

function closeTag() {
  document.getElementById('tag-modal').hidden = true;
  document.removeEventListener('keydown', tagEscHandler);
}
const tagEscHandler = makeModalEscHandler(closeTag);

function parseQuery(q) {
  // Split on " - " or just take the first half as artist; user can override fields directly.
  // Falls back to using the whole string as both artist and album hints.
  const parts = q.split(/\s+-\s+|\s+—\s+/);
  if (parts.length >= 2) return { artist: parts[0].trim(), album: parts.slice(1).join(' - ').trim() };
  // Try to split heuristically: if the string has 4+ words, first ~half as artist.
  const words = q.split(/\s+/);
  if (words.length >= 4) {
    const mid = Math.ceil(words.length / 2);
    return { artist: words.slice(0, mid).join(' '), album: words.slice(mid).join(' ') };
  }
  return { artist: q.trim(), album: q.trim() };
}

// Tag-panel candidate state — collection matches live alongside MB matches
// in two parallel arrays. `pickCandidate(i)` indexes into MB; collection
// picks go through `pickCollectionCandidate(release_id)` instead.
let tagPanelCollectionCandidates = [];

function _renderMbCard(c, i) {
  return `
    <div class="candidate" data-i="${i}" onclick="pickCandidate(${i})">
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
          ${c.format ? '<span class="pill">' + htmlEscape(c.format) + '</span>' : ''}
          <a class="ext-link" href="https://musicbrainz.org/release/${c.mbid}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Open on MusicBrainz">↗ MB</a>
        </div>
      </div>
    </div>`;
}

function _renderCollectionCard(c) {
  // External cover image straight from Discogs CDN — no /api/cover proxy
  // path because we only have that for MB releases. The thumb fails open.
  const img = c.cover_url ? `<img src="${htmlEscape(c.cover_url)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : '';
  const dUrl = `https://www.discogs.com/release/${c.discogs_release_id}`;
  return `
    <div class="candidate collection-cand" data-rid="${c.discogs_release_id}" onclick="pickCollectionCandidate(${c.discogs_release_id})">
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
}

async function runSearch() {
  const q = document.getElementById('t-search-q').value.trim();
  if (!q) return;
  // Prefer the typed left-side fields if they're filled, since they'll be more precise.
  const leftArtist = document.getElementById('t-artist').value.trim();
  const leftAlbum  = document.getElementById('t-album').value.trim();
  const body = (leftArtist || leftAlbum) ? { artist: leftArtist, album: leftAlbum } : parseQuery(q);
  const list = document.getElementById('t-candidates');
  document.getElementById('t-search-status').textContent = 'searching MusicBrainz…';
  list.innerHTML = '';
  try {
    const r = await fetch('/api/search', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    tagPanelCandidates = d.candidates || [];
    tagPanelCollectionCandidates = d.collection_candidates || [];
    const mbN = tagPanelCandidates.length;
    const colN = tagPanelCollectionCandidates.length;
    if (!mbN && !colN) {
      list.innerHTML = '<div class="empty-results">No matches. Try editing the search above.</div>';
      document.getElementById('t-search-status').textContent = '';
      return;
    }
    const status =
      (colN ? `${colN} from your collection` : '') +
      (colN && mbN ? ' · ' : '') +
      (mbN ? `${mbN} from MusicBrainz` : '') +
      ' — click to load details';
    document.getElementById('t-search-status').textContent = status;
    let html = '';
    if (colN) {
      html += '<div class="cand-section-header">From your collection</div>';
      html += tagPanelCollectionCandidates.map(_renderCollectionCard).join('');
    }
    if (mbN) {
      if (colN) html += '<div class="cand-section-header">MusicBrainz results</div>';
      html += tagPanelCandidates.map((c, i) => _renderMbCard(c, i)).join('');
    }
    list.innerHTML = html;
  } catch (e) {
    list.innerHTML = `<div class="empty-results err">search failed: ${htmlEscape(e.message)}</div>`;
    document.getElementById('t-search-status').textContent = '';
  }
}

async function pickCollectionCandidate(releaseId) {
  const c = tagPanelCollectionCandidates.find(x => x.discogs_release_id === releaseId);
  if (!c) return;
  document.querySelectorAll('.candidate').forEach(el =>
    el.classList.toggle('active', Number(el.dataset.rid) === releaseId));
  document.getElementById('t-search-status').textContent = `loading ${c.title}…`;
  try {
    const r = await fetch(`/api/release/discogs/${releaseId}`);
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    // Picking a Discogs-only candidate means we don't have an MBID to pass
    // to /api/apply (which uses the MBID to fetch CAA cover art). Clear the
    // panel-level mbid so apply doesn't try to embed a stale cover.
    tagPanelMbid = null;
    tagPanelDiscogsId = releaseId;
    setLeft({
      album: d.title, artist: d.artist, year: d.year, genre: d.genre,
      label: d.label, catalog_number: d.catalog_number, country: d.country,
      format: d.format, tracks: d.tracks,
    });
    if (d.cover_url) setCover(d.cover_url);
    const links = d.discogs_url
      ? `<a class="ext-link" href="${d.discogs_url}" target="_blank" rel="noopener">↗ Discogs</a>`
      : '';
    document.getElementById('t-search-status').innerHTML =
      `loaded · from your collection · ${links}`;
  } catch (e) {
    document.getElementById('t-search-status').textContent = 'load failed: ' + e.message;
  }
}

async function refreshCollection() {
  const btn = document.getElementById('t-collection-refresh');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/collection/refresh', { method: 'POST' });
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    toast(`✓ Discogs collection refreshed (${d.count} releases)`, 'ok');
    // Re-run the current search so the new cache is reflected immediately.
    if (document.getElementById('t-search-q').value.trim()) runSearch();
  } catch (e) {
    toast('✗ ' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function pickCandidate(i) {
  const c = tagPanelCandidates[i];
  if (!c) return;
  document.querySelectorAll('.candidate').forEach((el, j) => el.classList.toggle('active', j === i));
  document.getElementById('t-search-status').textContent = `loading ${c.title}…`;
  try {
    const r = await fetch(`/api/release/${c.mbid}`);
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    tagPanelMbid = d.mbid;
    tagPanelDiscogsId = d.discogs_id || null;
    setLeft({
      album: d.title, artist: d.artist, year: d.year, genre: d.genre,
      label: d.label, catalog_number: d.catalog_number, country: d.country,
      format: d.format, tracks: d.tracks,
    });
    setCover(d.cover_url);
    const mbHref = `https://musicbrainz.org/release/${d.mbid}`;
    const links = [
      `<a class="ext-link" href="${mbHref}" target="_blank" rel="noopener">↗ MusicBrainz</a>`,
      d.discogs_url ? `<a class="ext-link" href="${d.discogs_url}" target="_blank" rel="noopener">↗ Discogs</a>` : '',
    ].filter(Boolean).join(' · ');
    document.getElementById('t-search-status').innerHTML =
      `${d.discogs_id ? 'loaded · enriched from Discogs' : 'loaded · MB only'} · ${links}`;
  } catch (e) {
    document.getElementById('t-search-status').textContent = 'load failed: ' + e.message;
  }
}

async function applyTagPanel() {
  const fname = document.getElementById('tag-modal').dataset.fname;
  if (!fname) return;
  const tracks = document.getElementById('t-tracks').value
    .split('\n').map(s => s.trim()).filter(Boolean);
  const fields = {
    artist:         document.getElementById('t-artist').value.trim(),
    album:          document.getElementById('t-album').value.trim(),
    year:           document.getElementById('t-year').value.trim(),
    genre:          document.getElementById('t-genre').value.trim(),
    label:          document.getElementById('t-label').value.trim(),
    catalog_number: document.getElementById('t-catno').value.trim(),
    country:        document.getElementById('t-country').value.trim(),
    tracks,
  };
  if (!fields.artist || !fields.album) {
    toast('✗ Need at least artist + album', 'err');
    return;
  }
  try {
    const r = await fetch('/api/apply', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        filename: fname, fields,
        mbid: tagPanelMbid,
        discogs_release_id: tagPanelDiscogsId,
      })
    });
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    toast(`✓ Tagged → ${d.filename}`, 'ok');
    closeTag();
    if (d.album) refreshAlbums();
    else         refreshLib();
  } catch (e) { toast('✗ ' + e.message, 'err'); }
}

// ── Disk-space marker ─────────────────────────────────────────────────────
// Threshold comes from /api/config; default mirrors the server constant so
// the marker still flips red if the config request hasn't returned yet.
let lowSpaceGb = 2.0;

function updateDiskFree(gb) {
  const el = document.getElementById('disk-free');
  if (gb == null) { el.textContent = '— GB free'; el.classList.remove('low'); return; }
  el.textContent = gb + ' GB free';
  el.classList.toggle('low', gb < lowSpaceGb);
}

function renderVersion(v) {
  const el = document.getElementById('version-tag');
  if (!v) return;
  el.textContent = v;
  // git-describe between two tags looks like "v0.1.0-5-gabc1234"; that's a dev
  // build. A bare 7-char sha (no leading "v") is also a dev build.
  const isDev = /-g[0-9a-f]{7,}|-dirty|^[0-9a-f]{7,}$/i.test(v) || v === 'dev';
  el.classList.toggle('dev', isDev);
  el.title = isDev ? 'dev build' : 'release';
  el.hidden = false;
}

// ── Init ──────────────────────────────────────────────────────────────────
async function applyConfig() {
  try {
    const r = await fetch('/api/config');
    const c = await r.json();
    if (c.default_stream_url) {
      document.getElementById('stream-url').value = c.default_stream_url;
    }
    if (typeof c.default_gain_db === 'number') {
      pendingDefaultGainDb = c.default_gain_db;
    }
    if (typeof c.low_space_gb === 'number') lowSpaceGb = c.low_space_gb;
    renderVersion(c.version);
    // Wave-editor split defaults — applied to the modal whenever it's reopened.
    if (typeof c.default_split_normalize === 'boolean') {
      document.getElementById('we-normalize').checked = c.default_split_normalize;
    }
    if (typeof c.default_split_target_peak_db === 'number') {
      we.targetPeakDb = c.default_split_target_peak_db;
    }
    if (typeof c.default_split_bit_depth === 'number') {
      const sel = document.getElementById('we-bitdepth');
      const v = String(c.default_split_bit_depth);
      if ([...sel.options].some(o => o.value === v)) sel.value = v;
    }
    // Surface the "↻ collection" refresh button only when the server has
    // a Discogs username configured. The collection section itself is
    // server-driven (empty array → no header rendered) so no UI flag is
    // needed for that.
    if (c.discogs_collection_enabled) {
      const btn = document.getElementById('t-collection-refresh');
      if (btn) btn.hidden = false;
    }
    // auto_connect is now handled server-side at app startup; nothing to do
    // here besides letting the WS hello replay tell us the current state.
  } catch(e) { console.error('config fetch failed', e); }
}

// ── WebSocket-driven shared state ─────────────────────────────────────────
// One WS per tab. Carries VU peaks, clip latches, server-connect state,
// recording lifecycle, and shared log lines. The hello frame replays the
// current state so a fresh / refreshed tab catches up immediately.
let ws = null, wsReconnectMs = 1000;

function wsConnect() {
  const proto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/api/ws`);
  ws.onopen = () => { wsReconnectMs = 1000; };
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch(e) { return; }
    handleWsEvent(m);
  };
  ws.onclose = () => {
    // Decay meters and gray out status while disconnected.
    upstreamConnected = false;
    peak.L = peak.R = 0; lvl.L = lvl.R = 0;
    setTimeout(wsConnect, wsReconnectMs);
    wsReconnectMs = Math.min(wsReconnectMs * 2, 8000);
  };
  ws.onerror = () => { try { ws.close(); } catch(e) {} };
}

function handleWsEvent(m) {
  switch (m.type) {
    case 'hello': {
      // Replay buffered log lines (these don't get re-broadcast, so we
      // only render them once at connect time).
      if (Array.isArray(m.log)) {
        for (const e of m.log) renderLog(e.msg, e.level);
      }
      if (m.upstream) applyUpstreamState({
        connected: m.upstream.connected,
        fmt:       m.upstream.format,
      });
      if (m.upstream && m.upstream.url) {
        // Reflect the actually-connected URL into the input so the tab
        // shows what the server is using.
        const inp = document.getElementById('stream-url');
        if (inp && !document.activeElement?.isSameNode?.(inp)) inp.value = m.upstream.url;
      }
      if (m.upstream) {
        setClipBadge('L', !!m.upstream.clipped_l);
        setClipBadge('R', !!m.upstream.clipped_r);
        // Replay the last health snapshot if any. Empty object means we
        // haven't received a tick yet (just-connected); render `--` until
        // the first tick comes in.
        const h = m.upstream.health;
        applyHealthState(h && Object.keys(h).length ? h : null);
      }
      // Recover recording state on refresh / new tab.
      if (m.record && m.record.recording && m.record.sessions[0]) {
        const s = m.record.sessions[0];
        applyRecordState({
          active: true, paused: !!s.paused, sid: s.id,
          durationSec: s.duration, elapsedSec: s.elapsed,
        });
      } else {
        applyRecordState({ active: false });
      }
      // Run gain probe if upstream is connected and we have a URL.
      if (m.upstream && m.upstream.connected && m.upstream.url) {
        probeGain(m.upstream.url);
      }
      break;
    }
    case 'vu':
      updateMeter('L', m.peak_l || 0);
      updateMeter('R', m.peak_r || 0);
      // Server is the source of truth for clip latches; mirror.
      setClipBadge('L', !!m.clipped_l);
      setClipBadge('R', !!m.clipped_r);
      break;
    case 'clip':
      setClipBadge('L', !!m.clipped_l);
      setClipBadge('R', !!m.clipped_r);
      break;
    case 'upstream':
      applyUpstreamState({ connected: m.connected, fmt: m.format });
      if (m.connected && m.url) probeGain(m.url);
      break;
    case 'health':
      applyHealthState(m);
      break;
    case 'record':
      if (m.event === 'start') {
        applyRecordState({
          active: true, paused: false, sid: m.session_id,
          durationSec: m.duration, elapsedSec: 0,
        });
        // Server emits the log line via bus.log so it lands in the ring
        // buffer and replays to fresh tabs; record-state UI is the visible
        // feedback (red dot + timer), so no toast needed here.
      } else if (m.event === 'stop') {
        applyRecordState({ active: false });
        refreshLib().catch(() => {});
        // If this tab started the recording, open the tag panel as before.
        if (m.session_id && m.session_id === sessionId) openTag(m.filename);
        // applyRecordState already reset sessionId; clear any owner tag.
      } else if (m.event === 'pause') {
        // Server-authoritative elapsed — local clock would over-report after
        // a resume because recStartTimeMs isn't slid during the pause.
        applyRecordState({
          active: true, paused: true, sid: m.session_id || sessionId,
          durationSec: recDurationSec,
          elapsedSec: typeof m.elapsed === 'number' ? m.elapsed
                      : Math.floor((Date.now() - recStartTimeMs) / 1000),
        });
      } else if (m.event === 'resume') {
        applyRecordState({
          active: true, paused: false, sid: m.session_id || sessionId,
          durationSec: recDurationSec,
          elapsedSec: typeof m.elapsed === 'number' ? m.elapsed
                      : Math.floor((Date.now() - recStartTimeMs) / 1000),
        });
      }
      break;
    case 'log':
      renderLog(m.msg, m.level);
      break;
    case 'ping':
      break;
  }
}

function renderLog(msg, level) {
  log(msg, level || '');
}

// Periodic disk-free refresh — server pushes nothing for this since it
// changes slowly; a 30s poll is fine.
async function refreshDiskFree() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    updateDiskFree(d.disk_free_gb);
  } catch(e) {}
}

applyConfig();
ensureAudioGraph();
applyMuteState();
wsConnect();
refreshLib();
refreshAlbums();
refreshDiskFree();
setInterval(() => { refreshLib(); refreshAlbums(); }, 15000);
setInterval(refreshDiskFree, 30000);
