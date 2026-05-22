"""Static guards for stream-URL persistence + the recent-URLs datalist.

The behaviour (remember the last connected URL, seed the input from it over
the env default, offer recents in a datalist) is exercised in a browser by
Playwright. These substring checks are the cheap net for the wiring across
upstream.js / config.js / main.js / index.html.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = REPO_ROOT / "app" / "static" / "modules" / "upstream.js"
CONFIG = REPO_ROOT / "app" / "static" / "modules" / "config.js"
MAIN_JS = REPO_ROOT / "app" / "static" / "main.js"
INDEX = REPO_ROOT / "app" / "static" / "index.html"


@pytest.fixture(scope="module")
def upstream() -> str:
    return UPSTREAM.read_text(encoding="utf-8")


def test_memory_helpers_defined_and_exported(upstream):
    assert "export function lastStreamUrl" in upstream
    assert "export function rememberStreamUrl" in upstream
    assert "export function renderStreamUrlRecent" in upstream
    # Namespaced keys, consistent with the other vr.* / lib.* prefs.
    assert "'vr.streamUrl'" in upstream
    assert "'vr.streamUrlRecent'" in upstream


def test_remember_called_only_on_successful_connect(upstream):
    # The save sits in the r.ok branch of toggleConnect (alongside probeGain),
    # so only URLs that actually connected are remembered.
    assert "rememberStreamUrl(url); probeGain(url)" in upstream


def test_mru_dedups_and_caps(upstream):
    # Move-to-front + dedup + cap so the recent list stays short and unique.
    assert "STREAM_URL_RECENT_MAX" in upstream
    assert ".filter(x => x !== u)" in upstream
    assert ".slice(0, STREAM_URL_RECENT_MAX)" in upstream


def test_datalist_built_via_dom_not_innerhtml(upstream):
    # A stored URL must never be able to inject markup into the datalist, so
    # the render function builds option nodes rather than assigning innerHTML.
    assert "createElement('option')" in upstream
    render_fn = upstream.split("function renderStreamUrlRecent")[1].split("\n}\n")[0]
    assert "innerHTML" not in render_fn


def test_config_prefers_saved_url_over_env_default():
    cfg = CONFIG.read_text(encoding="utf-8")
    assert "import { lastStreamUrl }" in cfg
    # saved URL checked first; env default is the else branch.
    assert "const savedUrl = lastStreamUrl()" in cfg
    assert "else if (c.default_stream_url)" in cfg


def test_datalist_rendered_at_boot():
    main_js = MAIN_JS.read_text(encoding="utf-8")
    assert "renderStreamUrlRecent" in main_js
    html = INDEX.read_text(encoding="utf-8")
    assert 'list="stream-url-recent"' in html
    assert 'id="stream-url-recent"' in html
