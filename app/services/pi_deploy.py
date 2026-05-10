"""SSH-based deployment of the Pi capture service to a Raspberry Pi.

Replaces the manual `scp + ssh + systemd ceremony` in README.md ("Install
on the Pi") with a single POST. Targets a fresh Raspberry Pi OS install
where the only prerequisites are Python 3 and `alsa-utils` (both already
present), matching the README's stated preconditions.

Stays consistent with the project's "trusted single-user LAN" trust model:
no auth on the recorder itself, password is supplied per-deploy by the user
and never persisted.
"""
from __future__ import annotations

import logging
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    # Type-only import — no runtime cost when the test/import path doesn't
    # actually invoke deploy(). The real `import paramiko` lives inside
    # deploy() so a sibling test module that just imports `main` (which
    # imports this route module transitively) doesn't need paramiko on
    # the path. Runtime callers — the route + the explicit unit tests —
    # always have it because the runtime image installs it.
    import paramiko  # noqa: F401

log = logging.getLogger(__name__)


def _resolve_pi_source_dir() -> Path:
    """Find the directory containing server.py + pi-recorder.service.

    Two layouts are supported:
    - Container runtime: `/pi/` (Dockerfile copies the repo's pi/ here).
    - Repo / tests:      `<repo>/pi/`, two levels up from this module.
    """
    container_dir = Path("/pi")
    if (container_dir / "server.py").is_file():
        return container_dir
    return Path(__file__).resolve().parent.parent.parent / "pi"


PI_SOURCE_DIR = _resolve_pi_source_dir()


# Single sudo pipeline. Each step runs inside one `sh -c` so a failure
# aborts the rest (set -e). The escaped single quotes inside the heredoc
# would be a footgun; we keep the script in a triple-quoted Python string
# and feed it via stdin to `sh -s --` instead. Keeping it as a
# bash-on-stdin invocation also makes it trivial to add NEW steps later
# without re-quoting.
#
# `apt-get install` of python3 + alsa-utils is idempotent — both ship on
# a fresh Raspberry Pi OS install, so the apt step is a near-no-op there
# (a few seconds for the index update). Including it unconditionally lets
# the deploy bootstrap from a stripped-down Pi OS Lite image (or any
# Debian-derivative) without the user having to remember a manual prep
# step. DEBIAN_FRONTEND + -qq keep the log compact and the run hands-free.
_INSTALL_SCRIPT = """\
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 alsa-utils
mkdir -p /opt/pi-recorder
mv /tmp/server.py /opt/pi-recorder/server.py
mv /tmp/pi-recorder.service /etc/systemd/system/pi-recorder.service
systemctl daemon-reload
systemctl enable --now pi-recorder
"""


class DeployError(RuntimeError):
    """Raised for any non-success deployment outcome.

    Subclassed by the route layer to map to a 4xx vs 5xx response — but
    for the SSH path we collapse everything to "deploy failed" with a
    user-readable message; the categorical distinction (auth vs network
    vs sudo) lives only in the message text.
    """


def deploy(
    host: str,
    username: str,
    password: str,
    port: int = 22,
    on_log: Optional[Callable[[str], None]] = None,
    *,
    connect_timeout: float = 10.0,
    command_timeout: float = 60.0,
    _client_factory: Optional[Callable[[], paramiko.SSHClient]] = None,
) -> list[str]:
    """Connect, copy files, install systemd unit, enable + start.

    Returns the list of human-readable progress lines (also fed to `on_log`
    in real time). Raises `DeployError` with a friendly message on failure.

    `_client_factory` is a test seam — production callers leave it None and
    get a default `paramiko.SSHClient`.
    """
    # Lazy: keeps the import chain that fires from `from main import app`
    # paramiko-free, so tests that just want a TestClient (i.e. the great
    # majority of the suite) don't need paramiko installed.
    import paramiko

    lines: list[str] = []

    def emit(msg: str) -> None:
        lines.append(msg)
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                # The progress callback must never break the deploy.
                pass

    server_py = PI_SOURCE_DIR / "server.py"
    service_unit = PI_SOURCE_DIR / "pi-recorder.service"
    if not server_py.is_file() or not service_unit.is_file():
        raise DeployError(
            f"pi source files not found in {PI_SOURCE_DIR} "
            f"(expected server.py + pi-recorder.service)"
        )

    host = host.strip()
    username = username.strip()
    if not host:
        raise DeployError("host cannot be empty")
    if not username:
        raise DeployError("username cannot be empty")
    if not (1 <= port <= 65535):
        raise DeployError(f"invalid port {port}")

    emit(f"connecting to {username}@{host}:{port}…")

    client = (_client_factory or paramiko.SSHClient)()
    # AutoAdd matches the trust model: a single-user LAN where the user
    # already trusts the box they just typed the IP of. Not appropriate
    # for hostile networks — neither is the rest of this app.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=connect_timeout,
                auth_timeout=connect_timeout,
                banner_timeout=connect_timeout,
                # Force password auth: don't surprise the user by silently
                # logging in with an unrelated agent key on the recorder
                # host (which may not even have one) and don't fail when
                # the agent socket isn't forwarded.
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.AuthenticationException:
            raise DeployError(
                "authentication failed — wrong username or password"
            )
        except paramiko.SSHException as e:
            raise DeployError(f"SSH error: {e}")
        except socket.gaierror as e:
            raise DeployError(f"cannot resolve host '{host}': {e}")
        except (ConnectionRefusedError, socket.timeout, TimeoutError) as e:
            raise DeployError(f"cannot reach {host}:{port} — {e}")
        except OSError as e:
            raise DeployError(f"network error: {e}")

        emit("✓ connected")

        # ── 1. SFTP upload both files into /tmp (writable without sudo) ──
        emit("uploading server.py + pi-recorder.service to /tmp…")
        try:
            sftp = client.open_sftp()
        except Exception as e:
            raise DeployError(f"could not open SFTP channel: {e}")
        try:
            try:
                sftp.put(str(server_py), "/tmp/server.py")
                sftp.put(str(service_unit), "/tmp/pi-recorder.service")
            except Exception as e:
                raise DeployError(f"file upload failed: {e}")
        finally:
            try: sftp.close()
            except Exception: pass
        emit("✓ uploaded")

        # ── 2. Run the install script under sudo ─────────────────────────
        # `sudo -S -p ''` reads the password from stdin and suppresses the
        # prompt that would otherwise show up in stderr. `sh -s` reads the
        # script body from stdin too — paramiko's stdin is multiplexed for
        # both, so we send password\n followed by the script. Default
        # Raspberry Pi OS configures NOPASSWD for the `pi` user (sudo
        # ignores extra stdin in that case), so this works either way.
        cmd = "sudo -S -p '' sh -s"
        emit("running install (apt-get install python3 alsa-utils / mkdir / mv / systemctl)…")
        try:
            stdin, stdout, stderr = client.exec_command(
                cmd, get_pty=False, timeout=command_timeout,
            )
        except Exception as e:
            raise DeployError(f"could not start remote command: {e}")

        try:
            stdin.write(password + "\n")
            stdin.write(_INSTALL_SCRIPT)
            stdin.flush()
            try: stdin.channel.shutdown_write()
            except Exception: pass
        except Exception as e:
            raise DeployError(f"failed sending command: {e}")

        # Wait + collect. exec_command returns immediately; recv_exit_status
        # blocks until the remote shell finishes.
        rc = stdout.channel.recv_exit_status()
        out = _decode(stdout.read())
        err = _decode(stderr.read())

        for line in out.splitlines():
            if line.strip():
                emit(line.rstrip())
        for line in err.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Filter sudo's own prompt artifact in case the shell echoed it
            # despite -p '' (some sudo builds still emit "Password:" once).
            low = stripped.lower()
            if low.startswith("password") or low.startswith("[sudo]"):
                continue
            emit(stripped)

        if rc != 0:
            # Pick a useful error tail. sudo's "incorrect password" message
            # ends up here — surface it explicitly so the user knows it's a
            # sudo-password problem, not an SSH-password problem.
            tail = (err or out).strip().splitlines()
            msg = tail[-1] if tail else f"install commands exited {rc}"
            if "incorrect password" in msg.lower():
                msg = "sudo rejected the password (this is the SSH password; on default Pi OS sudo is NOPASSWD for the 'pi' user)"
            raise DeployError(msg)

        emit("✓ installed and enabled pi-recorder.service")

        # ── 3. systemctl is-active sanity check ──────────────────────────
        try:
            _, stdout, _ = client.exec_command(
                "systemctl is-active pi-recorder",
                get_pty=False, timeout=10,
            )
            active = _decode(stdout.read()).strip()
        except Exception:
            active = "unknown"
        emit(f"service is {active}")
        if active != "active":
            raise DeployError(
                f"pi-recorder service is '{active}', expected 'active' — "
                f"check `systemctl status pi-recorder` on the Pi"
            )

        emit("✓ deployment complete")
        return lines
    finally:
        try: client.close()
        except Exception: pass


def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")
