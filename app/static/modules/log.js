// Log panel + toast helper. The log panel is collapsed by default —
// most users never need it. The `<details>` summary still surfaces the
// most recent line as a one-liner so the latest event is visible without
// expanding. Logging always writes to the panel; only its visibility is
// gated by the `<details>` open state.

const LOG_KEY = 'vr.log.expanded';

export function log(msg, cls = '') {
  const el = document.getElementById('log');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
  // trim old lines
  while (el.children.length > 40) el.removeChild(el.firstChild);
  // Mirror the latest line into the collapsed-state summary so the user can
  // see the most recent event without expanding.
  const tail = document.getElementById('log-tail');
  if (tail) {
    tail.textContent = msg;
    if (cls) tail.className = 'log-tail ' + cls;
    else tail.className = 'log-tail';
  }
}

// Persist the user's expanded state across reloads. Default = collapsed.
export function wireLogCollapse() {
  const det = document.getElementById('log-details');
  if (!det) return;
  let saved = null;
  try { saved = localStorage.getItem(LOG_KEY); } catch (_) {}
  // Default to collapsed; only restore "open" if explicitly remembered.
  det.open = (saved === '1');
  det.addEventListener('toggle', () => {
    try { localStorage.setItem(LOG_KEY, det.open ? '1' : '0'); } catch (_) {}
  });
}

// Used for action results (✓ tagged, ✕ delete failed). Also writes to the
// sidebar log so the history is preserved when a toast fades.
export function toast(msg, kind = 'info') {
  log(msg, kind);
  const c = document.getElementById('toast-container');
  if (!c) return;
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.textContent = msg;
  c.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.remove(), 250);
  }, 3500);
}

// Persistent toast with a single inline action button — used when the
// user needs to make a deliberate choice (e.g. "stream dropped, reconnect?").
// Unlike `toast()` it does NOT auto-dismiss and is NOT mirrored into the
// log panel (the underlying event has already been logged by the server).
//
// Pass a stable `id` so callers can target the same toast across re-fires
// (e.g. multiple crash events fold into a single prompt) and dismiss it
// programmatically via `dismissActionToast(id)`. Calling twice with the
// same id is idempotent — the first toast stays put.
//
// `onClick` is awaited; while it runs the button is disabled and shows a
// "…" affordance so a slow /api/connect doesn't invite double-clicks.
export function toastAction({ id, msg, kind = 'info', actionLabel, onClick }) {
  const c = document.getElementById('toast-container');
  if (!c) return null;
  if (id && c.querySelector(`[data-toast-id="${id}"]`)) return null;
  const t = document.createElement('div');
  t.className = `toast ${kind} toast-action`;
  if (id) t.dataset.toastId = id;
  const span = document.createElement('span');
  span.textContent = msg;
  t.appendChild(span);
  const btn = document.createElement('button');
  btn.className = 'btn-tiny';
  btn.type = 'button';
  btn.textContent = actionLabel;
  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '…';
    try { await onClick(); } finally {
      // Always remove the toast — even on failure the user can re-click
      // the connect button in the sidebar; leaving a stuck "…" is worse.
      btn.textContent = orig;
      _dismissToastEl(t);
    }
  });
  t.appendChild(btn);
  c.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  return t;
}

// Dismiss an action-toast by the id supplied to `toastAction`. No-op if
// the toast is gone (already clicked, already dismissed). Used to clear
// the reconnect prompt from sibling tabs once any tab reconnects.
export function dismissActionToast(id) {
  if (!id) return;
  const c = document.getElementById('toast-container');
  if (!c) return;
  const t = c.querySelector(`[data-toast-id="${id}"]`);
  if (t) _dismissToastEl(t);
}

function _dismissToastEl(t) {
  t.classList.remove('show');
  setTimeout(() => t.remove(), 250);
}

export function renderLog(msg, level) {
  log(msg, level || '');
}
