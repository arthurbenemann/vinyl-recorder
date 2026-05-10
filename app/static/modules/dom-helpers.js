// Small DOM / HTML-construction helpers. Sit between util.js (pure
// formatting) and the table renderers in library.js / tagging.js.

import { htmlEscape } from './util.js';

// Build an action-cell button. Filenames flow through `data-fname` and the
// onclick reads `this.dataset.fname` instead of inlining the value as a JS
// string literal. The previous `onclick="fn('${htmlEscape(fn)}')"` pattern
// looked safe but isn't: the browser HTML-decodes the attribute BEFORE the
// JS sees it, so `&#39;` becomes a literal `'` and a filename like
// `'); alert(1);//` could break out of the JS string. Going through
// `data-` attributes (HTML escaping is enough; never gets re-parsed as JS)
// closes that whole class of bug.
export function actionBtn(handler, fname, opts = {}) {
  const {label = '', ariaLabel = '', cls = 'icon-btn', danger = false, kind = ''} = opts;
  const cl = danger ? `${cls} danger` : cls;
  const k = kind ? ` data-kind="${htmlEscape(kind)}"` : '';
  // `title` shows on hover; `aria-label` is what screen readers announce for
  // these icon-only buttons (the glyph alone is meaningless to AT). The
  // hover-title can stay short ("Delete"); `ariaLabel` carries the row
  // context ("Delete <album>") so AT announcements are meaningful out of
  // sequence.
  const titleAttr = label ? ` title="${htmlEscape(label)}"` : '';
  const a11yLabel = ariaLabel || label;
  const ariaAttr = a11yLabel ? ` aria-label="${htmlEscape(a11yLabel)}"` : '';
  return `<button class="${cl}" data-fname="${htmlEscape(fname)}"${k}${titleAttr}${ariaAttr} onclick="${handler}(this.dataset.fname${kind ? ', this.dataset.kind' : ''})">${opts.glyph || ''}</button>`;
}

export function downloadLink(href, label = 'Download', ariaLabel = '') {
  const lbl = htmlEscape(label);
  const aLbl = htmlEscape(ariaLabel || label);
  return `<a class="icon-btn" href="${href}" download title="${lbl}" aria-label="${aLbl}">↓</a>`;
}

// Cache the last HTML written into each tbody so we can skip the
// `innerHTML = ...` assignment when nothing changed. Rebuilding a table on
// every 15 s poll otherwise blows away scroll position, focus, and the
// inline-rename input.
const _lastTbodyHtml = new Map();
export function setTbodyIfChanged(tbody, html) {
  const id = tbody.id;
  if (_lastTbodyHtml.get(id) === html) return false;
  tbody.innerHTML = html;
  _lastTbodyHtml.set(id, html);
  return true;
}
