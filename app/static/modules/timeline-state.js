// Pure timeline helpers extracted from wave-editor.js.
//
// Two flavours of function here:
//   1. Fully-pure transforms (`_weRemapForSides`, `_weCutsFromTracklist`,
//      `_wePosLetter`) — all state arrives via arguments, output is data.
//      Easy to unit-test in a Node VM sandbox.
//   2. View-helpers that read `we` (the editor state in wave-editor.js).
//      They still don't *mutate* anything; they project the live state
//      into derived arrays/objects (`_weDerivedPositions`,
//      `_weEffectivePositions`, `_weSideBounds`, `_weCutGroupSpan`).
//
// The whole file is a classic script (no ES modules) — same convention
// as peaks.js / wave-editor.js. It must load BEFORE wave-editor.js so the
// `window.*` bindings are populated before the editor's helper call sites
// resolve. We read `window.we` lazily inside each view-helper so it's OK
// that `we` is defined later by wave-editor.js.

'use strict';

// ── _weRemapForSides ─────────────────────────────────────────────────────
// Translate a plan {cuts, titles, skipped, total} from one side-ordering
// to another. See the long-form rationale in wave-editor.js's call site.
function _weRemapForSides(oldSides, newSides, plan) {
  function offsetsFor(sides) {
    const offs = []; let off = 0;
    for (const s of sides) {
      offs.push(off);
      off += Number(s.duration_seconds) || 0;
    }
    return { offs, total: off };
  }
  const oldO = offsetsFor(oldSides);
  const newO = offsetsFor(newSides);
  const newOffByFn = new Map();
  newSides.forEach((s, i) => newOffByFn.set(s.filename, newO.offs[i]));
  const total = plan.total || oldO.total;

  function sideOf(t) {
    // Half-open intervals [offs[i], offs[i] + dur). Final side absorbs t = total.
    for (let i = 0; i < oldSides.length; i++) {
      const start = oldO.offs[i];
      const end   = start + (Number(oldSides[i].duration_seconds) || 0);
      if (t < end) return i;
    }
    return Math.max(0, oldSides.length - 1);
  }
  function remap(t) {
    const i = sideOf(t);
    const fn = oldSides[i].filename;
    const newOff = newOffByFn.get(fn);
    if (newOff == null) return Math.min(newO.total, t);
    return Math.max(0, Math.min(newO.total, newOff + (t - oldO.offs[i])));
  }

  const oldBoundaries = [0, ...plan.cuts, total];
  function originalRegion(t) {
    // Half-open in the same direction as sideOf so a region [a, b] is
    // matched by `t = (a + b) / 2`.
    for (let i = 0; i < oldBoundaries.length - 1; i++) {
      if (t >= oldBoundaries[i] && t < oldBoundaries[i + 1]) return i;
    }
    return Math.max(0, oldBoundaries.length - 2);
  }

  // Step 1 — augmented cut list. Add interior side boundaries that
  // aren't already a cut so every augmented region sits on one side.
  const interior = [...plan.cuts, ...oldO.offs.slice(1)]
    .filter(t => t > 0.001 && t < total - 0.001)
    .sort((a, b) => a - b);
  const augCuts = [];
  for (const t of interior) {
    if (!augCuts.length || Math.abs(augCuts[augCuts.length - 1] - t) > 0.001) {
      augCuts.push(t);
    }
  }
  const augBoundaries = [0, ...augCuts, total];

  // Step 2 — remap each augmented region.
  const regions = [];
  for (let i = 0; i < augBoundaries.length - 1; i++) {
    const a = augBoundaries[i], b = augBoundaries[i + 1];
    if (b <= a) continue;
    const origIdx = originalRegion((a + b) / 2);
    regions.push({
      newStart: remap(a),
      newEnd:   remap(b),
      title:    plan.titles[origIdx] || `Track ${origIdx + 1}`,
      skip:     !!plan.skipped[origIdx],
    });
  }
  regions.sort((a, b) => a.newStart - b.newStart);

  // Step 3 — coalesce adjacent regions whose origin (title + skip) is
  // identical and that meet at the same point in the new layout. This
  // collapses synthetic subdivisions introduced by step 1 when a cut
  // sat exactly on a side boundary in the old layout.
  const merged = [];
  for (const r of regions) {
    const last = merged[merged.length - 1];
    if (last && last.title === r.title && last.skip === r.skip
        && Math.abs(last.newEnd - r.newStart) < 0.001) {
      last.newEnd = r.newEnd;
    } else {
      merged.push({ newStart: r.newStart, newEnd: r.newEnd,
                    title: r.title, skip: r.skip });
    }
  }

  const cuts = [];
  for (let i = 0; i < merged.length - 1; i++) {
    const c = merged[i].newEnd;
    if (c > 0.001 && c < newO.total - 0.001) cuts.push(c);
  }
  return {
    willResetCuts: false,
    cuts,
    titles:  merged.map(r => r.title),
    skipped: merged.map(r => r.skip),
    oldCuts:    plan.cuts.slice(),
    oldTitles:  plan.titles.slice(),
    oldSkipped: plan.skipped.slice(),
  };
}

// ── _wePosLetter ─────────────────────────────────────────────────────────
// Discogs `position` looks like "A1", "B2", or "1-01" / "2-03" on
// multi-disc releases. The leading alphabetic prefix (or leading "N-"
// disc number) identifies the side / disc. Returns '' when nothing
// recognisable is at the start.
function _wePosLetter(pos) {
  const s = String(pos || '').trim();
  const m = s.match(/^([A-Za-z]+|\d+(?=-))/);
  return m ? m[0].toUpperCase() : '';
}

// ── _weCutsFromTracklist ─────────────────────────────────────────────────
// Discogs-apply path: turn a flat list of {title, duration, position} rows
// into the editor's cuts/titles/skipped/positions arrays.
//
// Layout is per recording side when the recording has ≥2 sides AND the
// tracklist carries side prefixes (A1, B2, …); otherwise the whole album is
// treated as a single side (global fallback). Within each side the side's
// tracks are spread across the side's time span — weighted by Discogs
// `duration_seconds` when the side carries usable durations, evenly split
// when it doesn't. Many vinyl releases ship without per-track durations; in
// that case the real boundaries are unknown, so an even seed (which the user
// drags onto detected silences) is the most useful starting point. The old
// cumulative-cursor layout collapsed every track of a durationless side onto
// the side's first track, leaving the rest zero-width ("doesn't fit").
//
// Every side interface gets a skipped "silence" region seeded in: a lead-in
// before each side's first track (the album lead-in for side 1, the needle-
// drop gap after each flip for later sides) and a lead-out after the final
// track. These render as ordinary grey "skip — not exported" rows with
// normal draggable handles, so the first track (A1) gets a handle and an
// editable start with no special-casing — it is simply region 1, sitting
// after the lead-in skip. The seed width is a small constant the user
// refines by dragging or via "suggest from silence".
const WE_SILENCE_SEED_S = 2;

function _weCutsFromTracklist(td, sides, total) {
  td = (td || []).filter(t => t && t.title);
  sides = sides || [];
  const n = sides.length;

  // Side boundaries in album time: B[0]=0 … B[n]=total. The last entry is
  // forced to `total` so a small rounding mismatch between the summed side
  // durations and the album total can't strand the final boundary.
  const B = [0];
  let acc = 0;
  for (let i = 0; i < n; i++) {
    acc += Number(sides[i] && sides[i].duration_seconds) || 0;
    B.push(acc);
  }
  if (n >= 1) B[n] = total;

  // Map first-seen Discogs side letter → recording side index. Letters past
  // the last recording side clamp onto the last side (so a Discogs C/D
  // release on a shorter recording stacks onto the final side rather than
  // collapsing it).
  const seen = new Map();
  for (const t of td) {
    const L = _wePosLetter(t.position);
    if (L && !seen.has(L)) seen.set(L, Math.min(n - 1, seen.size));
  }
  const usePerSide = n >= 2 && seen.size >= 2;

  // Group track indices by side, and capture each side's album-time span.
  // The global fallback treats the whole album as one side.
  let groups, spans;
  if (usePerSide) {
    groups = Array.from({ length: n }, () => []);
    spans  = Array.from({ length: n }, (_, i) => [B[i], B[i + 1]]);
    let cur = 0;
    for (let j = 0; j < td.length; j++) {
      const L = _wePosLetter(td[j].position);
      cur = (L && seen.has(L)) ? seen.get(L) : cur;
      groups[cur].push(j);
    }
  } else {
    groups = [td.map((_, j) => j)];
    spans  = [[0, total]];
  }

  const g = WE_SILENCE_SEED_S;
  const regions = [];                 // {start, end, title, skip, position}
  let overflow = 0;
  const skipRegion = (start, end) =>
    regions.push({ start, end, title: '', skip: true, position: '' });

  for (let i = 0; i < groups.length; i++) {
    const [lo, hi] = spans[i];
    const span = hi - lo;
    const idxs = groups[i];
    const isLast = i === groups.length - 1;
    if (span <= 0) continue;
    if (!idxs.length) { skipRegion(lo, hi); continue; }  // recorded side, no tracks

    // Seed a lead-in skip on every side and a lead-out skip after the final
    // track. Each gap is capped at a quarter of the side so even a very
    // short side keeps at least half its span for music.
    const gLead = Math.min(g, span * 0.25);
    const gTail = isLast ? Math.min(g, (span - gLead) * 0.25) : 0;
    const musicLo = lo + gLead;
    const musicHi = hi - gTail;
    const musicSpan = Math.max(0, musicHi - musicLo);
    if (gLead > 0) skipRegion(lo, musicLo);

    // Weight by real durations when the side carries them, else even split.
    const durs = idxs.map(j => Number(td[j].duration_seconds) || 0);
    const sum  = durs.reduce((a, b) => a + b, 0);
    const weights = sum > 0 ? durs : idxs.map(() => 1);
    const wsum = weights.reduce((a, b) => a + b, 0) || idxs.length;
    // Informational: the Discogs side runs longer than the recorded side
    // (wrong pressing / missing audio). We still scale it to fit.
    if (sum > musicSpan + 0.001) overflow += 1;

    let cum = 0;
    for (let m = 0; m < idxs.length; m++) {
      const a = musicLo + musicSpan * (cum / wsum);
      cum += weights[m];
      const b = musicLo + musicSpan * (cum / wsum);
      const j = idxs[m];
      regions.push({
        start: a, end: b, title: td[j].title, skip: false,
        position: String(td[j].position || '').trim(),
      });
    }
    if (gTail > 0) skipRegion(musicHi, hi);
  }

  // Project regions → parallel arrays. A cut sits at every interior region
  // boundary; titles/skipped/positions get exactly one entry per region so
  // they stay index-aligned with the [0, ...cuts, total] regions that
  // renderTracks rebuilds. musicSpan is always ≥ half a side, so no region
  // is zero-width and every boundary lands strictly inside (0, total).
  const round = (x) => Math.round(x * 1000) / 1000;
  const cuts = [], titles = [], skipped = [], positions = [];
  regions.forEach((r, idx) => {
    titles.push(r.title);
    skipped.push(r.skip);
    positions.push(r.position);
    if (idx < regions.length - 1) cuts.push(round(r.end));
  });

  return { cuts, titles, skipped, positions, overflow };
}

// ── Per-region sleeve-style labels (A1, A2, B1, …) ───────────────────────
// Two sources:
//   1. Discogs `position` per track, stashed in `we.positions` when a
//      tracklist is applied (Discogs path).
//   2. Auto-derived from `we.sides` + the current cut layout — the region's
//      start time picks its side letter, and a per-side counter assigns
//      the track index (manual-cut path). Skipped / unfit regions are
//      excluded from the per-side numbering, mirroring how the output
//      tracklist actually exports.
//
// _weEffectivePositions returns whichever is active so render code stays
// agnostic. Single-side records skip derivation — letters wouldn't add
// information over the plain sequential number.
function _weDerivedPositions() {
  const we = window.we || {};
  const sides = we.sides || [];
  const need = Math.max(1, (we.cuts || []).length + 1);
  if (sides.length < 2 || !(we.cuts || []).length) {
    return new Array(need).fill('');
  }
  const sideEnds = [];
  let acc = 0;
  for (const s of sides) {
    acc += Number(s.duration_seconds) || 0;
    sideEnds.push(acc);
  }
  const boundaries = [0, ...we.cuts, we.total];
  const out = new Array(need).fill('');
  const perSideCount = new Array(sides.length).fill(0);
  for (let i = 0; i < need; i++) {
    if (we.skipped && we.skipped[i]) continue;
    const start = boundaries[i];
    const end   = boundaries[i + 1] != null ? boundaries[i + 1] : we.total;
    if (end - start < 0.5) continue;  // matches renderTracks's "unfit" gate
    // First side whose end is strictly past `start` — i.e. the side the
    // region begins on. Falls back to the last side for the album-end
    // edge case (start === total).
    let sideIdx = sideEnds.findIndex(e => start < e - 0.001);
    if (sideIdx < 0) sideIdx = sideEnds.length - 1;
    perSideCount[sideIdx] += 1;
    out[i] = String.fromCharCode(65 + sideIdx) + perSideCount[sideIdx];
  }
  return out;
}

function _weEffectivePositions() {
  const we = window.we || {};
  const havePositions = (we.positions || []).some(p => p);
  if (havePositions) return we.positions.slice();
  return _weDerivedPositions();
}

// Album-time [lo, hi] of the recorded side that contains time `t`. A
// single-cut drag is clamped to this so a marker can't be dragged out of
// its raw recording region into an adjacent side. The last side's `hi` is
// `we.total` rather than the summed duration so a small rounding mismatch
// between side durations and the album total doesn't strand the boundary.
function _weSideBounds(t) {
  const we = window.we || {};
  const sides = we.sides || [];
  if (sides.length < 2) return [0, we.total];
  let lo = 0;
  for (let k = 0; k < sides.length; k++) {
    const hi = (k === sides.length - 1)
      ? we.total
      : lo + (Number(sides[k].duration_seconds) || 0);
    if (t < hi || k === sides.length - 1) return [lo, hi];
    lo = hi;
  }
  return [0, we.total];
}

// The contiguous run of cuts a shift+drag starting on cut `i` moves as a
// rigid group: cut `i` plus every later cut still inside the same raw
// recording side. Cuts on later sides are excluded so the group can't be
// pushed across a side boundary. Returns the span [i, last] and that
// side's album-time bounds.
function _weCutGroupSpan(i) {
  const we = window.we || {};
  const cuts = we.cuts || [];
  const [sideLo, sideHi] = _weSideBounds(cuts[i]);
  let last = i;
  while (last + 1 < cuts.length && cuts[last + 1] < sideHi) last++;
  return { i, last, sideLo, sideHi };
}

// ── _weEvenCuts ───────────────────────────────────────────────────────────
// Evenly-spaced internal cut times that divide [0, total] into `n` equal
// tracks — the fallback for a gapless side (live set, DJ mix, attacca
// classical) where silence detection finds nothing to cut on. Returns the
// n-1 interior boundaries, rounded to ms and clamped inside (0, total) so
// they survive the same dedupe/clamp the silence path applies. Empty when
// the length is unknown or n < 2. These are *seed* cuts: the user drags them
// onto the real boundaries using the waveform + audition.
function _weEvenCuts(total, n) {
  const count = Math.floor(Number(n));
  if (!(total > 0) || !(count >= 2)) return [];
  const cuts = [];
  for (let i = 1; i < count; i++) {
    const t = Math.round((total * i / count) * 1000) / 1000;
    if (t > 0.01 && t < total - 0.01) cuts.push(t);
  }
  return cuts;
}

// New album-time position for cut `i` after nudging it by `delta` seconds,
// clamped so it can't cross either neighbouring cut (which would reorder
// we.cuts and misalign the title/skip/position arrays indexed by region)
// or the [0, total] album bounds. Returns the clamped value (equal to
// cuts[i] when the nudge is fully absorbed by the clamp), or null for an
// out-of-range index. Pure — drives the keyboard ←/→ cut nudge.
function _weNudgedCutValue(cuts, i, delta, total) {
  if (!Array.isArray(cuts) || i < 0 || i >= cuts.length) return null;
  const EPS = 0.001;
  const lo = (i > 0 ? cuts[i - 1] : 0) + EPS;
  const hi = (i < cuts.length - 1 ? cuts[i + 1] : total) - EPS;
  return Math.max(lo, Math.min(hi, cuts[i] + delta));
}

// ── _weDetectSettingValue ────────────────────────────────────────────────
// Validate a persisted silence-detection setting (noise floor / min-silence
// / auto-skip threshold) read back from localStorage. Returns the parsed
// number when finite and within [min, max]; otherwise `fallback`, so a
// corrupt or stale stored value can never seed the detector with a NaN or
// out-of-range threshold. `max` is optional — pass null for the open-ended
// duration fields.
function _weDetectSettingValue(raw, fallback, min, max) {
  const n = parseFloat(raw);
  if (!isFinite(n)) return fallback;
  if (n < min) return fallback;
  if (max != null && n > max) return fallback;
  return n;
}

// The playback window for auditing a cut: `pre` seconds before to `post`
// seconds after the cut, clamped to [0, total]. Lets the user hear whether
// a track boundary lands cleanly without manual scrubbing. Pure — drives
// the wave-editor's "preview cut" key/button.
function _wePreviewWindow(cut, total, pre, post) {
  const t = Math.max(0, Number(total) || 0);
  const c = Math.max(0, Math.min(t, Number(cut) || 0));
  return {
    start: Math.max(0, c - (Number(pre)  || 0)),
    end:   Math.min(t, c + (Number(post) || 0)),
  };
}

// Advisory length flag for a track region, surfaced in the track list so an
// obvious mistake gets caught before export: a sub-10s region is almost
// always a stray / mis-detected cut, and a region longer than any single LP
// side (>25 min) usually means a missed cut spanning a side break. Returns
// '' (no flag), 'short', or 'long'. Skipped regions and sub-0.5s "doesn't
// fit" rows never flag (handled elsewhere). Purely advisory — never blocks.
function _weTrackLengthHint(seconds, skip) {
  if (skip) return '';
  const d = Number(seconds) || 0;
  if (d < 0.5) return '';
  if (d < 10) return 'short';
  if (d > 1500) return 'long';
  return '';
}


// ── Expose on window for wave-editor.js + unit tests ─────────────────────
if (typeof window !== 'undefined') {
  window._weRemapForSides      = _weRemapForSides;
  window._wePosLetter          = _wePosLetter;
  window._weCutsFromTracklist  = _weCutsFromTracklist;
  window._weDerivedPositions   = _weDerivedPositions;
  window._weEffectivePositions = _weEffectivePositions;
  window._weSideBounds         = _weSideBounds;
  window._weCutGroupSpan       = _weCutGroupSpan;
  window._weEvenCuts           = _weEvenCuts;
  window._weNudgedCutValue     = _weNudgedCutValue;
  window._weDetectSettingValue = _weDetectSettingValue;
  window._wePreviewWindow      = _wePreviewWindow;
  window._weTrackLengthHint    = _weTrackLengthHint;
}
