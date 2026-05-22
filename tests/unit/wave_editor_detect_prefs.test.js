// Node-runnable unit tests for `_weDetectSettingValue` (in
// app/static/modules/timeline-state.js).
//
// It validates a silence-detection threshold read back from localStorage
// before it's used to seed the editor's controls: parse the raw string,
// reject NaN / out-of-range values, fall back to the HTML default. This
// pins down the clamp boundaries and the "stale junk → default" guard so a
// corrupt stored pref can never feed the detector a NaN or an out-of-band
// threshold.
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

const val = win._weDetectSettingValue;
if (typeof val !== 'function') {
  throw new Error('_weDetectSettingValue not exposed on window');
}

let passed = 0, failed = 0;
function check(name, got, want) {
  if (got === want) {
    console.log(`✓ ${name}`);
    passed += 1;
  } else {
    console.error(`✗ ${name}\n   got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
    failed += 1;
  }
}

// ── valid values pass straight through ───────────────────────────────────
check('noise: in range int',        val('20',  8, 1, 127), 20);
check('noise: lower bound 1',        val('1',   8, 1, 127), 1);
check('noise: upper bound 127',      val('127', 8, 1, 127), 127);
check('mindur: in range float',      val('2.5', 1.5, 0.2, null), 2.5);
check('skiplong: open-ended high',   val('600', 15, 2, null), 600);

// ── out of range / junk → fallback ───────────────────────────────────────
check('noise: above max → default',  val('200', 8, 1, 127), 8);
check('noise: below min → default',  val('0',   8, 1, 127), 8);
check('noise: negative → default',   val('-5',  8, 1, 127), 8);
check('mindur: below min → default', val('0.1', 1.5, 0.2, null), 1.5);
check('skiplong: below min → default', val('1', 15, 2, null), 15);

// ── corrupt / missing stored values → fallback ───────────────────────────
check('null (no stored pref) → default',  val(null, 8, 1, 127), 8);
check('empty string → default',           val('',   8, 1, 127), 8);
check('non-numeric junk → default',       val('abc', 1.5, 0.2, null), 1.5);
check('NaN literal → default',            val('NaN', 8, 1, 127), 8);
check('Infinity literal → default',       val('Infinity', 15, 2, null), 15);

// parseFloat is lenient: "12abc" → 12, which is a deliberate accept (a
// trailing-unit typo still yields a usable number) — pin it so the
// behaviour is a choice, not an accident.
check('trailing junk parses leading number', val('12abc', 8, 1, 127), 12);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
