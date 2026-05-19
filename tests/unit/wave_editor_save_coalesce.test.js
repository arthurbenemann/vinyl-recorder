// Node-runnable unit tests for `_savePlanNow`'s concurrency / coalesce
// behaviour. The debounce in _persistDraft already collapses rapid edits
// into one POST, but a slow network can leave a fetch in flight when the
// next debounce fires. Two concurrent POSTs to /api/album/.../plan race
// on the server (write_manifest has no file lock), so the editor must
// only have one in flight at a time.
//
// Same VM-sandbox approach as wave_editor_remap.test.js: load
// wave-editor.js in a context that stubs the browser globals, then poke
// the editor's `we` state and call the (otherwise module-private)
// _savePlanNow / _persistDraft through window-exposed helpers we append
// to the source.
'use strict';
const fs   = require('fs');
const path = require('path');
const vm   = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'wave-editor.js'),
  'utf8',
);

// Controllable fetch: every call returns a promise we resolve manually.
// Tests can inspect `fetchCalls` to see when a POST went out and what
// payload it carried, and call `resolveNext()` to release the in-flight
// promise like the network would.
const fetchCalls = [];
let pendingResolvers = [];
function makeFetch() {
  return function fakeFetch(url, opts) {
    const call = { url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null };
    fetchCalls.push(call);
    return new Promise((resolve, reject) => {
      pendingResolvers.push({ resolve, reject, call });
    });
  };
}
function resolveNext(ok = true) {
  const next = pendingResolvers.shift();
  if (!next) throw new Error('resolveNext: no pending fetch');
  next.resolve({ ok });
  return next.call;
}

const sandbox = {
  window:           {},
  document:         {
    getElementById:    (id) => ({ value: '', hidden: false, textContent: '' }),
    querySelectorAll:  () => [],
    addEventListener:  () => {},
  },
  fetch:            makeFetch(),
  console:          console,
  htmlEscape:       (s) => String(s == null ? '' : s),
  setTimeout:       setTimeout,
  clearTimeout:     clearTimeout,
  setInterval:      setInterval,
  clearInterval:    clearInterval,
  Date:             Date,
};
vm.createContext(sandbox);

// Append closure accessors so the test can reach private state.
// _savePlanNow / _persistDraft / _flushPlanSave are module-private; `we`
// is too. The trailing block exposes them on `window` for the test to
// drive directly.
const HARNESS = `
if (typeof window !== 'undefined') {
  window.__we              = we;
  window.__savePlanNow     = _savePlanNow;
  window.__persistDraft    = _persistDraft;
  window.__flushPlanSave   = _flushPlanSave;
  window.__getTimer        = () => _planSaveTimer;
  window.__getInFlight     = () => _planSaveInFlight;
}
`;
vm.runInContext(SRC + HARNESS, sandbox);

const we                = sandbox.window.__we;
const savePlanNow       = sandbox.window.__savePlanNow;
const persistDraft      = sandbox.window.__persistDraft;
const flushPlanSave     = sandbox.window.__flushPlanSave;
const getTimer          = sandbox.window.__getTimer;
const getInFlight       = sandbox.window.__getInFlight;

if (typeof savePlanNow !== 'function') {
  throw new Error('_savePlanNow not exposed on window');
}

let passed = 0, failed = 0;
function check(name, fn) {
  return fn().then(() => {
    console.log(`  ✓ ${name}`);
    passed += 1;
  }).catch((e) => {
    console.log(`  ✗ ${name}\n      ${e.message || e}`);
    failed += 1;
  });
}

function reset() {
  fetchCalls.length = 0;
  pendingResolvers.length = 0;
  we.albumId  = 'album-x';
  we.total    = 30;
  we.cuts     = [];
  we.titles   = ['Track 1'];
  we.skipped  = [false];
  we.positions = [''];
  we.loaded   = true;
  we.dirty    = true;
}

// Tick the event loop a few times so chained then() callbacks settle.
function flush() {
  return new Promise(r => setImmediate(r))
    .then(() => new Promise(r => setImmediate(r)))
    .then(() => new Promise(r => setImmediate(r)));
}

(async () => {
  await check('single _savePlanNow posts once and clears dirty on success', async () => {
    reset();
    const p = savePlanNow();
    if (fetchCalls.length !== 1) throw new Error(`expected 1 fetch, got ${fetchCalls.length}`);
    if (getInFlight() == null) throw new Error('in-flight should be set during fetch');
    resolveNext(true);
    await p;
    if (we.dirty !== false)   throw new Error('dirty should clear after successful save');
    if (getInFlight() != null) throw new Error('in-flight should be cleared after resolve');
  });

  await check('two rapid _savePlanNow calls coalesce — only one fetch in flight', async () => {
    reset();
    // First call: posts immediately.
    const p1 = savePlanNow();
    if (fetchCalls.length !== 1) throw new Error(`expected 1 fetch, got ${fetchCalls.length}`);
    // Second call before the first resolves: must NOT issue a second fetch.
    // It should observe _planSaveInFlight and reschedule via setTimeout.
    we.cuts   = [10];
    we.titles = ['T1', 'T2'];
    we.skipped = [false, false];
    we.dirty  = true;
    const p2 = savePlanNow();
    await p2;  // synchronous early-return; resolves immediately
    if (fetchCalls.length !== 1) {
      throw new Error(`second call should not have started a fetch, got ${fetchCalls.length}`);
    }
    if (getTimer() == null) throw new Error('second call should have scheduled a retry timer');
    // Resolve the first fetch.
    resolveNext(true);
    await p1;
    if (getInFlight() != null) throw new Error('in-flight should clear after first resolves');
  });

  await check('rescheduled save fires after the in-flight one resolves, picking up latest state', async () => {
    reset();
    we.cuts    = [5];
    we.titles  = ['First', 'Second'];
    we.skipped = [false, false];
    we.dirty   = true;
    const p1 = savePlanNow();
    if (fetchCalls.length !== 1) throw new Error('first fetch missing');
    if (!fetchCalls[0].body.tracks || fetchCalls[0].body.tracks[0].title !== 'First') {
      throw new Error('first body should reflect initial state');
    }
    // While the first POST is in flight, the user makes another edit.
    we.cuts    = [5, 15];
    we.titles  = ['First', 'Second', 'Third'];
    we.skipped = [false, false, false];
    we.dirty   = true;
    await savePlanNow();   // returns immediately, schedules retry
    if (fetchCalls.length !== 1) throw new Error('second fetch leaked while first was in flight');
    // Resolve first POST. _planSaveInFlight clears. The reschedule will
    // fire after ~200ms; wait it out.
    resolveNext(true);
    await flush();
    await new Promise(r => setTimeout(r, 260));
    await flush();
    if (fetchCalls.length !== 2) {
      throw new Error(`expected reschedule to fire a second fetch, got ${fetchCalls.length}`);
    }
    const body2 = fetchCalls[1].body;
    if (!body2.tracks || body2.tracks.length !== 3) {
      throw new Error(`second body should carry 3 tracks, got ${body2.tracks && body2.tracks.length}`);
    }
    if (body2.tracks[2].title !== 'Third') {
      throw new Error(`second body should include the Third track: ${JSON.stringify(body2.tracks)}`);
    }
    resolveNext(true);
    await flush();
    if (we.dirty !== false) throw new Error('dirty should clear after second save');
  });

  await check('_persistDraft fired twice in quick succession still only posts once at a time', async () => {
    reset();
    // First debounce burst.
    persistDraft();
    persistDraft();
    persistDraft();
    // Only one timer should be pending — _persistDraft clears prior ones.
    if (getTimer() == null) throw new Error('persistDraft should arm a debounce timer');
    // Wait out the 500ms debounce.
    await new Promise(r => setTimeout(r, 540));
    if (fetchCalls.length !== 1) throw new Error(`expected 1 fetch after debounce, got ${fetchCalls.length}`);
    // Edit again while the fetch is in flight — should NOT add a second fetch.
    we.dirty = true;
    persistDraft();
    await new Promise(r => setTimeout(r, 540));
    if (fetchCalls.length !== 1) {
      throw new Error(`second persistDraft cycle leaked a fetch while first was in flight (got ${fetchCalls.length})`);
    }
    // Resolve the in-flight, then the reschedule should kick in.
    resolveNext(true);
    await flush();
    await new Promise(r => setTimeout(r, 260));
    await flush();
    if (fetchCalls.length !== 2) {
      throw new Error(`expected reschedule to fire after resolve (got ${fetchCalls.length})`);
    }
    resolveNext(true);
    await flush();
  });

  await check('_flushPlanSave awaits in-flight save and then posts the latest state', async () => {
    reset();
    // Start an in-flight save with state A.
    we.cuts   = [];
    we.titles = ['A'];
    we.skipped = [false];
    const p1 = savePlanNow();
    if (fetchCalls.length !== 1) throw new Error('first fetch missing');
    // User edits to state B, then closes the modal → _flushPlanSave.
    // Simulate a pending debounce timer that flush should clear.
    we.cuts   = [10];
    we.titles = ['A', 'B'];
    we.skipped = [false, false];
    we.dirty  = true;
    persistDraft();   // arms the 500 ms debounce timer
    const flushP = flushPlanSave();
    // The flush should clear the timer immediately, then wait for the
    // in-flight to resolve, then post the latest state.
    // First we resolve the in-flight.
    resolveNext(true);
    await flush();
    // Now flush() should issue a second POST for state B.
    if (fetchCalls.length !== 2) {
      throw new Error(`flush should issue a second POST after in-flight resolves (got ${fetchCalls.length})`);
    }
    const body2 = fetchCalls[1].body;
    if (body2.tracks.length !== 2 || body2.tracks[1].title !== 'B') {
      throw new Error(`flush body should reflect state B: ${JSON.stringify(body2.tracks)}`);
    }
    resolveNext(true);
    await flushP;
    await p1;
  });

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error('test harness crash:', e);
  process.exit(2);
});
