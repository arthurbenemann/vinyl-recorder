"""Static guards for silence-detection setting persistence.

The behaviour (seed the noise-floor / min-silence / auto-skip controls from
localStorage on open, save each change) is exercised end-to-end by the
Playwright suite, but the wiring lives across three files. These cheap
substring checks catch a refactor that drops the hydration call, the save
listener, or the validated-helper export — failures the node unit test
(which only sees the pure helper) wouldn't surface.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WAVE_EDITOR = REPO_ROOT / "app" / "static" / "wave-editor.js"
TIMELINE = REPO_ROOT / "app" / "static" / "modules" / "timeline-state.js"
INDEX = REPO_ROOT / "app" / "static" / "index.html"

# The three persisted controls and the localStorage key each maps to. Kept in
# one place so a key rename has to update both the code and this test in step.
PREFS = {
    "we-noise": "we.noiseInt8",
    "we-mindur": "we.minSilence",
    "we-skiplong": "we.skipLong",
}


@pytest.fixture(scope="module")
def wave_js() -> str:
    return WAVE_EDITOR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def timeline_js() -> str:
    return TIMELINE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_validation_helper_defined_and_exported(timeline_js):
    # The clamp/parse guard must be exposed on window so wave-editor.js (a
    # classic script) can call it as a bare global and the node test can reach
    # it in the sandbox.
    assert "function _weDetectSettingValue" in timeline_js
    assert "window._weDetectSettingValue = _weDetectSettingValue" in timeline_js


def test_hydrate_helper_defined_and_invoked_on_open(wave_js):
    assert "function _weHydrateDetectSettings" in wave_js
    # openWaveEditor must actually call it — count >= 2 means definition + at
    # least one call site.
    assert wave_js.count("_weHydrateDetectSettings") >= 2


def test_every_control_persists_to_a_namespaced_key(wave_js, html):
    for input_id, ls_key in PREFS.items():
        assert f'id="{input_id}"' in html, f"{input_id} control missing from index.html"
        assert ls_key in wave_js, f"{ls_key} not referenced in wave-editor.js"


def test_change_listener_is_wired_once(wave_js):
    # A change handler writes the value back; the dataset guard keeps it from
    # stacking across repeated openWaveEditor calls.
    assert "addEventListener('change'" in wave_js
    assert "dataset.persistWired" in wave_js


def test_localstorage_access_is_guarded(wave_js):
    # Private-mode / disabled-storage browsers throw on localStorage access;
    # both the read and write paths swallow that so the editor still opens.
    assert "_weStoredPref" in wave_js
    assert "_weSavePref" in wave_js
    assert "try { return localStorage.getItem" in wave_js
    assert "try { localStorage.setItem" in wave_js
