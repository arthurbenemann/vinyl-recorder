// Server config + version probe. Loads /api/config once at startup and
// applies values to:
//
//   - the stream-URL input default
//   - the gain slider's pending DEFAULT_GAIN_DB (applied after first probe)
//   - the disk-free threshold
//   - the version-tag pill
//   - the wave-editor split defaults (normalize toggle, target peak, bit depth)
//   - the Discogs collection refresh button visibility
//
// The `we` object lives in the classic-script wave-editor.js, exposed to
// the module world via a tiny inline bridge in index.html.

import { state } from './state.js';
import { renderVersion } from './library.js';

export async function applyConfig() {
  try {
    const r = await fetch('/api/config');
    const c = await r.json();
    if (c.default_stream_url) {
      document.getElementById('stream-url').value = c.default_stream_url;
    }
    if (typeof c.default_gain_db === 'number') {
      state.pendingDefaultGainDb = c.default_gain_db;
    }
    if (typeof c.low_space_gb === 'number') state.lowSpaceGb = c.low_space_gb;
    renderVersion(c.version);
    // Wave-editor split defaults — applied to the modal whenever it's reopened.
    if (typeof c.default_split_normalize === 'boolean') {
      document.getElementById('we-normalize').checked = c.default_split_normalize;
    }
    if (typeof c.default_split_target_peak_db === 'number' && window.we) {
      window.we.targetPeakDb = c.default_split_target_peak_db;
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
    // Auto-stop on silence defaults. Only fill the form if the user
    // hasn't already saved their own preferences via localStorage —
    // ops-set env defaults shouldn't clobber a per-user override.
    const enEl  = document.getElementById('autostop-enable');
    const secEl = document.getElementById('autostop-seconds');
    const thrEl = document.getElementById('autostop-threshold');
    if (enEl && localStorage.getItem('autoStopOnSilence') === null
        && typeof c.default_auto_stop_on_silence === 'boolean') {
      enEl.checked = c.default_auto_stop_on_silence;
    }
    if (secEl && localStorage.getItem('autoStopSilenceSeconds') === null
        && typeof c.default_silence_seconds === 'number') {
      secEl.value = String(c.default_silence_seconds);
    }
    if (thrEl && localStorage.getItem('autoStopSilenceDb') === null
        && typeof c.default_silence_threshold_db === 'number') {
      thrEl.value = String(c.default_silence_threshold_db);
    }
    // wireAutoStopForm has already attached `change` listeners by the
    // time we get here (applyConfig is awaited later in boot). Trigger
    // it so the hint + collapsed-state stay in sync with whatever we
    // just seeded — without this, the user sees stale form state until
    // they hand-toggle an input.
    if (enEl) enEl.dispatchEvent(new Event('change'));
    // auto_connect is now handled server-side at app startup; nothing to do
    // here besides letting the WS hello replay tell us the current state.
  } catch(e) { console.error('config fetch failed', e); }
}
