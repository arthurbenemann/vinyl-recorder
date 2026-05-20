// First-run onboarding overlay. Explains the implicit Raw → Album → Music
// pipeline that new users can't otherwise see: side recordings (Raw) get
// combined + tagged into an Album, which is then split into the finished
// tagged tracks that live under Music.
//
// Shown automatically once — gated on a localStorage flag — and re-openable
// any time from the header ⋮ menu's "how it works" item. Mirrors the
// pi-deploy modal's open/close/focus-return/Esc plumbing so the look and
// keyboard behaviour match the rest of the app's dialogs.

import { makeModalEscHandler } from './util.js';

// The single source of truth for "has this browser seen onboarding". A
// plain string flag in localStorage; presence (any truthy value) means
// dismissed. Namespaced under `vr.` like the app's other client-side keys.
const ONBOARDED_KEY = 'vr.onboarded';

let _onboardingFocusReturn = null;

function _seen() {
  try { return !!localStorage.getItem(ONBOARDED_KEY); } catch (e) { return false; }
}

function _markSeen() {
  try { localStorage.setItem(ONBOARDED_KEY, '1'); } catch (e) {}
}

export function openOnboarding() {
  const m = document.getElementById('onboarding-modal');
  if (!m) return;
  _onboardingFocusReturn = document.activeElement;
  m.hidden = false;
  document.addEventListener('keydown', onboardingEscHandler);
  // Focus the primary "Got it" button so keyboard / AT users land on the
  // dismiss affordance and Tab cycles inside the dialog (focus trap lives
  // in makeModalEscHandler).
  const btn = document.getElementById('onboarding-got-it');
  if (btn) btn.focus();
}

export function closeOnboarding() {
  const m = document.getElementById('onboarding-modal');
  if (!m) return;
  m.hidden = true;
  document.removeEventListener('keydown', onboardingEscHandler);
  // Any close path counts as "seen" — Got it, the X, click-outside, and
  // Escape all set the flag so the overlay never auto-shows again.
  _markSeen();
  if (_onboardingFocusReturn && typeof _onboardingFocusReturn.focus === 'function') {
    try { _onboardingFocusReturn.focus(); } catch (e) {}
  }
  _onboardingFocusReturn = null;
}

const onboardingEscHandler = makeModalEscHandler(closeOnboarding, 'onboarding-modal');

// Auto-show on first load only. Call after the page's main init so the
// overlay doesn't paint before the app behind it is ready. A no-op when
// the flag is already set, which keeps it from fighting the reconnect
// prompt (a toast, fired only on a crash) or any returning-user session.
export function initOnboarding() {
  if (_seen()) return;
  openOnboarding();
}
