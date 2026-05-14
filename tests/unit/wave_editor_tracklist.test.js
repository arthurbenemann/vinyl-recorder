// Node-runnable unit tests for `_weCutsFromTracklist`.
//
// The Discogs-apply path turns a flat list of {title, duration, position}
// rows into the editor's cuts/titles/skipped/positions arrays. When the
// recording has multiple sides AND the tracklist carries side-prefixed
// positions (A1, B2, …), durations cumulate per side and each side change
// emits an end-of-side cut + a draggable side-start cut with a skipped
// gap region between (runout + needle-drop dead time). This file pins
// down both that path and the global-cumulative fallback.
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
// `we` is a module-level const inside wave-editor.js — expose it so the
// _weSideBounds cases can drive the editor's side/total state directly.
vm.runInContext(SRC + '\nif (typeof window !== "undefined") window.__we = we;', sandbox);

const cutsFor    = win._weCutsFromTracklist;
const letter     = win._wePosLetter;
const sideBounds = win._weSideBounds;
const groupSpan  = win._weCutGroupSpan;
const weState    = win.__we;
if (typeof cutsFor !== 'function' || typeof letter !== 'function'
    || typeof sideBounds !== 'function' || typeof groupSpan !== 'function'
    || !weState) {
  throw new Error('helpers not exposed on window');
}

let passed = 0, failed = 0;
function eq(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (!eq(a[i], b[i])) return false;
    return true;
  }
  return a === b;
}
function check(name, got, want) {
  for (const k of Object.keys(want)) {
    if (!eq(got[k], want[k])) {
      console.error(`✗ ${name}\n   ${k} got=${JSON.stringify(got[k])} want=${JSON.stringify(want[k])}`);
      failed += 1;
      return;
    }
  }
  console.log(`✓ ${name}`);
  passed += 1;
}
const t = (rows) => rows.map(([title, secs, pos]) =>
  ({ title, duration_seconds: secs, position: pos }));
const s = (...durs) => durs.map(d => ({ duration_seconds: d }));

// ── _wePosLetter
check('letter: A1', { x: letter('A1') },          { x: 'A' });
check('letter: B12', { x: letter('B12') },        { x: 'B' });
check('letter: 1-01', { x: letter('1-01') },      { x: '1' });
check('letter: 2-03', { x: letter('2-03') },      { x: '2' });
check('letter: empty', { x: letter('') },         { x: '' });
check('letter: bare 7', { x: letter('7') },       { x: '' });

// ── per-side anchoring
check('per-side: A1 A2 B1 B2 (slack at end of side A)',
  cutsFor(t([['T1',3,'A1'],['T2',4,'A2'],['T3',5,'B1'],['T4',4,'B2']]), s(8,10), 18),
  { cuts: [3,7,8,13],
    titles: ['T1','T2','','T3','T4'],
    skipped: [false,false,true,false,false],
    positions: ['A1','A2','','B1','B2'],
    overflow: 0 });

check('per-side: cumulative fits side A exactly (no gap region)',
  cutsFor(t([['T1',3,'A1'],['T2',4,'A2'],['T3',1,'A3'],['T4',5,'B1']]), s(8,10), 18),
  { cuts: [3,7,8],
    titles: ['T1','T2','T3','T4'],
    skipped: [false,false,false,false],
    positions: ['A1','A2','A3','B1'],
    overflow: 0 });

check('per-side: side A overflows — last A track clamped, overflow counted',
  cutsFor(t([['T1',3,'A1'],['T2',4,'A2'],['T3',4,'A3'],['T4',5,'B1']]), s(8,10), 18),
  { cuts: [3,7,8],
    titles: ['T1','T2','T3','T4'],
    overflow: 1 });

check('per-side: 3-side release emits a draggable side-start cut at each boundary',
  cutsFor(t([['T1',3,'A1'],['T2',4,'A2'],['T3',5,'B1'],['T4',4,'B2'],['T5',2,'C1'],['T6',3,'C2']]),
    s(8,10,6), 24),
  { cuts: [3,7,8,13,17,18,20],
    skipped: [false,false,true,false,false,true,false,false],
    positions: ['A1','A2','','B1','B2','','C1','C2'],
    overflow: 0 });

check('per-side: multi-disc "1-01" / "2-01" treated as sides 0 and 1',
  cutsFor(t([['T1',3,'1-01'],['T2',4,'1-02'],['T3',5,'2-01'],['T4',4,'2-02']]), s(8,10), 18),
  { cuts: [3,7,8,13],
    skipped: [false,false,true,false,false],
    positions: ['1-01','1-02','','2-01','2-02'],
    overflow: 0 });

// ── fallback paths
check('fallback: no positions → global cumulative, no skip regions',
  cutsFor(t([['T1',3,''],['T2',4,''],['T3',5,''],['T4',4,'']]), s(8,10), 18),
  { cuts: [3,7,12],
    titles: ['T1','T2','T3','T4'],
    skipped: [false,false,false,false],
    positions: ['','','',''],
    overflow: 0 });

check('fallback: single-side recording → global cumulative even with positions',
  cutsFor(t([['T1',3,'A1'],['T2',4,'A2'],['T3',5,'A3'],['T4',4,'A4']]), s(18), 18),
  { cuts: [3,7,12],
    titles: ['T1','T2','T3','T4'],
    skipped: [false,false,false,false],
    overflow: 0 });

check('fallback: only one side letter present → global cumulative',
  cutsFor(t([['T1',3,'A1'],['T2',4,'A2']]), s(8,10), 18),
  { cuts: [3],
    titles: ['T1','T2'],
    skipped: [false,false],
    overflow: 0 });

// ── _weSideBounds — drag-clamp window for a cut at time `t`
function bounds(sides, total, time) {
  weState.sides = sides.map(d => ({ duration_seconds: d }));
  weState.total = total;
  return { b: sideBounds(time) };
}
check('sideBounds: 2 sides, cut inside side A',  bounds([28,30],58,24.08), { b:[0,28] });
check('sideBounds: 2 sides, cut on the A/B boundary → side B', bounds([28,30],58,28), { b:[28,58] });
check('sideBounds: 2 sides, cut inside side B',  bounds([28,30],58,40),    { b:[28,58] });
check('sideBounds: 2 sides, cut at album end',   bounds([28,30],58,58),    { b:[28,58] });
check('sideBounds: single side spans whole album', bounds([60],60,10),     { b:[0,60] });
check('sideBounds: 3 sides, middle side',        bounds([20,20,20],60,35), { b:[20,40] });
check('sideBounds: last side hi clamps to total when durations under-sum',
  bounds([28,29],58,45), { b:[28,58] });

// ── _weCutGroupSpan — which cuts a shift+drag moves as a rigid group
function span(sides, total, cuts, i) {
  weState.sides = sides.map(d => ({ duration_seconds: d }));
  weState.total = total;
  weState.cuts  = cuts.slice();
  const r = groupSpan(i);
  return { i: r.i, last: r.last, sideLo: r.sideLo, sideHi: r.sideHi };
}
// cuts [8,18,24] on side A (0-28), [40] on side B (28-58).
check('cutGroupSpan: grab first A-side cut → group is all A-side cuts only',
  span([28,30], 58, [8,18,24,40], 0), { i:0, last:2, sideLo:0, sideHi:28 });
check('cutGroupSpan: grab middle A-side cut → group is i..lastA',
  span([28,30], 58, [8,18,24,40], 1), { i:1, last:2, sideLo:0, sideHi:28 });
check('cutGroupSpan: grab the B-side cut → group is just that cut',
  span([28,30], 58, [8,18,24,40], 3), { i:3, last:3, sideLo:28, sideHi:58 });
check('cutGroupSpan: cut sitting exactly on the side boundary belongs to side B',
  span([28,30], 58, [8,28,40], 1), { i:1, last:2, sideLo:28, sideHi:58 });
check('cutGroupSpan: single-side album → group runs to the last cut',
  span([58], 58, [8,18,24,40], 1), { i:1, last:3, sideLo:0, sideHi:58 });

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
