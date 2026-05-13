// Record / pause / stop control + the timer that drives the on-screen
// elapsed-seconds display.
//
// Recording state is server-side and broadcast over WS, so any tab can stop
// a session that another tab started. The handlers here only issue the API
// call; visible UI changes happen in applyRecordState() (driven by WS).

import { fmt } from './util.js';
import { parseError } from './api.js';
import { toast } from './log.js';
import { state } from './state.js';
import { updateSdot } from './upstream.js';

let recStartTimeMs = 0;        // local clock anchor for the elapsed timer
let recDurationSec = 0;        // 0 = unlimited
let recTimerInterval = null;
// Snapshot of the dur-sel value before the user's mid-recording edit. Lets
// us revert the dropdown if the server rejects the new cap (409 — would
// leave too little slack). Not used while idle (the value just rides into
// the next start_recording POST).
let _durSelLastValue = null;

// Silence auto-stop persists across page reloads via localStorage so the
// user only configures it once. applyConfig (in config.js) seeds it from
// /api/config the first time the page is loaded on a fresh browser.
// The detection threshold (dBFS) is an ops/calibration knob set via
// SILENCE_THRESHOLD_DB on the server; the UI carries only the duration
// (matching the existing time-limit dropdown's shape).
const LS_AS_SECONDS = 'autoStopSilenceSeconds';


// Read the silence-auto-stop seconds from the dropdown. Value "0" maps
// to "feature off" (the ∞ option) — same convention the duration cap
// uses, so the server's auto_stop_on_silence flag is derived from it.
function readSilenceSel() {
  const sel = document.getElementById('silence-sel');
  return Math.max(0, parseInt((sel && sel.value) || '0', 10));
}


export function wireSilenceSel() {
  const sel = document.getElementById('silence-sel');
  if (!sel) return;
  // Hydrate from localStorage if present; applyConfig will only fill
  // unset keys, so a user who changed their setting keeps it.
  const ls = localStorage.getItem(LS_AS_SECONDS);
  if (ls !== null && [...sel.options].some(o => o.value === ls)) {
    sel.value = ls;
  }
  sel.addEventListener('change', () => {
    try { localStorage.setItem(LS_AS_SECONDS, String(sel.value)); } catch (e) {}
  });
}

// The pause/resume WS branches use the duration that was current at the
// last applyRecordState call, so expose it for ws.js.
export function getRecDurationSec() { return recDurationSec; }
export function getRecStartTimeMs() { return recStartTimeMs; }

export async function toggleRec() {
  if (state.recording) {
    if (!state.sessionId) {
      // Another tab owns the active session and we don't know its id yet —
      // /api/status returns the list of live sessions; pick the first.
      try {
        const s = await (await fetch('/api/status')).json();
        state.sessionId = (s.sessions && s.sessions[0] && s.sessions[0].id) || null;
      } catch(e) {}
    }
    if (!state.sessionId) {
      toast('✗ no active session id available', 'err');
      return;
    }
    try {
      const r = await fetch(`/api/record/stop/${state.sessionId}`, { method: 'POST' });
      if (!r.ok) throw new Error(await parseError(r));
      // The actual UI flip happens via the WS `record:stop` event.
    } catch(e) { toast('✗ ' + e.message, 'err'); }
  } else {
    if (!state.upstreamConnected) {
      toast('✗ connect to a stream first', 'err');
      return;
    }
    const url = document.getElementById('stream-url').value;
    const dur = parseInt(document.getElementById('dur-sel').value);
    const silenceSec = readSilenceSel();
    try {
      const r = await fetch('/api/record/start', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          stream_url: url, duration: dur,
          // 0 = ∞ disabled; positive seconds enables the watcher. The
          // detection threshold itself is set ops-side via the
          // SILENCE_THRESHOLD_DB env var — no per-recording override.
          auto_stop_on_silence: silenceSec > 0,
          silence_seconds:      silenceSec,
        }),
      });
      if (!r.ok) throw new Error(await parseError(r));
    } catch(e) { toast('✗ ' + e.message, 'err'); }
  }
}

export async function togglePause() {
  if (!state.sessionId) return;
  const path = state.paused ? 'resume' : 'pause';
  try {
    const r = await fetch(`/api/record/${path}/${state.sessionId}`, { method: 'POST' });
    if (!r.ok) throw new Error(await parseError(r));
    // UI updates via the WS `record:pause`/`resume` event.
  } catch (e) { toast('✗ ' + e.message, 'err'); }
}

// `applyRecordState` is the single place that mutates the visible recording
// UI — called by WS hellos (replay on connect) and live record events.
export function applyRecordState({ active, paused: isPaused, sid, durationSec, elapsedSec }) {
  state.recording = !!active;
  state.paused    = !!isPaused;
  state.sessionId = active ? (sid || null) : null;

  const recBtn   = document.getElementById('recbtn');
  const pauseBtn = document.getElementById('pausebtn');
  const stext    = document.getElementById('stext');
  const hint     = document.getElementById('timer-hint');
  const prog     = document.getElementById('prog');

  recBtn.classList.toggle('active', state.recording);
  pauseBtn.hidden = !state.recording;
  pauseBtn.classList.toggle('paused', state.paused);
  pauseBtn.textContent = state.paused ? '▶' : '‖';
  pauseBtn.title       = state.paused ? 'Resume' : 'Pause';

  if (state.recording) {
    stext.textContent = state.paused ? 'paused' : 'recording';
    hint.textContent = state.paused ? 'paused — click ▶ to resume'
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
    stext.textContent = state.upstreamConnected ? 'connected' : 'disconnected';
    hint.textContent = 'click ● to start recording';
    prog.style.width = '0%';
    document.getElementById('timer').textContent = fmt(0);
    // No active recording → no silence countdown either. Reset and hide
    // so a leftover bar from a previous recording isn't visible.
    applySilenceProgress({ progress: 0, cap_seconds: 0 });
  }
  updateSdot();
}

// Mirror of the duration progress bar for the auto-stop-on-silence
// countdown. The server emits this every ~500 ms while a recording is
// in flight (only when auto-stop is enabled); the CSS transition smooths
// the half-second steps. Visible whenever the recording has auto-stop
// configured (`cap_seconds > 0`) — the bar sits at 0% during normal
// audio and fills as the smoothed RMS stays below threshold. Recording
// finalises at the same instant the bar reaches 100%, mirroring the
// duration bar's "fill = stop" semantics.
export function applySilenceProgress({ progress, cap_seconds }) {
  const wrap = document.getElementById('silence-prog-wrap');
  const fill = document.getElementById('silence-prog');
  if (!wrap || !fill) return;
  const p = Number(progress) || 0;
  const visible = !!state.recording && (cap_seconds || 0) > 0;
  wrap.hidden = !visible;
  fill.style.width = Math.min(100, p * 100) + '%';
}

// Wire the duration dropdown so that while a recording is live, changing
// it POSTs the new cap to the server instead of waiting for the next
// start. Server enforces extension-always-OK + reduction-needs-5min-slack;
// a 409 reverts the dropdown to its prior value so the UI never lies.
export function wireDurationSel() {
  const sel = document.getElementById('dur-sel');
  if (!sel) return;
  // Track the value at focus time so we can revert on rejection without
  // racing the user's next click. focus fires before change, every time.
  sel.addEventListener('focus', () => { _durSelLastValue = sel.value; });
  sel.addEventListener('change', async () => {
    // Idle: nothing to push. The next toggleRec() will read this value.
    if (!state.recording || !state.sessionId) {
      _durSelLastValue = sel.value;
      return;
    }
    const prev = _durSelLastValue ?? sel.value;
    const next = parseInt(sel.value, 10);
    try {
      const r = await fetch(`/api/record/duration/${state.sessionId}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ duration: next }),
      });
      if (!r.ok) {
        const msg = await parseError(r);
        sel.value = prev;             // revert — server refused
        toast('✗ ' + msg, 'err');
        return;
      }
      _durSelLastValue = sel.value;
      // applyRecordState fires via WS `record:duration` and updates the
      // progress bar anchor; no local mutation needed here.
    } catch (e) {
      sel.value = prev;
      toast('✗ ' + e.message, 'err');
    }
  });
}

// Called by the WS handler when the server broadcasts a duration edit.
// Re-anchors the progress bar against the new cap; the timer keeps
// running from the same recStartTimeMs.
export function applyDurationChange(newDurationSec, elapsedSec) {
  recDurationSec = newDurationSec || 0;
  // Snap the local clock anchor to the server-reported elapsed so the
  // progress bar lines up with the new cap even if a small WS gap had
  // accumulated drift.
  recStartTimeMs = Date.now() - (elapsedSec || 0) * 1000;
  // Reflect the new cap on the dropdown so the user's view of "where am
  // I" matches the new cap, even when the edit came from another tab.
  const sel = document.getElementById('dur-sel');
  if (sel) {
    const v = String(newDurationSec || 0);
    if ([...sel.options].some(o => o.value === v)) {
      sel.value = v;
      _durSelLastValue = v;
    }
  }
  tickRecTimer();
}

export function tickRecTimer() {
  if (!state.recording) return;
  if (state.paused) return;
  const elapsed = Math.floor((Date.now() - recStartTimeMs) / 1000);
  document.getElementById('timer').textContent = fmt(elapsed);
  if (recDurationSec > 0) {
    document.getElementById('prog').style.width =
      Math.min(elapsed / recDurationSec * 100, 100) + '%';
  }
}
