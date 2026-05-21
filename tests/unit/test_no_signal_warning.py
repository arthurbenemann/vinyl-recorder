"""Unit tests for `_should_warn_no_signal` in routes.recordings.

The watcher emits a "recording, but no audio detected" warning when a take
runs `NO_SIGNAL_WARN_SECONDS` without the upstream peak ever clearing the
floor — catching a dead take that auto-stop-on-silence can't (it only fires
after it has *seen* signal). This pins the pure decision; the live watcher /
ffmpeg plumbing is exercised by the synthetic-event e2e.
"""
from routes.recordings import NO_SIGNAL_WARN_SECONDS, _should_warn_no_signal


def test_warns_after_threshold_with_no_signal():
    assert _should_warn_no_signal(
        signal_seen=False, elapsed=NO_SIGNAL_WARN_SECONDS,
        paused=False, already_warned=False) is True
    assert _should_warn_no_signal(
        signal_seen=False, elapsed=NO_SIGNAL_WARN_SECONDS + 5,
        paused=False, already_warned=False) is True


def test_no_warn_before_threshold():
    # Normal needle-cueing window — must not false-positive.
    assert _should_warn_no_signal(
        signal_seen=False, elapsed=NO_SIGNAL_WARN_SECONDS - 0.1,
        paused=False, already_warned=False) is False


def test_no_warn_when_signal_seen():
    assert _should_warn_no_signal(
        signal_seen=True, elapsed=NO_SIGNAL_WARN_SECONDS * 2,
        paused=False, already_warned=False) is False


def test_no_warn_when_paused():
    assert _should_warn_no_signal(
        signal_seen=False, elapsed=NO_SIGNAL_WARN_SECONDS * 2,
        paused=True, already_warned=False) is False


def test_warns_only_once():
    assert _should_warn_no_signal(
        signal_seen=False, elapsed=NO_SIGNAL_WARN_SECONDS * 2,
        paused=False, already_warned=True) is False
