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

export function renderLog(msg, level) {
  log(msg, level || '');
}
