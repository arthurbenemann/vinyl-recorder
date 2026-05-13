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

// Auto-stop on silence settings persist across page reloads via
// localStorage so the user only configures them once. applyConfig (in
// config.js) seeds these from /api/config the first time the page is
// loaded on a fresh browser.
const LS_AUTOSTOP   = 'autoStopOnSilence';
const LS_AS_SECONDS = 'autoStopSilenceSeconds';
const LS_AS_DB      = 'autoStopSilenceDb';

function readAutoStopForm() {
  const en   = document.getElementById('autostop-enable');
  const sec  = document.getElementById('autostop-seconds');
  const thr  = document.getElementById('autostop-threshold');
  return {
    enabled:     !!(en && en.checked),
    seconds:     Math.max(1, parseInt((sec && sec.value) || '20', 10)),
    threshold_db: Math.max(-100, Math.min(0, parseFloat((thr && thr.value) || '-50'))),
  };
}

function persistAutoStopForm(f) {
  try {
    localStorage.setItem(LS_AUTOSTOP,   f.enabled ? '1' : '0');
    localStorage.setItem(LS_AS_SECONDS, String(f.seconds));
    localStorage.setItem(LS_AS_DB,      String(f.threshold_db));
  } catch (e) {}
}

// Reflect the auto-stop config on the summary line so the user can see
// the current setting at a glance without expanding the panel.
function updateAutoStopHint(f) {
  const hint = document.getElementById('autostop-hint');
  if (!hint) return;
  if (f.enabled) {
    hint.hidden = false;
    hint.textContent = `${f.seconds}s · ${f.threshold_db.toFixed(0)} dB`;
  } else {
    hint.hidden = true;
    hint.textContent = '';
  }
}

export function wireAutoStopForm() {
  const en  = document.getElementById('autostop-enable');
  const sec = document.getElementById('autostop-seconds');
  const thr = document.getElementById('autostop-threshold');
  if (!en || !sec || !thr) return;
  // Hydrate from localStorage if present; applyConfig will only fill
  // unset keys, so a user who changed their settings keeps them.
  const lsEn  = localStorage.getItem(LS_AUTOSTOP);
  const lsSec = localStorage.getItem(LS_AS_SECONDS);
  const lsDb  = localStorage.getItem(LS_AS_DB);
  if (lsEn  !== null) en.checked  = lsEn === '1';
  if (lsSec !== null) sec.value   = lsSec;
  if (lsDb  !== null) thr.value   = lsDb;
  const onChange = () => {
    const f = readAutoStopForm();
    persistAutoStopForm(f);
    updateAutoStopHint(f);
    // Open the details when enabled so the user can see the inputs;
    // collapse otherwise to save sidebar space.
    const det = document.getElementById('autostop-details');
    if (det) det.open = f.enabled;
  };
  en.addEventListener('change', onChange);
  sec.addEventListener('change', onChange);
  thr.addEventListener('change', onChange);
  updateAutoStopHint(readAutoStopForm());
  const det = document.getElementById('autostop-details');
  if (det) det.open = en.checked;
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
    const as  = readAutoStopForm();
    try {
      const r = await fetch('/api/record/start', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          stream_url: url, duration: dur,
          auto_stop_on_silence: as.enabled,
          silence_seconds:      as.seconds,
          silence_threshold_db: as.threshold_db,
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
    // No active recording → no silence-countdown either. Reset and hide
    // so a leftover bar from a previous recording isn't visible.
    applySilenceProgress({ progress: 0, elapsed_seconds: 0,
                           cap_seconds: 0, armed: false });
  }
  updateSdot();
}

// Mirror of the duration progress bar for the auto-stop-on-silence
// countdown. Server emits this every ~500 ms while a recording is
// armed; the CSS transition smooths the steps so the bar looks like
// a continuous fill from 0 → silence_seconds. When audio comes back
// above threshold the server reports progress=0 and the bar drains
// to empty. Pinned to the existing #prog-wrap layout so visually it
// reads as a second progress dimension on the same recording.
export function applySilenceProgress({ progress, elapsed_seconds,
                                       cap_seconds, armed }) {
  const wrap = document.getElementById('silence-prog-wrap');
  const fill = document.getElementById('silence-prog');
  const label = document.getElementById('silence-prog-label');
  if (!wrap || !fill) return;
  const p = Number(progress) || 0;
  // Hide unless the watcher is armed AND silence is actively accumulating.
  // Lead-in silence (not armed) and post-arming audio (progress=0) both
  // render as hidden so the bar only appears when it carries information.
  const visible = !!state.recording && !!armed && (cap_seconds > 0) && p > 0;
  wrap.hidden = !visible;
  fill.style.width = Math.min(100, p * 100) + '%';
  if (label) {
    if (visible) {
      const remaining = Math.max(0, (cap_seconds || 0) - (elapsed_seconds || 0));
      label.textContent = `auto-stop in ${Math.ceil(remaining)}s`;
    } else {
      label.textContent = '';
    }
  }
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
