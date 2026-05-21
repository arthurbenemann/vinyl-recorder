// Node-runnable unit tests for `_wePreviewWindow` — the pure window math
// behind the wave-editor's "preview cut" (audition a track boundary).
//
// The helper lives in `app/static/modules/timeline-state.js` (a classic
// script). Load it in a VM sandbox with the browser globals stubbed, then
// call the function exposed on `window`. Failures throw → non-zero exit so
// the pytest wrapper surfaces them.
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
  window:   win,
  document: { getElementById: () => null, querySelectorAll: () => [],
              addEventListener: () => {} },
  console:  console,
};
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);

const win_fn = win._wePreviewWindow;
if (typeof win_fn !== 'function') {
  throw new Error('_wePreviewWindow not exposed on window');
}

let passed = 0, failed = 0;
function approx(a, b, tol = 1e-6) { return Math.abs(a - b) <= tol; }
function check(name, cond) {
  if (cond) { passed++; }
  else { failed++; console.error('FAIL:', name); }
}

// Mid-album cut: full window on both sides.
let w = win_fn(60, 300, 2, 2);
check('mid start', approx(w.start, 58));
check('mid end',   approx(w.end, 62));

// Cut near the start clamps the window's start to 0.
w = win_fn(1, 300, 2, 2);
check('near-start clamps to 0', approx(w.start, 0));
check('near-start end', approx(w.end, 3));

// Cut near the end clamps the window's end to total.
w = win_fn(299, 300, 2, 2);
check('near-end start', approx(w.start, 297));
check('near-end clamps to total', approx(w.end, 300));

// Asymmetric pre/post honoured.
w = win_fn(100, 300, 5, 1);
check('asymmetric start', approx(w.start, 95));
check('asymmetric end',   approx(w.end, 101));

// Cut beyond total is clamped before windowing.
w = win_fn(400, 300, 2, 2);
check('over-total clamps cut', approx(w.start, 298) && approx(w.end, 300));

console.log(`wave_editor_preview: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
