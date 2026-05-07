// Client-side waveform: parse audiowaveform .dat files and render onto a
// canvas. Replaces the per-zoom server-side `showwavespic` PNG render —
// after the initial fetch all zoom and pan is local, sub-millisecond
// canvas redraw with no network round-trip.
//
// One .dat per album (mono, `-b 8`, `-z 256`) keeps the payload small
// (~45 KB per minute of audio at 96 kHz).

'use strict';

// Parse the BBC audiowaveform binary v2 format (8-bit min/max).
//   header (24 bytes, little-endian):
//     i32 version, u32 flags, i32 sample_rate, i32 samples_per_pixel,
//     u32 length, i32 channels.
//   body: 2 * length signed bytes (interleaved min, max), per channel.
//
// We always pre-mix to mono on the server side via `ffmpeg -ac 1 |
// audiowaveform` so length * 2 bytes is the expected payload length.
function parsePeaks(arrayBuffer) {
  const view = new DataView(arrayBuffer);
  if (view.byteLength < 24) throw new Error('peak file too short');
  const version          = view.getInt32(0,  true);
  const flags            = view.getUint32(4, true);
  const sampleRate       = view.getInt32(8,  true);
  const samplesPerPixel  = view.getInt32(12, true);
  const length           = view.getUint32(16, true);
  const channels         = view.getInt32(20, true);
  const bits = (flags & 0x1) ? 8 : 16;
  if (bits !== 8) throw new Error('expected 8-bit peak data; got ' + bits);
  const expected = length * 2 * (channels || 1);
  const body = new Int8Array(arrayBuffer, 24,
    Math.min(expected, arrayBuffer.byteLength - 24));
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

  // Per-column min/max accumulators, in int8 space.
  const cols = new Int16Array(W * 2);
  for (let i = 0; i < W * 2; i += 2) { cols[i] = 127; cols[i + 1] = -128; }
  for (let i = i0; i < i1; i++) {
    const t = i * bucketSec;
    const col = Math.floor(((t - viewStart) / len) * W);
    if (col < 0 || col >= W) continue;
    const minV = body[2 * i];
    const maxV = body[2 * i + 1];
    const j = col * 2;
    if (minV < cols[j])     cols[j]     = minV;
    if (maxV > cols[j + 1]) cols[j + 1] = maxV;
  }
  for (let c = 0; c < W; c++) {
    const j = c * 2;
    const minV = cols[j];
    const maxV = cols[j + 1];
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
