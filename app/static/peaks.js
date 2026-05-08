// Client-side waveform: parse audiowaveform .dat files and render onto a
// canvas. Replaces the per-zoom server-side `showwavespic` PNG render —
// after the initial fetch all zoom and pan is local, sub-millisecond
// canvas redraw with no network round-trip.
//
// One .dat per side (mono, `-b 8`, `-z 256`) — the editor fetches them
// in parallel and `drawPeaks` stitches them at draw time. Per-side keeps
// the payload small (~45 KB per minute of audio at 96 kHz) and avoids a
// multi-second concat-then-render round trip on first open.

'use strict';

// Parse a BBC audiowaveform binary dat (8-bit min/max). The format has two
// header layouts (see WaveformBuffer.cpp `version = channels==1 ? 1 : 2`):
//
//   v1 (mono, 20-byte header, body starts at offset 20):
//     i32 version=1, u32 flags, i32 sample_rate, i32 samples_per_pixel,
//     u32 length
//
//   v2 (multi-channel, 24-byte header, body starts at offset 24):
//     i32 version=2, u32 flags, i32 sample_rate, i32 samples_per_pixel,
//     u32 length, i32 channels
//
// audiowaveform downmixes to mono by default, so the common case is v1.
// Misreading a v1 file as v2 produces a bogus `channels` (the first 4
// body bytes interpreted as int32) — that overflowed the Int8Array
// length parameter and surfaced as "Invalid typed array length".
function parsePeaks(arrayBuffer) {
  const view = new DataView(arrayBuffer);
  if (view.byteLength < 20) throw new Error('peak file too short');
  const version          = view.getInt32(0,  true);
  const flags            = view.getUint32(4, true);
  const sampleRate       = view.getInt32(8,  true);
  const samplesPerPixel  = view.getInt32(12, true);
  const length           = view.getUint32(16, true);
  let channels, headerSize;
  if (version === 1) {
    channels = 1;
    headerSize = 20;
  } else if (version === 2) {
    if (view.byteLength < 24) throw new Error('short v2 audiowaveform header');
    channels = view.getInt32(20, true);
    headerSize = 24;
  } else {
    throw new Error('unsupported audiowaveform dat version: ' + version);
  }
  const bits = (flags & 0x1) ? 8 : 16;
  if (bits !== 8) throw new Error('expected 8-bit peak data; got ' + bits);
  if (channels < 1 || channels > 8) {
    throw new Error('unexpected channel count: ' + channels);
  }
  const expectedBody = length * 2 * channels;
  const availableBody = arrayBuffer.byteLength - headerSize;
  // Trust the smaller of the two so a truncated payload doesn't blow up
  // the Int8Array constructor; clamp to >=0 so it can never go negative.
  const bodyBytes = Math.max(0, Math.min(expectedBody, availableBody));
  const body = new Int8Array(arrayBuffer, headerSize, bodyBytes);
  const duration = sampleRate > 0
    ? (length * samplesPerPixel) / sampleRate
    : 0;
  return { version, flags, sampleRate, samplesPerPixel, length, channels,
           bits, body, duration };
}

async function _loadOneSidePeaks(albumId, sideIdx) {
  const r = await fetch(
    `/api/album/${encodeURIComponent(albumId)}/peaks/${sideIdx}`);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return parsePeaks(await r.arrayBuffer());
}

// Fetch one .peaks.dat per side in parallel. Returns
//   { sides: [{peaks, offset, duration}, ...], total }
// where `offset` is the cumulative album-time start of each side. The
// caller (wave-editor) feeds this into drawPeaks() which resolves
// columns -> (sideIdx, sideTime) at draw time. All sides come from the
// same upstream session, so samplesPerPixel + sampleRate are identical
// across them; we validate that on load and throw a clear error if not.
async function loadAlbumPeaks(albumId, sides) {
  if (!Array.isArray(sides) || !sides.length) {
    throw new Error('loadAlbumPeaks needs a non-empty sides[] array');
  }
  const peaksPerSide = await Promise.all(
    sides.map((_, i) => _loadOneSidePeaks(albumId, i)));

  // The render math assumes uniform bucket size and sample rate. Bail
  // explicitly if the upstream changed mid-album — the alternative is
  // silently misaligned playhead/cuts.
  const ref = peaksPerSide[0];
  for (let i = 1; i < peaksPerSide.length; i++) {
    const p = peaksPerSide[i];
    if (p.samplesPerPixel !== ref.samplesPerPixel
        || p.sampleRate     !== ref.sampleRate
        || p.channels       !== ref.channels) {
      throw new Error(
        `side ${i} peaks header mismatch: `
        + `spp=${p.samplesPerPixel}/${ref.samplesPerPixel}, `
        + `sr=${p.sampleRate}/${ref.sampleRate}, `
        + `ch=${p.channels}/${ref.channels}`);
    }
  }

  const out = [];
  let offset = 0;
  for (let i = 0; i < peaksPerSide.length; i++) {
    const p = peaksPerSide[i];
    // Prefer the manifest's authoritative duration when available — the
    // dat's own duration is bucket-quantised (off by up to one bucket
    // ≈ 2.7 ms at 96 kHz). Falls back to the dat-derived value if the
    // manifest doesn't have it (older clients of /api/albums).
    const dur = (sides[i] && typeof sides[i].duration_seconds === 'number')
      ? sides[i].duration_seconds
      : p.duration;
    out.push({ peaks: p, offset, duration: dur });
    offset += dur;
  }
  return { sides: out, total: offset };
}

// Draw the time range [viewStart, viewEnd] onto `canvas`. We compute one
// (min, max) pair per pixel column by walking the buckets that fall in
// the column's time slice, then render the envelope as a vertical line.
//
// Accepts either a single parsed dat (legacy single-side form) or the
// multi-side struct produced by loadAlbumPeaks (`{sides, total}`). The
// multi-side branch resolves each column to a (sideIdx, sideTime) before
// reducing buckets, so the envelope reads as continuous album-time even
// though each side has its own dat.
function drawPeaks(canvas, peaks, viewStart, viewEnd, color) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.width;
  const cssH = canvas.clientHeight || canvas.height;
  const W = Math.max(1, Math.round(cssW * dpr));
  const H = Math.max(1, Math.round(cssH * dpr));
  if (canvas.width !== W) canvas.width = W;
  if (canvas.height !== H) canvas.height = H;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = color || '#6db3ff';
  const len = Math.max(1e-6, viewEnd - viewStart);
  const mid = H / 2;

  // Normalise to the multi-side shape so the loop has one path. A bare
  // parsed dat becomes a single side at offset 0.
  let sides;
  if (peaks && Array.isArray(peaks.sides)) {
    sides = peaks.sides;
  } else if (peaks && peaks.body) {
    sides = [{ peaks: peaks, offset: 0, duration: peaks.duration || 0 }];
  } else {
    ctx.fillRect(0, Math.round(mid), W, 1);  // empty centerline placeholder
    return;
  }
  if (!sides.length) {
    ctx.fillRect(0, Math.round(mid), W, 1);
    return;
  }

  const ref = sides[0].peaks;
  const sr = ref.sampleRate;
  const spp = ref.samplesPerPixel;
  const bucketSec = spp / sr;
  if (bucketSec <= 0) return;
  const channels = Math.max(1, ref.channels || 1);
  const bucketBytes = 2 * channels;

  // Pre-compute, for each pixel column, the side whose time range
  // covers that column's start. Sides are in order with monotonic
  // `offset`; a linear scan in lockstep with the column loop is cheap.
  for (let c = 0; c < W; c++) {
    const tStart = viewStart + (c / W) * len;
    const tEnd   = viewStart + ((c + 1) / W) * len;

    let minV = 127, maxV = -128;
    // Walk every side that overlaps [tStart, tEnd]. In practice all but
    // one side contribute zero buckets, but a column that straddles a
    // side boundary will visit two — both are reduced into the same
    // (min, max) so the envelope reads as continuous.
    for (let s = 0; s < sides.length; s++) {
      const side = sides[s];
      const sStart = side.offset;
      const sEnd   = side.offset + side.duration;
      if (sEnd <= tStart) continue;
      if (sStart >= tEnd) break;
      const local0 = Math.max(0, tStart - sStart);
      const local1 = Math.max(local0, tEnd - sStart);
      const body = side.peaks.body;
      const length = side.peaks.length;
      const b0 = Math.max(0, Math.min(length, Math.floor(local0 / bucketSec)));
      let b1 = Math.max(b0, Math.min(length, Math.ceil(local1 / bucketSec)));
      if (b1 <= b0) b1 = Math.min(length, b0 + 1);  // ensure ≥1 bucket per column
      for (let i = b0; i < b1; i++) {
        const base = i * bucketBytes;
        for (let ch = 0; ch < channels; ch++) {
          const m1 = body[base + 2 * ch];
          const m2 = body[base + 2 * ch + 1];
          if (m1 < minV) minV = m1;
          if (m2 > maxV) maxV = m2;
        }
      }
    }
    if (minV > maxV) continue;
    const yMin = mid - (maxV / 127) * (H / 2);
    const yMax = mid - (minV / 127) * (H / 2);
    const h = Math.max(1, yMax - yMin);
    ctx.fillRect(c, yMin, 1, h);
  }
  // Centerline so all-quiet sections still register visually.
  ctx.fillRect(0, Math.round(mid), W, 1);
}

// Approximate album peak in dBFS, computed from the loaded .dat. Mid-bin
// reconstruction: int8 v -> amplitude (v*256 + 127.5)/32768. Accurate to
// ±0.07 dB at vinyl-typical peaks (within 6 dB of full scale). Shown as
// "~ -X.X dB" in the editor until astats has run, at which point the
// exact value replaces it.
function approxPeakDbFromPeaks(peaks) {
  if (!peaks) return null;
  // Same dual-shape input as drawPeaks: either a parsed dat or a
  // multi-side struct from loadAlbumPeaks.
  let bodies;
  if (Array.isArray(peaks.sides)) {
    bodies = peaks.sides.map(s => s.peaks && s.peaks.body).filter(Boolean);
  } else if (peaks.body) {
    bodies = [peaks.body];
  } else {
    return null;
  }
  let peakInt = 0;
  for (const body of bodies) {
    for (let i = 0; i < body.length; i++) {
      const v = body[i];
      const a = v < 0 ? -v : v;
      if (a > peakInt) peakInt = a;
    }
  }
  if (peakInt <= 0) return null;
  const amp = (peakInt * 256 + 127.5) / 32768;
  return 20 * Math.log10(Math.min(1, amp));
}

window.parsePeaks = parsePeaks;
window.loadAlbumPeaks = loadAlbumPeaks;
window.drawPeaks = drawPeaks;
window.approxPeakDbFromPeaks = approxPeakDbFromPeaks;
