// Node-runnable unit tests for `_weNudgedCutValue` — the pure clamp math
// behind the wave-editor's keyboard ←/→ cut nudge.
//
// The helper lives in `app/static/modules/timeline-state.js` (a classic
// script). Load it in a VM sandbox that stubs the browser globals, then
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

const nudge = win._weNudgedCutValue;
if (typeof nudge !== 'function') {
  throw new Error('_weNudgedCutValue not exposed on window');
}

let passed = 0, failed = 0;
function approx(a, b, tol = 1e-6) { return Math.abs(a - b) <= tol; }
function check(name, cond) {
  if (cond) { passed++; }
  else { failed++; console.error('FAIL:', name); }
}

// Free nudge inside the neighbour gap.
check('nudge right within gap',
  approx(nudge([10, 20, 30], 1, 0.1, 40), 20.1));
check('nudge left within gap',
  approx(nudge([10, 20, 30], 1, -0.1, 40), 19.9));

// Clamp against the next / previous neighbour (can't reorder).
check('clamp to next neighbour',
  approx(nudge([10, 20, 30], 1, 100, 40), 30 - 0.001));
check('clamp to prev neighbour',
  approx(nudge([10, 20, 30], 1, -100, 40), 10 + 0.001));

// First cut clamps to 0; last cut clamps to total.
check('first cut clamps to 0',
  approx(nudge([5], 0, -100, 40), 0.001));
check('last cut clamps to total',
  approx(nudge([5], 0, 100, 40), 40 - 0.001));

// Out-of-range index and empty array → null.
check('negative index → null', nudge([10], -1, 0.1, 40) === null);
check('index past end → null', nudge([10], 5, 0.1, 40) === null);
check('empty cuts → null', nudge([], 0, 0.1, 40) === null);
check('non-array → null', nudge(null, 0, 0.1, 40) === null);

console.log(`wave_editor_nudge: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
