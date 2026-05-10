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
  }
  updateSdot();
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
