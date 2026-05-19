// Node-runnable unit tests for `weAudio` — the multi-side playback
// wrapper extracted to `app/static/modules/audio-manager.js`.
//
// `weAudio` pokes at a real `<audio>` element in production. The sandbox
// here installs a `FakeAudio` (just enough surface area to satisfy every
// method weAudio calls): src/currentTime/duration setters/getters, play
// + pause that resolve immediately, an event-listener registry that
// supports loadedmetadata, and a manual `_fireEnded()` to drive the
// end-of-side advancement path.
//
// Failures throw, which gives a non-zero exit so the pytest runner
// surfaces them.
'use strict';
const fs   = require('fs');
const path = require('path');
const vm   = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'modules', 'audio-manager.js'),
  'utf8',
);

let fakeAudio;
function makeFakeAudio() {
  return {
    src:          '',
    currentTime:  0,
    duration:     0,
    paused:       true,
    _listeners:   {},      // {eventName: [fn, ...]}
    _onended:     null,
    _ontimeupdate: null,
    set onended(fn)        { this._onended = fn; },
    get onended()          { return this._onended; },
    set ontimeupdate(fn)   { this._ontimeupdate = fn; },
    get ontimeupdate()     { return this._ontimeupdate; },
    load()                 { /* no-op */ },
    play() {
      this.paused = false;
      return Promise.resolve();
    },
    pause() {
      this.paused = true;
    },
    addEventListener(event, fn) {
      (this._listeners[event] = this._listeners[event] || []).push(fn);
    },
    removeEventListener(event, fn) {
      const arr = this._listeners[event] || [];
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    },
    // Test helpers ↓ — not part of the real HTMLAudioElement API
    _fireLoadedMetadata() {
      for (const fn of (this._listeners.loadedmetadata || []).slice()) fn();
    },
    _fireEnded() {
      if (this._onended) this._onended();
    },
  };
}

const win = {};
const sandbox = {
  window:   win,
  document: {
    getElementById: (id) => (id === 'we-audio') ? fakeAudio : null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  console:      console,
  setTimeout:   setTimeout,
  clearTimeout: clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);

const weAudio = win.weAudio;
if (!weAudio || typeof weAudio.init !== 'function') {
  throw new Error('weAudio not exposed on window');
}

let passed = 0, failed = 0;
function approx(a, b, tol = 0.001) { return Math.abs(a - b) <= tol; }
function test(name, fn) {
  try {
    fakeAudio = makeFakeAudio();
    fn();
    passed++;
    console.log(`✓ ${name}`);
  } catch (e) {
    failed++;
    console.log(`✗ ${name}\n      ${e.stack || e.message}`);
  }
}

// Two-side album: A=10s, B=15s, total=25s.
function initTwoSide() {
  weAudio.init('album-1', [
    { filename: 'A.flac', duration_seconds: 10 },
    { filename: 'B.flac', duration_seconds: 15 },
  ]);
}

test('init populates sides with cumulative offsets and loads side 0', () => {
  initTwoSide();
  if (weAudio.albumId !== 'album-1') throw new Error('albumId not stashed');
  if (weAudio.sides.length !== 2) throw new Error('sides.length: ' + weAudio.sides.length);
  if (weAudio.sides[0].offset !== 0) throw new Error('side 0 offset: ' + weAudio.sides[0].offset);
  if (weAudio.sides[1].offset !== 10) throw new Error('side 1 offset: ' + weAudio.sides[1].offset);
  if (!fakeAudio.src.includes('sides/0/audio')) {
    throw new Error('expected side 0 to be loaded, src=' + fakeAudio.src);
  }
  if (weAudio.currentSideIdx !== 0) throw new Error('currentSideIdx: ' + weAudio.currentSideIdx);
});

test('totalDuration sums every side', () => {
  initTwoSide();
  if (weAudio.totalDuration() !== 25) throw new Error('total: ' + weAudio.totalDuration());
});

test('totalDuration is 0 before init', () => {
  // Fresh fake — init() was not called for this test (test() runs the body
  // after replacing fakeAudio, but weAudio carries state across tests; do
  // an explicit release first to mimic editor close).
  weAudio.release();
  if (weAudio.totalDuration() !== 0) throw new Error('total: ' + weAudio.totalDuration());
});

test('seek within current side just sets currentTime on the audio element', () => {
  initTwoSide();
  weAudio.seek(5);
  if (!approx(fakeAudio.currentTime, 5)) throw new Error('currentTime: ' + fakeAudio.currentTime);
  if (weAudio.currentSideIdx !== 0) throw new Error('side switched: ' + weAudio.currentSideIdx);
});

test('seek across the side boundary swaps src and applies currentTime on loadedmetadata', () => {
  initTwoSide();
  // Pretend playback was running so we can verify the apply() also resumes it.
  fakeAudio.paused = false;
  weAudio.seek(12);
  // Side switch happens immediately; currentTime gets applied only after
  // loadedmetadata fires (mirrors the real <audio> behaviour where setting
  // currentTime before metadata is loaded throws InvalidStateError).
  if (weAudio.currentSideIdx !== 1) throw new Error('expected side 1, got ' + weAudio.currentSideIdx);
  if (!fakeAudio.src.includes('sides/1/audio')) {
    throw new Error('src not swapped: ' + fakeAudio.src);
  }
  if (fakeAudio.currentTime !== 0) {
    throw new Error('currentTime should still be 0 before loadedmetadata, got ' + fakeAudio.currentTime);
  }
  fakeAudio._fireLoadedMetadata();
  // 12 album-time - 10 (side 1 offset) = 2 local-time
  if (!approx(fakeAudio.currentTime, 2)) {
    throw new Error('local currentTime: ' + fakeAudio.currentTime);
  }
  if (fakeAudio.paused) throw new Error('resume after side swap dropped the play state');
});

test('seek clamps album-time into [0, totalDuration]', () => {
  initTwoSide();
  weAudio.seek(-5);
  if (!approx(fakeAudio.currentTime, 0)) throw new Error('low clamp: ' + fakeAudio.currentTime);
  weAudio.seek(999);
  fakeAudio._fireLoadedMetadata();
  // 25 album-time - 10 (side 1 offset) = 15 local
  if (!approx(fakeAudio.currentTime, 15)) {
    throw new Error('high clamp: ' + fakeAudio.currentTime);
  }
});

test('_onSideEnded advances to the next side and resumes playback', () => {
  initTwoSide();
  weAudio.play();
  fakeAudio._fireEnded();
  if (weAudio.currentSideIdx !== 1) {
    throw new Error('expected to advance to side 1, got ' + weAudio.currentSideIdx);
  }
  if (!fakeAudio.src.includes('sides/1/audio')) {
    throw new Error('src not advanced: ' + fakeAudio.src);
  }
  if (fakeAudio.paused) {
    throw new Error('next side should auto-play, but audio is paused');
  }
});

test('_onSideEnded on the final side calls onEnded callback', () => {
  initTwoSide();
  // Advance to side 1 first (the last side).
  weAudio.seek(12);
  fakeAudio._fireLoadedMetadata();
  let onEndedCalls = 0;
  weAudio.onEnded = () => { onEndedCalls += 1; };
  fakeAudio._fireEnded();
  if (onEndedCalls !== 1) {
    throw new Error('expected onEnded once, got ' + onEndedCalls);
  }
  // currentSideIdx must stay on the last side — no further advancement.
  if (weAudio.currentSideIdx !== 1) {
    throw new Error('side should stay at 1, got ' + weAudio.currentSideIdx);
  }
});

test('_onSideEnded with no onEnded callback does not throw', () => {
  initTwoSide();
  weAudio.seek(12);
  fakeAudio._fireLoadedMetadata();
  weAudio.onEnded = null;
  // Should be a no-op, not crash.
  fakeAudio._fireEnded();
});

test('currentTime getter translates side-local audio time back to album time', () => {
  initTwoSide();
  // While still on side 0:
  fakeAudio.currentTime = 4;
  if (!approx(weAudio.currentTime, 4)) {
    throw new Error('side-0 album time: ' + weAudio.currentTime);
  }
  // After advancing to side 1:
  weAudio.seek(12);
  fakeAudio._fireLoadedMetadata();
  fakeAudio.currentTime = 3;  // local time within side 1
  // Album time = 10 (side 1 offset) + 3 = 13
  if (!approx(weAudio.currentTime, 13)) {
    throw new Error('side-1 album time: ' + weAudio.currentTime);
  }
});

test('currentTime is 0 before init', () => {
  weAudio.release();
  if (weAudio.currentTime !== 0) throw new Error('pre-init currentTime: ' + weAudio.currentTime);
});

test('release clears state and stops the audio element', () => {
  initTwoSide();
  weAudio.play();
  weAudio.release();
  if (weAudio.albumId !== null) throw new Error('albumId not cleared: ' + weAudio.albumId);
  if (weAudio.sides.length !== 0) throw new Error('sides not cleared');
  if (weAudio.currentSideIdx !== 0) throw new Error('currentSideIdx not reset');
  if (fakeAudio.src !== '') throw new Error('src not blanked: ' + fakeAudio.src);
  if (!fakeAudio.paused) throw new Error('audio not paused after release');
});

test('hasSrc is true after init, false after release', () => {
  initTwoSide();
  if (!weAudio.hasSrc) throw new Error('expected hasSrc true after init');
  weAudio.release();
  if (weAudio.hasSrc) throw new Error('expected hasSrc false after release');
});

test('init wires onTimeUpdate callback to <audio>.ontimeupdate', () => {
  let tickCount = 0;
  weAudio.onTimeUpdate = () => { tickCount += 1; };
  initTwoSide();
  // weAudio.init wires audio.ontimeupdate to forward to weAudio.onTimeUpdate.
  if (typeof fakeAudio.ontimeupdate !== 'function') {
    throw new Error('audio.ontimeupdate not wired');
  }
  fakeAudio.ontimeupdate();
  if (tickCount !== 1) throw new Error('onTimeUpdate not invoked: ' + tickCount);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
