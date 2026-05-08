"""Unit tests for the VERSION resolver.

`_resolve_version` has three fallbacks in order: ``app/VERSION`` file
(baked by Dockerfile), ``git describe`` against the working copy, then
the literal ``"dev"``. Each branch is exercised with a stub Path
constructor so the helper sees an isolated tmp dir.
"""
from pathlib import Path as RealPath

import version as version_mod


def _redirect_path_to(tmp_path, monkeypatch):
    """Replace ``version.Path`` with a callable that pretends ``__file__``
    lives inside ``tmp_path``. Then ``Path(__file__).parent`` resolves to
    ``tmp_path`` and the helper looks for ``VERSION`` there."""
    def fake_path(_arg):
        return tmp_path / "fake_module.py"

    monkeypatch.setattr(version_mod, "Path", fake_path)


def test_resolve_version_prefers_baked_file(tmp_path, monkeypatch):
    """The Dockerfile drops a static ``VERSION`` next to the app. When
    present, _resolve_version returns its trimmed contents and skips the
    git probe entirely."""
    (tmp_path / "VERSION").write_text("v1.2.3-from-file\n")
    _redirect_path_to(tmp_path, monkeypatch)

    def _no_git(*a, **kw):
        raise AssertionError("git should not be invoked when VERSION exists")

    monkeypatch.setattr(version_mod.subprocess, "check_output", _no_git)
    assert version_mod._resolve_version() == "v1.2.3-from-file"


def test_resolve_version_falls_back_to_git_describe(tmp_path, monkeypatch):
    """No VERSION file → run `git describe` and return its trimmed output."""
    _redirect_path_to(tmp_path, monkeypatch)
    monkeypatch.setattr(
        version_mod.subprocess, "check_output",
        lambda *a, **kw: "v0.7.0-3-gabcdef\n",
    )
    assert version_mod._resolve_version() == "v0.7.0-3-gabcdef"


def test_resolve_version_returns_dev_when_all_fallbacks_fail(tmp_path, monkeypatch):
    _redirect_path_to(tmp_path, monkeypatch)

    def boom(*a, **kw):
        raise OSError("git not on PATH")

    monkeypatch.setattr(version_mod.subprocess, "check_output", boom)
    assert version_mod._resolve_version() == "dev"


def test_resolve_version_treats_empty_file_as_missing(tmp_path, monkeypatch):
    """A VERSION file that exists but is empty/whitespace shouldn't
    resolve to an empty string — keep falling through to git/dev."""
    (tmp_path / "VERSION").write_text("   \n")
    _redirect_path_to(tmp_path, monkeypatch)
    monkeypatch.setattr(
        version_mod.subprocess, "check_output",
        lambda *a, **kw: "v9.9.9\n",
    )
    assert version_mod._resolve_version() == "v9.9.9"


def test_module_level_version_is_a_string():
    # Sanity: the import-time resolution always lands on something string-y,
    # never None — surfaced via /api/config.version.
    assert isinstance(version_mod.VERSION, str)
    assert version_mod.VERSION  # non-empty
    # Restore the real Path type after the monkeypatched tests above.
    assert version_mod.Path is RealPath or callable(version_mod.Path)
