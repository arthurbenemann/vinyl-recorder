// Node-runnable unit test for the wave-editor's plan-save 409 path.
//
// Bug #2 from PR-H's bug-hunt: two tabs editing the same album's plan
// silently overwrite. The fix is optimistic concurrency: the editor
// sends its `we.planVersion`, and on 409 it must (a) surface a toast,
// (b) keep `we.dirty` true so the user can recover, and (c) latch
// `we.planConflict` so the debounce loop doesn't spam guaranteed-409
// writes.
//
// This test stubs the browser globals enough to load wave-editor.js,
// drives `_savePlanNow` against a fake fetch that returns 409, and
// asserts the contract above.

'use strict';
const fs   = require('fs');
const path = require('path');
const vm   = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'wave-editor.js'),
  'utf8',
);

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`  ✓ ${name}`);
    passed++;
  } else {
    console.error(`  ✗ ${name}${detail ? ' — ' + detail : ''}`);
    failed++;
  }
}

// ── Sandbox setup ────────────────────────────────────────────────────────
// The editor pokes at `document` for the format/bit-depth/sample-rate
// selectors AND for the `we-saved` host where the reload button lands.
// Stub each with a minimal element shape — the test doesn't need real
// DOM, just the shape `_showPlanConflictToast` reads.

function makeElement(tag) {
  return {
    tagName:   tag,
    children:  [],
    value:     '',
    hidden:    false,
    textContent: '',
    title:     '',
    type:      '',
    className: '',
    disabled:  false,
    appendChild(child) { this.children.push(child); child.parent = this; return child; },
    removeChild(child) {
      const i = this.children.indexOf(child);
      if (i >= 0) this.children.splice(i, 1);
      return child;
    },
    addEventListener() {},
    focus() {},
    onclick: null,
    parent:  null,
  };
}

const elements = {
  'we-format':       makeElement('select'),
  'we-bitdepth':     makeElement('select'),
  'we-sample-rate':  makeElement('select'),
  'we-saved':        makeElement('span'),
};
elements['we-format'].value      = 'flac';
elements['we-bitdepth'].value    = '0';
elements['we-sample-rate'].value = '0';

const documentStub = {
  getElementById:   (id) => elements[id] || null,
  querySelectorAll: () => [],
  querySelector:    () => null,
  addEventListener: () => {},
  createElement:    (tag) => makeElement(tag),
};

// Capture toast() calls so the test can assert the conflict UX.
const toastCalls = [];
const win = {
  toast: (msg, kind) => { toastCalls.push({ msg, kind }); },
};

// fetchMode drives what the next fetch() resolves to. The test flips
// this between "409" (conflict path) and "200" (happy path) to cover
// both branches with one shared module load.
let fetchMode = 'ok';
let fetchCalls = [];
function fetchStub(url, opts) {
  const call = { url, opts };
  fetchCalls.push(call);
  if (fetchMode === '409') {
    return Promise.resolve({
      ok:     false,
      status: 409,
      json:   () => Promise.resolve({
        detail:       'plan version mismatch',
        plan:         { tracks: [{ title: 'other-tab', duration_seconds: 5, skip: false }] },
        plan_version: 42,
      }),
    });
  }
  // Default: 200 with bumped version. parseInt on the body to extract
  // the expected_version for assertion convenience.
  return Promise.resolve({
    ok:     true,
    status: 200,
    json:   () => Promise.resolve({
      plan:         { tracks: [{ title: 'ok', duration_seconds: 1, skip: false }] },
      plan_version: 7,
    }),
  });
}

const sandbox = {
  window:           win,
  document:         documentStub,
  fetch:            fetchStub,
  console:          console,
  htmlEscape:       (s) => String(s == null ? '' : s),
  setTimeout:       setTimeout,
  clearTimeout:     clearTimeout,
  setInterval:      () => 0,
  clearInterval:    () => {},
  Date:             Date,
  Math:             Math,
  JSON:             JSON,
  Number:           Number,
  Promise:          Promise,
  parseInt:         parseInt,
  parseFloat:       parseFloat,
  isNaN:            isNaN,
};
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);

const savePlanNow = win._savePlanNow;
const we          = win._weEditorState;
if (typeof savePlanNow !== 'function' || !we) {
  throw new Error('_savePlanNow / we not exposed on window');
}

// ── Test 1: happy-path save updates we.planVersion + clears dirty ────────
async function testHappyPathSave() {
  fetchMode  = 'ok';
  toastCalls.length = 0;
  fetchCalls.length = 0;
  // Seed editor state: one album, dirty, loaded, version 3 from a prior
  // load. The fake fetch will return 200 with plan_version=7.
  we.albumId       = 'aaaaaaaa';
  we.total         = 10;
  we.cuts          = [];
  we.titles        = ['Track 1'];
  we.skipped       = [false];
  we.loaded        = true;
  we.dirty         = true;
  we.planVersion   = 3;
  we.planConflict  = false;

  await savePlanNow();

  check('happy path: fetch was called',           fetchCalls.length === 1);
  const body = JSON.parse(fetchCalls[0].opts.body);
  check('happy path: expected_version sent',      body.expected_version === 3,
    `got expected_version=${body.expected_version}`);
  check('happy path: tracks sent',                Array.isArray(body.tracks) && body.tracks.length === 1);
  check('happy path: dirty cleared after 200',    we.dirty === false);
  check('happy path: planVersion updated from response', we.planVersion === 7,
    `got planVersion=${we.planVersion}`);
  check('happy path: no conflict latched',        we.planConflict === false);
  check('happy path: no error toast shown',       toastCalls.length === 0);
}

// ── Test 2: 409 sets planConflict + keeps dirty + surfaces toast ─────────
async function testConflictPathSave() {
  fetchMode  = '409';
  toastCalls.length = 0;
  fetchCalls.length = 0;
  // Clear out the we-saved host's children from any prior test so the
  // assertion about the Reload button isn't polluted.
  elements['we-saved'].children.length = 0;
  elements['we-saved'].textContent = '';
  elements['we-saved'].hidden = true;
  we.albumId       = 'aaaaaaaa';
  we.total         = 10;
  we.cuts          = [];
  we.titles        = ['Track 1'];
  we.skipped       = [false];
  we.loaded        = true;
  we.dirty         = true;
  we.planVersion   = 3;
  we.planConflict  = false;

  await savePlanNow();

  check('409 path: fetch was called',             fetchCalls.length === 1);
  check('409 path: planConflict latch set',       we.planConflict === true);
  check('409 path: dirty stays true for retry',   we.dirty === true);
  check('409 path: toast shown',                  toastCalls.length === 1,
    `toastCalls=${JSON.stringify(toastCalls)}`);
  check('409 path: toast mentions reload',
    toastCalls.length > 0 && /reload/i.test(toastCalls[0].msg),
    `toast msg=${toastCalls.length ? toastCalls[0].msg : '(none)'}`);
  check('409 path: toast is err kind',
    toastCalls.length > 0 && toastCalls[0].kind === 'err');
  check('409 path: we-saved host shows a Reload action',
    elements['we-saved'].children.length === 1 &&
      elements['we-saved'].children[0].tagName === 'button' &&
      /reload/i.test(elements['we-saved'].children[0].textContent));
}

// ── Test 3: after 409, subsequent _savePlanNow short-circuits ────────────
async function testConflictLatchSuppressesSave() {
  // Re-run save with the latch still set — fetch must NOT be called
  // again because the conflict latch is the gate that breaks the
  // every-keystroke-collides loop.
  fetchMode  = 'ok';
  fetchCalls.length = 0;
  we.dirty   = true;          // user kept editing
  // we.planConflict is still true from the prior test
  await savePlanNow();
  check('latch: no fetch fires while planConflict latched',
    fetchCalls.length === 0);
}

// ── Run ──────────────────────────────────────────────────────────────────
(async () => {
  await testHappyPathSave();
  await testConflictPathSave();
  await testConflictLatchSuppressesSave();
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed === 0 ? 0 : 1);
})();
