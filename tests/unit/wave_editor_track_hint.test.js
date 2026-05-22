// Node-runnable unit tests for `_weTrackLengthHint` — the advisory short/
// long flag shown on track rows in the wave editor.
//
// Loads `app/static/modules/timeline-state.js` (a classic script) in a VM
// sandbox with browser globals stubbed, then calls the exposed function.
// Failures throw → non-zero exit so the pytest wrapper surfaces them.
'use strict';
const fs   = require('fs');
const path = require('path');
const vm   = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'modules', 'timeline-state.js'),
  'utf8',
);

const win = {};
const sandbox = { window: win,
  document: { getElementById: () => null, querySelectorAll: () => [], addEventListener: () => {} },
  console };
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);

const hint = win._weTrackLengthHint;
if (typeof hint !== 'function') throw new Error('_weTrackLengthHint not exposed');

let passed = 0, failed = 0;
function check(name, cond) { if (cond) passed++; else { failed++; console.error('FAIL:', name); } }

check('sub-10s flags short', hint(4, false) === 'short');
check('9.9s flags short',    hint(9.9, false) === 'short');
check('normal track no flag', hint(210, false) === '');
check('exactly 10s no flag',  hint(10, false) === '');
check('over 25min flags long', hint(1600, false) === 'long');
check('25min boundary no flag', hint(1500, false) === '');
check('sub-0.5s no flag (handled as doesn\'t-fit)', hint(0.2, false) === '');
check('skipped never flags (short)', hint(4, true) === '');
check('skipped never flags (long)', hint(1600, true) === '');
check('zero/garbage no flag', hint(0, false) === '' && hint(NaN, false) === '');

console.log(`wave_editor_track_hint: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
