// Tag panel — dual-mode modal that handles three flows:
//
//   { filename }   → promote a single raw side into a one-side album
//   { album_id }   → patch metadata on an existing album
//   { filenames }  → combine N selected raw sides into a new album
//
// MusicBrainz + Discogs lookups feed the right-hand candidate list; the
// left-hand form is the user's edited truth and is what /api/apply
// receives. Dirty tracking + flash-on-pick come from a snapshot the
// modal takes when it opens. The combine-mode-specific bits (sides
// reorder, drag/drop) live in combine.js.

import { htmlEscape, makeModalEscHandler } from './util.js';
import { parseError } from './api.js';
import { toast } from './log.js';
import { state } from './state.js';
import { stopPreview, getPreviewFname } from './library.js';
import { refreshLib } from './library.js';
import { refreshAlbums } from './albums.js';
import { renderCombineSides } from './combine.js';

let tagPanelMbid = null;        // mbid of currently-picked candidate (drives cover embed on apply)
let tagPanelDiscogsId = null;   // Discogs release id — persisted so the wave editor can auto-load tracks later
let tagPanelCandidates = [];
// Tracks the auto-populated search query so re-opens don't clobber user edits.
let tagPanelAutoQuery = '';
// Snapshot of left-column values when the modal opened — `formDirty` is true
// when any current value diverges, which drives the unsaved badge + pulse.
let tagPanelInitialFields = null;
let tagPanelDirty = false;
// IDs of left-column inputs we flash on candidate-pick + watch for dirty edits.
const TAG_LEFT_FIELD_IDS = [
  't-album', 't-artist', 't-year', 't-genre',
  't-label', 't-catno', 't-country', 't-format', 't-tracks',
];

// Tracks the kind of row currently bound to the tag panel — `{album_id}`
// when retagging an existing album, `{filename}` when promoting a raw side.
// applyTagPanel() reads this to choose the correct /api/apply payload shape.
let tagPanelTarget = null;

// Tag-panel candidate state — collection matches live alongside MB matches
// in two parallel arrays. `pickCandidate(i)` indexes into MB; collection
// picks go through `pickCollectionCandidate(release_id)` instead.
let tagPanelCollectionCandidates = [];

// Combine state owned here so applyTagPanel and closeTag can read+reset it
// alongside the rest of the panel state. combine.js consumes / mutates via
// the accessors below.
let combineOrder = [];
export function getCombineOrder() { return combineOrder; }
export function setCombineOrder(arr) { combineOrder = arr; }
export function setTagPanelTarget(t) { tagPanelTarget = t; }

let _tagFocusReturn = null;

function setLeft(fields) {
  document.getElementById('t-album').value   = fields.album   ?? '';
  document.getElementById('t-artist').value  = fields.artist  ?? '';
  document.getElementById('t-year').value    = fields.year    ?? '';
  document.getElementById('t-genre').value   = fields.genre   ?? '';
  document.getElementById('t-label').value   = fields.label   ?? '';
  document.getElementById('t-catno').value   = fields.catalog_number ?? '';
  document.getElementById('t-country').value = fields.country ?? '';
  document.getElementById('t-format').value  = fields.format  ?? '';
  if (fields.tracks !== undefined) {
    const arr = Array.isArray(fields.tracks) ? fields.tracks
      : String(fields.tracks || '').split(' / ').filter(Boolean);
    document.getElementById('t-tracks').value = arr.join('\n');
  }
}

function setCover(url) {
  const box = document.getElementById('cover-preview');
  if (!url) { box.innerHTML = 'no cover'; return; }
  const img = new Image();
  img.onload = () => { box.innerHTML = ''; box.appendChild(img); };
  img.onerror = () => { box.innerHTML = 'no cover'; };
  img.src = url;
}

export function openTagAlbum(album_id) {
  // The tag panel is keyed off filesByName; albums live in albumsByName.
  // Mirror the album entry into filesByName so openTag finds it.
  const a = state.albumsByName[album_id];
  if (!a) return;
  state.filesByName[album_id] = a;
  tagPanelTarget = { album_id };
  openTag(album_id);
}

export function openTag(fname) {
  const f = state.filesByName[fname];
  if (!f) return;
  // openTagAlbum / openCombine pre-set tagPanelTarget for non-default modes.
  // For a direct openTag call (recording-finished WS event, inline rename
  // fallthrough), fall through to single-side promote and clear any stale
  // album_id / filenames target left from a previous panel.
  if (!tagPanelTarget || tagPanelTarget.filename !== undefined) {
    tagPanelTarget = { filename: fname };
  }
  const isCombine = tagPanelTarget.filenames !== undefined;
  tagPanelMbid = null;
  tagPanelDiscogsId = null;
  tagPanelCandidates = [];
  // Title + apply-button copy + sides reorder visibility track the mode.
  document.getElementById('tag-modal-title').textContent =
    isCombine ? 'Combine into album' : 'Tag album';
  document.getElementById('tag-apply-btn').textContent =
    isCombine ? 'combine' : 'apply tags';
  document.getElementById('combine-sides-section').hidden = !isCombine;
  if (isCombine) {
    const n = tagPanelTarget.filenames.length;
    document.getElementById('tag-filename').textContent =
      `${n} side${n === 1 ? '' : 's'} → new album`;
    renderCombineSides();
  } else {
    document.getElementById('tag-filename').textContent = fname;
  }
  setLeft({
    album: f.album, artist: f.artist, year: f.year, genre: f.genre,
    label: f.label, catalog_number: f.catalog_number, country: f.country,
    format: '', tracks: f.tracks,
  });
  setCover(null);
  // Pre-fill the search query from existing tags so a single click runs the search.
  // Only overwrite the search field when it's empty or still holds whatever we
  // last auto-filled — anything the user typed manually wins.
  const q = [f.artist, f.album].filter(Boolean).join(' ');
  const searchEl = document.getElementById('t-search-q');
  const current = searchEl.value;
  const userTyped = current && current !== tagPanelAutoQuery;
  if (!userTyped) {
    searchEl.value = q;
    tagPanelAutoQuery = q;
  }
  document.getElementById('t-candidates').innerHTML =
    '<div class="empty-results">Hit search to look up this album on MusicBrainz.</div>';
  document.getElementById('t-search-status').textContent = '';
  document.getElementById('tag-modal').dataset.fname = fname;
  // Snapshot the freshly-loaded form so we can detect divergence for the
  // dirty badge / pulse on the apply button. Reset dirty flag + any leftover
  // flash classes from a previous invocation.
  tagPanelInitialFields = _readTagFields();
  tagPanelDirty = false;
  _setTagDirtyUI(false);
  for (const id of TAG_LEFT_FIELD_IDS) {
    document.getElementById(id)?.classList.remove('field-applied');
  }
  // Remember whatever was focused so closeTag can restore focus to it —
  // keyboard / screen-reader users should land back on the button that
  // opened the modal, not at the top of the document.
  _tagFocusReturn = document.activeElement;
  document.getElementById('tag-modal').hidden = false;
  document.addEventListener('keydown', tagEscHandler);
  // Move focus into the modal so screen readers announce its content and
  // the next Tab keeps the user inside it.
  const firstInput = document.querySelector('#tag-modal input, #tag-modal button, #tag-modal select');
  if (firstInput) firstInput.focus();
  // If we have a usable search query (artist+album already known), kick off
  // the MB search automatically so the candidate list is populated by the
  // time the user looks at it. Defer one tick so focus management above has
  // settled. Skip when the field is empty (e.g. combine of untagged sides).
  if (searchEl.value.trim()) {
    setTimeout(() => {
      // Bail if the modal closed in the meantime, or the user already
      // started typing something different (treat that as their intent).
      if (document.getElementById('tag-modal').hidden) return;
      if (searchEl.value !== tagPanelAutoQuery) return;
      runSearch();
    }, 60);
  }
}

export function closeTag() {
  document.getElementById('tag-modal').hidden = true;
  document.removeEventListener('keydown', tagEscHandler);
  document.getElementById('combine-sides-section').hidden = true;
  // Stop any preview playback so the row's badge resets and audio stops.
  if (getPreviewFname()) stopPreview();
  tagPanelTarget = null;
  combineOrder = [];
  // Reset dirty state so the unsaved badge / pulse don't bleed into the
  // next invocation.
  tagPanelInitialFields = null;
  tagPanelDirty = false;
  _setTagDirtyUI(false);
  if (_tagFocusReturn && typeof _tagFocusReturn.focus === 'function') {
    try { _tagFocusReturn.focus(); } catch (e) { /* element gone */ }
  }
  _tagFocusReturn = null;
}

// Read the current left-column form values into a flat dict so we can compare
// against the snapshot taken when the modal opened.
function _readTagFields() {
  const out = {};
  for (const id of TAG_LEFT_FIELD_IDS) {
    out[id] = document.getElementById(id)?.value ?? '';
  }
  return out;
}

// Toggle the unsaved badge + apply-button pulse to match `dirty`. Forces a
// reflow when re-adding `pulse-once` so the keyframe animation actually
// restarts on each clean→dirty transition (not just the very first one).
function _setTagDirtyUI(dirty) {
  const btn = document.getElementById('tag-apply-btn');
  const badge = document.getElementById('tag-unsaved-badge');
  if (btn) {
    btn.classList.remove('pulse-once');
    if (dirty) {
      void btn.offsetWidth;
      btn.classList.add('pulse-once');
    }
  }
  if (badge) badge.hidden = !dirty;
}

// Recompute the dirty flag against the snapshot. Called from input listeners
// and after pickCandidate / pickCollectionCandidate write into the form.
function _recomputeTagDirty() {
  if (!tagPanelInitialFields) return;
  const cur = _readTagFields();
  const dirty = TAG_LEFT_FIELD_IDS.some(id => cur[id] !== tagPanelInitialFields[id]);
  if (dirty !== tagPanelDirty) {
    tagPanelDirty = dirty;
    _setTagDirtyUI(dirty);
  }
}

// Wire up live dirty-tracking on the left-column inputs once at boot.
// `_recomputeTagDirty` is a no-op until tagPanelInitialFields is set, so
// these listeners are safe even when the modal is closed.
export function wireTagDirtyTracking() {
  for (const id of TAG_LEFT_FIELD_IDS) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', _recomputeTagDirty);
  }
}

// Briefly highlight any left-column input whose value differs from `before`.
// Driven by a CSS transition on .field-applied so the border eases back out
// once the class is removed.
function _flashChangedFields(before) {
  const FLASH_MS = 600;
  for (const id of TAG_LEFT_FIELD_IDS) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (el.value === (before?.[id] ?? '')) continue;
    el.classList.remove('field-applied');
    // Force reflow so re-adding the class restarts the transition even when
    // two candidates are clicked back-to-back.
    void el.offsetWidth;
    el.classList.add('field-applied');
    setTimeout(() => el.classList.remove('field-applied'), FLASH_MS);
  }
}
const tagEscHandler = makeModalEscHandler(closeTag, 'tag-modal');

function parseQuery(q) {
  // Split on " - " or just take the first half as artist; user can override fields directly.
  // Falls back to using the whole string as both artist and album hints.
  const parts = q.split(/\s+-\s+|\s+—\s+/);
  if (parts.length >= 2) return { artist: parts[0].trim(), album: parts.slice(1).join(' - ').trim() };
  // Try to split heuristically: if the string has 4+ words, first ~half as artist.
  const words = q.split(/\s+/);
  if (words.length >= 4) {
    const mid = Math.ceil(words.length / 2);
    return { artist: words.slice(0, mid).join(' '), album: words.slice(mid).join(' ') };
  }
  return { artist: q.trim(), album: q.trim() };
}

function _renderMbCard(c, i) {
  return `
    <div class="candidate" data-i="${i}" onclick="pickCandidate(${i})">
      <div class="candidate-thumb"><img src="/api/cover/${c.mbid}" loading="lazy" onerror="this.remove()"></div>
      <div class="candidate-body">
        <div class="candidate-title">
          <span class="ct-text">${htmlEscape(c.title)}</span>
          ${c.score != null ? `<span class="score">${c.score}%</span>` : ''}
        </div>
        <div class="candidate-sub">
          ${htmlEscape(c.artist)} · ${htmlEscape(c.year || '?')}
          ${c.label ? '· ' + htmlEscape(c.label) : ''}
          ${c.catalog_number ? '<span class="pill">' + htmlEscape(c.catalog_number) + '</span>' : ''}
          ${c.country ? '<span class="pill">' + htmlEscape(c.country) + '</span>' : ''}
          ${c.format ? '<span class="pill">' + htmlEscape(c.format) + '</span>' : ''}
          <a class="ext-link" href="https://musicbrainz.org/release/${c.mbid}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Open on MusicBrainz">↗ MB</a>
        </div>
      </div>
    </div>`;
}

function _renderCollectionCard(c) {
  // External cover image straight from Discogs CDN — no /api/cover proxy
  // path because we only have that for MB releases. The thumb fails open.
  const img = c.cover_url ? `<img src="${htmlEscape(c.cover_url)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : '';
  const dUrl = `https://www.discogs.com/release/${c.discogs_release_id}`;
  return `
    <div class="candidate collection-cand" data-rid="${c.discogs_release_id}" onclick="pickCollectionCandidate(${c.discogs_release_id})">
      <div class="candidate-thumb">${img}</div>
      <div class="candidate-body">
        <div class="candidate-title">
          <span class="ct-text">${htmlEscape(c.title)}</span>
          ${c.score != null ? `<span class="score">${c.score}%</span>` : ''}
        </div>
        <div class="candidate-sub">
          ${htmlEscape(c.artist)} · ${htmlEscape(c.year || '?')}
          ${c.label ? '· ' + htmlEscape(c.label) : ''}
          ${c.catno ? '<span class="pill">' + htmlEscape(c.catno) + '</span>' : ''}
          ${c.format ? '<span class="pill">' + htmlEscape(c.format) + '</span>' : ''}
          <a class="ext-link" href="${dUrl}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Open on Discogs">↗ Discogs</a>
        </div>
      </div>
    </div>`;
}

export async function runSearch() {
  const q = document.getElementById('t-search-q').value.trim();
  if (!q) return;
  // Prefer the typed left-side fields if they're filled, since they'll be more precise.
  const leftArtist = document.getElementById('t-artist').value.trim();
  const leftAlbum  = document.getElementById('t-album').value.trim();
  const body = (leftArtist || leftAlbum) ? { artist: leftArtist, album: leftAlbum } : parseQuery(q);
  const list = document.getElementById('t-candidates');
  document.getElementById('t-search-status').textContent = 'searching MusicBrainz…';
  list.innerHTML = '';
  try {
    const r = await fetch('/api/search', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    tagPanelCandidates = d.candidates || [];
    tagPanelCollectionCandidates = d.collection_candidates || [];
    const mbN = tagPanelCandidates.length;
    const colN = tagPanelCollectionCandidates.length;
    if (!mbN && !colN) {
      list.innerHTML = '<div class="empty-results">No matches. Try editing the search above.</div>';
      document.getElementById('t-search-status').textContent = '';
      return;
    }
    const status =
      (colN ? `${colN} from your collection` : '') +
      (colN && mbN ? ' · ' : '') +
      (mbN ? `${mbN} from MusicBrainz` : '') +
      ' — click to load details';
    document.getElementById('t-search-status').textContent = status;
    let html = '';
    if (colN) {
      html += '<div class="cand-section-header">From your collection</div>';
      html += tagPanelCollectionCandidates.map(_renderCollectionCard).join('');
    }
    if (mbN) {
      if (colN) html += '<div class="cand-section-header">MusicBrainz results</div>';
      html += tagPanelCandidates.map((c, i) => _renderMbCard(c, i)).join('');
    }
    list.innerHTML = html;
  } catch (e) {
    list.innerHTML = `<div class="empty-results err">search failed: ${htmlEscape(e.message)}</div>`;
    document.getElementById('t-search-status').textContent = '';
  }
}

export async function pickCollectionCandidate(releaseId) {
  const c = tagPanelCollectionCandidates.find(x => x.discogs_release_id === releaseId);
  if (!c) return;
  document.querySelectorAll('.candidate').forEach(el =>
    el.classList.toggle('active', Number(el.dataset.rid) === releaseId));
  document.getElementById('t-search-status').textContent = `loading ${c.title}…`;
  try {
    const r = await fetch(`/api/release/discogs/${releaseId}`);
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    // Picking a Discogs-only candidate means we don't have an MBID to pass
    // to /api/apply (which uses the MBID to fetch CAA cover art). Clear the
    // panel-level mbid so apply doesn't try to embed a stale cover.
    tagPanelMbid = null;
    tagPanelDiscogsId = releaseId;
    const before = _readTagFields();
    setLeft({
      album: d.title, artist: d.artist, year: d.year, genre: d.genre,
      label: d.label, catalog_number: d.catalog_number, country: d.country,
      format: d.format, tracks: d.tracks,
    });
    _flashChangedFields(before);
    _recomputeTagDirty();
    if (d.cover_url) setCover(d.cover_url);
    const links = d.discogs_url
      ? `<a class="ext-link" href="${d.discogs_url}" target="_blank" rel="noopener">↗ Discogs</a>`
      : '';
    document.getElementById('t-search-status').innerHTML =
      `loaded · from your collection · ${links}`;
  } catch (e) {
    document.getElementById('t-search-status').textContent = 'load failed: ' + e.message;
  }
}

export async function refreshCollection() {
  const btn = document.getElementById('t-collection-refresh');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/collection/refresh', { method: 'POST' });
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    toast(`✓ Discogs collection refreshed (${d.count} releases)`, 'ok');
    // Re-run the current search so the new cache is reflected immediately.
    if (document.getElementById('t-search-q').value.trim()) runSearch();
  } catch (e) {
    toast('✗ ' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function pickCandidate(i) {
  const c = tagPanelCandidates[i];
  if (!c) return;
  document.querySelectorAll('.candidate').forEach((el, j) => el.classList.toggle('active', j === i));
  document.getElementById('t-search-status').textContent = `loading ${c.title}…`;
  try {
    const r = await fetch(`/api/release/${c.mbid}`);
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    tagPanelMbid = d.mbid;
    tagPanelDiscogsId = d.discogs_id || null;
    const before = _readTagFields();
    setLeft({
      album: d.title, artist: d.artist, year: d.year, genre: d.genre,
      label: d.label, catalog_number: d.catalog_number, country: d.country,
      format: d.format, tracks: d.tracks,
    });
    _flashChangedFields(before);
    _recomputeTagDirty();
    setCover(d.cover_url);
    const mbHref = `https://musicbrainz.org/release/${d.mbid}`;
    const links = [
      `<a class="ext-link" href="${mbHref}" target="_blank" rel="noopener">↗ MusicBrainz</a>`,
      d.discogs_url ? `<a class="ext-link" href="${d.discogs_url}" target="_blank" rel="noopener">↗ Discogs</a>` : '',
    ].filter(Boolean).join(' · ');
    document.getElementById('t-search-status').innerHTML =
      `${d.discogs_id ? 'loaded · enriched from Discogs' : 'loaded · MB only'} · ${links}`;
  } catch (e) {
    document.getElementById('t-search-status').textContent = 'load failed: ' + e.message;
  }
}

export async function applyTagPanel() {
  const fname = document.getElementById('tag-modal').dataset.fname;
  if (!fname) return;
  const tracks = document.getElementById('t-tracks').value
    .split('\n').map(s => s.trim()).filter(Boolean);
  const fields = {
    artist:         document.getElementById('t-artist').value.trim(),
    album:          document.getElementById('t-album').value.trim(),
    year:           document.getElementById('t-year').value.trim(),
    genre:          document.getElementById('t-genre').value.trim(),
    label:          document.getElementById('t-label').value.trim(),
    catalog_number: document.getElementById('t-catno').value.trim(),
    country:        document.getElementById('t-country').value.trim(),
    tracks,
  };
  if (!fields.artist || !fields.album) {
    toast('✗ Need at least artist + album', 'err');
    return;
  }
  // tagPanelTarget tells the server which mode we're in:
  //   { album_id }  → patch existing
  //   { filenames } → combine N raw sides into a new album
  //   { filename }  → promote a single raw side (fallback)
  const target = tagPanelTarget || { filename: fname };
  const isCombine = target.filenames !== undefined;
  try {
    const r = await fetch('/api/apply', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        ...target, fields,
        mbid: tagPanelMbid,
        discogs_release_id: tagPanelDiscogsId,
      })
    });
    if (!r.ok) throw new Error(await parseError(r));
    await r.json();
    if (isCombine) {
      const n = target.filenames.length;
      toast(`✓ Combined ${n} side${n === 1 ? '' : 's'} · ${fields.artist} — ${fields.album}`, 'ok');
      state.selected.clear();
    } else {
      toast(`✓ Tagged ${fields.artist} — ${fields.album}`, 'ok');
    }
    closeTag();
    refreshLib();
    refreshAlbums();
  } catch (e) { toast('✗ ' + e.message, 'err'); }
}
