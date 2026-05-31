// Node-runnable unit tests for `approxNoiseFloorDbFromPeaks` (peaks.js).
//
// Verifies that the 5th-percentile histogram approach returns:
//   • a dB estimate within ±2 dB of the actual noise floor for synthetic data
//   • quantized=true only when the estimate falls in the lowest histogram bin
//   • null for all-zero or empty inputs
//   • correct results for both the bare-dat and multi-side shapes
'use strict';
const fs   = require('fs');
const path = require('path');
const vm   = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'peaks.js'),
  'utf8',
);

const win = {};
const sandbox = {
  window: win,
  Int16Array, Int32Array, DataView, ArrayBuffer,
  Math, Array,
  console,
};
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);

const fn = win.approxNoiseFloorDbFromPeaks;
if (typeof fn !== 'function') {
  throw new Error('approxNoiseFloorDbFromPeaks not exposed on window');
}

let passed = 0, failed = 0;

function check(name, got, want) {
  if (got === want) {
    console.log(`✓ ${name}`);
    passed++;
  } else {
    console.error(`✗ ${name}\n   got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
    failed++;
  }
}

function checkClose(name, got, want, tol) {
  if (got != null && Math.abs(got - want) <= tol) {
    console.log(`✓ ${name}  (${got.toFixed(2)} ≈ ${want.toFixed(2)}, tol ±${tol})`);
    passed++;
  } else {
    console.error(`✗ ${name}\n   got=${got} want≈${want} tol=±${tol}`);
    failed++;
  }
}

// Build a minimal peaks object that `approxNoiseFloorDbFromPeaks` accepts.
// values is a plain Array of integers; we wrap it in Int16Array to simulate
// how parsePeaks delivers the body.
function makePeaks(values) {
  const body = new Int16Array(values);
  return { body, duration: 1, length: values.length >> 1,
           sampleRate: 96000, samplesPerPixel: 256, channels: 1, bits: 16 };
}

// Exact dB value that bin b's midpoint maps to.
function binDb(b) {
  return 20 * Math.log10(Math.max(1, b * 64 + 32) / 32768);
}

// ── basic shape ──────────────────────────────────────────────────────────────

check('null input → null',  fn(null), null);
check('no body shape → null', fn({}), null);

const allZero = makePeaks(new Array(200).fill(0));
check('all-zero body → null', fn(allZero), null);

// ── estimate accuracy ────────────────────────────────────────────────────────

// 5% of values at amplitude 100 (noise), 95% at amplitude 20000 (music).
// 5th-percentile → bin 1 (64–127), midpoint 96. Actual noise: -50.3 dBFS.
const noiseAmp = 100;
const musicAmp = 20000;
const noiseDb  = 20 * Math.log10(noiseAmp / 32768);
const mixedBody = [...new Array(50).fill(noiseAmp), ...new Array(950).fill(musicAmp)];
const r1 = fn(makePeaks(mixedBody));
checkClose('5% noise + 95% music → dB within 2 dB of actual', r1.db, noiseDb, 2);
check(     '5% noise + 95% music → not quantized',             r1.quantized, false);

// ── uniform body ─────────────────────────────────────────────────────────────

// All 200 values at amplitude 500 → 5th percentile in bin 7, midpoint 480.
const uniformBody = new Array(200).fill(500);
const r2 = fn(makePeaks(uniformBody));
checkClose('uniform amp-500 body → dB within 1 dB of bin midpoint', r2.db, binDb(7), 1);
check(     'uniform amp-500 body → not quantized', r2.quantized, false);

// ── negative values treated as absolute ──────────────────────────────────────

const negBody = [...new Array(50).fill(-100), ...new Array(950).fill(-20000)];
const r3 = fn(makePeaks(negBody));
checkClose('negative values: dB matches positive equivalent', r3.db, r1.db, 0.001);
check(     'negative values: quantized matches', r3.quantized, r1.quantized);

// ── quantization floor ───────────────────────────────────────────────────────

// 5% of values at amplitude 5 (below bin-0 midpoint of 32). The 5th-percentile
// lands in bin 0 → quantized=true, dB = binDb(0) ≈ -60.2.
const veryQuiet = [...new Array(50).fill(5), ...new Array(950).fill(20000)];
const r4 = fn(makePeaks(veryQuiet));
checkClose('very quiet noise → dB = bin-0 midpoint', r4.db, binDb(0), 0.001);
check(     'very quiet noise → quantized=true',       r4.quantized, true);

// ── multi-side shape ─────────────────────────────────────────────────────────

// Same data split across two sides — result should match the flat case.
const side0 = makePeaks([...new Array(25).fill(noiseAmp), ...new Array(475).fill(musicAmp)]);
const side1 = makePeaks([...new Array(25).fill(noiseAmp), ...new Array(475).fill(musicAmp)]);
const multiSide = {
  sides: [
    { peaks: side0, offset: 0,  duration: 5 },
    { peaks: side1, offset: 5,  duration: 5 },
  ],
  total: 10,
};
const r5 = fn(multiSide);
checkClose('multi-side: dB matches flat equivalent', r5.db, r1.db, 0.001);
check(     'multi-side: quantized matches',          r5.quantized, r1.quantized);

// ── single-value edge case ───────────────────────────────────────────────────

// One non-zero value should not crash; 5th percentile of 1 value = that value.
const singleVal = fn(makePeaks([1000]));
check('single non-zero value → not null', singleVal !== null, true);
check('single non-zero value → not quantized', singleVal.quantized, false);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
