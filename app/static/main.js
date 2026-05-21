// Orchestrator. Pulls each focused module in `./modules/` and wires the
// page-init sequence. The bulk of the logic lives in the modules; this
// file is intentionally thin.
//
// A note on `window.foo = foo` re-exports below: `index.html` and the
// dynamically-generated table rows still use inline `onclick="foo(...)"`
// attributes, which are evaluated in the *global* scope — they can't see
// module-scoped names. We attach every entry-point function to `window`
// so those handlers keep working byte-for-byte. wave-editor.js + peaks.js
// are still classic scripts that share script-scope, so they also rely
// on these globals (toast, parseError, etc.).

import { state } from './modules/state.js';
import { startMeterIdleTicker, clearClip } from './modules/meter.js';
import {
  toggleConnect, toggleMute, ensureAudioGraph, applyMuteState,
  wireGainSlider, toggleHeaderMenu, closeHeaderMenu, toggleHealthPanel,
  toggleSidebar, restoreSidebarState,
} from './modules/upstream.js';
import { toggleRec, togglePause, wireSilenceSel, wireDurationSel } from './modules/recording.js';
import { wireLogCollapse } from './modules/log.js';
import {
  refreshLib, refreshDiskFree,
  togglePreview, togglePreviewTrack, deleteFile, startInlineRename,
  toggleRow, toggleAll, clearSelection, bulkDelete,
  wireSectionCollapse, previewIs,
} from './modules/library.js';
import {
  refreshAlbums, scanAndRefreshAlbums,
  toggleAlbumRow, toggleMusicRow, toggleAllAlbums, toggleAllMusic,
  clearAlbumsSelection, clearMusicSelection,
  bulkDeleteAlbums, bulkDeleteMusic,
  deleteAlbum, demoteAlbumKeepMusic, demoteAlbumDrop,
  purgeAlbumSources, bulkPurgeMusic,
  recordAlbumFailure, clearAlbumFailure, noteAlbumSuccess,
} from './modules/albums.js';
import {
  setSort, onLibSearchInput, clearLibFilter, wireSortableHeaders,
} from './modules/sort-filter.js';
import {
  openTag, openTagAlbum, closeTag,
  runSearch, pickCandidate, pickCollectionCandidate, refreshCollection,
  onFindInput, onFindEnter, wireFindSubtitleLive,
  applyTagPanel, wireTagDirtyTracking,
} from './modules/tagging.js';
import {
  openCombine, moveSide,
  combineDragStart, combineDragOver, combineDragLeave, combineDrop, combineDragEnd,
} from './modules/combine.js';
import {
  openPiDeploy, closePiDeploy, runPiDeploy,
} from './modules/pi-deploy.js';
import {
  openOnboarding, closeOnboarding, initOnboarding,
} from './modules/onboarding.js';
import { wsConnect, sendVisibility } from './modules/ws.js';
import { applyConfig } from './modules/config.js';
import { parseError, withJobProgress, showBar, hideBar } from './modules/api.js';
import { toast } from './modules/log.js';
import {
  htmlEscape, fmtSourceFormat, fmtDuration, toastWithUndo,
} from './modules/util.js';

// ── window-attached entry points ─────────────────────────────────────────
// All of the inline-handler-targeted names. Keep this list in sync when
// adding a new `onclick="newName(...)"`.
//
// The wave-editor.js / peaks.js classic scripts also reach for some of
// these via `typeof toast === 'function'` etc.; same window slot is fine.

// upstream / connection
window.toggleConnect = toggleConnect;
window.toggleMute = toggleMute;
window.toggleHeaderMenu = toggleHeaderMenu;
window.closeHeaderMenu = closeHeaderMenu;
window.toggleHealthPanel = toggleHealthPanel;
window.toggleSidebar = toggleSidebar;
window.clearClip = clearClip;

// recording
window.toggleRec = toggleRec;
window.togglePause = togglePause;

// library / albums
window.refreshLib = refreshLib;
window.refreshAlbums = refreshAlbums;
window.scanAndRefreshAlbums = scanAndRefreshAlbums;
// The "↻ refresh" action runs the music/ orphan scan + reloads both
// sections. The 15 s poll deliberately skips the scan (it's a user-driven
// concern, not something that needs to happen on every tick).
window.refreshAll = () => { scanAndRefreshAlbums(); refreshLib(); };
window.togglePreview = togglePreview;
window.togglePreviewTrack = togglePreviewTrack;
window.deleteFile = deleteFile;
window.startInlineRename = startInlineRename;
window.toggleRow = toggleRow;
window.toggleAll = toggleAll;
window.clearSelection = clearSelection;
window.bulkDelete = bulkDelete;
window.toggleAlbumRow = toggleAlbumRow;
window.toggleMusicRow = toggleMusicRow;
window.toggleAllAlbums = toggleAllAlbums;
window.toggleAllMusic = toggleAllMusic;
window.clearAlbumsSelection = clearAlbumsSelection;
window.clearMusicSelection = clearMusicSelection;
window.bulkDeleteAlbums = bulkDeleteAlbums;
window.bulkDeleteMusic = bulkDeleteMusic;
window.deleteAlbum = deleteAlbum;
window.demoteAlbumKeepMusic = demoteAlbumKeepMusic;
window.demoteAlbumDrop = demoteAlbumDrop;
window.purgeAlbumSources = purgeAlbumSources;
window.bulkPurgeMusic = bulkPurgeMusic;
window.clearAlbumFailure = clearAlbumFailure;
// Wave-editor calls these via `typeof noteAlbumSuccess === 'function'` /
// `typeof recordAlbumFailure === 'function'` — keep the global names.
window.noteAlbumSuccess = noteAlbumSuccess;
window.recordAlbumFailure = recordAlbumFailure;
window.previewIs = previewIs;

// sort / filter / search
window.setSort = setSort;
window.onLibSearchInput = onLibSearchInput;
window.clearLibFilter = clearLibFilter;

// tag panel + combine
window.openTag = openTag;
window.openTagAlbum = openTagAlbum;
window.closeTag = closeTag;
window.runSearch = runSearch;
window.pickCandidate = pickCandidate;
window.pickCollectionCandidate = pickCollectionCandidate;
window.refreshCollection = refreshCollection;
window.onFindInput = onFindInput;
window.onFindEnter = onFindEnter;
window.applyTagPanel = applyTagPanel;
window.openCombine = openCombine;
window.moveSide = moveSide;
window.combineDragStart = combineDragStart;
window.combineDragOver = combineDragOver;
window.combineDragLeave = combineDragLeave;
window.combineDrop = combineDrop;
window.combineDragEnd = combineDragEnd;

// pi deploy
window.openPiDeploy = openPiDeploy;
window.closePiDeploy = closePiDeploy;
window.runPiDeploy = runPiDeploy;

// onboarding overlay
window.openOnboarding = openOnboarding;
window.closeOnboarding = closeOnboarding;

// Globals consumed by wave-editor.js (a classic script that used to share
// script-scope with the old monolithic main.js). The name on `window` is
// the same name wave-editor.js reads. Only the names actually referenced
// by wave-editor.js / peaks.js / inline onclick handlers are exposed —
// other util helpers stay module-scoped.
window.toast = toast;
// Exposed for the classic-script wave-editor.js (clear-cuts undo).
window.toastWithUndo = toastWithUndo;
window.parseError = parseError;
window.withJobProgress = withJobProgress;
window.showBar = showBar;
window.hideBar = hideBar;
window.htmlEscape = htmlEscape;
window.fmtSourceFormat = fmtSourceFormat;
window.fmtDuration = fmtDuration;

// `albumsByName` / `filesByName` were module-scope `let` bindings in the
// monolithic file. wave-editor.js reads them as bare names; expose live
// views through getters so writes from refreshAlbums / refreshLib remain
// visible without us having to keep a separate cached reference in sync.
Object.defineProperty(window, 'albumsByName', {
  configurable: false,
  get() { return state.albumsByName; },
});
Object.defineProperty(window, 'filesByName', {
  configurable: false,
  get() { return state.filesByName; },
});

// ── boot ─────────────────────────────────────────────────────────────────
// In the original, these calls ran at the bottom of the classic-script
// main.js — so they fired AFTER index.html parsed. Wrapping in
// DOMContentLoaded keeps the same ordering when main.js is loaded as a
// module (which defers anyway, but DOMContentLoaded is still the contract
// the rest of the page is built around).
document.addEventListener('DOMContentLoaded', () => {
  startMeterIdleTicker();
  wireGainSlider();
  restoreSidebarState();
  wireTagDirtyTracking();
  wireFindSubtitleLive();
  wireSectionCollapse();

  applyConfig();
  wireSilenceSel();
  wireDurationSel();
  ensureAudioGraph();
  applyMuteState();
  wireSortableHeaders();
  wireLogCollapse();
  wsConnect();
  refreshLib();
  // Scan music/ for manually-added albums on first paint; subsequent polls
  // skip the scan and just refresh the listing.
  scanAndRefreshAlbums();
  refreshDiskFree();

  _startLibPoll();
  setInterval(refreshDiskFree, 30000);

  // First-run onboarding overlay. Fires after the main init sequence so
  // the app behind it is already wired; a no-op once the `vr.onboarded`
  // localStorage flag is set, so returning users never see it.
  initOnboarding();

  // Polling pauses while the tab is hidden — a backgrounded laptop or
  // tabbed-out user shouldn't keep firing fetches. `visibilitychange` fires
  // when the tab comes back, at which point we refresh once and resume the
  // interval. Avoids the buildup of pending requests that browsers used to
  // queue while the tab was throttled.
  document.addEventListener('visibilitychange', () => {
    // Always tell the server about the flip — drives the upstream lifecycle
    // hold so ffmpeg can idle while no tab is visible.
    sendVisibility();
    if (document.hidden) {
      _stopLibPoll();
    } else {
      refreshLib().catch(() => {});
      refreshAlbums().catch(() => {});
      refreshDiskFree().catch(() => {});
      _startLibPoll();
    }
  });
});

let _libPollTimer = null;
function _startLibPoll() {
  if (_libPollTimer) return;
  _libPollTimer = setInterval(() => { refreshLib(); refreshAlbums(); }, 15000);
}
function _stopLibPoll() {
  if (_libPollTimer) { clearInterval(_libPollTimer); _libPollTimer = null; }
}

// Global keyboard shortcut: `R` toggles record/stop. Suppressed while the
// user is typing in any form field (input/textarea/select/contenteditable)
// or while a modal is open — modals install their own scoped key handlers
// and would otherwise see two interpretations of the same keystroke. The
// record button's title + aria-label advertise the shortcut.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'r' && e.key !== 'R') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && t.matches && t.matches('input, textarea, select, [contenteditable="true"]')) return;
  // Modals scope their own shortcuts (Escape to close, Tab to cycle); skip
  // R to avoid stepping on text input inside them.
  const openModal = document.querySelector('.modal-backdrop:not([hidden])');
  if (openModal) return;
  e.preventDefault();
  if (typeof toggleRec === 'function') toggleRec();
});
