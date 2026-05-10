"""Smoke + unit coverage for the in-app Pi deployer.

The SSH connection is fully mocked through paramiko: tests assert the
service calls the right paramiko surfaces in the right order, that the
NDJSON stream the route emits has the expected frame shape, and that
the deploy never echoes the literal sudo password into the modal log
(the regression that originally surfaced as `sh: 1: <password>: not
found`). The real network path is verified by the e2e suite (and,
ultimately, by aiming the modal at a Pi).
"""
from __future__ import annotations

import json
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


def _read_ndjson(response) -> list[dict]:
    """Decode an NDJSON streaming body into a list of message dicts.

    `httpx.Response.text` already buffers the whole body — fine for tests
    where the stream is short. The route writes one JSON object per
    \\n-terminated line; blank lines are ignored."""
    out: list[dict] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


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


def _fake_client_factory(*, install_exit: int = 0,
                          install_lines: list[str] | None = None,
                          isactive_out: bytes = b"active\n",
                          sudo_nopasswd: bool = True,
                          connect_raises: Exception | None = None,
                          puts: list | None = None,
                          install_payload: list[str] | None = None,
                          ) -> tuple[MagicMock, callable]:
    """Build a paramiko.SSHClient stand-in with controlled exit + outputs.

    The test seam in pi_deploy.deploy uses three paramiko surfaces:

    1. `client.exec_command("sudo -n true …")` — probe whether sudo is
       NOPASSWD on the remote. `sudo_nopasswd=True` makes that probe
       exit 0 (no password needed); False makes it exit 1 (sudo will
       want a password and we'll feed it).
    2. `client.get_transport().open_session()` — channel for the actual
       install pipeline. Output it should "produce" goes in
       `install_lines`; the bytes the deploy writes to it are recorded
       in `install_payload`.
    3. `client.exec_command("systemctl is-active pi-recorder")` — sanity
       check at the end.
    """
    if puts is None:
        puts = []
    if install_lines is None:
        install_lines = []
    if install_payload is None:
        install_payload = []

    sftp = MagicMock()
    sftp.put.side_effect = lambda local, remote: puts.append((local, remote))

    # ── install channel ──────────────────────────────────────────────────
    install_chan = MagicMock()
    install_chan.recv_exit_status.return_value = install_exit

    def _capture_payload(payload):
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        install_payload.append(payload)
    install_chan.sendall.side_effect = _capture_payload

    # makefile() returns a file-like; the deploy iterates readline() until
    # it returns an empty string (EOF). Each install_lines entry is a
    # full line (newline appended here for the readline contract).
    install_file = MagicMock()
    line_iter = iter([line + "\n" for line in install_lines] + [""])
    install_file.readline.side_effect = lambda: next(line_iter)
    install_chan.makefile.return_value = install_file

    transport = MagicMock()
    transport.open_session.return_value = install_chan

    # ── sudo NOPASSWD probe ──────────────────────────────────────────────
    probe_chan = MagicMock()
    probe_chan.recv_exit_status.return_value = 0 if sudo_nopasswd else 1
    probe_stdout = MagicMock()
    probe_stdout.channel = probe_chan

    # ── systemctl is-active ──────────────────────────────────────────────
    isactive_stdout = MagicMock()
    isactive_stdout.read.return_value = isactive_out
    isactive_chan = MagicMock()
    isactive_chan.recv_exit_status.return_value = 0
    isactive_stdout.channel = isactive_chan

    client = MagicMock()
    client.open_sftp.return_value = sftp
    client.get_transport.return_value = transport
    # Two top-level exec_command calls: the probe, then the is-active.
    # The install pipeline goes via transport.open_session(), NOT through
    # client.exec_command, so it doesn't show up here.
    client.exec_command.side_effect = [
        (MagicMock(), probe_stdout, MagicMock()),
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
    payloads: list = []
    client, factory = _fake_client_factory(
        puts=puts, install_payload=payloads,
        install_lines=[
            "Get:1 http://raspbian.raspberrypi.com/raspbian bookworm InRelease",
            "Setting up python3 (3.11.2-1+b1) ...",
        ],
    )

    captured: list[str] = []
    lines = pi_deploy.deploy(
        host="pi.local", username="pi", password="raspberry",
        port=22, on_log=captured.append,
        _client_factory=factory,
    )
    assert lines == captured  # callback receives the same lines we return
    assert puts == [
        (str(src / "server.py"),           "/tmp/server.py"),
        (str(src / "pi-recorder.service"), "/tmp/pi-recorder.service"),
    ]
    # Connect kwargs propagate the request body verbatim.
    client.connect.assert_called_once()
    kwargs = client.connect.call_args.kwargs
    assert kwargs["hostname"] == "pi.local"
    assert kwargs["username"] == "pi"
    assert kwargs["password"] == "raspberry"
    assert kwargs["port"] == 22
    # Top-level exec_command was used for the sudo probe + is-active check.
    exec_calls = client.exec_command.call_args_list
    assert exec_calls[0].args[0].startswith("sudo -n true")
    assert exec_calls[1].args[0] == "systemctl is-active pi-recorder"
    # Install pipeline went through the channel API, with combined stderr
    # so the modal renders an interleaved log.
    chan = factory.return_value.get_transport.return_value.open_session.return_value
    chan.set_combine_stderr.assert_called_with(True)
    chan.exec_command.assert_called_once()
    install_cmd = chan.exec_command.call_args.args[0]
    assert install_cmd.startswith("sudo ")
    # Streamed apt output appears in the user-visible log.
    blob = "\n".join(captured)
    assert "connecting to pi@pi.local:22" in blob
    assert "uploading" in blob
    assert "Get:1 http://" in blob       # streamed apt-get update
    assert "Setting up python3" in blob  # streamed apt-get install
    assert "installed and enabled" in blob
    assert "service is active" in blob
    assert "deployment complete" in blob


def test_deploy_install_script_includes_apt_bootstrap(tmp_path, monkeypatch):
    """The install payload sent over the SSH channel must include
    `apt-get install ... python3 alsa-utils` so the deploy works on a
    stripped-down Pi OS Lite image, not just the default Raspberry Pi
    OS that ships them pre-installed."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    payloads: list = []
    _, factory = _fake_client_factory(install_payload=payloads)
    pi_deploy.deploy(host="pi.local", username="pi", password="x",
                     _client_factory=factory)
    script = "".join(payloads)
    assert "apt-get update" in script
    assert "apt-get install" in script
    assert "python3" in script and "alsa-utils" in script
    # set -e gives "abort on first failure" semantics — without it a
    # missing package would still leave the systemd unit enabled and the
    # user'd see ✓ for a half-installed deploy.
    assert "set -e" in script
    # noninteractive prevents apt from blocking on a config-prompt during
    # an in-place upgrade of an existing package.
    assert "DEBIAN_FRONTEND=noninteractive" in script


def test_deploy_does_not_leak_password_when_sudo_is_nopasswd(tmp_path, monkeypatch):
    """Regression: with NOPASSWD sudo (default Raspberry Pi OS), the
    earlier code wrote `password\\n` ahead of the install script even
    though sudo wouldn't consume it. The fall-through bytes were
    executed by `sh -s` as a command and the literal password ended up
    in the modal log via `sh: 1: <password>: not found` on stderr.

    Now: probe sudo first, only feed the password when it'll actually be
    consumed. Plus a substring scrubber as defense-in-depth."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    PASSWORD = "supersecret123"
    payloads: list = []
    captured: list[str] = []
    _, factory = _fake_client_factory(
        sudo_nopasswd=True,
        install_payload=payloads,
        # Simulate the historical leak: the remote shell echoes the
        # password via a `command not found` line. The scrubber must
        # redact it before emit() so the modal never sees it.
        install_lines=[
            "Reading package lists...",
            f"sh: 1: {PASSWORD}: not found",
            "Setting up python3...",
        ],
    )
    pi_deploy.deploy(host="pi.local", username="pi", password=PASSWORD,
                     _client_factory=factory, on_log=captured.append)

    # 1. Root-cause fix: the password is never sent over the install
    #    channel when sudo is NOPASSWD.
    sent = "".join(payloads)
    assert PASSWORD not in sent, \
        f"password leaked into install channel payload: {sent!r}"

    # 2. Defense-in-depth: even if the password DID end up in remote
    #    output, the scrubber redacts it before reaching the modal.
    blob = "\n".join(captured)
    assert PASSWORD not in blob, \
        f"password leaked into modal log: {blob!r}"
    assert "<redacted>" in blob, \
        "expected scrubber to fire on the simulated `command not found` line"


def test_deploy_with_password_sudo_feeds_password(tmp_path, monkeypatch):
    """When sudo asks for a password (NOPASSWD probe fails), the deploy
    falls back to `sudo -S -p '' sh -s` and feeds password\\n + script."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    payloads: list = []
    _, factory = _fake_client_factory(
        sudo_nopasswd=False, install_payload=payloads,
    )
    pi_deploy.deploy(host="pi.local", username="pi", password="rpi-secret",
                     _client_factory=factory)

    sent = "".join(payloads)
    # Password is the first line; script body follows.
    assert sent.startswith("rpi-secret\n")
    assert "apt-get install" in sent

    # Install command uses -S so sudo reads the password from stdin.
    chan = factory.return_value.get_transport.return_value.open_session.return_value
    install_cmd = chan.exec_command.call_args.args[0]
    assert "sudo -S" in install_cmd


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

    # Password-required path is the realistic place for an "incorrect
    # password" sudo failure (NOPASSWD wouldn't surface this).
    _, factory = _fake_client_factory(
        sudo_nopasswd=False,
        install_exit=1,
        install_lines=["sudo: 1 incorrect password attempt"],
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

    _, factory = _fake_client_factory(install_exit=0, isactive_out=b"failed\n")
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
# /api/pi/deploy route — NDJSON streaming
# ─────────────────────────────────────────────────────────────────────────────
def test_pi_deploy_route_streams_log_then_done(tmp_path, monkeypatch):
    """Successful deploy: each log line surfaces as a separate frame
    plus a final {"type": "done"} marker."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    def fake_deploy(host, username, password, port, on_log=None, **kw):
        # Verify the route forwarded the body correctly.
        assert host == "pi.local"
        assert username == "pi"
        assert password == "secret"
        assert port == 22
        if on_log is not None:
            on_log("▶ connecting")
            on_log("✓ deployment complete")
        return ["▶ connecting", "✓ deployment complete"]
    monkeypatch.setattr(pi_deploy, "deploy", fake_deploy)

    r = _client().post("/api/pi/deploy", json={
        "host": "pi.local", "username": "pi", "password": "secret",
    })
    assert r.status_code == 200
    msgs = _read_ndjson(r)
    log_lines = [m["line"] for m in msgs if m["type"] == "log"]
    assert log_lines == ["▶ connecting", "✓ deployment complete"]
    assert msgs[-1] == {"type": "done"}


def test_pi_deploy_route_streams_error_frame_on_deploy_error(tmp_path, monkeypatch):
    """A DeployError surfaces as an in-band {"type":"error"} frame, not
    HTTP 502. The status stays 200 because the response has already
    started streaming by the time we know the deploy failed — the client
    decides how to render the partial log it already has."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    def boom(*a, **kw):
        raise pi_deploy.DeployError("authentication failed — wrong password")
    monkeypatch.setattr(pi_deploy, "deploy", boom)

    r = _client().post("/api/pi/deploy", json={
        "host": "pi.local", "username": "pi", "password": "x",
    })
    assert r.status_code == 200
    msgs = _read_ndjson(r)
    assert any(
        m.get("type") == "error" and "authentication failed" in m.get("detail", "")
        for m in msgs
    )
    # No "done" frame on failure.
    assert not any(m.get("type") == "done" for m in msgs)


def test_pi_deploy_route_streams_error_frame_on_unexpected(tmp_path, monkeypatch):
    """An unexpected exception still streams a clean error frame
    (message text only — no stack trace leak)."""
    _fake_pi_source(tmp_path, monkeypatch)
    from services import pi_deploy

    def crash(*a, **kw):
        raise RuntimeError("oops")
    monkeypatch.setattr(pi_deploy, "deploy", crash)

    r = _client().post("/api/pi/deploy", json={
        "host": "pi.local", "username": "pi", "password": "x",
    })
    assert r.status_code == 200
    msgs = _read_ndjson(r)
    assert any(
        m.get("type") == "error" and "oops" in m.get("detail", "")
        for m in msgs
    )


def test_pi_deploy_route_validates_required_fields():
    """Missing host / password is a 422 from Pydantic — pre-stream
    validation still uses normal HTTP semantics."""
    r = _client().post("/api/pi/deploy", json={"username": "pi"})
    assert r.status_code == 422
