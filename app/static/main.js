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
  // The mask hides the unfilled portion of the bar, peak marks the latched
  // hold position. Both are written as CSS custom properties so the same
  // values drive horizontal (default) and vertical (collapsed-rail) tracks
  // without JS having to know which orientation is active.
  document.getElementById('mask-' + ch).style.setProperty('--vu-fill', (100 - pct) + '%');
  document.getElementById('peak-' + ch).style.setProperty('--vu-peak', Math.min(peak[ch] * 100, 99.5) + '%');
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

// Library "Recorded" column. Time-of-day for today, "MMM D" for older
// same-year rows (keeps the column narrow), year-bearing for prior years.
// fmtDateFull is used for the cell tooltip so the full timestamp is always
// one hover away.
function fmtDate(unix) {
  if (!unix) return '—';
  const d = new Date(unix * 1000);
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
  if (sameDay) {
    return d.toLocaleString(undefined, { hour: 'numeric', minute: '2-digit' });
  }
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleString(undefined, sameYear
    ? { month: 'short', day: 'numeric' }
    : { year: 'numeric', month: 'short', day: 'numeric' });
}
function fmtDateFull(unix) {
  if (!unix) return '';
  return new Date(unix * 1000).toLocaleString();
}

// Compact source-format readout for library/album tables: "24b / 96 ksps",
// "16b / 44.1 ksps". `b` = bit depth, `ksps` = kilo-samples-per-second.
// Returns "—" when the FLAC didn't expose readable format info.
function fmtSourceFormat(f) {
  if (!f.bit_depth || !f.sample_rate_khz) return '—';
  const sr = Number.isInteger(f.sample_rate_khz)
    ? f.sample_rate_khz
    : f.sample_rate_khz.toFixed(1);
  return `${f.bit_depth}b / ${sr} ksps`;
}

// ── Log helper ────────────────────────────────────────────────────────────
// The log panel is collapsed by default — most users never need it. The
// `<details>` summary still surfaces the most recent line as a one-liner so
// the latest event is visible without expanding. Logging always writes to
// the panel; only its visibility is gated by the `<details>` open state.
function log(msg, cls='') {
  const el = document.getElementById('log');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
  // trim old lines
  while (el.children.length > 40) el.removeChild(el.firstChild);
  // Mirror the latest line into the collapsed-state summary so the user can
  // see the most recent event without expanding.
  const tail = document.getElementById('log-tail');
  if (tail) {
    tail.textContent = msg;
    if (cls) tail.className = 'log-tail ' + cls;
    else tail.className = 'log-tail';
  }
}

// Persist the user's expanded state across reloads. Default = collapsed.
const LOG_KEY = 'vr.log.expanded';
function _wireLogCollapse() {
  const det = document.getElementById('log-details');
  if (!det) return;
  let saved = null;
  try { saved = localStorage.getItem(LOG_KEY); } catch (_) {}
  // Default to collapsed; only restore "open" if explicitly remembered.
  det.open = (saved === '1');
  det.addEventListener('toggle', () => {
    try { localStorage.setItem(LOG_KEY, det.open ? '1' : '0'); } catch (_) {}
  });
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
    stext.textContent = upstreamConnected ? 'connected' : 'disconnected';
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
    document.getElementById('stext').textContent = connected ? 'connected' : 'disconnected';
  }
  // Chevron is the click affordance for the health panel; only show it when
  // there's something to see (connected). The status-indicator itself is a
  // <button>, always clickable — the chevron just indicates the panel exists.
  const chevron = document.getElementById('health-chevron');
  if (chevron) chevron.hidden = !connected;
  updateSdot();
  if (!connected) {
    // Decay meters; clear gain slider since the Pi probe needs reconnect.
    peak.L = peak.R = 0; lvl.L = lvl.R = 0;
    hideGain();
    applyHealthState(null);
    // Auto-collapse the panel on disconnect so it doesn't linger empty.
    const panel = document.getElementById('health-panel');
    if (panel) panel.hidden = true;
    const ind = document.getElementById('status-indicator');
    if (ind) ind.setAttribute('aria-expanded', 'false');
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

// ── Header kebab menu ─────────────────────────────────────────────────────
// Single dropdown anchored to the ⋮ trigger; click-outside or ESC closes
// it, picking an item closes it too (the item handler calls closeHeaderMenu
// inline). Held in a separate function pair so a future second item doesn't
// have to re-derive the open/close logic.
function toggleHeaderMenu(e) {
  if (e) e.stopPropagation();
  const pop = document.getElementById('header-menu-pop');
  const btn = document.getElementById('header-menu-btn');
  if (!pop || !btn) return;
  if (pop.hidden) {
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    document.addEventListener('click', _headerMenuOutsideClick);
    document.addEventListener('keydown', _headerMenuEsc);
  } else {
    closeHeaderMenu();
  }
}
function closeHeaderMenu() {
  const pop = document.getElementById('header-menu-pop');
  const btn = document.getElementById('header-menu-btn');
  if (!pop || pop.hidden) return;
  pop.hidden = true;
  if (btn) btn.setAttribute('aria-expanded', 'false');
  document.removeEventListener('click', _headerMenuOutsideClick);
  document.removeEventListener('keydown', _headerMenuEsc);
}
function _headerMenuOutsideClick(e) {
  const pop = document.getElementById('header-menu-pop');
  if (!pop || pop.hidden) return;
  if (pop.contains(e.target)) return;
  // The trigger button toggles via its own onclick; ignore the same click
  // bubbling here so we don't immediately re-close the menu we just opened.
  const btn = document.getElementById('header-menu-btn');
  if (btn && btn.contains(e.target)) return;
  closeHeaderMenu();
}
function _headerMenuEsc(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    closeHeaderMenu();
    const btn = document.getElementById('header-menu-btn');
    if (btn) try { btn.focus(); } catch (e) {}
  }
}

function toggleHealthPanel() {
  if (!upstreamConnected) return;  // nothing to show until first health tick
  const panel = document.getElementById('health-panel');
  if (!panel) return;
  panel.hidden = !panel.hidden;
  const ind = document.getElementById('status-indicator');
  if (ind) ind.setAttribute('aria-expanded', String(!panel.hidden));
}

// ── Collapsible sidebar ──────────────────────────────────────────────────
// The capture panel can shrink to a 56px rail (REC button + tiny timer + mini
// VU). State persists across reloads; auto-expands when a recording starts so
// the user always sees the full transport view during capture.
const SIDEBAR_KEY = 'sidebarCollapsed';
function applySidebarState(collapsed) {
  const main = document.querySelector('.main');
  if (!main) return;
  if (collapsed) main.setAttribute('data-collapsed', '');
  else main.removeAttribute('data-collapsed');
  const btn = document.getElementById('sidebar-toggle');
  if (btn) {
    btn.setAttribute('aria-expanded', String(!collapsed));
    btn.title = collapsed ? 'Expand panel' : 'Collapse panel';
  }
}
function toggleSidebar() {
  const collapsed = !document.querySelector('.main').hasAttribute('data-collapsed');
  applySidebarState(collapsed);
  try { localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0'); } catch (_) {}
}
document.addEventListener('DOMContentLoaded', () => {
  let saved = null;
  try { saved = localStorage.getItem(SIDEBAR_KEY); } catch (_) {}
  applySidebarState(saved === '1');  // default = expanded
});

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
    // Reflect sort state for screen readers. `aria-sort` on a th is the
    // standard signal — "ascending" / "descending" on the active column,
    // "none" on the others.
    th.setAttribute('aria-sort',
      active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none');
  });
}

// Make `.sortable` headers keyboard-activatable. The HTML uses bare `<th>`
// with `onclick`, which mouse users can hit but keyboard users can't reach
// (TH isn't focusable by default). We add role=button + tabindex on first
// load and forward Enter/Space to the same setSort handler the click uses.
function _wireSortableHeaders() {
  document.querySelectorAll('.lib-table th.sortable').forEach(th => {
    if (th.dataset.kbWired === '1') return;
    th.dataset.kbWired = '1';
    th.setAttribute('role', 'button');
    th.setAttribute('tabindex', '0');
    th.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        setSort(th.dataset.sort);
      }
    });
  });
}

function htmlEscape(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Build an action-cell button. Filenames flow through `data-fname` and the
// onclick reads `this.dataset.fname` instead of inlining the value as a JS
// string literal. The previous `onclick="fn('${htmlEscape(fn)}')"` pattern
// looked safe but isn't: the browser HTML-decodes the attribute BEFORE the
// JS sees it, so `&#39;` becomes a literal `'` and a filename like
// `'); alert(1);//` could break out of the JS string. Going through
// `data-` attributes (HTML escaping is enough; never gets re-parsed as JS)
// closes that whole class of bug.
function _actionBtn(handler, fname, opts = {}) {
  const {label = '', cls = 'icon-btn', danger = false, kind = ''} = opts;
  const cl = danger ? `${cls} danger` : cls;
  const k = kind ? ` data-kind="${htmlEscape(kind)}"` : '';
  // `title` shows on hover; `aria-label` is what screen readers announce for
  // these icon-only buttons (the glyph alone is meaningless to AT).
  const a11y = label ? ` title="${htmlEscape(label)}" aria-label="${htmlEscape(label)}"` : '';
  return `<button class="${cl}" data-fname="${htmlEscape(fname)}"${k}${a11y} onclick="${handler}(this.dataset.fname${kind ? ', this.dataset.kind' : ''})">${opts.glyph || ''}</button>`;
}

function _downloadLink(href, label = 'Download') {
  const lbl = htmlEscape(label);
  return `<a class="icon-btn" href="${href}" download title="${lbl}" aria-label="${lbl}">↓</a>`;
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
  if (combineBtn) {
    combineBtn.disabled = selected.size < 1;
    combineBtn.textContent = selected.size === 1 ? 'tag as album' : 'combine into album';
  }
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

// Cache the last HTML written into each tbody so we can skip the
// `innerHTML = ...` assignment when nothing changed. Rebuilding a table on
// every 15 s poll otherwise blows away scroll position, focus, and the
// inline-rename input.
const _lastTbodyHtml = new Map();
function _setTbodyIfChanged(tbody, html) {
  const id = tbody.id;
  if (_lastTbodyHtml.get(id) === html) return false;
  tbody.innerHTML = html;
  _lastTbodyHtml.set(id, html);
  return true;
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
    _setTbodyIfChanged(tbody, `<tr><td colspan="9" class="empty-lib">${msg}</td></tr>`);
    updateBulkBar();
    return;
  }
  const _libRowsHtml = files.map(f => {
      const fn = htmlEscape(f.filename);
      const isSel = selected.has(f.filename) ? 'checked' : '';
      const playing = previewIs(f.filename, 'lib') ? 'playing' : '';
      const playGlyph = previewIs(f.filename, 'lib') ? '⏸' : '▶';
      const titleText = htmlEscape(f.album || f.filename.replace('.flac',''));
      // Raw rows are by definition untagged: the dblclick rename always
      // applies, the amber accent bar (.row-untagged in style.css) is
      // unconditional. The handler lives on the whole <td> so the entire
      // cell — including padding and whitespace to the right of short
      // titles — is a click target.
      const previewBtn = `<button class="icon-btn preview-btn ${playing}" data-fname="${fn}" data-kind="lib" title="Preview" aria-label="Preview" onclick="togglePreview(this.dataset.fname, this.dataset.kind)">${playGlyph}</button>`;
      const dlLink = _downloadLink(`/api/download/${encodeURIComponent(f.filename)}`, 'Download');
      const delBtn = _actionBtn('deleteFile', f.filename, {label: 'Delete', glyph: '✕', danger: true});
      // Inline rename pencil: same handler the dblclick uses. Filename
      // travels via data-fname (HTML escaped) — see _actionBtn for the
      // XSS rationale on never inlining values into the JS string.
      const renameGlyph = `<button class="rename-glyph" data-fname="${fn}" title="Rename" aria-label="Rename" onclick="event.stopPropagation();startInlineRename(this.dataset.fname, this.previousElementSibling)">✎</button>`;
      return `
      <tr class="row-untagged">
        <td class="col-check" data-col="check"><input type="checkbox" class="row-check" data-fname="${fn}" ${isSel}
            onclick="toggleRow(this.dataset.fname, this.checked)"></td>
        <td data-col="album" style="font-weight:500" ondblclick="startInlineRename(this.dataset.fname, this.querySelector('.row-title-text'))" data-fname="${fn}" title="Double-click to rename">
          <div class="row-title">
            <span class="row-thumb"><img src="/api/file-cover/${encodeURIComponent(f.filename)}" loading="lazy" onerror="this.remove()"></span>
            <span class="row-title-text">${titleText}</span>${renameGlyph}
          </div>
        </td>
        <td data-col="artist" style="color:var(--muted)">${htmlEscape(f.artist || '—')}</td>
        <td data-col="year" style="color:var(--muted)">${htmlEscape(f.year || '—')}</td>
        <td data-col="recorded" style="color:var(--muted);white-space:nowrap" title="${htmlEscape(fmtDateFull(f.mtime))}">${htmlEscape(fmtDate(f.mtime))}</td>
        <td data-col="length" style="color:var(--muted)">${fmtDuration(f.duration_seconds)}</td>
        <td data-col="size" style="color:var(--muted)">${f.size_mb} MB</td>
        <td data-col="fmt" style="color:var(--muted);font-variant-numeric:tabular-nums" title="N-bit / M kHz">${fmtSourceFormat(f)}</td>
        <td data-col="actions" style="white-space:nowrap;text-align:right">${previewBtn}${dlLink}${delBtn}</td>
      </tr>`;
  }).join('');
  _setTbodyIfChanged(tbody, _libRowsHtml);
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
  if (!f) return;
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

// Tracks selected album filenames per section. In-progress (un-split albums)
// and Music (split-completed albums) keep separate selection sets so a
// "delete selected" in one section doesn't pull rows from the other.
let albumsSelected = new Set();   // in-progress section
let musicSelected  = new Set();   // music section

function _albumsBySplit(split) {
  return Object.values(albumsByName).filter(a => !!a.split === !!split);
}

function updateAlbumsBulkBar() {
  const bar = document.getElementById('albums-bulk-bar');
  const cnt = document.getElementById('albums-bulk-count');
  if (!bar || !cnt) return;
  cnt.textContent = albumsSelected.size;
  bar.classList.toggle('hidden', albumsSelected.size === 0);
  const total = _albumsBySplit(false).length;
  const checkAll = document.getElementById('albums-check-all');
  if (checkAll) checkAll.checked = total > 0 && albumsSelected.size === total;
}

function updateMusicBulkBar() {
  const bar = document.getElementById('music-bulk-bar');
  const cnt = document.getElementById('music-bulk-count');
  if (!bar || !cnt) return;
  cnt.textContent = musicSelected.size;
  bar.classList.toggle('hidden', musicSelected.size === 0);
  const total = _albumsBySplit(true).length;
  const checkAll = document.getElementById('music-check-all');
  if (checkAll) checkAll.checked = total > 0 && musicSelected.size === total;
}

function toggleAlbumRow(fname, checked) {
  if (checked) albumsSelected.add(fname); else albumsSelected.delete(fname);
  updateAlbumsBulkBar();
}

function toggleMusicRow(fname, checked) {
  if (checked) musicSelected.add(fname); else musicSelected.delete(fname);
  updateMusicBulkBar();
}

function toggleAllAlbums(checked) {
  if (checked) _albumsBySplit(false).forEach(a => albumsSelected.add(a.album_id));
  else albumsSelected.clear();
  document.querySelectorAll('.album-row-check').forEach(cb => { cb.checked = checked; });
  updateAlbumsBulkBar();
}

function toggleAllMusic(checked) {
  if (checked) _albumsBySplit(true).forEach(a => musicSelected.add(a.album_id));
  else musicSelected.clear();
  document.querySelectorAll('.music-row-check').forEach(cb => { cb.checked = checked; });
  updateMusicBulkBar();
}

function clearAlbumsSelection() {
  albumsSelected.clear();
  refreshAlbums();
  updateAlbumsBulkBar();
}

function clearMusicSelection() {
  musicSelected.clear();
  refreshAlbums();
  updateMusicBulkBar();
}

async function _bulkDeleteAlbumNames(ids, label) {
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} ${label}? Music tracks emitted from these albums will also be removed.`)) return;
  for (const album_id of ids) {
    try { await fetch(`/api/albums/${album_id}`, { method: 'DELETE' }); }
    catch (e) { console.error(e); }
  }
  toast(`✓ Deleted ${ids.length} ${label}`, 'ok');
}

async function bulkDeleteAlbums() {
  const names = [...albumsSelected];
  await _bulkDeleteAlbumNames(names, names.length === 1 ? 'album' : 'albums');
  albumsSelected.clear();
  refreshAlbums();
}

async function bulkDeleteMusic() {
  const names = [...musicSelected];
  await _bulkDeleteAlbumNames(names, names.length === 1 ? 'album' : 'albums');
  musicSelected.clear();
  refreshAlbums();
}

async function refreshAlbums() {
  try {
    const r = await fetch('/api/albums');
    const d = await r.json();
    albumsByName = {};
    (d.albums || []).forEach(a => albumsByName[a.album_id] = a);
    [...albumsSelected].forEach(id => {
      if (!albumsByName[id] || albumsByName[id].split) albumsSelected.delete(id);
    });
    [...musicSelected].forEach(id => {
      if (!albumsByName[id] || !albumsByName[id].split) musicSelected.delete(id);
    });
    // Drop stale failures for albums that no longer exist (deleted/demoted).
    [...albumErrors.keys()].forEach(id => {
      if (!albumsByName[id]) albumErrors.delete(id);
    });
    refreshAlbumsRender();
  } catch (e) { console.error(e); }
}

// ── Per-album failure tracking (client-only, session-scoped) ──────────────
// Long-running album jobs (split, measure, normalize, silence-detect) only
// surface failures via a 4 s toast today, which is easy to miss. We keep a
// session Map<album_id, "<op>: <message>"> populated by the editor's catch
// blocks, render a `.fail-pill` in the row's status cell when set, and clear
// the entry on dismiss / next successful run / album removal.
//
// Trade-off note (in PR description): persistence on the album manifest was
// considered but skipped to keep this PR small. Refreshes / new tabs lose
// the indicator; that's an acceptable cost for the UX win and avoids
// touching the jobs registry or albums.json schema.
const albumErrors = new Map();

function recordAlbumFailure(albumId, op, message) {
  if (!albumId) return;
  const msg = String(message || '').trim() || 'unknown error';
  albumErrors.set(albumId, { op, message: msg, ts: Date.now() });
  // Re-render so the row picks up the pill without waiting for the next
  // poll (15 s); refreshAlbums() pulls fresh data, refreshAlbumsRender()
  // just re-paints from in-memory state.
  refreshAlbumsRender();
}

function clearAlbumFailure(albumId) {
  if (!albumErrors.has(albumId)) return;
  albumErrors.delete(albumId);
  refreshAlbumsRender();
}

// On album success — measure / split / silence — drop any stale failure
// pill so a re-run that worked clears the warning. Called from the editor.
function noteAlbumSuccess(albumId) { clearAlbumFailure(albumId); }

function _albumRowHtml(a, opts) {
  // The "fn" key is the album_id (a slug like 7f3a8c91); HTML escaping is
  // unnecessary (the slug regex is `[a-z0-9_-]+`) but cheap and safe in
  // case a hand-named drop-in dir made it here. All action buttons go
  // through `data-fname` rather than inlining the value as a JS string
  // literal — see the comment on `_actionBtn` for the XSS rationale.
  const fn = htmlEscape(a.album_id);
  const isSel = opts.selected.has(a.album_id) ? 'checked' : '';
  const baseCount = a.split
    ? (a.track_count
        ? `<a class="track-count-link" data-fname="${fn}" onclick="toggleTracks(this.dataset.fname)">${a.track_count} tracks</a>`
        : '—')
    : `${a.side_count || '—'}`;
  // Failure pill — only present when the editor recorded a failure for this
  // album in the current session. Clicking dismisses; the title shows the
  // full server-side message (which can be long).
  const err = albumErrors.get(a.album_id);
  const failPill = err
    ? ` <button class="fail-pill" data-fname="${fn}"
        title="${htmlEscape(err.op + ': ' + err.message)} — click to dismiss"
        aria-label="${htmlEscape('Failed: ' + err.op + ' — click to dismiss')}"
        onclick="clearAlbumFailure(this.dataset.fname)">failed: ${htmlEscape(err.op)}</button>`
    : '';
  const countCell = `${baseCount}${failPill}`;
  const splitTitle = a.split ? 'Re-edit splits' : 'Split into tracks';
  // Demote button is offered on every album; for split albums the dialog
  // warns that music/ stays put.
  const demoteHandler = a.split ? 'demoteAlbumKeepMusic' : 'demoteAlbumDrop';
  const demoteLabel = a.split ? 'Demote to Raw (music/ files preserved)' : 'Demote to Raw';
  const tagBtn = _actionBtn('openTagAlbum', a.album_id, {label: 'Edit tags', glyph: '✎'});
  const splitBtn = _actionBtn('openWaveEditor', a.album_id, {label: splitTitle, glyph: '✂'});
  const demBtn = _actionBtn(demoteHandler, a.album_id, {label: demoteLabel, glyph: '⤺'});
  const delBtn = _actionBtn('deleteAlbum', a.album_id, {label: 'Delete album', glyph: '✕', danger: true});
  return `
  <tr data-album-id="${fn}">
    <td class="col-check" data-col="check"><input type="checkbox" class="${opts.checkClass}" data-fname="${fn}" ${isSel}
        onclick="${opts.toggleRow}(this.dataset.fname, this.checked)"></td>
    <td data-col="album" style="font-weight:500">
      <div class="row-title">
        <span class="row-thumb"><img src="/api/file-cover/${fn}" loading="lazy" onerror="this.remove()"></span>
        <span class="row-title-text">${htmlEscape(a.album || '(untitled album)')}</span>
      </div>
    </td>
    <td data-col="artist" style="color:var(--muted)">${htmlEscape(a.artist || '—')}</td>
    <td data-col="year" style="color:var(--muted)">${htmlEscape(a.year || '—')}</td>
    <td data-col="recorded" style="color:var(--muted);white-space:nowrap" title="${htmlEscape(fmtDateFull(a.mtime))}">${htmlEscape(fmtDate(a.mtime))}</td>
    <td data-col="length" style="color:var(--muted)">${fmtDuration(a.duration_seconds)}</td>
    <td data-col="size" style="color:var(--muted)">${a.size_mb} MB</td>
    <td data-col="fmt" style="color:var(--muted);font-variant-numeric:tabular-nums" title="N-bit / M kHz">${fmtSourceFormat(a)}</td>
    <td data-col="status" style="color:var(--muted)">${countCell}</td>
    <td data-col="actions" style="white-space:nowrap;text-align:right">${tagBtn}${splitBtn}${demBtn}${delBtn}</td>
  </tr>`;
}

// `demoteAlbum(album_id, musicPreserved)` is the underlying call; the row
// renderer can't invoke it via `data-fname` alone because it carries a
// second arg. Wrap it as two single-arg helpers so the data-attribute
// pattern still works.
function demoteAlbumKeepMusic(album_id) { return demoteAlbum(album_id, true); }
function demoteAlbumDrop(album_id)      { return demoteAlbum(album_id, false); }

function _renderAlbumSection(opts) {
  // opts: { all, countId, tbodyId, label, emptyMsg, checkClass,
  //         toggleRow, updateBulkBar, selected }
  const filtered = sortFiles(opts.all.filter(rowMatches));
  const total = opts.all.length;
  const shown = filtered.length;
  const filterActive = !!libFilterText.trim();
  const countEl = document.getElementById(opts.countId);
  if (countEl) {
    countEl.textContent = filterActive
      ? `${shown} of ${total} ${opts.label}${total === 1 ? '' : 's'}`
      : `${total} ${opts.label}${total === 1 ? '' : 's'}`;
  }
  const tbody = document.getElementById(opts.tbodyId);
  if (!tbody) return;
  if (!filtered.length) {
    const colspan = tbody.parentElement.querySelector('thead tr').children.length;
    const msg = total === 0 ? opts.emptyMsg : 'No matches for current filter.';
    _setTbodyIfChanged(tbody, `<tr><td colspan="${colspan}" class="empty-lib">${msg}</td></tr>`);
    opts.updateBulkBar();
    return;
  }
  _setTbodyIfChanged(tbody, filtered.map(a => _albumRowHtml(a, opts)).join(''));
  opts.updateBulkBar();
}

function refreshAlbumsRender() {
  _renderAlbumSection({
    all:           _albumsBySplit(false),
    countId:       'in-progress-count',
    tbodyId:       'albums-tbody',
    label:         'album',
    emptyMsg:      'No albums in progress.',
    checkClass:    'album-row-check',
    toggleRow:     'toggleAlbumRow',
    updateBulkBar: updateAlbumsBulkBar,
    selected:      albumsSelected,
  });
  _renderAlbumSection({
    all:           _albumsBySplit(true),
    countId:       'music-count',
    tbodyId:       'music-tbody',
    label:         'album',
    emptyMsg:      'No split albums yet.',
    checkClass:    'music-row-check',
    toggleRow:     'toggleMusicRow',
    updateBulkBar: updateMusicBulkBar,
    selected:      musicSelected,
  });
}

// Tracks the kind of row currently bound to the tag panel — `{album_id}`
// when retagging an existing album, `{filename}` when promoting a raw side.
// applyTagPanel() reads this to choose the correct /api/apply payload shape.
let tagPanelTarget = null;

function openTagAlbum(album_id) {
  // The tag panel is keyed off filesByName; albums live in albumsByName.
  // Mirror the album entry into filesByName so openTag finds it.
  const a = albumsByName[album_id];
  if (!a) return;
  filesByName[album_id] = a;
  tagPanelTarget = { album_id };
  openTag(album_id);
}

async function deleteAlbum(album_id) {
  const a = albumsByName[album_id];
  const label = (a && a.album) || album_id;
  const splitWarn = (a && a.split)
    ? `\n\nThe music/${a.music_relpath || '...'} folder will be removed too.`
    : '';
  if (!confirm(`Delete album "${label}"?${splitWarn}`)) return;
  const r = await fetch(`/api/albums/${album_id}`, { method: 'DELETE' });
  if (r.ok) {
    toast(`✓ Album deleted — ${label}`, 'ok');
    refreshAlbums();
  } else {
    toast('✗ delete failed', 'err');
  }
}

async function demoteAlbum(album_id, isSplit) {
  const a = albumsByName[album_id];
  const label = (a && a.album) || album_id;
  const sideCount = (a && a.side_count) || '?';
  const tail = isSplit
    ? `\n\nThe already-emitted music/${(a && a.music_relpath) || '...'} folder will be left untouched.`
    : '';
  const msg = `Demote "${label}" back to Raw?\n\n${sideCount} side(s) will be moved into raw/. Album metadata will be discarded.${tail}`;
  if (!confirm(msg)) return;
  const r = await fetch(`/api/album/${album_id}/demote`, { method: 'POST' });
  if (r.ok) {
    toast(`✓ Demoted — ${label}`, 'ok');
    refreshLib();
    refreshAlbums();
  } else {
    toast('✗ demote failed', 'err');
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
  // Albums no longer have a single-file download (they're folders). Library
  // rows (raw sides) keep the same `/api/download/{filename}` route.
  preview.audio.src = '/api/download/' + encodeURIComponent(fname);
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

// ── Combine into album ────────────────────────────────────────────────────
// Combine reuses the tag-panel modal: openCombine sets tagPanelTarget to
// `{ filenames }`, then openTag flips the modal into combine mode (sides
// reorder visible, title/button copy switched). applyTagPanel handles the
// `filenames` target by POSTing to /api/apply, which calls create_album
// under the hood.
let combineOrder = [];

function openCombine() {
  if (selected.size < 1) return;
  // Default order: oldest recorded first (typical A→B→C→D).
  combineOrder = [...selected].sort((a, b) =>
    (filesByName[a]?.mtime || 0) - (filesByName[b]?.mtime || 0)
  );
  // Seed the tag panel from the most-tagged side (artist+album wins, then
  // artist alone), so left fields + the search query pre-fill usefully.
  const score = f => (f.artist ? 2 : 0) + (f.album ? 1 : 0);
  const seed = combineOrder
    .map(fn => filesByName[fn])
    .filter(Boolean)
    .sort((a, b) => score(b) - score(a))[0];
  tagPanelTarget = { filenames: combineOrder.slice() };
  openTag(seed?.filename || combineOrder[0]);
}

function renderCombineSides() {
  const host = document.getElementById('combine-sides');
  host.innerHTML = combineOrder.map((fn, i) => {
    const f = filesByName[fn] || {};
    const label = f.album ? `${f.album}${f.artist ? ' · ' + f.artist : ''}` : fn;
    const recorded = f.mtime ? fmtDate(f.mtime) : '—';
    const isFirst = i === 0, isLast = i === combineOrder.length - 1;
    // Reuses the library's `preview` state — same kind ('lib') so the
    // single shared <audio> + visual badge cover both row sets without
    // a parallel state machine. preview-btn keeps the badge in sync.
    const playing = previewIs(fn, 'lib') ? 'playing' : '';
    const playGlyph = previewIs(fn, 'lib') ? '⏸' : '▶';
    return `
      <div class="side-row" draggable="true" data-i="${i}"
           ondragstart="combineDragStart(event, ${i})"
           ondragover="combineDragOver(event, ${i})"
           ondragleave="combineDragLeave(event)"
           ondrop="combineDrop(event, ${i})"
           ondragend="combineDragEnd(event)">
        <div class="drag-handle" title="Drag to reorder">≡</div>
        <div class="num">${i + 1}.</div>
        <button class="play-side preview-btn ${playing}" data-fname="${htmlEscape(fn)}" data-kind="lib" title="Preview" onclick="togglePreview(this.dataset.fname, this.dataset.kind)">${playGlyph}</button>
        <div class="name" title="${htmlEscape(fn)}">${htmlEscape(label)}</div>
        <div class="meta">${htmlEscape(recorded)} · ${f.size_mb || '—'} MB</div>
        <div class="arrows">
          <button class="arrow-btn" onclick="moveSide(${i}, -1)" ${isFirst ? 'disabled' : ''} title="Move up" aria-label="Move up">▲</button>
          <button class="arrow-btn" onclick="moveSide(${i},  1)" ${isLast  ? 'disabled' : ''} title="Move down" aria-label="Move down">▼</button>
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

// ── Tag panel ─────────────────────────────────────────────────────────────
let tagPanelMbid = null;        // mbid of currently-picked candidate (drives cover embed on apply)
let tagPanelDiscogsId = null;   // Discogs release id — persisted so the wave editor can auto-load tracks later
let tagPanelCandidates = [];
// Tracks the auto-populated search query so re-opens don't clobber user edits.
let tagPanelAutoQuery = '';
// Snapshot of left-column values when the modal opened — `formDirty` is true
// when any current value diverges, which drives the unsaved badge + pulse.
let tagPanelInitialFields = null;
let tagPanelDirty = false;
// IDs of left-column inputs we flash on candidate-pick + watch for dirty edits.
const TAG_LEFT_FIELD_IDS = [
  't-album', 't-artist', 't-year', 't-genre',
  't-label', 't-catno', 't-country', 't-format', 't-tracks',
];

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
  // openTagAlbum / openCombine pre-set tagPanelTarget for non-default modes.
  // For a direct openTag call (recording-finished WS event, inline rename
  // fallthrough), fall through to single-side promote and clear any stale
  // album_id / filenames target left from a previous panel.
  if (!tagPanelTarget || tagPanelTarget.filename !== undefined) {
    tagPanelTarget = { filename: fname };
  }
  const isCombine = tagPanelTarget.filenames !== undefined;
  tagPanelMbid = null;
  tagPanelDiscogsId = null;
  tagPanelCandidates = [];
  // Title + apply-button copy + sides reorder visibility track the mode.
  document.getElementById('tag-modal-title').textContent =
    isCombine ? 'Combine into album' : 'Tag album';
  document.getElementById('tag-apply-btn').textContent =
    isCombine ? 'combine' : 'apply tags';
  document.getElementById('combine-sides-section').hidden = !isCombine;
  if (isCombine) {
    const n = tagPanelTarget.filenames.length;
    document.getElementById('tag-filename').textContent =
      `${n} side${n === 1 ? '' : 's'} → new album`;
    renderCombineSides();
  } else {
    document.getElementById('tag-filename').textContent = fname;
  }
  setLeft({
    album: f.album, artist: f.artist, year: f.year, genre: f.genre,
    label: f.label, catalog_number: f.catalog_number, country: f.country,
    format: '', tracks: f.tracks,
  });
  setCover(null);
  // Pre-fill the search query from existing tags so a single click runs the search.
  // Only overwrite the search field when it's empty or still holds whatever we
  // last auto-filled — anything the user typed manually wins.
  const q = [f.artist, f.album].filter(Boolean).join(' ');
  const searchEl = document.getElementById('t-search-q');
  const current = searchEl.value;
  const userTyped = current && current !== tagPanelAutoQuery;
  if (!userTyped) {
    searchEl.value = q;
    tagPanelAutoQuery = q;
  }
  document.getElementById('t-candidates').innerHTML =
    '<div class="empty-results">Hit search to look up this album on MusicBrainz.</div>';
  document.getElementById('t-search-status').textContent = '';
  document.getElementById('tag-modal').dataset.fname = fname;
  // Snapshot the freshly-loaded form so we can detect divergence for the
  // dirty badge / pulse on the apply button. Reset dirty flag + any leftover
  // flash classes from a previous invocation.
  tagPanelInitialFields = _readTagFields();
  tagPanelDirty = false;
  _setTagDirtyUI(false);
  for (const id of TAG_LEFT_FIELD_IDS) {
    document.getElementById(id)?.classList.remove('field-applied');
  }
  // Remember whatever was focused so closeTag can restore focus to it —
  // keyboard / screen-reader users should land back on the button that
  // opened the modal, not at the top of the document.
  _tagFocusReturn = document.activeElement;
  document.getElementById('tag-modal').hidden = false;
  document.addEventListener('keydown', tagEscHandler);
  // Move focus into the modal so screen readers announce its content and
  // the next Tab keeps the user inside it.
  const firstInput = document.querySelector('#tag-modal input, #tag-modal button, #tag-modal select');
  if (firstInput) firstInput.focus();
  // If we have a usable search query (artist+album already known), kick off
  // the MB search automatically so the candidate list is populated by the
  // time the user looks at it. Defer one tick so focus management above has
  // settled. Skip when the field is empty (e.g. combine of untagged sides).
  if (searchEl.value.trim()) {
    setTimeout(() => {
      // Bail if the modal closed in the meantime, or the user already
      // started typing something different (treat that as their intent).
      if (document.getElementById('tag-modal').hidden) return;
      if (searchEl.value !== tagPanelAutoQuery) return;
      runSearch();
    }, 60);
  }
}

let _tagFocusReturn = null;

function closeTag() {
  document.getElementById('tag-modal').hidden = true;
  document.removeEventListener('keydown', tagEscHandler);
  document.getElementById('combine-sides-section').hidden = true;
  // Stop any preview playback so the row's badge resets and audio stops.
  if (preview.fname) stopPreview();
  tagPanelTarget = null;
  combineOrder = [];
  // Reset dirty state so the unsaved badge / pulse don't bleed into the
  // next invocation.
  tagPanelInitialFields = null;
  tagPanelDirty = false;
  _setTagDirtyUI(false);
  if (_tagFocusReturn && typeof _tagFocusReturn.focus === 'function') {
    try { _tagFocusReturn.focus(); } catch (e) { /* element gone */ }
  }
  _tagFocusReturn = null;
}

// Read the current left-column form values into a flat dict so we can compare
// against the snapshot taken when the modal opened.
function _readTagFields() {
  const out = {};
  for (const id of TAG_LEFT_FIELD_IDS) {
    out[id] = document.getElementById(id)?.value ?? '';
  }
  return out;
}

// Toggle the unsaved badge + apply-button pulse to match `dirty`. Forces a
// reflow when re-adding `pulse-once` so the keyframe animation actually
// restarts on each clean→dirty transition (not just the very first one).
function _setTagDirtyUI(dirty) {
  const btn = document.getElementById('tag-apply-btn');
  const badge = document.getElementById('tag-unsaved-badge');
  if (btn) {
    btn.classList.remove('pulse-once');
    if (dirty) {
      void btn.offsetWidth;
      btn.classList.add('pulse-once');
    }
  }
  if (badge) badge.hidden = !dirty;
}

// Recompute the dirty flag against the snapshot. Called from input listeners
// and after pickCandidate / pickCollectionCandidate write into the form.
function _recomputeTagDirty() {
  if (!tagPanelInitialFields) return;
  const cur = _readTagFields();
  const dirty = TAG_LEFT_FIELD_IDS.some(id => cur[id] !== tagPanelInitialFields[id]);
  if (dirty !== tagPanelDirty) {
    tagPanelDirty = dirty;
    _setTagDirtyUI(dirty);
  }
}

// Wire up live dirty-tracking on the left-column inputs once at boot.
// `_recomputeTagDirty` is a no-op until tagPanelInitialFields is set, so
// these listeners are safe even when the modal is closed.
document.addEventListener('DOMContentLoaded', () => {
  for (const id of TAG_LEFT_FIELD_IDS) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', _recomputeTagDirty);
  }
});

// Briefly highlight any left-column input whose value differs from `before`.
// Driven by a CSS transition on .field-applied so the border eases back out
// once the class is removed.
function _flashChangedFields(before) {
  const FLASH_MS = 600;
  for (const id of TAG_LEFT_FIELD_IDS) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (el.value === (before?.[id] ?? '')) continue;
    el.classList.remove('field-applied');
    // Force reflow so re-adding the class restarts the transition even when
    // two candidates are clicked back-to-back.
    void el.offsetWidth;
    el.classList.add('field-applied');
    setTimeout(() => el.classList.remove('field-applied'), FLASH_MS);
  }
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
    const before = _readTagFields();
    setLeft({
      album: d.title, artist: d.artist, year: d.year, genre: d.genre,
      label: d.label, catalog_number: d.catalog_number, country: d.country,
      format: d.format, tracks: d.tracks,
    });
    _flashChangedFields(before);
    _recomputeTagDirty();
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
    const before = _readTagFields();
    setLeft({
      album: d.title, artist: d.artist, year: d.year, genre: d.genre,
      label: d.label, catalog_number: d.catalog_number, country: d.country,
      format: d.format, tracks: d.tracks,
    });
    _flashChangedFields(before);
    _recomputeTagDirty();
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
  // tagPanelTarget tells the server which mode we're in:
  //   { album_id }  → patch existing
  //   { filenames } → combine N raw sides into a new album
  //   { filename }  → promote a single raw side (fallback)
  const target = tagPanelTarget || { filename: fname };
  const isCombine = target.filenames !== undefined;
  try {
    const r = await fetch('/api/apply', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        ...target, fields,
        mbid: tagPanelMbid,
        discogs_release_id: tagPanelDiscogsId,
      })
    });
    if (!r.ok) throw new Error(await parseError(r));
    await r.json();
    if (isCombine) {
      const n = target.filenames.length;
      toast(`✓ Combined ${n} side${n === 1 ? '' : 's'} · ${fields.artist} — ${fields.album}`, 'ok');
      selected.clear();
    } else {
      toast(`✓ Tagged ${fields.artist} — ${fields.album}`, 'ok');
    }
    closeTag();
    refreshLib();
    refreshAlbums();
  } catch (e) { toast('✗ ' + e.message, 'err'); }
}

// ── Disk-space marker ─────────────────────────────────────────────────────
// Threshold comes from /api/config; default mirrors the server constant so
// the marker still flips red if the config request hasn't returned yet.
// Below `lowSpaceGb` the marker is red; below `warnSpaceGb` it's amber.
let lowSpaceGb = 2.0;
const warnSpaceGb = 10.0;

function updateDiskFree(gb) {
  const el = document.getElementById('disk-free');
  if (gb == null) {
    el.textContent = '— GB free';
    el.classList.remove('low', 'warn');
    return;
  }
  el.textContent = gb + ' GB free';
  const low = gb < lowSpaceGb;
  const warn = !low && gb < warnSpaceGb;
  el.classList.toggle('low', low);
  el.classList.toggle('warn', warn);
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
let wsReconnectTimer = null;  // single pending reconnect — avoids racing pairs

function wsConnect() {
  // Cancel any pending reconnect — `onerror` calls `ws.close()` which fires
  // `onclose` which schedules a reconnect; without this, two reconnects can
  // race and the loser leaks a half-open socket.
  if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  const proto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/api/ws`);
  ws.onopen = () => {
    wsReconnectMs = 1000;
    // Library / albums weren't replayed in the `hello` snapshot, so a long
    // disconnect leaves the UI showing stale rows until the 15 s poll catches
    // up. Refresh on reconnect so the user sees current state immediately.
    refreshLib().catch(() => {});
    refreshAlbums().catch(() => {});
    refreshDiskFree().catch(() => {});
  };
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch(e) { return; }
    handleWsEvent(m);
  };
  ws.onclose = () => {
    // Decay meters and gray out status while disconnected.
    upstreamConnected = false;
    peak.L = peak.R = 0; lvl.L = lvl.R = 0;
    if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
    wsReconnectTimer = setTimeout(() => { wsReconnectTimer = null; wsConnect(); }, wsReconnectMs);
    wsReconnectMs = Math.min(wsReconnectMs * 2, 8000);
  };
  ws.onerror = () => {
    // `close()` will fire `onclose` which handles the reconnect; ignore the
    // raw error event to keep a single scheduling source.
    if (ws && ws.readyState !== WebSocket.CLOSED) {
      try { ws.close(); } catch(e) {}
    }
  };
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
      // Recover recording state on refresh / new tab. Optional-chain the
      // sessions array — a server payload that omits it (older shape, future
      // protocol shrinkage) shouldn't throw "cannot read properties of
      // undefined" and break the whole hello handler.
      if (m.record && m.record.recording && m.record.sessions?.[0]) {
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

// Persist the open/closed state of each library section across reloads, so
// users who collapse "Music" once don't have to do it every page load.
function _wireSectionCollapse(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const key = `vr.section.${id}`;
  const saved = localStorage.getItem(key);
  if (saved === 'closed') el.open = false;
  else if (saved === 'open') el.open = true;
  el.addEventListener('toggle', () => {
    localStorage.setItem(key, el.open ? 'open' : 'closed');
  });
}
['raw-section', 'in-progress-section', 'music-section'].forEach(_wireSectionCollapse);

// ── Pi deploy modal ───────────────────────────────────────────────────────
// Pushes pi/server.py + pi-recorder.service to a Raspberry Pi over SSH.
// Mirrors the manual scp/ssh ceremony documented in README.md "Install on
// the Pi". Host + username persist in localStorage so a repeat push
// (server.py update) only needs the password.
const PI_DEPLOY_HOST_KEY = 'piDeploy.host';
const PI_DEPLOY_USER_KEY = 'piDeploy.user';
let _piDeployFocusReturn = null;

function openPiDeploy() {
  _piDeployFocusReturn = document.activeElement;
  const m = document.getElementById('pi-deploy-modal');
  if (!m) return;
  // Restore last-used host/user; pull a sensible default for host from
  // the configured stream URL when nothing's saved yet (e.g. on a fresh
  // install the user typed http://pi-recorder:8000/stream into
  // DEFAULT_STREAM_URL — that hostname is the deploy target too).
  let savedHost = '';
  try { savedHost = localStorage.getItem(PI_DEPLOY_HOST_KEY) || ''; } catch(e) {}
  if (!savedHost) {
    try {
      const u = new URL(document.getElementById('stream-url').value);
      // Skip the default SomaFM example — only suggest hostnames that
      // could plausibly be a Pi (the `/info` endpoint is the canonical
      // signal but probing it from here is overkill for a placeholder).
      if (u.hostname && !/somafm\.com$/i.test(u.hostname)) savedHost = u.hostname;
    } catch (e) {}
  }
  document.getElementById('pi-host').value = savedHost;
  let savedUser = 'pi';
  try { savedUser = localStorage.getItem(PI_DEPLOY_USER_KEY) || 'pi'; } catch(e) {}
  document.getElementById('pi-user').value = savedUser;
  document.getElementById('pi-pass').value = '';
  // Clear prior log so a re-open after a failed deploy starts fresh.
  const logEl = document.getElementById('pi-deploy-log');
  logEl.innerHTML = '';
  logEl.hidden = true;
  m.hidden = false;
  document.addEventListener('keydown', piDeployEscHandler);
  // Focus the first empty field so a returning user doesn't have to
  // tab through the saved ones.
  const firstEmpty = ['pi-host', 'pi-user', 'pi-pass']
    .map(id => document.getElementById(id))
    .find(el => !el.value);
  (firstEmpty || document.getElementById('pi-pass')).focus();
}

function closePiDeploy() {
  document.getElementById('pi-deploy-modal').hidden = true;
  document.removeEventListener('keydown', piDeployEscHandler);
  // Wipe the password field on close so it never lingers in DOM if the
  // user reopens the modal later.
  const pw = document.getElementById('pi-pass');
  if (pw) pw.value = '';
  if (_piDeployFocusReturn && typeof _piDeployFocusReturn.focus === 'function') {
    try { _piDeployFocusReturn.focus(); } catch (e) {}
  }
  _piDeployFocusReturn = null;
}
const piDeployEscHandler = makeModalEscHandler(closePiDeploy);

function _piDeployLogLine(text, kind) {
  const logEl = document.getElementById('pi-deploy-log');
  if (!logEl) return;
  logEl.hidden = false;
  const div = document.createElement('div');
  if (kind) div.className = kind;
  div.textContent = text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

async function runPiDeploy() {
  const host = document.getElementById('pi-host').value.trim();
  const username = document.getElementById('pi-user').value.trim();
  const password = document.getElementById('pi-pass').value;
  if (!host) { toast('✗ host is required', 'err'); return; }
  if (!username) { toast('✗ username is required', 'err'); return; }
  if (!password) { toast('✗ password is required', 'err'); return; }
  // Persist non-secret fields so a re-deploy only needs the password.
  try {
    localStorage.setItem(PI_DEPLOY_HOST_KEY, host);
    localStorage.setItem(PI_DEPLOY_USER_KEY, username);
  } catch (e) {}

  const goBtn = document.getElementById('pi-deploy-go');
  const headerBtn = document.getElementById('pi-deploy-btn');
  goBtn.disabled = true; goBtn.textContent = 'deploying…';
  if (headerBtn) headerBtn.disabled = true;
  const logEl = document.getElementById('pi-deploy-log');
  logEl.innerHTML = '';
  logEl.hidden = false;
  _piDeployLogLine(`▶ deploying to ${username}@${host}…`, 'info');

  try {
    const r = await fetch('/api/pi/deploy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host, username, password }),
    });
    if (!r.ok) {
      // Pre-stream failure (e.g. 422 validation) — body is regular JSON.
      let detail = 'HTTP ' + r.status;
      try { detail = (await r.json()).detail || detail; } catch (e) {}
      _piDeployLogLine('✗ ' + detail, 'err');
      toast('✗ pi deploy failed: ' + detail, 'err');
      return;
    }
    // Streamed NDJSON: one JSON object per \n-terminated chunk. Parse
    // and render as each line arrives so the modal updates live during
    // the apt step (which can be the slowest phase on a fresh Pi OS).
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let succeeded = false;
    let errorDetail = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) !== -1) {
        const raw = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!raw) continue;
        let msg;
        try { msg = JSON.parse(raw); }
        catch (e) { _piDeployLogLine(raw); continue; }
        if (msg.type === 'log')   _piDeployLogLine(msg.line);
        else if (msg.type === 'done')  succeeded = true;
        else if (msg.type === 'error') errorDetail = msg.detail || 'deploy failed';
      }
    }
    if (succeeded) {
      _piDeployLogLine('✓ pi-recorder is up. you can now point the stream URL at this host.', 'ok');
      toast('✓ pi deployed to ' + host, 'ok');
    } else {
      const detail = errorDetail || 'deploy ended without a result';
      _piDeployLogLine('✗ ' + detail, 'err');
      toast('✗ pi deploy failed: ' + detail, 'err');
    }
  } catch (e) {
    _piDeployLogLine('✗ ' + (e.message || e), 'err');
    toast('✗ pi deploy failed: ' + (e.message || e), 'err');
  } finally {
    goBtn.disabled = false; goBtn.textContent = 'deploy';
    if (headerBtn) headerBtn.disabled = false;
  }
}

applyConfig();
ensureAudioGraph();
applyMuteState();
_wireSortableHeaders();
_wireLogCollapse();
wsConnect();
refreshLib();
refreshAlbums();
refreshDiskFree();

// Polling pauses while the tab is hidden — a backgrounded laptop or
// tabbed-out user shouldn't keep firing fetches. `visibilitychange` fires
// when the tab comes back, at which point we refresh once and resume the
// interval. Avoids the buildup of pending requests that browsers used to
// queue while the tab was throttled.
let _libPollTimer = null;
function _startLibPoll() {
  if (_libPollTimer) return;
  _libPollTimer = setInterval(() => { refreshLib(); refreshAlbums(); }, 15000);
}
function _stopLibPoll() {
  if (_libPollTimer) { clearInterval(_libPollTimer); _libPollTimer = null; }
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    _stopLibPoll();
  } else {
    refreshLib().catch(() => {});
    refreshAlbums().catch(() => {});
    refreshDiskFree().catch(() => {});
    _startLibPoll();
  }
});
_startLibPoll();
setInterval(refreshDiskFree, 30000);
