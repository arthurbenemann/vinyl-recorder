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
let tagPanelCoverFile = null;   // user-picked custom cover File, uploaded after apply resolves the album id
let tagPanelCandidates = [];
// Snapshot of left-column values when the modal opened — `formDirty` is true
// when any current value diverges, which drives the unsaved badge + pulse.
let tagPanelInitialFields = null;
let tagPanelDirty = false;
// IDs of left-column inputs we flash on candidate-pick + watch for dirty edits.
const TAG_LEFT_FIELD_IDS = [
  't-album', 't-artist', 't-year', 't-genre',
  't-label', 't-catno', 't-country', 't-format',
  't-composer', 't-conductor', 't-tracks',
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
  document.getElementById('t-composer').value  = fields.composer  ?? '';
  document.getElementById('t-conductor').value = fields.conductor ?? '';
  if (fields.tracks !== undefined) {
    // Three input shapes accepted:
    //  - array of titles                  → one per line
    //  - string already with newlines     → used as-is (pre-rendered by
    //    _renderTrackLines into "M:SS - Title" form)
    //  - legacy " / "-joined string       → split + line-per-track
    let body;
    if (Array.isArray(fields.tracks)) {
      body = fields.tracks.join('\n');
    } else {
      const s = String(fields.tracks || '');
      body = s.includes('\n') ? s : s.split(' / ').filter(Boolean).join('\n');
    }
    document.getElementById('t-tracks').value = body;
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

// User picked a custom cover via the file input. Preview it locally (no
// server round-trip) and hold the File; applyTagPanel uploads it once the
// album id is known. The server re-encodes + validates, so the only check
// here is a friendly "that's not an image" guard.
export function onCoverFileSelected(file) {
  if (!file) return;
  if (!/^image\//.test(file.type || '')) {
    toast('✗ Please choose an image file', 'err');
    return;
  }
  tagPanelCoverFile = file;
  const reader = new FileReader();
  reader.onload = () => setCover(reader.result);
  reader.readAsDataURL(file);
  _recomputeTagDirty();
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
  // Drop any cover the previous panel held + clear the file input so a stale
  // pick can't ride along with the next album's apply.
  tagPanelCoverFile = null;
  const coverInput = document.getElementById('t-cover-file');
  if (coverInput) coverInput.value = '';
  // Title + apply-button copy + sides reorder visibility track the mode.
  document.getElementById('tag-modal-title').textContent =
    isCombine ? 'Combine into album' : 'Tag album';
  document.getElementById('tag-apply-btn').textContent =
    isCombine ? 'combine' : 'apply tags';
  // "& edit" applies whenever we're CREATING an album — combining N sides or
  // promoting a single one — since both lead straight into the split editor.
  // Hidden when retagging an existing album (album_id), where the library row
  // already has its own "split into tracks" button.
  const applyEditBtn = document.getElementById('tag-apply-edit-btn');
  if (applyEditBtn) {
    const isNewAlbum = tagPanelTarget.album_id === undefined;
    applyEditBtn.hidden = !isNewAlbum;
    applyEditBtn.textContent = isCombine ? 'combine & edit ▸' : 'apply & edit ▸';
  }
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
  // Reset the unified find-a-release bar each time the panel opens.
  const findInp = document.getElementById('t-search');
  if (findInp) findInp.value = '';
  const findClr = document.getElementById('t-search-clear');
  if (findClr) findClr.hidden = true;
  // Subtitle starts in "mb" mode (empty bar) — it'll show the live
  // "Enter to search MB for «…»" preview if Artist/Album are filled.
  _updateFindSubtitle(_findMode(''));
  document.getElementById('t-candidates').innerHTML =
    '<div class="empty-results">Type above to filter your collection, paste a Discogs or MusicBrainz link, or hit Enter to search MusicBrainz using the Artist + Album fields on the left.</div>';
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
  // Fetch the cached Discogs collection in the background so the filter
  // input lights up the moment the panel is interactive. Cheap when cached
  // server-side (just returns the in-process list).
  _loadCollectionForFilter();
  // If the album already carries artist + album, kick off the MB search
  // automatically so candidates are visible by the time the user looks.
  // Skip on untagged sides (combine of unnamed rows).
  if (f.artist || f.album) {
    setTimeout(() => {
      if (document.getElementById('tag-modal').hidden) return;
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

// Cached Discogs collection list (one fetch per panel open). The right-column
// "filter your collection" input scans this in-memory so filtering is instant
// and offline — no per-keystroke /api/search round-trip.
let tagPanelCollectionAll = [];
let tagPanelCollectionLoaded = false;

// Tease a Discogs release id out of pasted text. Accepts:
//   - https://www.discogs.com/release/12345 (with or without slug, locale prefix)
//   - https://www.discogs.com/release/12345-Artist-Title
//   - [r12345]  (Discogs's marketplace shorthand)
//   - 12345     (bare id, ≥ 3 digits — guards against accidental year paste)
// Returns the integer id or null.
function _parseDiscogsId(s) {
  const t = String(s || '').trim();
  if (!t) return null;
  const m = t.match(/discogs\.com\/(?:[^/]+\/)?release\/(\d+)/i)
         || t.match(/\[r(\d+)\]/i)
         || t.match(/^(\d{3,})$/);
  return m ? parseInt(m[1], 10) : null;
}

// Tease a MusicBrainz *release* MBID out of pasted text. Accepts:
//   - https://musicbrainz.org/release/<uuid> (any subdomain, slug, query)
//   - a bare 8-4-4-4-12 UUID
// Returns the lowercased uuid or null. Deliberately requires `/release/`
// before the uuid (not /release-group/, /recording/, /artist/) so a pasted
// link to some other entity isn't fed to /api/release/{mbid} as if it were
// a release. The bare-UUID branch only fires when the whole input is a
// single MBID, so a UUID buried in unrelated text won't trigger a fetch.
const _MBID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const _MB_URL_RE = new RegExp(`musicbrainz\\.org/release/(${_MBID})`, 'i');
const _MB_BARE_RE = new RegExp(`^(${_MBID})$`, 'i');
function _parseMbReleaseMbid(s) {
  const t = String(s || '').trim();
  if (!t) return null;
  const m = t.match(_MB_URL_RE) || t.match(_MB_BARE_RE);
  return m ? m[1].toLowerCase() : null;
}

function _normalizeForFilter(s) {
  return String(s || '').toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
}

// Re-rank candidates so vinyl-format releases float above CDs / digital.
// MB and Discogs both expose `format` as a comma-joined string; we just
// substring-match on the obvious tokens. Stable: within each group the
// server's relevance order is preserved.
const _VINYL_RE = /\b(vinyl|lp|7\"|10\"|12\"|shellac)\b/i;
function _isVinyl(c) {
  const f = (c && (c.format || (Array.isArray(c.formats) ? c.formats.join(' ') : ''))) || '';
  return _VINYL_RE.test(f);
}
function _vinylFirst(cands) {
  const v = [], rest = [];
  for (const c of cands) (_isVinyl(c) ? v : rest).push(c);
  return v.concat(rest);
}

// Format a duration in seconds as M:SS for the tracks textarea.
function _fmtTrackDuration(sec) {
  const n = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
}

// Build the tracks-textarea body from a candidate's `track_details`. Each
// line is `M:SS - Title` when a duration is known, plain title otherwise.
// applyTagPanel strips the `M:SS - ` prefix back off before persisting, so
// the textarea is a faithful preview AND a single source of truth.
function _renderTrackLines(tracksOrDetails) {
  if (!tracksOrDetails) return '';
  const arr = Array.isArray(tracksOrDetails) ? tracksOrDetails : [];
  return arr.map(t => {
    if (typeof t === 'string') return t;
    const title = (t && t.title) || '';
    const dur   = t && t.duration_seconds;
    return (dur != null) ? `${_fmtTrackDuration(dur)} - ${title}` : title;
  }).join('\n');
}

// Strip a leading "M:SS - " (or "MM:SS - ") off a tracks-textarea line so
// the persisted titles don't carry the inline duration preview.
function _stripTrackDuration(line) {
  return String(line || '').replace(/^\s*\d{1,2}:\d{2}\s*-\s*/, '').trim();
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
          ${c.track_count ? '<span class="pill" title="Total tracks on this release — match it to your record to pick the right pressing.">' + c.track_count + ' tracks</span>' : ''}
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
  // Source of truth is the left-column Artist + Album fields. The
  // standalone search bar was retired (it required users to format their
  // input as "artist + album", which was a fiddly second source of truth).
  const artist = document.getElementById('t-artist').value.trim();
  const album  = document.getElementById('t-album').value.trim();
  if (!artist && !album) {
    document.getElementById('t-search-status').textContent =
      'Fill Artist or Album on the left, then ↗ search MusicBrainz.';
    return;
  }
  const list = document.getElementById('t-candidates');
  document.getElementById('t-search-status').textContent = 'searching MusicBrainz…';
  list.innerHTML = '';
  try {
    const r = await fetch('/api/search', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ artist, album })
    });
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    // Vinyl-preference re-rank: keep LPs / 12" / 7" etc. above CD-only
    // pressings within each result group. Server order (relevance) is
    // preserved as the secondary sort via _vinylFirst's stable partition.
    tagPanelCandidates = _vinylFirst(d.candidates || []);
    tagPanelCollectionCandidates = _vinylFirst(d.collection_candidates || []);
    const mbN = tagPanelCandidates.length;
    const colN = tagPanelCollectionCandidates.length;
    if (!mbN && !colN) {
      list.innerHTML = '<div class="empty-results">No matches. Try editing Artist / Album on the left and search again.</div>';
      document.getElementById('t-search-status').textContent = '';
      return;
    }
    // Result counts + the click-to-load hint live in the section headers
    // now — keeping a separate status line under the search bar would
    // duplicate that information and waste a row.
    document.getElementById('t-search-status').textContent = '';
    let html = '';
    if (colN) {
      html += `<div class="cand-section-header">From your collection · ${colN} match${colN === 1 ? '' : 'es'} · click to load</div>`;
      html += tagPanelCollectionCandidates.map(_renderCollectionCard).join('');
    }
    if (mbN) {
      html += `<div class="cand-section-header">MusicBrainz · ${mbN} result${mbN === 1 ? '' : 's'} · click to load</div>`;
      html += tagPanelCandidates.map((c, i) => _renderMbCard(c, i)).join('');
    }
    list.innerHTML = html;
  } catch (e) {
    list.innerHTML = `<div class="empty-results err">search failed: ${htmlEscape(e.message)}</div>`;
    document.getElementById('t-search-status').textContent = '';
  }
}

// ── Unified find-a-release bar ────────────────────────────────────────────
// The right-column "Find a release" input has three behaviours, picked on
// every keystroke from the value + the left-column Artist/Album fields:
//
//   1. value matches a Discogs URL / id / [rN] → "discogs" mode.
//      Subtitle hints "→ fetch Discogs release N (Enter)". Enter fetches.
//   2. value is non-empty free text             → "filter" mode.
//      Subtitle shows "N collection matches". The candidates list is
//      re-rendered live with collection items only (no network).
//   3. value is empty                            → "mb" mode.
//      Subtitle shows what an Enter press will search for ("«artist · album»")
//      pulled from the left fields. Disabled when both fields are empty.

function _findMode(raw) {
  const t = String(raw || '').trim();
  if (!t) return { kind: 'mb' };
  const id = _parseDiscogsId(t);
  if (id) return { kind: 'discogs', id };
  const mbid = _parseMbReleaseMbid(t);
  if (mbid) return { kind: 'mb-release', mbid };
  return { kind: 'filter', text: t };
}

function _updateFindSubtitle(mode) {
  const el = document.getElementById('t-find-subtitle');
  if (!el) return;
  el.classList.remove('mode-discogs', 'mode-mb', 'mode-disabled');
  if (mode.kind === 'discogs') {
    el.classList.add('mode-discogs');
    el.innerHTML = `→ fetch Discogs release <span class="t-find-emph">${mode.id}</span> · <span class="t-find-emph">Enter</span> to load`;
    return;
  }
  if (mode.kind === 'mb-release') {
    el.classList.add('mode-mb');
    el.innerHTML = `→ fetch MusicBrainz release <span class="t-find-emph">${mode.mbid.slice(0, 8)}…</span> · <span class="t-find-emph">Enter</span> to load`;
    return;
  }
  if (mode.kind === 'filter') {
    // _filterCollection (called from onFindInput) sets the precise match
    // count in the search-status line, so the subtitle just describes the
    // mode at a glance.
    el.textContent = tagPanelCollectionAll.length
      ? `filtering your collection (${tagPanelCollectionAll.length} records)`
      : 'no collection loaded — type Artist + Album on the left and hit Enter';
    return;
  }
  // mb mode
  const artist = document.getElementById('t-artist').value.trim();
  const album  = document.getElementById('t-album').value.trim();
  if (!artist && !album) {
    el.classList.add('mode-disabled');
    el.textContent = 'fill Artist + Album on the left to search MusicBrainz';
    return;
  }
  el.classList.add('mode-mb');
  const label = [artist, album].filter(Boolean).join(' · ');
  el.innerHTML = `→ <span class="t-find-emph">Enter</span> to search MusicBrainz for «<span class="t-find-emph">${htmlEscape(label)}</span>»`;
}

function _filterCollection(text) {
  const list = document.getElementById('t-candidates');
  const q = _normalizeForFilter(text.trim());
  const tokens = q ? q.split(/\s+/).filter(Boolean) : [];
  // No DISCOGS_USERNAME configured → no collection to filter; nudge the
  // user toward the MB-search path instead of letting "no matches" imply
  // a stale collection problem.
  if (!tagPanelCollectionAll.length) {
    list.innerHTML = '<div class="empty-results">No Discogs collection is configured. Fill Artist + Album on the left and clear the bar, then hit <strong>Enter</strong> to search MusicBrainz.</div>';
    document.getElementById('t-search-status').textContent = '';
    return;
  }
  const matches = [];
  if (tokens.length) {
    for (const rel of tagPanelCollectionAll) {
      const hay = _normalizeForFilter(`${rel.artist || ''} ${rel.title || ''} ${rel.label || ''} ${rel.catno || ''}`);
      if (tokens.every(t => hay.includes(t))) matches.push(rel);
      if (matches.length >= 50) break;        // cap so a one-letter filter doesn't paint thousands of rows
    }
  }
  tagPanelCollectionCandidates = _vinylFirst(matches);
  if (!matches.length) {
    list.innerHTML = '<div class="empty-results">No collection releases match. Clear the bar and hit <strong>Enter</strong> to search MusicBrainz instead.</div>';
    document.getElementById('t-search-status').textContent = '';
    return;
  }
  document.getElementById('t-search-status').textContent = '';
  const n = matches.length;
  const refineHint = n === 50 ? ' · refine to narrow' : '';
  list.innerHTML =
    `<div class="cand-section-header">From your collection · ${n} match${n === 1 ? '' : 'es'}${refineHint} · click to load</div>`
    + tagPanelCollectionCandidates.map(_renderCollectionCard).join('');
}

// Public handler: fires on every input/paste of #t-search. Toggles the
// inline × clear button, updates the subtitle, and re-paints the
// candidates panel to match the new mode so a stale filter-mode message
// doesn't leak into discogs/mb mode.
export function onFindInput(v) {
  const inp = document.getElementById('t-search');
  const clr = document.getElementById('t-search-clear');
  if (clr) clr.hidden = !(inp && inp.value);
  const mode = _findMode(v);
  _updateFindSubtitle(mode);
  const list = document.getElementById('t-candidates');
  const status = document.getElementById('t-search-status');
  if (mode.kind === 'filter') {
    _filterCollection(mode.text);
    return;
  }
  if (mode.kind === 'discogs') {
    // Don't blow away a previously-loaded MB candidate list — but if the
    // panel is currently showing a filter-mode hint, swap it for one that
    // matches discogs mode so the message tracks reality.
    if (list && list.querySelector('.empty-results')) {
      list.innerHTML = '<div class="empty-results">Press <strong>Enter</strong> to fetch this Discogs release and fill the tags on the left.</div>';
    }
    if (status && status.textContent.startsWith('No collection')) status.textContent = '';
    if (status && status.textContent.includes('from your collection')) status.textContent = '';
    return;
  }
  if (mode.kind === 'mb-release') {
    if (list && list.querySelector('.empty-results')) {
      list.innerHTML = '<div class="empty-results">Press <strong>Enter</strong> to fetch this MusicBrainz release and fill the tags on the left.</div>';
    }
    if (status && status.textContent.startsWith('No collection')) status.textContent = '';
    if (status && status.textContent.includes('from your collection')) status.textContent = '';
    return;
  }
  // mb mode (empty bar). Wipe a stale filter-mode hint; keep candidate
  // cards that the user can still click on.
  if (list && list.querySelector('.empty-results')) {
    list.innerHTML = '<div class="empty-results">Type above to filter your collection, paste a Discogs or MusicBrainz link, or hit <strong>Enter</strong> to search MusicBrainz using the Artist + Album fields on the left.</div>';
  }
  if (status && status.textContent.includes('from your collection')) status.textContent = '';
}

// Public handler: fires on Enter inside #t-search. Dispatches by mode.
export async function onFindEnter() {
  const inp = document.getElementById('t-search');
  const mode = _findMode(inp ? inp.value : '');
  if (mode.kind === 'discogs') {
    await _fetchDiscogsRelease(mode.id);
    if (inp) { inp.value = ''; onFindInput(''); }
    return;
  }
  if (mode.kind === 'mb-release') {
    await _fetchMbReleaseByMbid(mode.mbid);
    if (inp) { inp.value = ''; onFindInput(''); }
    return;
  }
  if (mode.kind === 'mb') {
    runSearch();
    return;
  }
  // filter mode: Enter is a no-op (filtering is already live). If the
  // user wanted MB instead, they clear the bar and hit Enter again.
}

// Refresh the subtitle whenever the user edits Artist or Album so the
// "Enter to search MusicBrainz for «…»" preview stays current.
export function wireFindSubtitleLive() {
  const sync = () => {
    const inp = document.getElementById('t-search');
    _updateFindSubtitle(_findMode(inp ? inp.value : ''));
  };
  for (const id of ['t-artist', 't-album']) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', sync);
  }
}

async function _fetchDiscogsRelease(id) {
  const status = document.getElementById('t-search-status');
  status.textContent = `fetching Discogs release ${id}…`;
  try {
    const r = await fetch(`/api/release/discogs/${id}`);
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    tagPanelMbid = null;
    tagPanelDiscogsId = id;
    const before = _readTagFields();
    setLeft({
      album: d.title, artist: d.artist, year: d.year, genre: d.genre,
      label: d.label, catalog_number: d.catalog_number, country: d.country,
      format: d.format, composer: d.composer, conductor: d.conductor,
      tracks: _renderTrackLines(d.track_details && d.track_details.length ? d.track_details : d.tracks),
    });
    _flashChangedFields(before);
    _recomputeTagDirty();
    if (d.cover_url) setCover(d.cover_url);
    const link = d.discogs_url
      ? `<a class="ext-link" href="${d.discogs_url}" target="_blank" rel="noopener">↗ Discogs</a>`
      : '';
    status.innerHTML = `loaded · from Discogs paste · ${link}`;
  } catch (e) {
    status.textContent = 'load failed: ' + e.message;
  }
}

// Fetch the full owned-collection list once per tag-panel open. Server
// returns {releases: []} when no DISCOGS_USERNAME is set; the subtitle
// + ↻ refresh button adapt accordingly.
async function _loadCollectionForFilter() {
  if (tagPanelCollectionLoaded) return;
  tagPanelCollectionLoaded = true;
  try {
    const r = await fetch('/api/collection');
    if (!r.ok) return;
    const d = await r.json();
    tagPanelCollectionAll = Array.isArray(d.releases) ? d.releases : [];
  } catch (e) {
    tagPanelCollectionAll = [];
  }
  const refresh = document.getElementById('t-collection-refresh');
  if (refresh) refresh.hidden = !tagPanelCollectionAll.length;
  // Nudge the subtitle so it shows "N collection records" the moment
  // data lands, without needing to type.
  const inp = document.getElementById('t-search');
  _updateFindSubtitle(_findMode(inp ? inp.value : ''));
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
      format: d.format, composer: d.composer, conductor: d.conductor,
      tracks: _renderTrackLines(d.track_details && d.track_details.length ? d.track_details : d.tracks),
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
    // Reset the cached list so _loadCollectionForFilter re-fetches it,
    // and re-run the live filter (if any) against the new data.
    tagPanelCollectionLoaded = false;
    tagPanelCollectionAll    = [];
    await _loadCollectionForFilter();
    const inp = document.getElementById('t-search');
    if (inp && inp.value.trim()) onFindInput(inp.value);
  } catch (e) {
    toast('✗ ' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Fetch a full MB release, fill the left-hand tag fields, embed the cover,
// and write the result status. Shared by the candidate-pick path and the
// paste-an-MBID path; `source` only changes the status wording.
async function _loadMbRelease(mbid, source) {
  const status = document.getElementById('t-search-status');
  try {
    const r = await fetch(`/api/release/${encodeURIComponent(mbid)}`);
    if (!r.ok) throw new Error(await parseError(r));
    const d = await r.json();
    tagPanelMbid = d.mbid;
    tagPanelDiscogsId = d.discogs_id || null;
    const before = _readTagFields();
    setLeft({
      album: d.title, artist: d.artist, year: d.year, genre: d.genre,
      label: d.label, catalog_number: d.catalog_number, country: d.country,
      format: d.format, composer: d.composer, conductor: d.conductor,
      tracks: _renderTrackLines(d.track_details && d.track_details.length ? d.track_details : d.tracks),
    });
    _flashChangedFields(before);
    _recomputeTagDirty();
    setCover(d.cover_url);
    const mbHref = `https://musicbrainz.org/release/${d.mbid}`;
    const links = [
      `<a class="ext-link" href="${mbHref}" target="_blank" rel="noopener">↗ MusicBrainz</a>`,
      d.discogs_url ? `<a class="ext-link" href="${d.discogs_url}" target="_blank" rel="noopener">↗ Discogs</a>` : '',
    ].filter(Boolean).join(' · ');
    const lead = source === 'paste'
      ? 'loaded · from MusicBrainz paste'
      : (d.discogs_id ? 'loaded · enriched from Discogs' : 'loaded · MB only');
    status.innerHTML = `${lead} · ${links}`;
  } catch (e) {
    status.textContent = 'load failed: ' + e.message;
  }
}

// Upload the held custom cover (if any) against a now-known album id.
// Returns a short note to fold into the apply toast: '' (nothing to do or
// not applicable), ' · cover set', or ' · cover failed'. Never throws.
async function _uploadHeldCover(albumId) {
  if (!tagPanelCoverFile || !albumId) return '';
  try {
    const fd = new FormData();
    fd.append('file', tagPanelCoverFile);
    const cr = await fetch(`/api/file-cover/${encodeURIComponent(albumId)}`,
                           { method: 'POST', body: fd });
    if (!cr.ok) throw new Error(await parseError(cr));
    return ' · cover set';
  } catch (e) {
    return ' · cover failed';
  }
}

// Paste path: user dropped a musicbrainz.org/release/<id> URL or a bare
// MBID into the find bar. Mirrors _fetchDiscogsRelease — there's no
// candidate card to highlight, so just load by id.
async function _fetchMbReleaseByMbid(mbid) {
  document.getElementById('t-search-status').textContent =
    `fetching MusicBrainz release ${mbid.slice(0, 8)}…`;
  await _loadMbRelease(mbid, 'paste');
}

export async function pickCandidate(i) {
  const c = tagPanelCandidates[i];
  if (!c) return;
  // Match by data-i, not DOM-position: when both a "From your collection"
  // and a "MusicBrainz" section are rendered, the MB cards live below the
  // collection cards in the list, so a position-based comparison would
  // light up the collection card at the same offset instead of the MB
  // card the user clicked. Collection cards have no data-i, so their
  // Number(undefined) → NaN never equals i.
  document.querySelectorAll('.candidate').forEach(el =>
    el.classList.toggle('active', Number(el.dataset.i) === i));
  document.getElementById('t-search-status').textContent = `loading ${c.title}…`;
  await _loadMbRelease(c.mbid, 'candidate');
}

export async function applyTagPanel(thenEdit = false) {
  const fname = document.getElementById('tag-modal').dataset.fname;
  if (!fname) return;
  // The textarea may carry the "M:SS - Title" preview format when the
  // user picked a candidate with track durations. Strip the leading
  // timecode back off so persisted tags hold just the titles.
  const tracks = document.getElementById('t-tracks').value
    .split('\n').map(_stripTrackDuration).filter(Boolean);
  const fields = {
    artist:         document.getElementById('t-artist').value.trim(),
    album:          document.getElementById('t-album').value.trim(),
    year:           document.getElementById('t-year').value.trim(),
    genre:          document.getElementById('t-genre').value.trim(),
    label:          document.getElementById('t-label').value.trim(),
    catalog_number: document.getElementById('t-catno').value.trim(),
    country:        document.getElementById('t-country').value.trim(),
    composer:  document.getElementById('t-composer').value.trim(),
    conductor: document.getElementById('t-conductor').value.trim(),
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
    const result = await r.json();   // { album_id }
    // A custom cover is uploaded against the album id the apply just
    // resolved (combine/promote create it server-side). Non-fatal — the tags
    // are already saved — so a cover hiccup only annotates the toast.
    const newAlbumId = (result && result.album_id) || target.album_id;
    const coverNote = await _uploadHeldCover(newAlbumId);
    const ok = coverNote !== ' · cover failed';
    if (isCombine) {
      const n = target.filenames.length;
      toast(`✓ Combined ${n} side${n === 1 ? '' : 's'} · ${fields.artist} — ${fields.album}${coverNote}`, ok ? 'ok' : 'err');
      state.selected.clear();
    } else {
      toast(`✓ Tagged ${fields.artist} — ${fields.album}${coverNote}`, ok ? 'ok' : 'err');
    }
    closeTag();
    refreshLib();
    const albumsReady = refreshAlbums();
    // "& edit" jumps straight into the split editor on the new album, saving
    // a trip back to the library to find the row. openWaveEditor reads the
    // album from `albumsByName`, so wait for the refresh to land it first.
    if (thenEdit && result && result.album_id) {
      await albumsReady;
      if (typeof window.openWaveEditor === 'function') {
        window.openWaveEditor(result.album_id);
      }
    }
  } catch (e) { toast('✗ ' + e.message, 'err'); }
}
