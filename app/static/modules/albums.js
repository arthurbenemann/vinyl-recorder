// In-progress + Music sections. /api/albums returns both kinds in a
// single payload — the renderer splits on the `split` flag. Same row
// shape as the Raw library; this module focuses on bulk selection,
// per-album failure pills, and the demote / delete actions.

import { htmlEscape, fmtDate, fmtDateFull, fmtSourceFormat, fmtDuration } from './util.js';
import { toast } from './log.js';
import { state } from './state.js';
import { actionBtn, setTbodyIfChanged } from './dom-helpers.js';
import { sortFiles, rowMatches } from './sort-filter.js';
import { refreshLib } from './library.js';

function _albumsBySplit(split) {
  return Object.values(state.albumsByName).filter(a => !!a.split === !!split);
}

export function updateAlbumsBulkBar() {
  const bar = document.getElementById('albums-bulk-bar');
  const cnt = document.getElementById('albums-bulk-count');
  if (!bar || !cnt) return;
  cnt.textContent = state.albumsSelected.size;
  bar.classList.toggle('hidden', state.albumsSelected.size === 0);
  const total = _albumsBySplit(false).length;
  const checkAll = document.getElementById('albums-check-all');
  if (checkAll) checkAll.checked = total > 0 && state.albumsSelected.size === total;
}

export function updateMusicBulkBar() {
  const bar = document.getElementById('music-bulk-bar');
  const cnt = document.getElementById('music-bulk-count');
  if (!bar || !cnt) return;
  cnt.textContent = state.musicSelected.size;
  bar.classList.toggle('hidden', state.musicSelected.size === 0);
  const total = _albumsBySplit(true).length;
  const checkAll = document.getElementById('music-check-all');
  if (checkAll) checkAll.checked = total > 0 && state.musicSelected.size === total;
}

export function toggleAlbumRow(fname, checked) {
  if (checked) state.albumsSelected.add(fname); else state.albumsSelected.delete(fname);
  updateAlbumsBulkBar();
}

export function toggleMusicRow(fname, checked) {
  if (checked) state.musicSelected.add(fname); else state.musicSelected.delete(fname);
  updateMusicBulkBar();
}

export function toggleAllAlbums(checked) {
  if (checked) _albumsBySplit(false).forEach(a => state.albumsSelected.add(a.album_id));
  else state.albumsSelected.clear();
  document.querySelectorAll('.album-row-check').forEach(cb => { cb.checked = checked; });
  updateAlbumsBulkBar();
}

export function toggleAllMusic(checked) {
  if (checked) _albumsBySplit(true).forEach(a => state.musicSelected.add(a.album_id));
  else state.musicSelected.clear();
  document.querySelectorAll('.music-row-check').forEach(cb => { cb.checked = checked; });
  updateMusicBulkBar();
}

export function clearAlbumsSelection() {
  state.albumsSelected.clear();
  refreshAlbums();
  updateAlbumsBulkBar();
}

export function clearMusicSelection() {
  state.musicSelected.clear();
  refreshAlbums();
  updateMusicBulkBar();
}

async function _bulkDeleteAlbumNames(ids, label) {
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} ${label}? Music tracks emitted from these albums will also be removed.`)) return;
  for (const album_id of ids) {
    try { await fetch(`/api/albums/${album_id}`, { method: 'DELETE' }); }
    catch (e) { console.error(e); }
  }
  toast(`✓ Deleted ${ids.length} ${label}`, 'ok');
}

export async function bulkDeleteAlbums() {
  const names = [...state.albumsSelected];
  await _bulkDeleteAlbumNames(names, names.length === 1 ? 'album' : 'albums');
  state.albumsSelected.clear();
  refreshAlbums();
}

export async function bulkDeleteMusic() {
  const names = [...state.musicSelected];
  await _bulkDeleteAlbumNames(names, names.length === 1 ? 'album' : 'albums');
  state.musicSelected.clear();
  refreshAlbums();
}

// ── Delete originals (sides + cache) for split albums ─────────────────────
// "Locks in" the splits/encoding by removing the source audio, while keeping
// album.json so the row stays visible in the Music section.

function _fmtMb(mb) {
  if (mb == null) return '0 MB';
  if (mb >= 1024) return (mb / 1024).toFixed(2) + ' GB';
  return mb.toFixed(1) + ' MB';
}

async function _purgeOriginalsForIds(ids) {
  // Filter to split albums that still have originals on disk — the backend
  // would 409 the others, but it's nicer to drop them before confirming so
  // the freed-size estimate matches reality.
  const purgeable = ids
    .map(id => state.albumsByName[id])
    .filter(a => a && a.split && !a.sources_purged);
  if (!purgeable.length) {
    toast('Nothing to delete — originals already removed.', 'info');
    return 0;
  }
  const totalMb = purgeable.reduce((s, a) => s + (a.size_mb || 0), 0);
  const noun = purgeable.length === 1 ? 'album' : 'albums';
  const msg = `Delete originals for ${purgeable.length} ${noun}?\n\n` +
    `This frees ~${_fmtMb(totalMb)} of disk.\n\n` +
    `Locks in the current splits and encoding — the wave editor will no ` +
    `longer be able to re-split or re-encode these albums. The music/ ` +
    `tracks already on disk are kept.`;
  if (!confirm(msg)) return 0;
  let okCount = 0;
  let freedBytes = 0;
  for (const a of purgeable) {
    try {
      const r = await fetch(`/api/album/${a.album_id}/purge-sources`, { method: 'POST' });
      if (r.ok) {
        const d = await r.json();
        freedBytes += d.bytes_freed || 0;
        okCount++;
      }
    } catch (e) { console.error(e); }
  }
  const freedMb = freedBytes / 1e6;
  toast(`✓ Deleted originals for ${okCount} ${okCount === 1 ? 'album' : 'albums'} — freed ${_fmtMb(freedMb)}`, 'ok');
  return okCount;
}

export async function bulkPurgeMusic() {
  const names = [...state.musicSelected];
  await _purgeOriginalsForIds(names);
  state.musicSelected.clear();
  refreshAlbums();
}

export async function purgeAlbumSources(album_id) {
  await _purgeOriginalsForIds([album_id]);
  refreshAlbums();
}

export async function refreshAlbums() {
  try {
    const r = await fetch('/api/albums');
    const d = await r.json();
    state.albumsByName = {};
    (d.albums || []).forEach(a => state.albumsByName[a.album_id] = a);
    [...state.albumsSelected].forEach(id => {
      if (!state.albumsByName[id] || state.albumsByName[id].split) state.albumsSelected.delete(id);
    });
    [...state.musicSelected].forEach(id => {
      if (!state.albumsByName[id] || !state.albumsByName[id].split) state.musicSelected.delete(id);
    });
    // Drop stale failures for albums that no longer exist (deleted/demoted).
    [...albumErrors.keys()].forEach(id => {
      if (!state.albumsByName[id]) albumErrors.delete(id);
    });
    refreshAlbumsRender();
  } catch (e) { console.error(e); }
}

// ── Per-album failure tracking (client-only, session-scoped) ──────────────
// Long-running album jobs (split, measure, normalize, silence-detect) only
// surface failures via a 4 s toast today, which is easy to miss. We keep a
// session Map<album_id, "<op>: <message>"> populated by the editor's catch
// blocks, render a `.fail-pill` in the row's status cell when set, and clear
// the entry on dismiss / next successful run / album removal.
//
// Trade-off note (in PR description): persistence on the album manifest was
// considered but skipped to keep this PR small. Refreshes / new tabs lose
// the indicator; that's an acceptable cost for the UX win and avoids
// touching the jobs registry or albums.json schema.
const albumErrors = new Map();

export function recordAlbumFailure(albumId, op, message) {
  if (!albumId) return;
  const msg = String(message || '').trim() || 'unknown error';
  albumErrors.set(albumId, { op, message: msg, ts: Date.now() });
  // Re-render so the row picks up the pill without waiting for the next
  // poll (15 s); refreshAlbums() pulls fresh data, refreshAlbumsRender()
  // just re-paints from in-memory state.
  refreshAlbumsRender();
}

export function clearAlbumFailure(albumId) {
  if (!albumErrors.has(albumId)) return;
  albumErrors.delete(albumId);
  refreshAlbumsRender();
}

// On album success — measure / split / silence — drop any stale failure
// pill so a re-run that worked clears the warning. Called from the editor.
export function noteAlbumSuccess(albumId) { clearAlbumFailure(albumId); }

function _albumRowHtml(a, opts) {
  // The "fn" key is the album_id (a slug like 7f3a8c91); HTML escaping is
  // unnecessary (the slug regex is `[a-z0-9_-]+`) but cheap and safe in
  // case a hand-named drop-in dir made it here. All action buttons go
  // through `data-fname` rather than inlining the value as a JS string
  // literal — see the comment on `actionBtn` for the XSS rationale.
  const fn = htmlEscape(a.album_id);
  const isSel = opts.selected.has(a.album_id) ? 'checked' : '';
  const baseCount = a.split
    ? (a.track_count
        ? `<a class="track-count-link" data-fname="${fn}" onclick="toggleTracks(this.dataset.fname)">${a.track_count} tracks</a>`
        : '—')
    : `${a.side_count || '—'}`;
  // Failure pill — only present when the editor recorded a failure for this
  // album in the current session. Clicking dismisses; the title shows the
  // full server-side message (which can be long).
  const err = albumErrors.get(a.album_id);
  const failPill = err
    ? ` <button class="fail-pill" data-fname="${fn}"
        title="${htmlEscape(err.op + ': ' + err.message)} — click to dismiss"
        aria-label="${htmlEscape('Failed: ' + err.op + ' — click to dismiss')}"
        onclick="clearAlbumFailure(this.dataset.fname)">failed: ${htmlEscape(err.op)}</button>`
    : '';
  // Sources-purged pill — informs the user the album is "locked" (no source
  // audio left, so the wave editor / demote / re-split paths are gone).
  const lockedPill = a.sources_purged
    ? ` <span class="locked-pill" title="Originals deleted — splits and encoding are locked in. The wave editor can no longer re-split or re-encode this album." aria-label="Originals deleted — locked">🔒 locked</span>`
    : '';
  const countCell = `${baseCount}${failPill}${lockedPill}`;
  const splitTitle = a.split ? 'Re-edit splits' : 'Split into tracks';
  // Demote button is offered on every album; for split albums the dialog
  // warns that music/ stays put.
  const demoteHandler = a.split ? 'demoteAlbumKeepMusic' : 'demoteAlbumDrop';
  const demoteLabel = a.split ? 'Demote to Raw (music/ files preserved)' : 'Demote to Raw';
  // Context for screen readers — every action mentions the album so the
  // announcement makes sense without first navigating to the row's title.
  const albumLabel = a.album || '(untitled album)';
  const artistLabel = a.artist || '';
  const ctx = artistLabel ? `${albumLabel} — ${artistLabel}` : albumLabel;
  const demoteAria = a.split
    ? `Move ${ctx} sides back to raw — music files preserved`
    : `Move ${ctx} sides back to raw`;
  const tagBtn = actionBtn('openTagAlbum', a.album_id, {label: 'Edit tags', ariaLabel: 'Edit tags for ' + ctx, glyph: '✎'});
  // Wave-editor + demote depend on the side FLACs still being on disk. Once
  // the user has purged the originals the row is "locked" — only tag-edit
  // and delete (and on split albums, no purge button either) remain.
  const splitBtn = a.sources_purged
    ? ''
    : actionBtn('openWaveEditor', a.album_id, {label: splitTitle, ariaLabel: splitTitle + ' for ' + ctx, glyph: '✂'});
  const demBtn = a.sources_purged
    ? ''
    : actionBtn(demoteHandler, a.album_id, {label: demoteLabel, ariaLabel: demoteAria, glyph: '⤺'});
  // Only split albums with originals still present get the "delete sources"
  // affordance. Sized in MB so the user can weigh the cost-of-freeing before
  // committing.
  const purgeBtn = (a.split && !a.sources_purged)
    ? actionBtn('purgeAlbumSources', a.album_id, {
        label: `Delete originals (~${a.size_mb || 0} MB)`,
        ariaLabel: `Delete original sides for ${ctx} — frees about ${a.size_mb || 0} MB and locks in splits`,
        glyph: '🗜',
      })
    : '';
  const delBtn = actionBtn('deleteAlbum', a.album_id, {label: 'Delete album', ariaLabel: 'Delete album ' + ctx, glyph: '✕', danger: true});
  const checkboxAria = htmlEscape('Select ' + ctx + ' for bulk action');
  return `
  <tr data-album-id="${fn}">
    <td class="col-check" data-col="check"><input type="checkbox" class="${opts.checkClass}" data-fname="${fn}" ${isSel} aria-label="${checkboxAria}"
        onclick="${opts.toggleRow}(this.dataset.fname, this.checked)"></td>
    <td data-col="album" style="font-weight:500">
      <div class="row-title">
        <span class="row-thumb"><img src="/api/file-cover/${fn}" loading="lazy" onerror="this.remove()"></span>
        <span class="row-title-text">${htmlEscape(a.album || '(untitled album)')}</span>
      </div>
    </td>
    <td data-col="artist" style="color:var(--muted)">${htmlEscape(a.artist || '—')}</td>
    <td data-col="year" style="color:var(--muted)">${htmlEscape(a.year || '—')}</td>
    <td data-col="recorded" style="color:var(--muted);white-space:nowrap" title="${htmlEscape(fmtDateFull(a.mtime))}">${htmlEscape(fmtDate(a.mtime))}</td>
    <td data-col="length" style="color:var(--muted)">${fmtDuration(a.duration_seconds)}</td>
    <td data-col="size" style="color:var(--muted)">${a.size_mb} MB</td>
    <td data-col="fmt" style="color:var(--muted);font-variant-numeric:tabular-nums" title="N-bit / M kHz">${fmtSourceFormat(a)}</td>
    <td data-col="status" style="color:var(--muted)">${countCell}</td>
    <td data-col="actions" style="white-space:nowrap;text-align:right">${tagBtn}${splitBtn}${demBtn}${purgeBtn}${delBtn}</td>
  </tr>`;
}

// `demoteAlbum(album_id, musicPreserved)` is the underlying call; the row
// renderer can't invoke it via `data-fname` alone because it carries a
// second arg. Wrap it as two single-arg helpers so the data-attribute
// pattern still works.
export function demoteAlbumKeepMusic(album_id) { return demoteAlbum(album_id, true); }
export function demoteAlbumDrop(album_id)      { return demoteAlbum(album_id, false); }

function _renderAlbumSection(opts) {
  // opts: { all, countId, tbodyId, label, emptyMsg, checkClass,
  //         toggleRow, updateBulkBar, selected }
  const filtered = sortFiles(opts.all.filter(rowMatches));
  const total = opts.all.length;
  const shown = filtered.length;
  const filterActive = !!state.libFilterText.trim();
  const countEl = document.getElementById(opts.countId);
  if (countEl) {
    countEl.textContent = filterActive
      ? `${shown} of ${total} ${opts.label}${total === 1 ? '' : 's'}`
      : `${total} ${opts.label}${total === 1 ? '' : 's'}`;
  }
  const tbody = document.getElementById(opts.tbodyId);
  if (!tbody) return;
  if (!filtered.length) {
    const colspan = tbody.parentElement.querySelector('thead tr').children.length;
    const msg = total === 0 ? opts.emptyMsg : 'No matches for current filter.';
    setTbodyIfChanged(tbody, `<tr><td colspan="${colspan}" class="empty-lib">${msg}</td></tr>`);
    opts.updateBulkBar();
    return;
  }
  setTbodyIfChanged(tbody, filtered.map(a => _albumRowHtml(a, opts)).join(''));
  opts.updateBulkBar();
}

export function refreshAlbumsRender() {
  _renderAlbumSection({
    all:           _albumsBySplit(false),
    countId:       'in-progress-count',
    tbodyId:       'albums-tbody',
    label:         'album',
    emptyMsg:      'No albums in progress.',
    checkClass:    'album-row-check',
    toggleRow:     'toggleAlbumRow',
    updateBulkBar: updateAlbumsBulkBar,
    selected:      state.albumsSelected,
  });
  _renderAlbumSection({
    all:           _albumsBySplit(true),
    countId:       'music-count',
    tbodyId:       'music-tbody',
    label:         'album',
    emptyMsg:      'No split albums yet.',
    checkClass:    'music-row-check',
    toggleRow:     'toggleMusicRow',
    updateBulkBar: updateMusicBulkBar,
    selected:      state.musicSelected,
  });
}

export async function deleteAlbum(album_id) {
  const a = state.albumsByName[album_id];
  const label = (a && a.album) || album_id;
  const splitWarn = (a && a.split)
    ? `\n\nThe music/${a.music_relpath || '...'} folder will be removed too.`
    : '';
  if (!confirm(`Delete album "${label}"?${splitWarn}`)) return;
  const r = await fetch(`/api/albums/${album_id}`, { method: 'DELETE' });
  if (r.ok) {
    toast(`✓ Album deleted — ${label}`, 'ok');
    refreshAlbums();
  } else {
    toast('✗ delete failed', 'err');
  }
}

export async function demoteAlbum(album_id, isSplit) {
  const a = state.albumsByName[album_id];
  const label = (a && a.album) || album_id;
  const sideCount = (a && a.side_count) || '?';
  const tail = isSplit
    ? `\n\nThe already-emitted music/${(a && a.music_relpath) || '...'} folder will be left untouched.`
    : '';
  const msg = `Demote "${label}" back to Raw?\n\n${sideCount} side(s) will be moved into raw/. Album metadata will be discarded.${tail}`;
  if (!confirm(msg)) return;
  const r = await fetch(`/api/album/${album_id}/demote`, { method: 'POST' });
  if (r.ok) {
    toast(`✓ Demoted — ${label}`, 'ok');
    refreshLib();
    refreshAlbums();
  } else {
    toast('✗ demote failed', 'err');
  }
}
