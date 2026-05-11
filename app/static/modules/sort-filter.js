// Sort + filter logic shared between the Raw library, In-progress, and
// Music sections. The library and albums modules both render the same
// table shape, so the row-matching predicate, sort comparator, and the
// header-state updater all live here.

import { state } from './state.js';
import { refreshLib, refreshLibRender } from './library.js';
import { refreshAlbumsRender } from './albums.js';

const SORT_KEYS = {
  album:  f => (f.album  || f.filename).toLowerCase(),
  artist: f => (f.artist || '').toLowerCase(),
  year:   f => parseInt(f.year, 10) || 0,
  length: f => f.duration_seconds || 0,
  size:   f => f.size_mb || 0,
  // Sort by total information rate (bps × Hz) so 24/96 ranks above 16/44.1.
  fmt:    f => (f.bit_depth || 0) * (f.sample_rate_khz || 0),
  date:   f => f.mtime || 0,
};

export function rowMatches(f) {
  const q = state.libFilterText.trim().toLowerCase();
  if (!q) return true;
  const hay = [
    f.filename, f.artist, f.album, f.year, f.genre, f.label, f.catalog_number,
  ].filter(Boolean).join(' ').toLowerCase();
  return hay.includes(q);
}

export function applyLibFilterControls() {
  const inp = document.getElementById('lib-search');
  if (inp) {
    if (document.activeElement !== inp) inp.value = state.libFilterText;
    inp.classList.toggle('active', !!state.libFilterText.trim());
  }
  const clr = document.getElementById('lib-filter-clear');
  if (clr) clr.hidden = !state.libFilterText.trim();
}

export function onLibSearchInput(v) {
  state.libFilterText = v;
  localStorage.setItem('lib.filterText', v);
  refreshLibRender();
  refreshAlbumsRender();
  _announceLibCount();
}

export function clearLibFilter() {
  state.libFilterText = '';
  localStorage.removeItem('lib.filterText');
  refreshLibRender();
  refreshAlbumsRender();
  _announceLibCount();
}

// Push a polite "N results for …" message into the library search status
// live region so screen-reader users get audible feedback when the filter
// changes. Re-counts across the three sections (raw / in-progress / music)
// so the announcement reflects the visible row total. Empty when no filter
// is active — the live region's `aria-atomic="true"` then drops the prior
// count from the AT buffer instead of repeating it.
function _announceLibCount() {
  const el = document.getElementById('lib-search-status');
  if (!el) return;
  const q = state.libFilterText.trim();
  if (!q) { el.textContent = ''; return; }
  let n = 0;
  for (const f of Object.values(state.filesByName)) if (rowMatches(f)) n += 1;
  for (const a of Object.values(state.albumsByName)) if (rowMatches(a)) n += 1;
  el.textContent = `${n} ${n === 1 ? 'result' : 'results'} for "${q}"`;
}

export function setSort(col) {
  if (state.sortBy === col) {
    state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    state.sortBy = col;
    // Sensible default direction per column: text → asc, numeric/date → desc.
    state.sortDir = (col === 'album' || col === 'artist') ? 'asc' : 'desc';
  }
  localStorage.setItem('lib.sortBy',  state.sortBy);
  localStorage.setItem('lib.sortDir', state.sortDir);
  refreshLib();
  refreshAlbumsRender();
}

export function sortFiles(files) {
  const key = SORT_KEYS[state.sortBy] || SORT_KEYS.date;
  const dir = state.sortDir === 'asc' ? 1 : -1;
  // Stable secondary sort by mtime (newest first) so equal keys keep a
  // predictable order between renders.
  return files.slice().sort((a, b) => {
    const ka = key(a), kb = key(b);
    if (ka < kb) return -1 * dir;
    if (ka > kb) return  1 * dir;
    return (b.mtime || 0) - (a.mtime || 0);
  });
}

export function updateSortHeaders() {
  const arrow = state.sortDir === 'asc' ? '▲' : '▼';
  document.querySelectorAll('.lib-table th.sortable').forEach(th => {
    const active = th.dataset.sort === state.sortBy;
    th.classList.toggle('sorted', active);
    th.querySelector('.sort-arrow').textContent = active ? arrow : '';
    // Reflect sort state for screen readers. `aria-sort` on a th is the
    // standard signal — "ascending" / "descending" on the active column,
    // "none" on the others.
    th.setAttribute('aria-sort',
      active ? (state.sortDir === 'asc' ? 'ascending' : 'descending') : 'none');
  });
}

// Make `.sortable` headers keyboard-activatable. The HTML uses bare `<th>`
// with `onclick`, which mouse users can hit but keyboard users can't reach
// (TH isn't focusable by default). We add role=button + tabindex on first
// load and forward Enter/Space to the same setSort handler the click uses.
export function wireSortableHeaders() {
  document.querySelectorAll('.lib-table th.sortable').forEach(th => {
    if (th.dataset.kbWired === '1') return;
    th.dataset.kbWired = '1';
    th.setAttribute('role', 'button');
    th.setAttribute('tabindex', '0');
    th.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        setSort(th.dataset.sort);
      }
    });
  });
}
