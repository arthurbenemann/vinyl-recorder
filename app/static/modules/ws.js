// One WebSocket per tab. Carries VU peaks, clip latches, server-connect
// state, recording lifecycle, and shared log lines. The `hello` frame
// replays the current state so a fresh / refreshed tab catches up
// immediately.

import { state } from './state.js';
import { setClipBadge, updateMeter, decayMeters } from './meter.js';
import { applyUpstreamState, applyHealthState, probeGain } from './upstream.js';
import { applyRecordState, applyDurationChange, applySilenceProgress, applySilenceSecondsChange, getRecDurationSec, getRecStartTimeMs } from './recording.js';
import { renderLog } from './log.js';
import { refreshLib, refreshDiskFree } from './library.js';
import { refreshAlbums } from './albums.js';
import { openTag } from './tagging.js';

let ws = null, wsReconnectMs = 1000;
let wsReconnectTimer = null;  // single pending reconnect — avoids racing pairs

// Visibility hint sent to the server over the WS so it can decide whether
// to keep ffmpeg up. A visible tab counts as a lifecycle holder; a hidden
// tab releases the hold and lets the upstream idle (with a few-second
// grace) until any tab comes back. Recording sessions and playback proxy
// connections also hold the upstream alive independently — closing all
// tabs mid-recording does NOT tear ffmpeg down.
export function sendVisibility() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(JSON.stringify({
      type:   'visibility',
      hidden: !!document.hidden,
    }));
  } catch (_) {}
}

export function wsConnect() {
  // Cancel any pending reconnect — `onerror` calls `ws.close()` which fires
  // `onclose` which schedules a reconnect; without this, two reconnects can
  // race and the loser leaks a half-open socket.
  if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  const proto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/api/ws`);
  ws.onopen = () => {
    wsReconnectMs = 1000;
    // Tell the server our current visibility so it can decide whether
    // to keep the upstream ffmpeg alive or let it idle. The server
    // assumes "visible" on first connect; sending this immediately
    // covers the case where the tab opened in the background.
    sendVisibility();
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
    state.upstreamConnected = false;
    decayMeters();
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
        connected:  m.upstream.connected,
        configured: m.upstream.configured,
        fmt:        m.upstream.format,
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
      // Run gain probe if upstream is configured and we have a URL.
      // (gain queries hit the Pi /gain endpoint, not /stream — they
      // don't depend on whether ffmpeg is currently up.)
      if (m.upstream && (m.upstream.configured || m.upstream.connected) && m.upstream.url) {
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
      applyUpstreamState({
        connected:  m.connected,
        configured: m.configured,
        fmt:        m.format,
      });
      if ((m.configured || m.connected) && m.url) probeGain(m.url);
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
        if (m.session_id && m.session_id === state.sessionId) openTag(m.filename);
        // applyRecordState already reset sessionId; clear any owner tag.
      } else if (m.event === 'pause') {
        // Server-authoritative elapsed — local clock would over-report after
        // a resume because recStartTimeMs isn't slid during the pause.
        applyRecordState({
          active: true, paused: true, sid: m.session_id || state.sessionId,
          durationSec: getRecDurationSec(),
          elapsedSec: typeof m.elapsed === 'number' ? m.elapsed
                      : Math.floor((Date.now() - getRecStartTimeMs()) / 1000),
        });
      } else if (m.event === 'resume') {
        applyRecordState({
          active: true, paused: false, sid: m.session_id || state.sessionId,
          durationSec: getRecDurationSec(),
          elapsedSec: typeof m.elapsed === 'number' ? m.elapsed
                      : Math.floor((Date.now() - getRecStartTimeMs()) / 1000),
        });
      } else if (m.event === 'duration') {
        // Server-driven cap change — broadcasts here so every tab's
        // progress bar re-anchors against the new cap, including the tab
        // that originated the edit (the POST response says 200 but the WS
        // event is what triggers the visible UI update).
        applyDurationChange(m.duration, m.elapsed);
      } else if (m.event === 'silence') {
        // Server-driven silence-cap change — same shape as `duration`
        // but the bar itself is server-authoritative (every `silence`
        // event carries the live cap), so we just need to re-anchor
        // the dropdown across tabs so every tab shows the same value.
        applySilenceSecondsChange(m.silence_seconds);
      }
      break;
    case 'silence':
      // Watcher-tick snapshot of the silence-countdown state. Drives the
      // small "auto-stop in Ns" bar under the recording progress so the
      // user can see how close the recording is to finalising itself.
      applySilenceProgress(m);
      break;
    case 'log':
      renderLog(m.msg, m.level);
      break;
    case 'ping':
      break;
  }
}
