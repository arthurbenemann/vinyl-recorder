"""Unit tests for services/ffmpeg.disk_space_error."""
from services import ffmpeg as ffmpeg_mod


def test_disk_space_error_returns_none_when_above_threshold(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "disk_free_gb", lambda: 50.0)
    assert ffmpeg_mod.disk_space_error(2.0, "recording") is None


def test_disk_space_error_message_includes_op_and_numbers(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "disk_free_gb", lambda: 0.5)
    msg = ffmpeg_mod.disk_space_error(2.0, "split")
    # All three pieces of context that the UI relies on must be present.
    assert "split" in msg
    assert "0.5 GB free" in msg
    assert "2.0 GB" in msg


def test_disk_space_error_at_exact_threshold_passes(monkeypatch):
    # Boundary: disk_space_error uses `>=` so equal is OK.
    monkeypatch.setattr(ffmpeg_mod, "disk_free_gb", lambda: 2.0)
    assert ffmpeg_mod.disk_space_error(2.0, "recording") is None
