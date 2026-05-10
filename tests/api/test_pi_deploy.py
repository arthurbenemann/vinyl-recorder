"""Smoke + unit coverage for the in-app Pi deployer.

The SSH connection is fully mocked through paramiko: tests assert the
service calls the right paramiko surfaces in the right order, and that
the route maps DeployError → 502 with a useful detail string. The real
network path is verified by the e2e suite (and, ultimately, by aiming
the modal at a Pi).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# These tests genuinely exercise paramiko — even mocked, the module has to
# import. Skip the whole file (rather than erroring on collection) so a
# slim local env without paramiko still runs the rest of the suite.
paramiko = pytest.importorskip("paramiko")


def _client():
    from main import app
    return TestClient(app)


# ─────────────────────────────────────────────────────────────────────────────
# pi_deploy.deploy() unit tests
# ─────────────────────────────────────────────────────────────────────────────
def _fake_pi_source(tmp_path: Path, monkeypatch) -> Path:
    """Create stub server.py + pi-recorder.service in a tmp directory and
    point the service at it. Avoids touching the real `pi/` files when
    running tests outside the container."""
    (tmp_path / "server.py").write_text("# stub")
    (tmp_path / "pi-recorder.service").write_text("[Service]\nExecStart=/bin/true\n")
    from services import pi_deploy
    monkeypatch.setattr(pi_deploy, "PI_SOURCE_DIR", tmp_path)
    return tmp_path


def _fake_client_factory(*, exit_status: int = 0, isactive_out: bytes = b"active\n",
                          install_out: bytes = b"", install_err: bytes = b"",
                          connect_raises: Exception | None = None,
                          puts: list | None = None) -> tuple[MagicMock, callable]:
    """Build a paramiko.SSHClient stand-in with controlled exit + outputs.

    The returned (mock_client, factory) tuple lets a test inspect what
    methods got called after the deploy runs. `puts` is the list to which
    sftp.put(local, remote) calls are appended."""
    if puts is None:
        puts = []
    sftp = MagicMock()
    sftp.put.side_effect = lambda local, remote: puts.append((local, remote))

    install_chan = MagicMock()
    install_chan.recv_exit_status.return_value = exit_status
    install_stdout = MagicMock()
    install_stdout.channel = install_chan
    install_stdout.read.return_value = install_out
    install_stderr = MagicMock()
    install_stderr.read.return_value = install_err
    install_stdin = MagicMock()
    install_stdin.channel = MagicMock()

    isactive_stdout = MagicMock()
    isactive_stdout.read.return_value = isactive_out
    isactive_chan = MagicMock()
    isactive_chan.recv_exit_status.return_value = 0
    isactive_stdout.channel = isactive_chan

    # exec_command is called twice — first for `sudo -S sh -s`, second
    # for `systemctl is-active pi-recorder`. Use a side_effect list so the
    # mock returns each in order.
    client = MagicMock()
    client.open_sftp.return_value = sftp
    client.exec_command.side_effect = [
        (install_stdin, install_stdout, install_stderr),
        (MagicMock(), isactive_stdout, MagicMock()),
    ]
    if connect_raises is not None:
        client.connect.side_effect = connect_raises

    factory = MagicMock(return_value=client)
    return client, factory


def test_deploy_happy_path_uploads_and_runs_install(tmp_path, monkeypatch):
    src = _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    puts: list = []
    client, factory = _fake_client_factory(puts=puts)

    captured: list[str] = []
    lines = pi_deploy.deploy(
        host="pi.local", username="pi", password="raspberry",
        port=22, on_log=captured.append,
        _client_factory=factory,
    )
    assert lines == captured  # callback receives the same lines we return
    # Both files were uploaded, in order.
    assert puts == [
        (str(src / "server.py"),           "/tmp/server.py"),
        (str(src / "pi-recorder.service"), "/tmp/pi-recorder.service"),
    ]
    # paramiko.SSHClient.connect was called with host/port/credentials.
    client.connect.assert_called_once()
    connect_kwargs = client.connect.call_args.kwargs
    assert connect_kwargs["hostname"] == "pi.local"
    assert connect_kwargs["username"] == "pi"
    assert connect_kwargs["password"] == "raspberry"
    assert connect_kwargs["port"] == 22
    # First exec is the sudo install pipeline, second is the
    # systemctl is-active check. Order matters — the route relies on it.
    install_calls = client.exec_command.call_args_list
    assert install_calls[0].args[0].startswith("sudo -S")
    assert install_calls[1].args[0] == "systemctl is-active pi-recorder"
    # And our progress log captures the high-level steps.
    blob = "\n".join(captured)
    assert "connecting to pi@pi.local:22" in blob
    assert "uploading" in blob
    assert "installed and enabled" in blob
    assert "service is active" in blob
    assert "deployment complete" in blob


def test_deploy_auth_failure_raises_friendly_error(tmp_path, monkeypatch):
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    _, factory = _fake_client_factory(
        connect_raises=paramiko.AuthenticationException("bad password"),
    )
    with pytest.raises(pi_deploy.DeployError, match="authentication failed"):
        pi_deploy.deploy(
            host="pi.local", username="pi", password="bad",
            _client_factory=factory,
        )


def test_deploy_install_nonzero_exit_surfaces_stderr(tmp_path, monkeypatch):
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    _, factory = _fake_client_factory(
        exit_status=1,
        install_err=b"sudo: 1 incorrect password attempt\n",
    )
    with pytest.raises(pi_deploy.DeployError) as excinfo:
        pi_deploy.deploy(
            host="pi.local", username="pi", password="x",
            _client_factory=factory,
        )
    msg = str(excinfo.value).lower()
    # Either the raw "incorrect password" tail or our friendly remap is fine.
    assert "password" in msg


def test_deploy_isactive_failed_state_raises(tmp_path, monkeypatch):
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    _, factory = _fake_client_factory(
        exit_status=0, isactive_out=b"failed\n",
    )
    with pytest.raises(pi_deploy.DeployError, match="failed"):
        pi_deploy.deploy(
            host="pi.local", username="pi", password="x",
            _client_factory=factory,
        )


def test_deploy_missing_source_files_raises(tmp_path, monkeypatch):
    # Empty directory — no server.py/pi-recorder.service to ship.
    from services import pi_deploy
    monkeypatch.setattr(pi_deploy, "PI_SOURCE_DIR", tmp_path)
    _, factory = _fake_client_factory()
    with pytest.raises(pi_deploy.DeployError, match="pi source files not found"):
        pi_deploy.deploy(
            host="pi.local", username="pi", password="x",
            _client_factory=factory,
        )


def test_deploy_validates_blank_host(tmp_path, monkeypatch):
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy
    _, factory = _fake_client_factory()
    with pytest.raises(pi_deploy.DeployError, match="host"):
        pi_deploy.deploy(host="", username="pi", password="x",
                         _client_factory=factory)


# ─────────────────────────────────────────────────────────────────────────────
# /api/pi/deploy route
# ─────────────────────────────────────────────────────────────────────────────
def test_pi_deploy_route_happy_path(tmp_path, monkeypatch):
    """Successful deploy → 200 with the line-by-line log surfaced."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    def fake_deploy(host, username, password, port, on_log=None, **kw):
        # Verify the route forwarded the body correctly.
        assert host == "pi.local"
        assert username == "pi"
        assert password == "secret"
        assert port == 2222
        return ["▶ connecting", "✓ deployment complete"]

    monkeypatch.setattr(pi_deploy, "deploy", fake_deploy)

    r = _client().post("/api/pi/deploy", json={
        "host": "pi.local", "username": "pi",
        "password": "secret", "port": 2222,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["log"] == ["▶ connecting", "✓ deployment complete"]


def test_pi_deploy_route_deploy_error_is_502(tmp_path, monkeypatch):
    """A DeployError from the service maps to HTTP 502 with the message."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    def boom(*a, **kw):
        raise pi_deploy.DeployError("authentication failed — wrong password")

    monkeypatch.setattr(pi_deploy, "deploy", boom)

    r = _client().post("/api/pi/deploy", json={
        "host": "pi.local", "username": "pi", "password": "x",
    })
    assert r.status_code == 502
    assert "authentication failed" in r.json()["detail"]


def test_pi_deploy_route_unexpected_error_is_500(tmp_path, monkeypatch):
    """Unhandled exception → 500 (not a leaky stack trace)."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    def crash(*a, **kw):
        raise RuntimeError("oops")

    monkeypatch.setattr(pi_deploy, "deploy", crash)

    r = _client().post("/api/pi/deploy", json={
        "host": "pi.local", "username": "pi", "password": "x",
    })
    assert r.status_code == 500
    assert "oops" in r.json()["detail"]


def test_pi_deploy_route_validates_required_fields():
    """Missing host / password is a 422 from Pydantic."""
    r = _client().post("/api/pi/deploy", json={"username": "pi"})
    assert r.status_code == 422
