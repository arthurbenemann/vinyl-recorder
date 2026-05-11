// Raw recordings table — the "library" section. Loads /api/recordings,
// renders the table, drives the bulk-action bar, owns the inline-rename
// flow, and hosts the shared inline-preview <audio>.
//
// Sorting / filtering state is shared with the Albums sections (so a
// single header click reflows all three tables); the helpers live in
// `sort-filter.js` and are imported by both renderers.

import { htmlEscape, fmtDate, fmtDateFull, fmtSourceFormat, fmtDuration } from './util.js';
import { parseError } from './api.js';
import { toast } from './log.js';
import { state } from './state.js';
import { actionBtn, downloadLink, setTbodyIfChanged } from './dom-helpers.js';
import { sortFiles, rowMatches, applyLibFilterControls, updateSortHeaders } from './sort-filter.js';
import { refreshAlbumsRender } from './albums.js';

// ── Bulk selection (Raw section) ─────────────────────────────────────────
export function updateBulkBar() {
  const bar = document.getElementById('bulk-bar');
  document.getElementById('bulk-count').textContent = state.selected.size;
  bar.classList.toggle('hidden', state.selected.size === 0);
  // "Check All" reflects the state of the CURRENTLY VISIBLE rows so a
  // filtered list can still be batch-selected predictably. Off-screen
  // (filtered-out) selections are preserved untouched.
  const visible = state.libVisibleNames.size;
  let visSelected = 0;
  for (const n of state.libVisibleNames) if (state.selected.has(n)) visSelected += 1;
  const checkAll = document.getElementById('check-all');
  if (checkAll) checkAll.checked = visible > 0 && visSelected === visible;
  const combineBtn = document.getElementById('combine-btn');
  if (combineBtn) {
    combineBtn.disabled = state.selected.size < 1;
    combineBtn.textContent = state.selected.size === 1 ? 'tag as album' : 'combine into album';
  }
}

export function toggleRow(fname, checked) {
  if (checked) state.selected.add(fname); else state.selected.delete(fname);
  updateBulkBar();
}

export function toggleAll(checked) {
  // Operate only on currently visible rows so a filter narrows the bulk
  // operation. Items hidden by the filter keep their selection state.
  if (checked) state.libVisibleNames.forEach(fn => state.selected.add(fn));
  else         state.libVisibleNames.forEach(fn => state.selected.delete(fn));
  document.querySelectorAll('.row-check').forEach(cb => { cb.checked = checked; });
  updateBulkBar();
}

export function clearSelection() {
  state.selected.clear();
  document.querySelectorAll('.row-check').forEach(cb => cb.checked = false);
  const ca = document.getElementById('check-all'); if (ca) ca.checked = false;
  updateBulkBar();
}

export async function bulkDelete() {
  if (!state.selected.size) return;
  const names = [...state.selected];
  if (!confirm(`Delete ${names.length} recording${names.length===1?'':'s'}? This cannot be undone.`)) return;
  const bar = document.getElementById('bulk-action-bar');
  const fill = document.getElementById('bulk-action-fill');
  document.getElementById('bulk-action-phase').textContent = 'deleting…';
  document.getElementById('bulk-action-pct').textContent = '';
  fill.classList.add('indeterminate');
  bar.hidden = false;
  try {
    const r = await fetch('/api/recordings/bulk-delete', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({filenames: names})
    });
    const d = await r.json();
    toast(`✓ Deleted ${d.deleted.length} file${d.deleted.length===1?'':'s'}`,
          d.missing?.length ? 'err' : 'ok');
    state.selected.clear();
    refreshLib();
  } catch(e) { toast('✗ ' + e.message, 'err'); }
  finally {
    bar.hidden = true;
    fill.classList.remove('indeterminate');
    fill.style.width = '0%';
  }
}

// ── Library refresh + render ─────────────────────────────────────────────
export async function refreshLib() {
  try {
    const r = await fetch('/api/recordings');
    const d = await r.json();
    updateDiskFree(d.disk_free_gb);
    state.filesByName = {};
    d.files.forEach(f => state.filesByName[f.filename] = f);
    // drop selections that no longer exist
    [...state.selected].forEach(fn => { if (!state.filesByName[fn]) state.selected.delete(fn); });
    refreshLibRender();
  } catch(e) { console.error(e); }
}

export function refreshLibRender() {
  applyLibFilterControls();
  const tbody = document.getElementById('lib-tbody');
  if (!tbody) return;
  const all = Object.values(state.filesByName);
  const filtered = all.filter(rowMatches);
  const files = sortFiles(filtered);
  state.libVisibleNames = new Set(files.map(f => f.filename));
  const total = all.length;
  const shown = files.length;
  const filterActive = !!state.libFilterText.trim();
  const countEl = document.getElementById('lib-count');
  if (countEl) {
    countEl.textContent = filterActive
      ? `${shown} of ${total} file${total===1?'':'s'}`
      : `${total} file${total===1?'':'s'}`;
  }
  updateSortHeaders();
  if (!files.length) {
    const msg = total === 0
      ? 'No recordings yet. Drop the needle!'
      : 'No matches for current filter.';
    setTbodyIfChanged(tbody, `<tr><td colspan="9" class="empty-lib">${msg}</td></tr>`);
    updateBulkBar();
    return;
  }
  const _libRowsHtml = files.map(f => {
      const fn = htmlEscape(f.filename);
      const isSel = state.selected.has(f.filename) ? 'checked' : '';
      const playing = previewIs(f.filename, 'lib') ? 'playing' : '';
      const playGlyph = previewIs(f.filename, 'lib') ? '⏸' : '▶';
      const titleText = htmlEscape(f.album || f.filename.replace('.flac',''));
      // Raw rows are by definition untagged: the dblclick rename always
      // applies, the amber accent bar (.row-untagged in style.css) is
      // unconditional. The handler lives on the whole <td> so the entire
      // cell — including padding and whitespace to the right of short
      // titles — is a click target.
      // Context for AT announcements — "Preview Album — Artist" is far more
      // useful out of context than the bare glyph. Falls back to the filename
      // when no album/artist tags exist (raw rows are typically untagged).
      const albumLabel = f.album || f.filename.replace(/\.flac$/, '');
      const artistLabel = f.artist || '';
      const ctx = artistLabel ? `${albumLabel} — ${artistLabel}` : albumLabel;
      const previewBtn = `<button class="icon-btn preview-btn ${playing}" data-fname="${fn}" data-kind="lib" title="Preview" aria-label="${htmlEscape('Preview ' + ctx)}" onclick="togglePreview(this.dataset.fname, this.dataset.kind)">${playGlyph}</button>`;
      const dlLink = downloadLink(`/api/download/${encodeURIComponent(f.filename)}`, 'Download', 'Download ' + ctx);
      const delBtn = actionBtn('deleteFile', f.filename, {label: 'Delete', ariaLabel: 'Delete ' + ctx, glyph: '✕', danger: true});
      // Inline rename pencil: same handler the dblclick uses. Filename
      // travels via data-fname (HTML escaped) — see actionBtn for the
      // XSS rationale on never inlining values into the JS string.
      const renameGlyph = `<button class="rename-glyph" data-fname="${fn}" title="Rename" aria-label="${htmlEscape('Rename ' + ctx)}" onclick="event.stopPropagation();startInlineRename(this.dataset.fname, this.previousElementSibling)">✎</button>`;
      // Per-row select checkbox: the bare checkbox has no label, so AT
      // hears only "checkbox unchecked". Adding the album to the aria-label
      // keeps the bulk-action row navigable when there are many rows.
      const checkboxAria = htmlEscape('Select ' + ctx + ' for bulk action');
      return `
      <tr class="row-untagged">
        <td class="col-check" data-col="check"><input type="checkbox" class="row-check" data-fname="${fn}" ${isSel} aria-label="${checkboxAria}"
            onclick="toggleRow(this.dataset.fname, this.checked)"></td>
        <td data-col="album" style="font-weight:500" ondblclick="startInlineRename(this.dataset.fname, this.querySelector('.row-title-text'))" data-fname="${fn}" title="Double-click to rename">
          <div class="row-title">
            <span class="row-thumb"><img src="/api/file-cover/${encodeURIComponent(f.filename)}" loading="lazy" onerror="this.remove()"></span>
            <span class="row-title-text">${titleText}</span>${renameGlyph}
          </div>
        </td>
        <td data-col="artist" style="color:var(--muted)">${htmlEscape(f.artist || '—')}</td>
        <td data-col="year" style="color:var(--muted)">${htmlEscape(f.year || '—')}</td>
        <td data-col="recorded" style="color:var(--muted);white-space:nowrap" title="${htmlEscape(fmtDateFull(f.mtime))}">${htmlEscape(fmtDate(f.mtime))}</td>
        <td data-col="length" style="color:var(--muted)">${fmtDuration(f.duration_seconds)}</td>
        <td data-col="size" style="color:var(--muted)">${f.size_mb} MB</td>
        <td data-col="fmt" style="color:var(--muted);font-variant-numeric:tabular-nums" title="N-bit / M kHz">${fmtSourceFormat(f)}</td>
        <td data-col="actions" style="white-space:nowrap;text-align:right">${previewBtn}${dlLink}${delBtn}</td>
      </tr>`;
  }).join('');
  setTbodyIfChanged(tbody, _libRowsHtml);
  updateBulkBar();
}

export async function deleteFile(fname) {
  if (!confirm(`Delete ${fname}? This cannot be undone.`)) return;
  try {
    const r = await fetch(`/api/recordings/${encodeURIComponent(fname)}`, { method: 'DELETE' });
    if (!r.ok) throw new Error(await parseError(r));
    state.selected.delete(fname);
    refreshLib();
  } catch (e) { toast('✗ delete failed: ' + e.message, 'err'); }
}

// Inline rename for untagged rows. Double-clicking the title swaps it for an
// input; Enter saves, Esc / blur cancels.
export function startInlineRename(fname, el) {
  const f = state.filesByName[fname];
  if (!f) return;
  const current = (f.album || f.filename.replace(/\.flac$/, '')).trim();
  const input = document.createElement('input');
  input.className = 'inline-rename';
  input.value = current;
  input.size = Math.max(20, Math.min(60, current.length + 4));
  el.style.display = 'none';
  el.parentNode.insertBefore(input, el.nextSibling);
  input.focus();
  input.select();
  let done = false;
  const cancel = () => { if (done) return; done = true; input.remove(); el.style.display = ''; };
  const save = async () => {
    if (done) return;
    const newName = input.value.trim();
    if (!newName || newName === current) return cancel();
    done = true;
    try {
      const r = await fetch(`/api/recordings/${encodeURIComponent(fname)}/rename`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ new_name: newName }),
      });
      if (!r.ok) throw new Error(await parseError(r));
      const d = await r.json();
      toast(`✓ Renamed → ${d.filename}`, 'ok');
      // The file moved — drop it from selection so we don't try to act on a
      // stale name later.
      state.selected.delete(fname);
      refreshLib();
    } catch (e) {
      toast('✗ rename failed: ' + e.message, 'err');
      input.remove();
      el.style.display = '';
    }
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });
  input.addEventListener('blur', cancel);
}

// ── Inline preview (library + album rows) ────────────────────────────────
// Single shared <audio>; clicking ▶ on a different row swaps the source.
// Keyed on (kind, fname) so a side and an album sharing a filename don't
// collide on either the visual badge or the download URL.
const preview = { audio: null, fname: null, kind: null };

export function previewIs(fname, kind) { return preview.fname === fname && preview.kind === kind; }
export function getPreviewFname() { return preview.fname; }

function _refreshPreviewButtons() {
  document.querySelectorAll('.preview-btn').forEach(btn => {
    const on = btn.dataset.fname === preview.fname && btn.dataset.kind === preview.kind;
    btn.classList.toggle('playing', on);
    btn.textContent = on ? '⏸' : '▶';
  });
}

export function togglePreview(fname, kind) {
  if (preview.fname === fname && preview.kind === kind) { stopPreview(); return; }
  stopPreview(/*silent=*/true);
  preview.fname = fname;
  preview.kind  = kind;
  if (!preview.audio) {
    preview.audio = new Audio();
    preview.audio.addEventListener('ended', () => stopPreview());
  }
  // Albums no longer have a single-file download (they're folders). Library
  // rows (raw sides) keep the same `/api/download/{filename}` route.
  preview.audio.src = '/api/download/' + encodeURIComponent(fname);
  preview.audio.play().catch(e => {
    toast('✗ preview failed: ' + e.message, 'err');
    stopPreview();
  });
  _refreshPreviewButtons();
}

export function togglePreviewTrack(album, trackname) {
  const key = album + '|' + trackname;
  if (preview.fname === key && preview.kind === 'track') { stopPreview(); return; }
  stopPreview(/*silent=*/true);
  preview.fname = key;
  preview.kind  = 'track';
  if (!preview.audio) {
    preview.audio = new Audio();
    preview.audio.addEventListener('ended', () => stopPreview());
  }
  preview.audio.src = '/api/album/' + encodeURIComponent(album) + '/track/' + encodeURIComponent(trackname);
  preview.audio.play().catch(e => {
    toast('✗ preview failed: ' + e.message, 'err');
    stopPreview();
  });
  _refreshPreviewButtons();
}

export function stopPreview(silent) {
  if (preview.audio) {
    try { preview.audio.pause(); preview.audio.src = ''; } catch (e) {}
  }
  preview.fname = null;
  preview.kind  = null;
  if (!silent) _refreshPreviewButtons();
}

// ── Disk-space marker ─────────────────────────────────────────────────────
// Threshold comes from /api/config; default mirrors the server constant so
// the marker still flips red if the config request hasn't returned yet.
// Below `lowSpaceGb` the marker is red; below `warnSpaceGb` it's amber.
export function updateDiskFree(gb) {
  const el = document.getElementById('disk-free');
  if (gb == null) {
    el.textContent = '— GB free';
    el.classList.remove('low', 'warn');
    return;
  }
  el.textContent = gb + ' GB free';
  const low = gb < state.lowSpaceGb;
  const warn = !low && gb < state.warnSpaceGb;
  el.classList.toggle('low', low);
  el.classList.toggle('warn', warn);
}

export function renderVersion(v) {
  const el = document.getElementById('version-tag');
  if (!v) return;
  el.textContent = v;
  // git-describe between two tags looks like "v0.1.0-5-gabc1234"; that's a dev
  // build. A bare 7-char sha (no leading "v") is also a dev build.
  const isDev = /-g[0-9a-f]{7,}|-dirty|^[0-9a-f]{7,}$/i.test(v) || v === 'dev';
  el.classList.toggle('dev', isDev);
  el.title = isDev ? 'dev build' : 'release';
  el.hidden = false;
}

// Persist the open/closed state of each library section across reloads, so
// users who collapse "Music" once don't have to do it every page load.
function _wireSectionCollapse(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const key = `vr.section.${id}`;
  const saved = localStorage.getItem(key);
  if (saved === 'closed') el.open = false;
  else if (saved === 'open') el.open = true;
  el.addEventListener('toggle', () => {
    localStorage.setItem(key, el.open ? 'open' : 'closed');
  });
}

export function wireSectionCollapse() {
  ['raw-section', 'in-progress-section', 'music-section'].forEach(_wireSectionCollapse);
}

// Periodic disk-free refresh — server pushes nothing for this since it
// changes slowly; a 30s poll is fine.
export async function refreshDiskFree() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    updateDiskFree(d.disk_free_gb);
  } catch(e) {}
}
