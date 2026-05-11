// fetch wrapper, error parsing, and job-progress helpers. Used by every
// module that talks to /api/*.

// Extract a friendly error message from a non-OK fetch response. FastAPI
// surfaces HTTPException(status, "msg") as {"detail": "msg"}; fall back to
// raw body / status code for anything else (e.g. proxy failures).
export async function parseError(resp) {
  let body = '';
  try { body = await resp.text(); } catch (e) {}
  if (body) {
    try {
      const j = JSON.parse(body);
      if (j && typeof j.detail === 'string') return j.detail;
    } catch (e) {}
    return body;
  }
  return 'HTTP ' + resp.status;
}

// ── Job progress bars ─────────────────────────────────────────────────────
// Long ffmpeg ops (combine / split / measure / detect-silences / waveform)
// publish progress under a client-supplied job_id via /api/jobs/<id>. The
// helpers below let a caller spin up a bar, post the request with the job_id
// in either the body or query string, poll while it's in flight, and tear
// the bar down on completion. Polling is done with a 250ms cadence — fast
// enough to feel live, slow enough not to hammer the server.

let _jobIdCounter = 0;
export function newJobId() {
  _jobIdCounter++;
  return 'j_' + Date.now().toString(36) + '_'
    + Math.random().toString(36).slice(2, 8) + '_' + _jobIdCounter;
}

function _setBar(barEl, progress, phase) {
  if (!barEl) return;
  const fill = barEl.querySelector('.job-bar-fill, .wpo-fill');
  const pct  = barEl.querySelector('.job-bar-pct');
  const ph   = barEl.querySelector('.job-bar-phase, .wpo-text');
  const w    = Math.max(0, Math.min(100, progress * 100));
  if (fill) fill.style.width = w.toFixed(1) + '%';
  if (pct)  pct.textContent  = Math.round(w) + '%';
  if (ph && phase) ph.textContent = phase;
}

export function showBar(barEl, label) {
  if (!barEl) return;
  _setBar(barEl, 0, label || 'working…');
  barEl.hidden = false;
}

export function hideBar(barEl) {
  if (!barEl) return;
  barEl.hidden = true;
  _setBar(barEl, 0, '');
}

// Run `fn(jobId)` and poll /api/jobs/<jobId> until either fn resolves or the
// server reports done. Updates `barEl` as progress comes in. Returns whatever
// fn returns. The bar is the caller's responsibility to show/hide — we only
// drive the fill width.
export async function withJobProgress(barEl, fn) {
  const jobId = newJobId();
  let stop = false;

  const poll = async () => {
    while (!stop) {
      try {
        const r = await fetch('/api/jobs/' + encodeURIComponent(jobId));
        if (r.ok) {
          const d = await r.json();
          _setBar(barEl, d.progress || 0, d.phase || '');
          if (d.done) break;
        }
        // 404 = job not started yet (server hasn't reached start_job); keep polling.
      } catch (e) { /* network blip — try again */ }
      await new Promise(res => setTimeout(res, 250));
    }
  };
  const pollPromise = poll();

  try {
    return await fn(jobId);
  } finally {
    stop = true;
    try { await pollPromise; } catch (e) {}
  }
}
