"""Unit tests for `app/services/jellyfin.py`.

The scan trigger is best-effort glue: it must POST the right request when
configured, stay completely silent when not, and never let a Jellyfin
failure propagate (the split that invoked it already succeeded). urllib is
stubbed — no network.
"""
import urllib.error
import urllib.request

import pytest

from services import jellyfin as jf


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(jf, "JELLYFIN_URL", "http://jellyfin:8096")
    monkeypatch.setattr(jf, "JELLYFIN_API_KEY", "sekrit")


class _FakeResponse:
    status = 204
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_disabled_without_url(monkeypatch):
    monkeypatch.setattr(jf, "JELLYFIN_URL", "")
    monkeypatch.setattr(jf, "JELLYFIN_API_KEY", "sekrit")
    assert not jf.enabled()


def test_disabled_without_api_key(monkeypatch):
    """URL alone isn't enough — /Library/Refresh needs an authenticated
    request, so a missing key disables the feature outright."""
    monkeypatch.setattr(jf, "JELLYFIN_URL", "http://jellyfin:8096")
    monkeypatch.setattr(jf, "JELLYFIN_API_KEY", "")
    assert not jf.enabled()


def test_trigger_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(jf, "JELLYFIN_URL", "")
    monkeypatch.setattr(jf, "JELLYFIN_API_KEY", "")

    def boom(*a, **kw):
        raise AssertionError("urlopen must not be called when disabled")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert jf.trigger_library_scan() is False


def test_trigger_posts_refresh_with_token(configured, monkeypatch):
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["token"] = req.get_header("X-emby-token")
        seen["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert jf.trigger_library_scan() is True
    assert seen["url"] == "http://jellyfin:8096/Library/Refresh"
    assert seen["method"] == "POST"
    assert seen["token"] == "sekrit"
    assert seen["timeout"] == jf._TIMEOUT_SECONDS


def test_trigger_swallows_http_errors(configured, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert jf.trigger_library_scan() is False  # no raise


def test_trigger_swallows_network_errors(configured, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert jf.trigger_library_scan() is False  # no raise


def test_bg_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(jf, "JELLYFIN_URL", "")
    monkeypatch.setattr(jf, "JELLYFIN_API_KEY", "")

    def boom(*a, **kw):
        raise AssertionError("no thread should be spawned when disabled")

    monkeypatch.setattr(jf.threading, "Thread", boom)
    jf.trigger_library_scan_bg()  # no raise, no thread


def test_bg_runs_trigger_on_a_thread(configured, monkeypatch):
    called = []
    monkeypatch.setattr(jf, "trigger_library_scan", lambda: called.append(True))
    jf.trigger_library_scan_bg()
    # The daemon thread is real; join it via a polling wait so the assert
    # isn't racy.
    import time
    for _ in range(100):
        if called:
            break
        time.sleep(0.01)
    assert called == [True]
