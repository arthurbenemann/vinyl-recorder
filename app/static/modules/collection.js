// Collection section: the user's Discogs collection rendered as a
// recording checklist below the Music section. Recorded releases (exact
// discogs_release_id match server-side) get a ✓; not-yet-recorded ones are
// dimmed so it's easy to see which records still need a needledrop. Rows
// are read-only apart from a ↗ link to the release on discogs.com — no
// selection, no bulk actions.
//
// The section is server-gated: it stays `hidden` unless /api/collection/
// status reports enabled (i.e. DISCOGS_USERNAME is configured). It is NOT
// on the 15 s poll — the collection itself changes rarely, and recorded
// status only moves when the albums list does, so refreshAlbums() pings
// `maybeRefreshOnAlbumsChange()` instead.

import { htmlEscape } from './util.js';
import { toast } from './log.js';
import { state } from './state.js';
import { setTbodyIfChanged } from './dom-helpers.js';
import { rowMatches } from './sort-filter.js';

// Adapted /api/collection/status releases. Each row carries the original
// fields plus `album`/`catalog_number` aliases so the shared rowMatches()
// predicate sees the same shape as the other library sections.
let _rows = [];
let _enabled = false;
// Signature of state.albumsByName at the last fetch — lets refreshAlbums()
// trigger a recompute only when an album appeared/disappeared or changed
// its Discogs id, instead of refetching on every 15 s poll tick.
let _albumsSig = null;

function _albumsSignature() {
  return Object.values(state.albumsByName)
    .map(a => `${a.album_id}:${a.discogs_release_id || ''}`)
    .sort()
    .join('|');
}

export async function refreshCollectionStatus() {
  _albumsSig = _albumsSignature();
  try {
    const r = await fetch('/api/collection/status');
    const d = await r.json();
    _enabled = !!d.enabled;
    const section = document.getElementById('collection-section');
    if (!section) return;
    section.hidden = !_enabled;
    if (!_enabled) return;
    _rows = (d.releases || []).map(rel => ({
      ...rel,
      album: rel.title,
      catalog_number: rel.catno,
    }));
    renderCollectionSection();
  } catch (e) { console.error(e); }
}

// Called from refreshAlbums() after every albums fetch. Cheap no-op unless
// the album set actually changed since the last status fetch.
export function maybeRefreshOnAlbumsChange() {
  if (!_enabled || _albumsSig === null) return;
  if (_albumsSignature() !== _albumsSig) refreshCollectionStatus();
}

// Hot-refresh the server-side Discogs cache (1 h TTL otherwise), then
// re-pull the checklist. Wired to the ↻ button in the section summary —
// useful right after adding a record on Discogs.
export async function refreshCollectionFromDiscogs() {
  const btn = document.getElementById('collection-refresh-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/collection/refresh', { method: 'POST' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    toast(`✓ Collection refreshed — ${d.count} release${d.count === 1 ? '' : 's'}`, 'ok');
  } catch (e) {
    console.error(e);
    toast('✗ collection refresh failed', 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
  return refreshCollectionStatus();
}

// Visible-row count for the screen-reader filter announcement in
// sort-filter.js. Zero when the section is disabled/hidden.
export function collectionVisibleCount() {
  if (!_enabled) return 0;
  return _rows.filter(rowMatches).length;
}

// Fixed artist→title sort (the section has no sortable headers in v1).
// Deliberately NOT "unrecorded first": the dimming already separates the
// states, and a stable alphabetical order preserves spatial memory as
// items flip to recorded instead of reshuffling under the user.
function _cmpRows(a, b) {
  const aa = (a.artist || '').toLowerCase(), ba = (b.artist || '').toLowerCase();
  if (aa !== ba) return aa < ba ? -1 : 1;
  const at = (a.title || '').toLowerCase(), bt = (b.title || '').toLowerCase();
  return at < bt ? -1 : at > bt ? 1 : 0;
}

function _rowHtml(r) {
  const title = r.title || '(untitled)';
  const ctx = r.artist ? `${title} — ${r.artist}` : title;
  const rid = Number(r.discogs_release_id) || 0;
  const thumb = r.cover_url
    ? `<span class="row-thumb"><img src="${htmlEscape(r.cover_url)}" loading="lazy" onerror="this.remove()"></span>`
    : '';
  const check = r.recorded
    ? `<span class="rec-check" title="${htmlEscape('Recorded — ' + ctx)}" aria-label="${htmlEscape('Recorded: ' + ctx)}">✓</span>`
    : '';
  const link = rid > 0
    ? `<a class="icon-btn" href="https://www.discogs.com/release/${rid}" target="_blank" rel="noopener"
         title="Open on Discogs" aria-label="${htmlEscape('Open ' + ctx + ' on Discogs')}">↗</a>`
    : '';
  return `
  <tr class="collection-row${r.recorded ? '' : ' unrecorded'}">
    <td data-col="album" style="font-weight:500">
      <div class="row-title">
        ${thumb}
        <span class="row-title-text">${htmlEscape(title)}</span>
      </div>
    </td>
    <td data-col="artist" style="color:var(--muted)">${htmlEscape(r.artist || '—')}</td>
    <td data-col="year" style="color:var(--muted)">${htmlEscape(r.year || '—')}</td>
    <td data-col="label" style="color:var(--muted);white-space:nowrap" title="${htmlEscape([r.label, r.catno].filter(Boolean).join(' · '))}">${htmlEscape([r.label, r.catno].filter(Boolean).join(' · ') || '—')}</td>
    <td data-col="status">${check}</td>
    <td data-col="actions" style="white-space:nowrap;text-align:right">${link}</td>
  </tr>`;
}

export function renderCollectionSection() {
  const tbody = document.getElementById('collection-tbody');
  const countEl = document.getElementById('collection-count');
  if (!tbody || !countEl) return;
  const total = _rows.length;
  const recorded = _rows.filter(r => r.recorded).length;
  const filtered = _rows.filter(rowMatches).sort(_cmpRows);
  const filterActive = !!state.libFilterText.trim();
  countEl.textContent = filterActive
    ? `${recorded} / ${total} recorded · ${filtered.length} shown`
    : `${recorded} / ${total} recorded`;
  if (!filtered.length) {
    const colspan = tbody.parentElement.querySelector('thead tr').children.length;
    const msg = total === 0
      ? 'Collection unavailable or empty — try the ↻ refresh above.'
      : 'No matches for current filter.';
    setTbodyIfChanged(tbody, `<tr><td colspan="${colspan}" class="empty-lib">${msg}</td></tr>`);
    return;
  }
  setTbodyIfChanged(tbody, filtered.map(_rowHtml).join(''));
}
