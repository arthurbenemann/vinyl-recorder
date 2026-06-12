// Changelog modal — lazy-fetches /api/changelog on first open and renders
// a scrollable list of releases. Mirrors the onboarding modal's open/close/
// Esc/focus-return plumbing so keyboard behaviour matches the rest of the app.

import { makeModalEscHandler } from './util.js';

let _data = null;
let _focusReturn = null;

export function openChangelog() {
  const modal = document.getElementById('changelog-modal');
  if (!modal) return;
  _focusReturn = document.activeElement;
  modal.hidden = false;
  document.addEventListener('keydown', _escHandler);
  const closeBtn = modal.querySelector('.btn-small');
  if (closeBtn) closeBtn.focus();
  if (!_data) _fetchAndRender();
}

export function closeChangelog() {
  const modal = document.getElementById('changelog-modal');
  if (!modal) return;
  modal.hidden = true;
  document.removeEventListener('keydown', _escHandler);
  if (_focusReturn && typeof _focusReturn.focus === 'function') {
    try { _focusReturn.focus(); } catch (_) {}
  }
  _focusReturn = null;
}

const _escHandler = makeModalEscHandler(closeChangelog, 'changelog-modal');

async function _fetchAndRender() {
  const body = document.getElementById('changelog-body');
  if (!body) return;
  try {
    const r = await fetch('/api/changelog');
    _data = await r.json();
    _render(body, _data);
  } catch (_) {
    body.textContent = 'Failed to load changelog.';
  }
}

function _render(container, releases) {
  container.innerHTML = '';
  for (const release of releases) {
    const div = document.createElement('div');
    div.className = 'cl-release';

    const hdr = document.createElement('div');
    hdr.className = 'cl-release-header';

    const ver = document.createElement('span');
    ver.className = 'cl-version';
    ver.textContent = release.version;

    const dt = document.createElement('span');
    dt.className = 'cl-date';
    dt.textContent = release.date;

    hdr.append(ver, dt);
    div.appendChild(hdr);

    for (const sec of release.sections) {
      const title = document.createElement('div');
      title.className = 'cl-section-title';
      title.textContent = sec.title;
      div.appendChild(title);

      const ul = document.createElement('ul');
      ul.className = 'cl-items';
      for (const item of sec.items) {
        const li = document.createElement('li');
        li.innerHTML = _mdBold(item);
        ul.appendChild(li);
      }
      div.appendChild(ul);
    }

    container.appendChild(div);
  }
}

// Minimal markdown: escape HTML, then restore **bold**.
function _mdBold(text) {
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}
