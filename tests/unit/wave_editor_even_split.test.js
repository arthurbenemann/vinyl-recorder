// Node-runnable unit tests for `_weEvenCuts` (in
// app/static/modules/timeline-state.js).
//
// It produces the n-1 evenly-spaced interior cut times that divide a side
// of length `total` into `n` equal tracks — the gapless-side fallback for
// the wave editor's "split evenly" popover. Pins the spacing, the ms
// rounding, the (0, total) clamp, and the n<2 / unknown-length guards.
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
  window:       win,
  document:     { getElementById: () => null, querySelectorAll: () => [],
                   addEventListener: () => {} },
  fetch:        () => Promise.reject(new Error('no fetch in sandbox')),
  console:      console,
  htmlEscape:   (s) => String(s == null ? '' : s),
  setTimeout:   setTimeout,
  clearTimeout: clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);

const even = win._weEvenCuts;
if (typeof even !== 'function') throw new Error('_weEvenCuts not exposed on window');

let passed = 0, failed = 0;
function eqArr(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
function check(name, got, want) {
  if (eqArr(got, want)) { console.log(`✓ ${name}`); passed += 1; }
  else { console.error(`✗ ${name}\n   got=${JSON.stringify(got)} want=${JSON.stringify(want)}`); failed += 1; }
}

// ── even spacing ──────────────────────────────────────────────────────────
check('120s into 4 → 3 cuts at quarters', even(120, 4), [30, 60, 90]);
check('120s into 2 → 1 cut at midpoint',  even(120, 2), [60]);
check('120s into 3 → halves of thirds',   even(120, 3), [40, 80]);

// ── ms rounding (non-integer divisions) ─────────────────────────────────
check('100s into 3 → ms-rounded thirds',  even(100, 3), [33.333, 66.667]);

// ── result length is always n-1 ──────────────────────────────────────────
check('10 tracks → 9 cuts', even(600, 10).length === 9 ? [9] : [even(600, 10).length], [9]);

// ── guards ────────────────────────────────────────────────────────────────
check('n < 2 → empty',          even(120, 1), []);
check('n = 0 → empty',          even(120, 0), []);
check('negative n → empty',     even(120, -3), []);
check('unknown length → empty', even(0, 4), []);
check('negative length → empty', even(-50, 4), []);

// ── tolerant of a string count (the input value arrives as text) ─────────
check('string "4" coerces',     even(120, '4'), [30, 60, 90]);
// fractional n floors (a stray 4.9 still means 4 tracks)
check('fractional n floors',    even(120, 4.9), [30, 60, 90]);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
