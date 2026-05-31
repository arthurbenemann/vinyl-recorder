// Node-runnable tests for the stats-line rendering logic in wave-editor.js.
//
// Exercises _renderApproxStats (pre-measure readout) and invalidateMeasure
// (cuts-changed banner) by mounting the script in a vm sandbox with just
// enough mock DOM to capture document.getElementById('we-stats-text').textContent.
'use strict';
const fs   = require('fs');
const path = require('path');
const vm   = require('vm');

// ── minimal DOM stubs ────────────────────────────────────────────────────────

const elements = {};
function mockEl(id, attrs = {}) {
  elements[id] = { id, textContent: '', style: {}, hidden: false, ...attrs };
  return elements[id];
}
// Pre-create the elements the code needs.
['we-stats-text', 'we-duration', 'we-mini-end', 'we-canvas',
 'we-mini-canvas', 'we-modal', 'we-sides', 'we-source-badge',
 'we-noise', 'we-noise-readout', 'we-mindur', 'we-mindur-readout',
 'we-skiplong', 'we-skiplong-readout'].forEach(id => mockEl(id));

const mockDoc = {
  getElementById: (id) => elements[id] || mockEl(id),
  querySelectorAll: () => [],
  addEventListener:  () => {},
  createElement: (tag) => ({
    tagName: tag, className: '', style: {}, textContent: '',
    appendChild: () => {}, addEventListener: () => {},
    querySelectorAll: () => [],
  }),
};

const mockCanvas = {
  getContext: () => ({
    clearRect: () => {}, fillRect: () => {}, fillStyle: '',
    getImageData: () => ({ data: new Uint8ClampedArray(4) }),
  }),
  width: 800, height: 200, clientWidth: 800, clientHeight: 200,
};
elements['we-canvas'] = mockCanvas;
elements['we-mini-canvas'] = mockCanvas;

const peaksSrc = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'peaks.js'), 'utf8');
const weSrc = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'wave-editor.js'), 'utf8');

const win = { devicePixelRatio: 1 };
const sandbox = {
  window:       win,
  document:     mockDoc,
  console,
  fetch:        () => Promise.reject(new Error('no fetch')),
  setTimeout, clearTimeout, setInterval, clearInterval,
  Int16Array, Int32Array, DataView, ArrayBuffer,
  Math, Array, Object, Promise, JSON, Error,
  requestAnimationFrame: (fn) => setTimeout(fn, 16),
  cancelAnimationFrame: clearTimeout,
  localStorage: { getItem: () => null, setItem: () => {} },
  HTMLElement:  function(){},
  htmlEscape:   (s) => String(s == null ? '' : s),
};
vm.createContext(sandbox);

// Load peaks.js first (exposes approxNoiseFloorDbFromPeaks on window).
vm.runInContext(peaksSrc, sandbox);

// Load wave-editor.js.
vm.runInContext(weSrc, sandbox);

// After load, grab the internals we need to drive.
const we = win._weEditorState;
if (!we) throw new Error('_weEditorState not exposed on window');

// ── test harness ─────────────────────────────────────────────────────────────

let passed = 0, failed = 0;
function check(name, got, want) {
  const ok = typeof want === 'string' ? got === want
           : (want instanceof RegExp ? want.test(got) : got === want);
  if (ok) {
    console.log(`✓ ${name}`);
    passed++;
  } else {
    console.error(`✗ ${name}\n   got=${JSON.stringify(got)}\n   want=${want}`);
    failed++;
  }
}

function statsText() { return elements['we-stats-text'].textContent; }
function render()    { win._weRenderApproxStats(); }

// Reset to a known state before each group.
function reset() {
  we.measured          = null;
  we.approxPeakDb      = null;
  we.approxNoiseFloorDb = null;
  we.approxNoiseFloorQuantized = false;
  elements['we-stats-text'].textContent = '';
}

// ── case 1: no peaks yet ─────────────────────────────────────────────────────
reset();
render();
check('no peaks → prompt to measure', statsText(),
  /click measure to compute peak/);

// ── case 2: peak only (noise null) ──────────────────────────────────────────
reset();
we.approxPeakDb = -3.2;
render();
check('peak only → ~peak, no noise', statsText(), /peak ~-3\.2 dB/);
check('peak only → no "noise" in text', !statsText().includes('noise'), true);
check('peak only → still prompts measure', statsText(), /click measure/);

// ── case 3: peak + noise estimate ──────────────────────────────────────────
reset();
we.approxPeakDb       = -3.2;
we.approxNoiseFloorDb = -50;
we.approxNoiseFloorQuantized = false;
render();
check('peak+noise → ~peak present',  statsText(), /peak ~-3\.2 dB/);
check('peak+noise → ~noise present', statsText(), /noise ~-50 dB/);
check('peak+noise → no ~> prefix',   !statsText().includes('~>'), true);
check('peak+noise → click measure',  statsText(), /click measure/);

// ── case 4: peak + quantized noise floor ────────────────────────────────────
reset();
we.approxPeakDb       = -3.2;
we.approxNoiseFloorDb = -60;
we.approxNoiseFloorQuantized = true;
render();
check('quantized → ~> prefix shown',  statsText(), /noise ~> -60 dB/);
check('quantized → peak still there', statsText(), /peak ~-3\.2 dB/);

// ── case 5: measured wins over approx ───────────────────────────────────────
reset();
we.approxPeakDb = -3.2;
we.approxNoiseFloorDb = -50;
we.measured = { peak_db: -3.0, noise_floor_db: -55.0 };
elements['we-stats-text'].textContent = 'sentinel';
render();
check('measured wins → text untouched', statsText(), 'sentinel');

// ── case 6: invalidateMeasure banner (cuts changed) ─────────────────────────
reset();
we.approxPeakDb       = -3.2;
we.approxNoiseFloorDb = -50;
we.approxNoiseFloorQuantized = false;
// Seed a stale measure so invalidateMeasure has something to clear.
we.measured = { peak_db: -3.0 };
win._weInvalidateMeasure();
check('cuts banner → ~peak',   statsText(), /peak ~-3\.2 dB/);
check('cuts banner → ~noise',  statsText(), /noise ~-50 dB/);
check('cuts banner → re-measure prompt', statsText(), /re-measure/);

// ── case 7: invalidateMeasure with quantized noise ───────────────────────────
reset();
we.approxPeakDb       = -3.2;
we.approxNoiseFloorDb = -60;
we.approxNoiseFloorQuantized = true;
we.measured = { peak_db: -3.0 };
win._weInvalidateMeasure();
check('cuts banner quantized → ~> prefix', statsText(), /noise ~> -60 dB/);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
