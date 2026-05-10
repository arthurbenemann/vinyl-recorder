// Pi deploy modal — pushes pi/server.py + pi-recorder.service to a
// Raspberry Pi over SSH. Mirrors the manual scp/ssh ceremony documented
// in README.md "Install on the Pi". Host + username persist in
// localStorage so a repeat push (server.py update) only needs the
// password.

import { makeModalEscHandler } from './util.js';
import { toast } from './log.js';

const PI_DEPLOY_HOST_KEY = 'piDeploy.host';
const PI_DEPLOY_USER_KEY = 'piDeploy.user';
let _piDeployFocusReturn = null;

export function openPiDeploy() {
  _piDeployFocusReturn = document.activeElement;
  const m = document.getElementById('pi-deploy-modal');
  if (!m) return;
  // Restore last-used host/user; pull a sensible default for host from
  // the configured stream URL when nothing's saved yet (e.g. on a fresh
  // install the user typed http://pi-recorder:8000/stream into
  // DEFAULT_STREAM_URL — that hostname is the deploy target too).
  let savedHost = '';
  try { savedHost = localStorage.getItem(PI_DEPLOY_HOST_KEY) || ''; } catch(e) {}
  if (!savedHost) {
    try {
      const u = new URL(document.getElementById('stream-url').value);
      // Skip the default SomaFM example — only suggest hostnames that
      // could plausibly be a Pi (the `/info` endpoint is the canonical
      // signal but probing it from here is overkill for a placeholder).
      if (u.hostname && !/somafm\.com$/i.test(u.hostname)) savedHost = u.hostname;
    } catch (e) {}
  }
  document.getElementById('pi-host').value = savedHost;
  let savedUser = 'pi';
  try { savedUser = localStorage.getItem(PI_DEPLOY_USER_KEY) || 'pi'; } catch(e) {}
  document.getElementById('pi-user').value = savedUser;
  document.getElementById('pi-pass').value = '';
  // Clear prior log so a re-open after a failed deploy starts fresh.
  const logEl = document.getElementById('pi-deploy-log');
  logEl.innerHTML = '';
  logEl.hidden = true;
  m.hidden = false;
  document.addEventListener('keydown', piDeployEscHandler);
  // Focus the first empty field so a returning user doesn't have to
  // tab through the saved ones.
  const firstEmpty = ['pi-host', 'pi-user', 'pi-pass']
    .map(id => document.getElementById(id))
    .find(el => !el.value);
  (firstEmpty || document.getElementById('pi-pass')).focus();
}

export function closePiDeploy() {
  document.getElementById('pi-deploy-modal').hidden = true;
  document.removeEventListener('keydown', piDeployEscHandler);
  // Wipe the password field on close so it never lingers in DOM if the
  // user reopens the modal later.
  const pw = document.getElementById('pi-pass');
  if (pw) pw.value = '';
  if (_piDeployFocusReturn && typeof _piDeployFocusReturn.focus === 'function') {
    try { _piDeployFocusReturn.focus(); } catch (e) {}
  }
  _piDeployFocusReturn = null;
}
const piDeployEscHandler = makeModalEscHandler(closePiDeploy, 'pi-deploy-modal');

function _piDeployLogLine(text, kind) {
  const logEl = document.getElementById('pi-deploy-log');
  if (!logEl) return;
  logEl.hidden = false;
  const div = document.createElement('div');
  if (kind) div.className = kind;
  div.textContent = text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

export async function runPiDeploy() {
  const host = document.getElementById('pi-host').value.trim();
  const username = document.getElementById('pi-user').value.trim();
  const password = document.getElementById('pi-pass').value;
  if (!host) { toast('✗ host is required', 'err'); return; }
  if (!username) { toast('✗ username is required', 'err'); return; }
  if (!password) { toast('✗ password is required', 'err'); return; }
  // Persist non-secret fields so a re-deploy only needs the password.
  try {
    localStorage.setItem(PI_DEPLOY_HOST_KEY, host);
    localStorage.setItem(PI_DEPLOY_USER_KEY, username);
  } catch (e) {}

  const goBtn = document.getElementById('pi-deploy-go');
  const headerBtn = document.getElementById('pi-deploy-btn');
  goBtn.disabled = true; goBtn.textContent = 'deploying…';
  if (headerBtn) headerBtn.disabled = true;
  const logEl = document.getElementById('pi-deploy-log');
  logEl.innerHTML = '';
  logEl.hidden = false;
  _piDeployLogLine(`▶ deploying to ${username}@${host}…`, 'info');

  try {
    const r = await fetch('/api/pi/deploy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host, username, password }),
    });
    if (!r.ok) {
      // Pre-stream failure (e.g. 422 validation) — body is regular JSON.
      let detail = 'HTTP ' + r.status;
      try { detail = (await r.json()).detail || detail; } catch (e) {}
      _piDeployLogLine('✗ ' + detail, 'err');
      toast('✗ pi deploy failed: ' + detail, 'err');
      return;
    }
    // Streamed NDJSON: one JSON object per \n-terminated chunk. Parse
    // and render as each line arrives so the modal updates live during
    // the apt step (which can be the slowest phase on a fresh Pi OS).
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let succeeded = false;
    let errorDetail = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) !== -1) {
        const raw = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!raw) continue;
        let msg;
        try { msg = JSON.parse(raw); }
        catch (e) { _piDeployLogLine(raw); continue; }
        if (msg.type === 'log')   _piDeployLogLine(msg.line);
        else if (msg.type === 'done')  succeeded = true;
        else if (msg.type === 'error') errorDetail = msg.detail || 'deploy failed';
      }
    }
    if (succeeded) {
      _piDeployLogLine('✓ pi-recorder is up. you can now point the stream URL at this host.', 'ok');
      toast('✓ pi deployed to ' + host, 'ok');
    } else {
      const detail = errorDetail || 'deploy ended without a result';
      _piDeployLogLine('✗ ' + detail, 'err');
      toast('✗ pi deploy failed: ' + detail, 'err');
    }
  } catch (e) {
    _piDeployLogLine('✗ ' + (e.message || e), 'err');
    toast('✗ pi deploy failed: ' + (e.message || e), 'err');
  } finally {
    goBtn.disabled = false; goBtn.textContent = 'deploy';
    if (headerBtn) headerBtn.disabled = false;
  }
}
