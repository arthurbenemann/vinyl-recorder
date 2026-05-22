// Server-side upstream connection lifecycle, local playback, ADC gain
// probe + slider, stream-health indicator, header kebab menu, and the
// collapsible sidebar.
//
// "Connect" means "tell the server to start pulling the upstream stream."
// The state is global — any tab can press it, every tab sees the same dot
// + button via the WebSocket. VU + recording both depend on this.

import { fmtBps, makeModalEscHandler } from './util.js';
import { parseError } from './api.js';
import { log } from './log.js';
import { state } from './state.js';
import { decayMeters } from './meter.js';

export async function toggleConnect() {
  if (state.upstreamConnected) {
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
      else { rememberStreamUrl(url); probeGain(url); }
    } catch(e) { /* network drop — WS will resync */ }
  }
}

// ── Local audio playback (just an <audio> element) ───────────────────────
// VU comes from the server over WebSocket, so we don't need WebAudio at
// all — a plain HTMLAudioElement plays the proxy stream directly to the
// system mixer. Mute is the element's `.muted` property (and a pause to
// stop the network). Created muted so the autoplay policy lets us prefetch.
export function ensureAudioGraph() {
  if (state.audioEl) return;
  state.audioEl = new Audio();
  // No crossOrigin: same-origin proxy, no WebAudio that would need it. The
  // attribute used to require CORS headers on the streaming response, which
  // Chrome can be picky about for chunked endpoints.
  state.audioEl.muted = true;
  state.audioEl.preload = 'none';  // wait until the user actually unmutes
  state.audioEl.addEventListener('error', () => {
    const err = state.audioEl.error;
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

export function applyMuteState() {
  const btn = document.getElementById('mute-btn');
  if (!btn) return;
  btn.textContent = state.muted ? '🔈 unmute' : '🔇 mute';
  btn.classList.toggle('active', !state.muted);
  if (state.audioEl) state.audioEl.muted = state.muted;
}

export async function toggleMute() {
  ensureAudioGraph();
  state.muted = !state.muted;
  if (!state.muted && state.upstreamConnected) {
    // Re-point at the proxy so we recover from a prior 409 (no upstream
    // when the page first loaded) or a disconnect/reconnect cycle that
    // left the element in an error state.
    state.audioEl.src = '/api/stream-proxy?t=' + Date.now();
    state.audioEl.muted = false;
    try { await state.audioEl.play(); } catch(e) {}
  } else if (state.muted) {
    // Pause + drop the src so the browser actually closes the network
    // connection. Otherwise the proxy stream keeps flowing on the server,
    // its ffmpeg eventually blocks on a full stdout pipe, and the orphan
    // subscriber sits around until the TCP connection finally times out.
    try { state.audioEl.pause(); } catch(e) {}
    try { state.audioEl.removeAttribute('src'); state.audioEl.load(); } catch(e) {}
  }
  applyMuteState();
}

// ── ADC gain (pi-recorder /info + /gain) ──────────────────────────────────
let gainBase = null;
let gainTimer = null;

export function hideGain() {
  document.getElementById('gain-row').hidden = true;
  gainBase = null;
}

export async function probeGain(streamUrl) {
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
    if (state.pendingDefaultGainDb !== null && Math.abs(state.pendingDefaultGainDb - info.gain_db) > 0.01) {
      const desired = state.pendingDefaultGainDb;
      state.pendingDefaultGainDb = null;
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

export function wireGainSlider() {
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
}

export function applyUpstreamState({ connected, configured, fmt: f }) {
  // Server distinguishes `configured` (URL set up, may be idle) from
  // `connected` / `live` (ffmpeg subprocess actually running). The UI
  // pill binds to `configured` — what the user cares about is "is the
  // session set up", not "is a subprocess running this exact ms" (it
  // tears down + respawns on demand to keep idle CPU at zero). Older
  // servers may not emit `configured`; fall back to `connected` so a
  // mixed-version client/server combo still works.
  const isConfigured = (typeof configured === 'boolean') ? configured : !!connected;
  state.upstreamConnected = isConfigured;
  // `connected` is the server's `live` flag (ffmpeg actually running now).
  // Old servers that only send one boolean collapse the two — fall back so
  // the live state tracks configured rather than getting stuck false.
  state.upstreamLive = (typeof connected === 'boolean') ? connected : isConfigured;
  const btn = document.getElementById('connect-btn');
  if (btn) btn.textContent = isConfigured ? 'disconnect' : 'connect';
  // Lock the URL input while configured — changing it has no effect until
  // disconnect anyway, so making it look uneditable matches reality.
  const urlInput = document.getElementById('stream-url');
  if (urlInput) urlInput.disabled = isConfigured;
  if (!state.recording) {
    document.getElementById('stext').textContent = upstreamStatusText();
  }
  // Chevron is the click affordance for the health panel; only show it when
  // there's something to see (configured). The status-indicator itself is a
  // <button>, always clickable — the chevron just indicates the panel exists.
  const chevron = document.getElementById('health-chevron');
  if (chevron) chevron.hidden = !isConfigured;
  updateSdot();
  if (!isConfigured) {
    // Decay meters; clear gain slider since the Pi probe needs reconnect.
    decayMeters();
    hideGain();
    applyHealthState(null);
    // Auto-collapse the panel on disconnect so it doesn't linger empty.
    const panel = document.getElementById('health-panel');
    if (panel) panel.hidden = true;
    const ind = document.getElementById('status-indicator');
    if (ind) ind.setAttribute('aria-expanded', 'false');
  }
}

// Single source of truth for the non-recording status label. Kept here (not
// inlined) so applyUpstreamState and recording.js's stop path agree on what
// "configured but not live" reads as.
export function upstreamStatusText() {
  if (!state.upstreamConnected) return 'disconnected';
  if (state.upstreamLive) return 'connected';
  // Configured but ffmpeg idle (demand-driven teardown, or not yet spawned).
  // A visible tab holds the stream so it's about to wake; a backgrounded tab
  // stays idle until it foregrounds or a recording/playback acquires.
  return (typeof document !== 'undefined' && document.visibilityState === 'visible')
    ? 'waking…' : 'idle';
}

// Single source of truth for the connection-dot color. Recording wins over
// health; a paused recording keeps a distinct color (the stream is still
// live, just not writing); "configured but idle" gets its own standby color
// so a frozen VU doesn't read as a healthy green.
let lastHealthLevel = null; // 'green' | 'yellow' | 'red' | null
export function updateSdot() {
  const sdot = document.getElementById('sdot');
  if (!sdot) return;
  if (state.recording) {
    // Paused: ffmpeg is still running, the session just isn't writing to the
    // FLAC. Show a distinct paused color rather than the same gray as
    // "nothing configured" so a pause never reads as a dropped stream.
    sdot.className = state.paused ? 'dot paused' : 'dot rec';
    return;
  }
  if (!state.upstreamConnected) {  // not configured
    sdot.className = 'dot';
    return;
  }
  if (!state.upstreamLive) {  // configured but ffmpeg idle / waking
    sdot.className = 'dot idle';
    return;
  }
  sdot.className = lastHealthLevel === 'yellow' ? 'dot warn'
                 : lastHealthLevel === 'red'    ? 'dot bad'
                 : 'dot ok';  // 'green' or null (no health tick yet)
}

// ── Stream-health indicator ───────────────────────────────────────────────
// Receives `health` WS events with bytes/sec, gap counts, level, etc.
// Updates a colored dot in the header and a collapsible stats panel.
export function applyHealthState(h) {
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
export function toggleHeaderMenu(e) {
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
export function closeHeaderMenu() {
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

export function toggleHealthPanel() {
  if (!state.upstreamConnected) return;  // nothing to show until first health tick
  const panel = document.getElementById('health-panel');
  if (!panel) return;
  panel.hidden = !panel.hidden;
  const ind = document.getElementById('status-indicator');
  if (ind) ind.setAttribute('aria-expanded', String(!panel.hidden));
}

// ── Stream URL memory ──────────────────────────────────────────────────────
// The URL input resets to DEFAULT_STREAM_URL (or the static demo placeholder)
// on every load. A deployment that doesn't set the env default leaves the
// user re-typing their turntable's stream URL every session. Remember the
// last successfully-connected URL + a short most-recently-used list, the
// latter surfaced as a <datalist> so switching between a couple of sources is
// a click. config.js seeds the input from `lastStreamUrl()` (preferred over
// the env default, same precedence the auto-stop pref uses).
const STREAM_URL_KEY = 'vr.streamUrl';
const STREAM_URL_RECENT_KEY = 'vr.streamUrlRecent';
const STREAM_URL_RECENT_MAX = 6;

export function lastStreamUrl() {
  try { return localStorage.getItem(STREAM_URL_KEY) || ''; } catch (_) { return ''; }
}

function _readRecentStreamUrls() {
  try {
    const arr = JSON.parse(localStorage.getItem(STREAM_URL_RECENT_KEY) || '[]');
    return Array.isArray(arr) ? arr.filter(u => typeof u === 'string' && u) : [];
  } catch (_) { return []; }
}

// Save the last-connected URL + push it to the front of the MRU list. Called
// from toggleConnect only on a successful connect, so the memory tracks URLs
// that actually worked.
export function rememberStreamUrl(url) {
  const u = String(url || '').trim();
  if (!u) return;
  try { localStorage.setItem(STREAM_URL_KEY, u); } catch (_) {}
  const recent = [u, ..._readRecentStreamUrls().filter(x => x !== u)]
    .slice(0, STREAM_URL_RECENT_MAX);
  try { localStorage.setItem(STREAM_URL_RECENT_KEY, JSON.stringify(recent)); } catch (_) {}
  renderStreamUrlRecent();
}

// (Re)populate the datalist behind #stream-url from the MRU list. Built via
// DOM nodes (not innerHTML) so a URL can never inject markup.
export function renderStreamUrlRecent() {
  const dl = document.getElementById('stream-url-recent');
  if (!dl) return;
  dl.replaceChildren();
  for (const u of _readRecentStreamUrls()) {
    const opt = document.createElement('option');
    opt.value = u;
    dl.appendChild(opt);
  }
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
export function toggleSidebar() {
  const collapsed = !document.querySelector('.main').hasAttribute('data-collapsed');
  applySidebarState(collapsed);
  try { localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0'); } catch (_) {}
}
export function restoreSidebarState() {
  let saved = null;
  try { saved = localStorage.getItem(SIDEBAR_KEY); } catch (_) {}
  applySidebarState(saved === '1');  // default = expanded
}
