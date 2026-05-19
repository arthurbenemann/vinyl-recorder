// Pure formatting / string helpers used across modules. No DOM access,
// no fetches; safe to import from anywhere — with the single exception
// of `actionToast` at the bottom, which writes to #toast-container so
// every module has one toast affordance to import.

export function dbStr(v) {
  if (v <= 0.0005) return '−∞';
  const db = 20 * Math.log10(v);
  return (db >= 0 ? '+' : '') + db.toFixed(1);
}

export function fmt(s) {
  return [Math.floor(s/3600), Math.floor((s%3600)/60), s%60]
    .map(n => String(n).padStart(2,'0')).join(':');
}

// Library "Recorded" column. Time-of-day for today, "MMM D" for older
// same-year rows (keeps the column narrow), year-bearing for prior years.
// fmtDateFull is used for the cell tooltip so the full timestamp is always
// one hover away.
export function fmtDate(unix) {
  if (!unix) return '—';
  const d = new Date(unix * 1000);
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
  if (sameDay) {
    return d.toLocaleString(undefined, { hour: 'numeric', minute: '2-digit' });
  }
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleString(undefined, sameYear
    ? { month: 'short', day: 'numeric' }
    : { year: 'numeric', month: 'short', day: 'numeric' });
}

export function fmtDateFull(unix) {
  if (!unix) return '';
  return new Date(unix * 1000).toLocaleString();
}

// Compact source-format readout for library/album tables: "24b / 96 ksps",
// "16b / 44.1 ksps". `b` = bit depth, `ksps` = kilo-samples-per-second.
// Returns "—" when the FLAC didn't expose readable format info.
// For album rows with `sides[]`, returns "mixed" when any side's
// (bit_depth, sample_rate_khz) differs from the rest.
export function fmtSourceFormat(f) {
  if (!f.bit_depth || !f.sample_rate_khz) return '—';
  const sides = Array.isArray(f.sides) ? f.sides : null;
  if (sides && sides.length > 1) {
    const k = s => `${s.bit_depth || ''}|${s.sample_rate_khz || ''}`;
    const first = k(sides[0]);
    for (let i = 1; i < sides.length; i++) {
      if (k(sides[i]) !== first) return 'mixed';
    }
  }
  const sr = Number.isInteger(f.sample_rate_khz)
    ? f.sample_rate_khz
    : f.sample_rate_khz.toFixed(1);
  return `${f.bit_depth}b / ${sr} ksps`;
}

export function fmtBps(bps) {
  if (!bps && bps !== 0) return '—';
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + ' MB/s';
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + ' kB/s';
  return bps + ' B/s';
}

export function fmtDuration(sec) {
  if (!sec) return '—';
  const s = Math.round(sec);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h > 0
    ? `${h}h ${String(m).padStart(2,'0')}m`
    : `${m}m ${String(ss).padStart(2,'0')}s`;
}

export function htmlEscape(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Build a keydown handler that closes a modal on Escape. If focus is in a
// text input, the first Escape just blurs the input — guards against losing
// half-typed metadata. A second Escape (or any Escape with focus elsewhere)
// closes the modal. Mirrors the wave editor's own ESC handling.
//
// The handler also installs a focus-trap: when a modal is open, Tab cycles
// inside the modal so keyboard / AT users can't accidentally land back on
// the body and lose context. The first/last focusable elements wrap to each
// other; everything else delegates to the browser's default Tab behaviour.
export function makeModalEscHandler(closeFn, modalId) {
  return function (e) {
    if (e.key === 'Escape') {
      const tag = (e.target.tagName || '').toUpperCase();
      if (tag === 'INPUT' || tag === 'TEXTAREA') {
        e.target.blur();
        return;
      }
      e.preventDefault();
      closeFn();
      return;
    }
    if (e.key === 'Tab' && modalId) {
      const m = document.getElementById(modalId);
      if (m && !m.hidden) trapModalFocus(m, e);
    }
  };
}

// Cycle Tab inside the given modal element. Call on Tab keydown when the
// modal is open. The browser's own Tab walking is fine within the modal —
// we only intervene at the wrap-around edges so Shift-Tab from the first
// focusable lands on the last, and Tab from the last lands on the first.
export function trapModalFocus(modalEl, e) {
  if (e.key !== 'Tab') return;
  const focusables = modalEl.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  );
  if (!focusables.length) return;
  // Only consider visible focusables — `hidden` attribute and display:none
  // would otherwise produce a no-op cycle when a section like
  // #combine-sides-section is collapsed.
  const visible = [];
  for (const el of focusables) {
    if (el.hidden) continue;
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') continue;
    visible.push(el);
  }
  if (!visible.length) return;
  const first = visible[0];
  const last  = visible[visible.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

// Toast with an inline Undo button — used to replace `confirm()` for
// destructive actions like delete. The undoFn receives no args; the
// caller closes over whatever ids/tokens it needs. The toast lingers
// for `timeoutMs` (default 5000); after that the Undo button is gone
// and so is the chance to recover.
//
// ARIA shape mirrors the existing `.toast` helper in log.js but with
// `role="alert"` so AT users get the announcement promptly. The Undo
// button is a real <button> with `tabindex="0"` so keyboard users can
// Shift-Tab to it from wherever focus landed after the delete.
//
// Returns nothing — fire-and-forget. If multiple toasts stack, the
// container's flex-direction:column lays them top-down.
export function toastWithUndo(message, undoFn, opts) {
  const o = opts || {};
  const timeoutMs = Number.isFinite(o.timeoutMs) ? o.timeoutMs : 5000;
  const kind = o.kind || 'info';
  const c = document.getElementById('toast-container');
  if (!c) {
    // No container — fall back to firing the message via console so the
    // calling code still has some signal. Should never happen in
    // production (index.html always renders #toast-container).
    console.warn('toastWithUndo: #toast-container missing —', message);
    return;
  }
  const t = document.createElement('div');
  t.className = `toast ${kind} toast-with-undo`;
  t.setAttribute('role', 'alert');
  const msgSpan = document.createElement('span');
  msgSpan.textContent = message;
  msgSpan.className = 'toast-msg';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'toast-undo';
  btn.textContent = 'Undo';
  // Set a clear AT label since "Undo" alone after a delete is ambiguous —
  // screen readers will hear the message first via role="alert", then
  // "Undo button" when they land on it.
  btn.setAttribute('aria-label', `Undo: ${message}`);
  t.appendChild(msgSpan);
  t.appendChild(btn);
  c.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  let resolved = false;
  const close = () => {
    if (resolved) return;
    resolved = true;
    t.classList.remove('show');
    setTimeout(() => t.remove(), 250);
  };
  btn.addEventListener('click', () => {
    if (resolved) return;
    resolved = true;
    // Visually freeze the button so the user has feedback while the
    // restore POST is in flight; the toast itself stays put until the
    // undoFn resolves (or rejects, which we ignore — the caller will
    // surface its own error toast).
    btn.disabled = true;
    btn.textContent = 'Undoing…';
    Promise.resolve().then(undoFn).finally(() => {
      t.classList.remove('show');
      setTimeout(() => t.remove(), 250);
    });
  });
  setTimeout(close, timeoutMs);
}

// Plain toast (no Undo). Useful when the operation isn't reversible
// but we still want to inform the user without an interrupting modal.
// Wraps the same DOM/animation/timeout dance as toastWithUndo so the
// look-and-feel stays consistent.
export function toastSimple(message, opts) {
  const o = opts || {};
  const timeoutMs = Number.isFinite(o.timeoutMs) ? o.timeoutMs : 3500;
  const kind = o.kind || 'info';
  const c = document.getElementById('toast-container');
  if (!c) return;
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.setAttribute('role', 'status');
  t.textContent = message;
  c.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.remove(), 250);
  }, timeoutMs);
}
