// Shared mutable client state.
//
// All connection/recording truth lives on the server; these mirrors are
// updated from WebSocket frames so every tab stays in lockstep.
//
// Exporting a single `state` object (rather than individual `let` bindings)
// so that reads from other modules see the latest mutations: ES module
// `let` exports are live bindings only when the exporting module mutates
// them — assigning back to a re-imported name is a syntax error. A shared
// object dodges that whole class of bug while keeping the call sites
// simple (`state.recording = true`, `state.sessionId`, etc.).

export const state = {
  recording: false,
  sessionId: null,
  paused: false,
  upstreamConnected: false,
  // Local — each tab decides its own playback volume.
  muted: true,
  audioEl: null,

  // Library data + selection sets.
  filesByName: {},
  albumsByName: {},
  selected: new Set(),
  albumsSelected: new Set(),
  musicSelected: new Set(),

  // Disk space thresholds (overwritten from /api/config).
  lowSpaceGb: 2.0,
  warnSpaceGb: 10.0,

  // Persisted across reloads; restored at boot, written from setSort/onLibSearchInput.
  sortBy:  localStorage.getItem('lib.sortBy')  || 'date',
  sortDir: localStorage.getItem('lib.sortDir') || 'desc',
  libFilterText: localStorage.getItem('lib.filterText') || '',
  libVisibleNames: new Set(),

  // Default gain to apply once after first probe (set from /api/config).
  pendingDefaultGainDb: null,
};
