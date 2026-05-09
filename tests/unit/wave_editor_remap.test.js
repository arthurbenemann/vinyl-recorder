// Node-runnable unit tests for `_weRemapForSides`.
//
// wave-editor.js is browser-targeted — it pokes at `document`, an
// `albumsByName` global, etc. We can't `require` it from Node directly,
// but the remap helper is pure: load the file in a sandbox that stubs
// the browser globals, then call into the exposed function. Failures
// throw, which propagates to a non-zero exit so the pytest runner
// surfaces them.
'use strict';
const fs   = require('fs');
const path = require('path');
const vm   = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'wave-editor.js'),
  'utf8',
);

const win = {};
const sandbox = {
  window:           win,
  document:         { getElementById: () => null, querySelectorAll: () => [],
                       addEventListener: () => {} },
  fetch:            () => Promise.reject(new Error('no fetch in sandbox')),
  console:          console,
  htmlEscape:       (s) => String(s == null ? '' : s),
  setTimeout:       setTimeout,
  clearTimeout:     clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);

const remap = win._weRemapForSides;
if (typeof remap !== 'function') {
  throw new Error('_weRemapForSides not exposed on window');
}

let passed = 0, failed = 0;
function approx(a, b, tol = 0.01) { return Math.abs(a - b) <= tol; }
function arrApprox(a, b, tol = 0.01) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (!approx(a[i], b[i], tol)) return false;
  return true;
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  ✓ ${name}`);
  } catch (e) {
    failed++;
    console.log(`  ✗ ${name}\n      ${e.message}`);
  }
}

const sideA = { filename: 'A.flac', duration_seconds: 10 };
const sideB = { filename: 'B.flac', duration_seconds: 12 };
const sideC = { filename: 'C.flac', duration_seconds: 8 };

test('identity reorder is a no-op', () => {
  // Cuts within sides AND a cross-boundary track at [7, 14]. Identity
  // reorder must reproduce the input exactly — adjacent regions sharing
  // title coalesce back into one even though they were augmented.
  const r = remap([sideA, sideB], [sideA, sideB], {
    cuts: [3, 7, 14],
    titles:  ['t1', 't2', 't3', 't4'],
    skipped: [false, false, false, false],
    total: 22,
  });
  if (!arrApprox(r.cuts, [3, 7, 14])) throw new Error('cuts: ' + r.cuts);
  if (!eq(r.titles, ['t1','t2','t3','t4'])) throw new Error('titles: ' + r.titles);
});

test('two-side swap moves within-side cuts cleanly', () => {
  // No cross-boundary cuts. Swap A↔B; cuts on each side ride along.
  // A: 0..10 with cut at 4. B: 10..22 with cut at 16. After swap
  // (B then A): B at 0..12 (old cut at 6 from B-local), A at 12..22
  // (old cut at 16 from A-local).
  const r = remap([sideA, sideB], [sideB, sideA], {
    cuts: [4, 16],
    titles:  ['a1', 'a2', 'b1'],
    skipped: [false, false, false],
    total: 22,
  });
  // Augmented cut list adds the boundary at 10. Sub-regions:
  //   [0,4] a1 → A, new [12, 16]
  //   [4,10] a2 → A (mid 7), new [16, 22]
  //   [10,16] a2 → B (since augmented; mid 13 in [10, 22] = old region 1) — but [10,16] mid = 13, originalRegion(13) = 1 (a2). So B-portion of a2 → new [0, 6]
  // Wait the original cuts were [4, 16] so old region 1 is [4, 16] which spans A→B. The augmented split puts:
  //   [4, 10] → still original region 1 (a2)
  //   [10, 16] → also original region 1 (a2)
  // After remap and sort, both halves of a2 land on different new sides
  // (B = [0, 6], A = [16, 22]). They DON'T merge because the new layout
  // has b1 ([6, 12]) and a1 ([12, 16]) between them.
  // Expected: cuts=[6, 12, 16, 22-ish), titles=[a2, b1, a1, a2]
  if (!arrApprox(r.cuts, [6, 12, 16])) throw new Error('cuts: ' + r.cuts);
  if (!eq(r.titles, ['a2', 'b1', 'a1', 'a2'])) throw new Error('titles: ' + r.titles);
});

test('three-side reorder anchors each track to its physical side', () => {
  // 3 sides A=10, B=12, C=8 → total 30. Cuts placed at side-internal
  // positions plus the boundaries: [4, 10, 18, 22, 25]. Six regions:
  //   r0 [0, 4]   a1  in A
  //   r1 [4, 10]  a2  in A
  //   r2 [10, 18] b1  in B
  //   r3 [18, 22] b2  in B
  //   r4 [22, 25] c1  in C
  //   r5 [25, 30] c2  in C
  // Reorder [C, A, B] → new offsets C=0, A=8, B=18.
  //   c1 → [0, 3], c2 → [3, 8], a1 → [8, 12], a2 → [12, 18],
  //   b1 → [18, 26], b2 → [26, 30].
  const r = remap([sideA, sideB, sideC], [sideC, sideA, sideB], {
    cuts: [4, 10, 18, 22, 25],
    titles:  ['a1','a2','b1','b2','c1','c2'],
    skipped: [false,false,false,true,false,false],
    total: 30,
  });
  if (!arrApprox(r.cuts, [3, 8, 12, 18, 26])) throw new Error('cuts: ' + r.cuts);
  if (!eq(r.titles, ['c1','c2','a1','a2','b1','b2'])) {
    throw new Error('titles: ' + r.titles);
  }
  if (!eq(r.skipped, [false,false,false,false,false,true])) {
    throw new Error('skipped: ' + r.skipped);
  }
});

test('no cuts → reorder leaves single Track 1 region', () => {
  const r = remap([sideA, sideB], [sideB, sideA], {
    cuts: [], titles: ['Track 1'], skipped: [false], total: 22,
  });
  // Augmentation adds 10. Regions [0, 10] and [10, 22] both share
  // title='Track 1' and skip=false → coalesce back to one.
  if (r.cuts.length !== 0) throw new Error('cuts not empty: ' + r.cuts);
  if (!eq(r.titles, ['Track 1'])) throw new Error('titles: ' + r.titles);
  if (!eq(r.skipped, [false])) throw new Error('skipped: ' + r.skipped);
});

test('cross-boundary track splits into two siblings sharing the title', () => {
  // Single cut at 5 (on B). Track 1 = [0, 5] spans A→B. Track 2 = [5, 22]
  // is wholly on B. After A↔B swap (B first):
  //   region [0, 5] origIdx=0 (Track 1). Augmented at A→B boundary 10:
  //     - [0, 5] → A→B... wait 5 < 10 so [0, 5] is on A. No augmentation
  //       inside, just one piece. Wait — 0 and 5 both on A, region is on A.
  // Hmm let me reconsider. The cut is at 5 (on A). region 0 = [0, 5] (A),
  // region 1 = [5, 22] (spans A→B).
  // Augmented cuts: [5, 10] (interior). region 1 splits into [5, 10] and
  // [10, 22], both originalRegion=1 (Track 2).
  //   [0, 5] Track 1 in A → new [12, 17]
  //   [5, 10] Track 2 in A → new [17, 22]
  //   [10, 22] Track 2 in B → new [0, 12]
  // Sort: Track 2 [0, 12], Track 1 [12, 17], Track 2 [17, 22]
  // Coalesce: Track 1 and the two Track 2's are not adjacent in new layout
  // (B portion at [0,12], then A portion at [17,22]). Track 1 sits between
  // them. So we get THREE regions: T2, T1, T2.
  const r = remap([sideA, sideB], [sideB, sideA], {
    cuts: [5],
    titles:  ['Track 1', 'Track 2'],
    skipped: [false, false],
    total: 22,
  });
  if (!arrApprox(r.cuts, [12, 17])) throw new Error('cuts: ' + r.cuts);
  if (!eq(r.titles, ['Track 2', 'Track 1', 'Track 2'])) {
    throw new Error('titles: ' + r.titles);
  }
});

test('cut on side boundary survives reorder cleanly', () => {
  // Cut at exactly 10 (the A/B boundary). Region 0 = A, region 1 = B.
  // Swap A↔B: new offsets B=0, A=12. Region 0 (a) → new [12, 22].
  // Region 1 (b) → new [0, 12]. Single new cut at 12.
  const r = remap([sideA, sideB], [sideB, sideA], {
    cuts: [10], titles: ['a', 'b'], skipped: [false, false], total: 22,
  });
  if (!arrApprox(r.cuts, [12])) throw new Error('cuts: ' + r.cuts);
  if (!eq(r.titles, ['b', 'a'])) throw new Error('titles: ' + r.titles);
});

test('preserves old arrays so the caller can revert', () => {
  const r = remap([sideA, sideB], [sideB, sideA], {
    cuts: [4], titles: ['t1', 't2'], skipped: [false, true], total: 22,
  });
  if (!eq(r.oldCuts, [4])) throw new Error('oldCuts: ' + r.oldCuts);
  if (!eq(r.oldTitles, ['t1','t2'])) throw new Error('oldTitles: ' + r.oldTitles);
  if (!eq(r.oldSkipped, [false, true])) throw new Error('oldSkipped: ' + r.oldSkipped);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
