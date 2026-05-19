// Multi-side audio playback wrapper.
//
// The editor presents the album as a single continuous timeline, but
// physically each side is its own FLAC. weAudio wraps the `<audio>` element
// so the rest of the editor can keep using album-time without caring about
// boundaries: seek() resolves album-time to the right side and swaps src
// when needed, currentTime returns album-time, and end-of-side advances
// to the next side automatically. The brief click at src swap lands inside
// the auto-detected silence bands at side flips, so it's invisible.
//
// Loaded as a classic script BEFORE wave-editor.js so the `weAudio`
// binding is in script-scope when the editor calls it. Also exposed on
// `window.weAudio` for completeness and unit-test ergonomics.

'use strict';

const weAudio = {
  albumId:        null,
  sides:          [],     // [{filename, duration_seconds, offset}, ...]
  currentSideIdx: 0,
  // Optional callback the editor wires up so onended can drive its
  // playingTrack / playingEnd state machine.
  onEnded:        null,
  onTimeUpdate:   null,

  _el() { return document.getElementById('we-audio'); },

  init(albumId, manifestSides) {
    // Manifest sides arrive without an `offset`; build the cumulative
    // album-time lookup once so seek() is O(log n) per call.
    let off = 0;
    this.albumId = albumId;
    this.sides = manifestSides.map(s => {
      const entry = {
        filename:         s.filename,
        duration_seconds: Number(s.duration_seconds) || 0,
        offset:           off,
      };
      off += entry.duration_seconds;
      return entry;
    });
    this.currentSideIdx = 0;
    const audio = this._el();
    if (!audio) return;
    audio.ontimeupdate = () => { if (this.onTimeUpdate) this.onTimeUpdate(); };
    audio.onended = () => this._onSideEnded();
    if (this.sides.length) {
      audio.src = this._sideUrl(0);
      audio.load();
    }
  },

  _sideUrl(idx) {
    return `/api/album/${encodeURIComponent(this.albumId)}/sides/${idx}/audio`;
  },

  _findSide(albumTime) {
    // Linear scan; albums have ≤ ~6 sides in practice and seek isn't hot.
    for (let i = 0; i < this.sides.length; i++) {
      const s = this.sides[i];
      if (albumTime < s.offset + s.duration_seconds) return i;
    }
    return Math.max(0, this.sides.length - 1);
  },

  _onSideEnded() {
    const audio = this._el();
    if (!audio) return;
    if (this.currentSideIdx < this.sides.length - 1) {
      // Advance to next side and continue playing without surfacing the
      // boundary to the editor's end-of-playback handler.
      this.currentSideIdx += 1;
      audio.src = this._sideUrl(this.currentSideIdx);
      audio.load();
      audio.play().catch(() => {});
    } else if (this.onEnded) {
      this.onEnded();
    }
  },

  // Album-time seek. Swaps `src` if the target falls outside the current
  // side; sets currentTime to the local position within that side.
  seek(albumTime) {
    const audio = this._el();
    if (!audio || !this.sides.length) return;
    const t = Math.max(0, Math.min(this.totalDuration(), albumTime));
    const idx = this._findSide(t);
    const local = Math.max(0, t - this.sides[idx].offset);
    if (idx !== this.currentSideIdx) {
      const wasPlaying = !audio.paused;
      this.currentSideIdx = idx;
      audio.src = this._sideUrl(idx);
      audio.load();
      // Apply currentTime once the new side reports a duration; until then
      // setting currentTime is a no-op or throws InvalidStateError.
      const apply = () => {
        try { audio.currentTime = local; } catch (e) {}
        if (wasPlaying) audio.play().catch(() => {});
        audio.removeEventListener('loadedmetadata', apply);
      };
      audio.addEventListener('loadedmetadata', apply);
    } else {
      try { audio.currentTime = local; } catch (e) {}
    }
  },

  get currentTime() {
    const audio = this._el();
    if (!audio || !this.sides.length) return 0;
    const side = this.sides[this.currentSideIdx];
    return (side ? side.offset : 0) + (audio.currentTime || 0);
  },

  get paused() {
    const audio = this._el();
    return !audio || audio.paused;
  },

  get hasSrc() { return this.sides.length > 0; },

  totalDuration() {
    if (!this.sides.length) return 0;
    const last = this.sides[this.sides.length - 1];
    return last.offset + last.duration_seconds;
  },

  play()   { const a = this._el(); if (a && a.src) a.play().catch(() => {}); },
  pause()  { const a = this._el(); if (a) { try { a.pause(); } catch (e) {} } },

  release() {
    const audio = this._el();
    if (audio) {
      try { audio.pause(); audio.src = ''; } catch (e) {}
    }
    this.albumId        = null;
    this.sides          = [];
    this.currentSideIdx = 0;
  },
};

if (typeof window !== 'undefined') window.weAudio = weAudio;
