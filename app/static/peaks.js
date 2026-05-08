// Client-side waveform: parse audiowaveform .dat files and render onto a
// canvas. Replaces the per-zoom server-side `showwavespic` PNG render —
// after the initial fetch all zoom and pan is local, sub-millisecond
// canvas redraw with no network round-trip.
//
// One .dat per album (mono, `-b 8`, `-z 256`) keeps the payload small
// (~45 KB per minute of audio at 96 kHz).

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

async function loadAlbumPeaks(albumId) {
  const r = await fetch(`/api/album/${encodeURIComponent(albumId)}/peaks`);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return parsePeaks(await r.arrayBuffer());
}

// Draw the time range [viewStart, viewEnd] onto `canvas`. We compute one
// (min, max) pair per pixel column by walking the buckets that fall in
// the column's time slice, then render the envelope as a vertical line.
// At deepest zoom each bucket spans multiple pixels — we still draw one
// line per column (with the bucket's value) so the envelope renders
// crisply rather than alising.
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

  if (!peaks || !peaks.body || !peaks.length) {
    ctx.fillRect(0, Math.round(mid), W, 1);  // empty centerline placeholder
    return;
  }

  const sr = peaks.sampleRate;
  const spp = peaks.samplesPerPixel;
  const bucketSec = spp / sr;
  if (bucketSec <= 0) return;

  // Window of buckets that intersect the view.
  const i0 = Math.max(0, Math.floor(viewStart / bucketSec));
  const i1 = Math.min(peaks.length, Math.ceil(viewEnd / bucketSec));
  const body = peaks.body;
  // For multi-channel dats every bucket holds (min,max) per channel
  // contiguously. Combine into a single envelope by taking the min across
  // all channels' min and max across all channels' max, so the rendered
  // wave shows the loudest extreme.
  const channels = Math.max(1, peaks.channels || 1);
  const bucketBytes = 2 * channels;

  // Iterate pixel columns and reduce every bucket whose time range
  // intersects the column into its (min, max). This handles both
  // bucket-per-pixel densities uniformly: when zoomed out a column spans
  // many buckets (envelope summary), when zoomed in many columns share
  // one bucket (the envelope reads as a continuous bar at full extent
  // rather than a comb of one-pixel spikes).
  for (let c = 0; c < W; c++) {
    const tStart = viewStart + (c / W) * len;
    const tEnd   = viewStart + ((c + 1) / W) * len;
    const b0 = Math.max(i0, Math.floor(tStart / bucketSec));
    let b1 = Math.min(i1, Math.ceil(tEnd / bucketSec));
    if (b1 <= b0) b1 = Math.min(i1, b0 + 1);  // ensure ≥1 bucket per column
    let minV = 127, maxV = -128;
    for (let i = b0; i < b1; i++) {
      const base = i * bucketBytes;
      for (let ch = 0; ch < channels; ch++) {
        const m1 = body[base + 2 * ch];
        const m2 = body[base + 2 * ch + 1];
        if (m1 < minV) minV = m1;
        if (m2 > maxV) maxV = m2;
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
  if (!peaks || !peaks.body) return null;
  const body = peaks.body;
  let peakInt = 0;
  for (let i = 0; i < body.length; i++) {
    const v = body[i];
    const a = v < 0 ? -v : v;
    if (a > peakInt) peakInt = a;
  }
  if (peakInt <= 0) return null;
  const amp = (peakInt * 256 + 127.5) / 32768;
  return 20 * Math.log10(Math.min(1, amp));
}

window.parsePeaks = parsePeaks;
window.loadAlbumPeaks = loadAlbumPeaks;
window.drawPeaks = drawPeaks;
window.approxPeakDbFromPeaks = approxPeakDbFromPeaks;
