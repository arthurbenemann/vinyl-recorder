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
import { lastStreamUrl } from './upstream.js';

export async function applyConfig() {
  try {
    const r = await fetch('/api/config');
    const c = await r.json();
    // A user's last-connected URL (localStorage) wins over the ops-set env
    // default, same precedence as the auto-stop pref below — otherwise a
    // per-user source choice would be clobbered on every reload.
    const savedUrl = lastStreamUrl();
    if (savedUrl) {
      document.getElementById('stream-url').value = savedUrl;
    } else if (c.default_stream_url) {
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
    if (typeof c.default_split_replaygain === 'boolean') {
      const rg = document.getElementById('we-replaygain');
      if (rg) rg.checked = c.default_split_replaygain;
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
    // Auto-stop-on-silence default. Only fill the dropdown if the user
    // hasn't already saved their preference via localStorage — ops-set
    // env defaults shouldn't clobber a per-user override. The detection
    // threshold (dBFS) is server-side only (SILENCE_THRESHOLD_DB) so
    // there's nothing to seed for it. When the env feature flag is off
    // (default_auto_stop_on_silence=false), seed the dropdown to "0"
    // (∞ disabled). When it's on but SILENCE_SECONDS doesn't match a
    // dropdown option exactly (the env can be any positive int but the
    // dropdown carries a quantised set of 10/20/30/60), snap to the
    // closest non-zero option rather than fall back to the HTML default.
    const silSel = document.getElementById('silence-sel');
    if (silSel && localStorage.getItem('autoStopSilenceSeconds') === null) {
      if (c.default_auto_stop_on_silence !== true) {
        silSel.value = '0';
      } else if (typeof c.default_silence_seconds === 'number') {
        const target = c.default_silence_seconds;
        const opts = [...silSel.options]
          .map(o => parseInt(o.value, 10))
          .filter(n => Number.isFinite(n) && n > 0);
        if (opts.length) {
          let best = opts[0];
          for (const n of opts) {
            if (Math.abs(n - target) < Math.abs(best - target)) best = n;
          }
          silSel.value = String(best);
        }
      }
    }
    // auto_connect is now handled server-side at app startup; nothing to do
    // here besides letting the WS hello replay tell us the current state.
  } catch(e) { console.error('config fetch failed', e); }
}
