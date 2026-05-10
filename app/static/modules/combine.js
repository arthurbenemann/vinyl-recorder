// "Combine into album" flow. Reuses the tag-panel modal: openCombine sets
// tagPanelTarget to `{ filenames }`, then openTag flips the modal into
// combine mode (sides reorder visible, title/button copy switched).
// applyTagPanel handles the `filenames` target by POSTing to /api/apply,
// which calls create_album under the hood.

import { htmlEscape, fmtDate } from './util.js';
import { state } from './state.js';
import { previewIs } from './library.js';
import { openTag, setCombineOrder, getCombineOrder, setTagPanelTarget } from './tagging.js';

let combineDragFrom = null;

export function openCombine() {
  if (state.selected.size < 1) return;
  // Default order: oldest recorded first (typical A→B→C→D).
  const combineOrder = [...state.selected].sort((a, b) =>
    (state.filesByName[a]?.mtime || 0) - (state.filesByName[b]?.mtime || 0)
  );
  setCombineOrder(combineOrder);
  // Seed the tag panel from the most-tagged side (artist+album wins, then
  // artist alone), so left fields + the search query pre-fill usefully.
  const score = f => (f.artist ? 2 : 0) + (f.album ? 1 : 0);
  const seed = combineOrder
    .map(fn => state.filesByName[fn])
    .filter(Boolean)
    .sort((a, b) => score(b) - score(a))[0];
  setTagPanelTarget({ filenames: combineOrder.slice() });
  openTag(seed?.filename || combineOrder[0]);
}

export function renderCombineSides() {
  const combineOrder = getCombineOrder();
  const host = document.getElementById('combine-sides');
  host.innerHTML = combineOrder.map((fn, i) => {
    const f = state.filesByName[fn] || {};
    const label = f.album ? `${f.album}${f.artist ? ' · ' + f.artist : ''}` : fn;
    const recorded = f.mtime ? fmtDate(f.mtime) : '—';
    const isFirst = i === 0, isLast = i === combineOrder.length - 1;
    // Reuses the library's `preview` state — same kind ('lib') so the
    // single shared <audio> + visual badge cover both row sets without
    // a parallel state machine. preview-btn keeps the badge in sync.
    const playing = previewIs(fn, 'lib') ? 'playing' : '';
    const playGlyph = previewIs(fn, 'lib') ? '⏸' : '▶';
    const previewAria = htmlEscape('Preview side ' + (i + 1) + ' — ' + label);
    const upAria = htmlEscape('Move side ' + (i + 1) + ' (' + label + ') up');
    const downAria = htmlEscape('Move side ' + (i + 1) + ' (' + label + ') down');
    return `
      <div class="side-row" draggable="true" data-i="${i}"
           ondragstart="combineDragStart(event, ${i})"
           ondragover="combineDragOver(event, ${i})"
           ondragleave="combineDragLeave(event)"
           ondrop="combineDrop(event, ${i})"
           ondragend="combineDragEnd(event)">
        <div class="drag-handle" title="Drag to reorder">≡</div>
        <div class="num">${i + 1}.</div>
        <button class="play-side preview-btn ${playing}" data-fname="${htmlEscape(fn)}" data-kind="lib" title="Preview" aria-label="${previewAria}" onclick="togglePreview(this.dataset.fname, this.dataset.kind)">${playGlyph}</button>
        <div class="name" title="${htmlEscape(fn)}">${htmlEscape(label)}</div>
        <div class="meta">${htmlEscape(recorded)} · ${f.size_mb || '—'} MB</div>
        <div class="arrows">
          <button class="arrow-btn" onclick="moveSide(${i}, -1)" ${isFirst ? 'disabled' : ''} title="Move up" aria-label="${upAria}">▲</button>
          <button class="arrow-btn" onclick="moveSide(${i},  1)" ${isLast  ? 'disabled' : ''} title="Move down" aria-label="${downAria}">▼</button>
        </div>
      </div>`;
  }).join('');
}

export function moveSide(i, delta) {
  const combineOrder = getCombineOrder();
  const j = i + delta;
  if (j < 0 || j >= combineOrder.length) return;
  [combineOrder[i], combineOrder[j]] = [combineOrder[j], combineOrder[i]];
  setCombineOrder(combineOrder);
  renderCombineSides();
}

// HTML5 drag-and-drop. The arrow buttons stay as a fallback for keyboard /
// touch users.
export function combineDragStart(e, i) {
  combineDragFrom = i;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(i));
  e.currentTarget.classList.add('dragging');
}
export function combineDragOver(e, i) {
  if (combineDragFrom == null || combineDragFrom === i) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drag-over');
}
export function combineDragLeave(e) { e.currentTarget.classList.remove('drag-over'); }
export function combineDrop(e, j) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  const i = combineDragFrom;
  combineDragFrom = null;
  if (i == null || i === j) return;
  const combineOrder = getCombineOrder();
  const item = combineOrder.splice(i, 1)[0];
  combineOrder.splice(j, 0, item);
  setCombineOrder(combineOrder);
  renderCombineSides();
}
export function combineDragEnd(e) {
  combineDragFrom = null;
  e.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.side-row.drag-over').forEach(r => r.classList.remove('drag-over'));
}
