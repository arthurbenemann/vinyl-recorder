// Stereo VU meter (driven by WS frames) + clip latch.
//
// Smoothing + peak-hold are still done locally so the meter looks the same
// even at low frame rates / poor connectivity. Raw peak values arrive at
// ~20 Hz from the server-side reader and feed updateMeter().
//
// Latched clip flags mirror server state. Click-to-clear fires a POST so
// every tab un-latches in sync.

import { dbStr } from './util.js';
import { state } from './state.js';

const lvl = { L: 0, R: 0 };
const peak = { L: 0, R: 0 };
const peakAge = { L: 0, R: 0 };  // frames since peak was set
const PEAK_HOLD_FRAMES = 30;     // ~1.5 s at 50ms tick
const PEAK_DECAY = 0.015;        // per frame after hold expires

const clipped = { L: false, R: false };

export function setClipBadge(ch, on) {
  clipped[ch] = !!on;
  document.getElementById('clip-' + ch).hidden = !on;
}

export function updateMeter(ch, level) {
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

export async function clearClip(ch) {
  // Server clears the latch and broadcasts to every tab.
  try {
    await fetch('/api/clip/clear?ch=' + encodeURIComponent(ch || ''),
                { method: 'POST' });
  } catch(e) { /* WS will eventually re-sync */ }
}

// Reset the local smoother / peak-hold state — called when the upstream
// disconnects or the WS goes down so the bars decay to 0 instead of
// staying frozen at the last reported level.
export function decayMeters() {
  peak.L = peak.R = 0;
  lvl.L = lvl.R = 0;
}

// At ~20 Hz of WS VU frames the smoother already looks tight, but a 50 ms
// local tick handles peak-hold decay during idle (no frames coming in).
export function startMeterIdleTicker() {
  setInterval(() => {
    if (!state.upstreamConnected) {
      updateMeter('L', 0);
      updateMeter('R', 0);
    }
  }, 50);
}
