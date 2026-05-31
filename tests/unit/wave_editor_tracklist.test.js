// Node-runnable unit tests for `_weCutsFromTracklist`, `_wePosLetter`,
// `_weSideBounds`, and `_weCutGroupSpan`. All four now live in
// `app/static/modules/timeline-state.js` (extracted from wave-editor.js).
//
// The Discogs-apply path turns a flat list of {title, duration, position}
// rows into the editor's cuts/titles/skipped/positions arrays. When the
// recording has multiple sides AND the tracklist carries side-prefixed
// positions (A1, B2, …), each side's tracks are spread across that side's
// span — weighted by duration when present, evenly split when not — and a
// skipped "silence" region is seeded at every side interface (a lead-in
// before each side's first track, plus a lead-out after the final track).
// The lead-in makes the first track region 1, so it gets a normal handle.
// Without side prefixes the whole album is laid out as one side. This file
// pins down both paths, including the durationless case (the old cumulative
// layout collapsed every track of a durationless side onto its first track).
//
// `_weSideBounds` / `_weCutGroupSpan` read editor state from
// `window.we`; the helper `setWe(...)` here pokes a fresh state shape
// into the sandbox window so each assertion is reproducible.
'use strict';
const fs   = require('fs');
const path = require('path');
const vm   = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'modules', 'timeline-state.js'),
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

const cutsFor    = win._weCutsFromTracklist;
const letter     = win._wePosLetter;
const sideBounds = win._weSideBounds;
const groupSpan  = win._weCutGroupSpan;
if (typeof cutsFor !== 'function' || typeof letter !== 'function'
    || typeof sideBounds !== 'function' || typeof groupSpan !== 'function') {
  throw new Error('helpers not exposed on window');
}

// `_weSideBounds` and `_weCutGroupSpan` read `window.we` lazily. Set up a
// fresh `we` object before each call so assertions stay reproducible.
function setWe(patch) {
  win.we = Object.assign({ sides: [], total: 0, cuts: [], skipped: [], positions: [] }, patch);
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

// Rebuild the region list a result implies, the same way renderTracks does.
function regionsOf(r, total) {
  const b = [0, ...r.cuts, total];
  return b.slice(0, -1).map((start, i) => ({
    start, end: b[i + 1], dur: b[i + 1] - start,
    title: r.titles[i], skip: r.skipped[i], pos: r.positions[i],
  }));
}
const aligned = (r) =>
  r.titles.length === r.cuts.length + 1 &&
  r.skipped.length === r.cuts.length + 1 &&
  r.positions.length === r.cuts.length + 1;

// ── per-side: durationless tracks even-split inside each side ─────────────
// The reported bug: a release with no Discogs durations collapsed every
// track onto its side's first track. Now they spread evenly.
// Silence layout (issue #75): the first side gets a lead-in at t=0 plus a
// lead-out at its end; every subsequent side gets only a lead-out.  This
// yields exactly ONE skip region at each side boundary (the preceding side's
// lead-out) so there's a single handle to position the flip gap.
check('per-side durationless: even split + lead-in/A-lead-out/single-flip-handle',
  cutsFor(t([['T1',null,'A1'],['T2',null,'A2'],['T3',null,'B1'],['T4',null,'B2']]), s(100,100), 200),
  { cuts: [2,50,98,100,149,198],
    titles: ['','T1','T2','','T3','T4',''],
    skipped: [true,false,false,true,false,false,true],
    positions: ['','A1','A2','','B1','B2',''],
    overflow: 0 });

// ── per-side: durations present weight the split (proportional fill) ──────
check('per-side weighted: 30/60 split fills side A 1:2',
  cutsFor(t([['T1',30,'A1'],['T2',60,'A2'],['T3',60,'B1'],['T4',30,'B2']]), s(92,94), 186),
  { cuts: [2,31.333,90,92,153.333,184],
    titles: ['','T1','T2','','T3','T4',''],
    skipped: [true,false,false,true,false,false,true],
    positions: ['','A1','A2','','B1','B2',''],
    overflow: 0 });

// ── per-side: a Discogs side longer than the recording is scaled + flagged
check('per-side overflow: oversize side is scaled to fit and counted',
  cutsFor(t([['T1',80,'A1'],['T2',80,'A2'],['T3',40,'B1'],['T4',40,'B2']]), s(100,100), 200),
  { cuts: [2,50,98,100,149,198], overflow: 1 });

// ── per-side: multi-disc "1-01" / "2-01" map to sides 0 and 1
check('per-side: multi-disc "1-01" / "2-01" treated as sides 0 and 1',
  cutsFor(t([['T1',null,'1-01'],['T2',null,'1-02'],['T3',null,'2-01'],['T4',null,'2-02']]), s(100,100), 200),
  { cuts: [2,50,98,100,149,198],
    positions: ['','1-01','1-02','','2-01','2-02',''],
    overflow: 0 });

// ── per-side: Discogs C-side on a 2-side recording stacks onto the last side
check('clamp: extra Discogs side stacks onto the recording\'s last side',
  cutsFor(t([['T1',null,'A1'],['T2',null,'A2'],['T3',null,'B1'],['T4',null,'B2'],['T5',null,'C1'],['T6',null,'C2']]),
    s(100,100), 200),
  { cuts: [2,50,98,100,124.5,149,173.5,198],
    titles: ['','T1','T2','','T3','T4','T5','T6',''],
    skipped: [true,false,false,true,false,false,false,false,true],
    positions: ['','A1','A2','','B1','B2','C1','C2',''],
    overflow: 0 });

// ── structural invariants on the real "It's Album Time" shape ─────────────
// 12 durationless tracks, 4 sides, continuous numbering (A1..A3, B4..B6, …).
const TT = cutsFor(
  t([['Intro',null,'A1'],['Leisure',null,'A2'],['Acapulco',null,'A3'],
     ['Svensk',null,'B4'],['Strandbar',null,'B5'],['Delorean',null,'B6'],
     ['Johnny',null,'C7'],['Alfonso',null,'C8'],['Swing1',null,'C9'],
     ['Swing2',null,'D10'],['OhJoy',null,'D11'],['Norse',null,'D12']]),
  s(100,100,100,100), 400);
const TTr = regionsOf(TT, 400);
check('album-time: parallel arrays stay index-aligned', { x: aligned(TT) }, { x: true });
check('album-time: no zero-width "doesn\'t fit" region',
  { x: TTr.every(g => g.dur >= 0.5) }, { x: true });
check('album-time: all 12 tracks present as non-skip regions, in order',
  { x: TTr.filter(g => !g.skip).map(g => g.title) },
  { x: ['Intro','Leisure','Acapulco','Svensk','Strandbar','Delorean',
        'Johnny','Alfonso','Swing1','Swing2','OhJoy','Norse'] });
check('album-time: non-skip positions follow the tracklist',
  { x: TTr.filter(g => !g.skip).map(g => g.pos) },
  { x: ['A1','A2','A3','B4','B5','B6','C7','C8','C9','D10','D11','D12'] });
check('album-time: region 0 is the lead-in skip at t=0 → A1 (region 1) gets a handle',
  { lead: TTr[0].skip && TTr[0].start === 0, a1: !TTr[1].skip && TTr[1].title === 'Intro' },
  { lead: true, a1: true });
check('album-time: lead-out skip ends at the album total',
  { x: TTr[TTr.length - 1].skip && Math.abs(TTr[TTr.length - 1].end - 400) < 0.001 },
  { x: true });
check('album-time: each side keeps exactly its 3 tracks (no side eats the rest)',
  { x: [0,1,2,3].map(k =>
      TTr.filter(g => !g.skip && g.start >= k*100 && g.start < (k+1)*100).length) },
  { x: [3,3,3,3] });
check('album-time: durationless → nothing overflows', { x: TT.overflow }, { x: 0 });

// ── fallback paths (no usable side prefixes → whole album is one side) ─────
check('fallback: no side letters → one side, even split + lead-in/lead-out',
  cutsFor(t([['T1',null,''],['T2',null,''],['T3',null,''],['T4',null,'']]), s(204), 204),
  { cuts: [2,52,102,152,202],
    titles: ['','T1','T2','T3','T4',''],
    skipped: [true,false,false,false,false,true],
    positions: ['','','','','',''],
    overflow: 0 });

check('fallback: single-side recording keeps positions, still gets lead-in/out',
  cutsFor(t([['T1',null,'A1'],['T2',null,'A2'],['T3',null,'A3'],['T4',null,'A4']]), s(204), 204),
  { cuts: [2,52,102,152,202],
    titles: ['','T1','T2','T3','T4',''],
    skipped: [true,false,false,false,false,true],
    positions: ['','A1','A2','A3','A4',''],
    overflow: 0 });

check('fallback: only one distinct side letter → global single-side layout',
  cutsFor(t([['T1',null,'A1'],['T2',null,'A2'],['T3',null,'A3'],['T4',null,'A4']]), s(100,104), 204),
  { cuts: [2,52,102,152,202],
    titles: ['','T1','T2','T3','T4',''],
    positions: ['','A1','A2','A3','A4',''],
    overflow: 0 });

// ── _weSideBounds — drag-clamp window for a cut at time `t`
function bounds(sides, total, time) {
  setWe({ sides: sides.map(d => ({ duration_seconds: d })), total });
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
  setWe({ sides: sides.map(d => ({ duration_seconds: d })), total, cuts: cuts.slice() });
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
