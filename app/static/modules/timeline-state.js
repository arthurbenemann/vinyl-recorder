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
// into the editor's cuts/titles/skipped/positions arrays. Per-side anchored
// when ≥2 sides AND tracklist carries side prefixes (A1, B2, …); falls back
// to a global cumulative cursor otherwise.
function _weCutsFromTracklist(td, sides, total) {
  const sideStarts = [0];
  let acc = 0;
  for (const s of sides) {
    acc += Number(s && s.duration_seconds) || 0;
    sideStarts.push(acc);
  }
  // Map first-seen Discogs side letter → recording side index. Letters past
  // the last recording side clamp onto the last side (so a Discogs C/D
  // release on a 2-side recording stacks onto side B rather than crashing).
  const seen = new Map();
  for (const t of td) {
    const L = _wePosLetter(t.position);
    if (L && !seen.has(L)) seen.set(L, Math.min(sides.length - 1, seen.size));
  }
  const usePerSide = sides.length >= 2 && seen.size >= 2;

  const cuts      = [];
  const titles    = [];
  const skipped   = [];
  const positions = [];
  let overflow = 0;

  if (!usePerSide) {
    // Global cumulative — original behaviour. One region per Discogs track.
    let cursor = 0;
    for (let j = 0; j < td.length; j++) {
      titles.push(td[j].title);
      skipped.push(false);
      positions.push(String(td[j].position || '').trim());
      if (j === td.length - 1) continue;
      cursor += Number(td[j].duration_seconds) || 0;
      if (cursor >= total)   { cuts.push(total); overflow += 1; }
      else if (cursor > 0)   { cuts.push(cursor); }
    }
    return { cuts, titles, skipped, positions, overflow };
  }

  let curSide = -1;
  let cursor  = 0;
  for (let j = 0; j < td.length; j++) {
    const Lcur = _wePosLetter(td[j].position);
    const sCur = (Lcur && seen.has(Lcur))
      ? seen.get(Lcur)
      : (curSide >= 0 ? curSide : 0);
    if (sCur !== curSide) {
      if (curSide >= 0) {
        // End-of-prev-side cut. Clamp the cumulative to the side boundary
        // so the previous side's last track can't bleed into the next
        // recorded side; if clamping happens, count overflow.
        const prevSideEnd = sideStarts[curSide + 1];
        const endCur = Math.min(cursor, prevSideEnd);
        if (cursor > prevSideEnd + 0.001) overflow += 1;
        if (endCur > 0 && endCur < total) cuts.push(endCur);
        // Side-start cut. If it lands strictly past the end-cut, insert a
        // skipped gap region between them — that's the runout + needle-
        // drop dead time. If they coincide (no slack on the previous side),
        // skip the gap and let the side change be a single cut.
        const sideStart = sideStarts[sCur];
        if (sideStart > endCur + 0.001 && sideStart < total) {
          cuts.push(sideStart);
          titles.push('');
          skipped.push(true);
          positions.push('');
        }
      }
      curSide = sCur;
      cursor  = sideStarts[curSide];
    }
    titles.push(td[j].title);
    skipped.push(false);
    positions.push(String(td[j].position || '').trim());
    cursor += Number(td[j].duration_seconds) || 0;
    if (j === td.length - 1) continue;
    // Don't emit a within-side cut here if the next track lives on a new
    // side — the side-change branch above will emit the boundary cuts.
    const Lnext = _wePosLetter(td[j + 1].position);
    const sNext = (Lnext && seen.has(Lnext)) ? seen.get(Lnext) : curSide;
    if (sNext !== curSide) continue;
    if (cursor >= total)   { cuts.push(total); overflow += 1; }
    else if (cursor > 0)   { cuts.push(cursor); }
  }
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


// ── Expose on window for wave-editor.js + unit tests ─────────────────────
if (typeof window !== 'undefined') {
  window._weRemapForSides      = _weRemapForSides;
  window._wePosLetter          = _wePosLetter;
  window._weCutsFromTracklist  = _weCutsFromTracklist;
  window._weDerivedPositions   = _weDerivedPositions;
  window._weEffectivePositions = _weEffectivePositions;
  window._weSideBounds         = _weSideBounds;
  window._weCutGroupSpan       = _weCutGroupSpan;
  window._weDetectSettingValue = _weDetectSettingValue;
}
