"""Account-owned deploy-key agent and conservative launch-host key evidence."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import logging
import os
import pwd
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal

from rcp.core.models import HiddenReadKeyEvidence
from rcp.limits import (
    AGENT_COMMAND_TIMEOUT_SECONDS,
    AGENT_POLL_SECONDS,
    AGENT_SOCKET_PATH_MAX_BYTES,
    AGENT_WORKER_DRAIN_POLL_SECONDS,
)
from rcp.rcp_home import private_directory, rcp_home
from rcp.server_ops.remote_git_credentials import _public_material

logger = logging.getLogger(__name__)


def agent_socket_path(home: Path | None = None) -> Path:
    return (home / ".rcp" if home is not None else rcp_home()) / "ssh-agent" / "agent.sock"


def _run(arguments: list[str], socket: str | None = None, *, text: str = ""):
    environment = dict(os.environ, SSH_ASKPASS_REQUIRE="never")
    if socket is not None:
        environment["SSH_AUTH_SOCK"] = socket
    return subprocess.run(
        arguments,
        input=text,
        capture_output=True,
        text=True,
        env=environment,
        timeout=AGENT_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )


def agent_status(home: Path | None = None) -> str:
    try:
        result = _run(["ssh-add", "-l"], str(agent_socket_path(home)))
        return "running" if result.returncode in (0, 1) else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def running_agent_socket() -> str | None:
    return str(agent_socket_path()) if agent_status() == "running" else None


def _fingerprint(public: str) -> str:
    blob = base64.b64decode(public.split()[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def confirm_key_evidence(
    *,
    private_key_paths: tuple[str, ...],
    kind: Literal["deploy_key", "ssh_identity"],
    agent_socket: str | None,
) -> tuple[HiddenReadKeyEvidence, ...]:
    """Only a matching private/public pair, listed identity and signature may hide a key."""
    listed: set[str] = set()
    if agent_socket:
        try:
            result = _run(["ssh-add", "-L"], agent_socket)
            if result.returncode == 0:
                listed = {_fingerprint(line) for line in result.stdout.splitlines()}
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass
    evidence = []
    for name in dict.fromkeys(private_key_paths):
        path = Path(name).expanduser().absolute()
        fingerprint = None
        confirmed = False
        try:
            public = Path(str(path) + ".pub").read_text()
            fingerprint = _fingerprint(public)
            if fingerprint in listed:
                # This also detects a stale adjacent public key. Encrypted keys that
                # cannot be checked noninteractively conservatively stay readable.
                derived = _run(["ssh-keygen", "-y", "-P", "", "-f", str(path)])
                if derived.returncode == 0 and _fingerprint(derived.stdout) == fingerprint:
                    signed = _run(
                        [
                            "ssh-keygen",
                            "-Y",
                            "sign",
                            "-U",
                            "-f",
                            str(path) + ".pub",
                            "-n",
                            "rcp-hidden-read",
                        ],
                        agent_socket,
                        text="RCP identity probe\n",
                    )
                    confirmed = signed.returncode == 0
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass
        evidence.append(
            HiddenReadKeyEvidence(
                path=str(path),
                kind=kind,
                public_key_fingerprint=fingerprint,
                agent_confirmed=confirmed,
                visibility="hidden" if confirmed else "readable",
            )
        )
    return tuple(evidence)


def user_ssh_identity_candidates(home: Path) -> tuple[str, ...]:
    home = Path(home)
    root = home / ".ssh"
    candidates = set(root.glob("id_*"))
    try:
        for line in (root / "config").read_text().splitlines():
            parts = shlex.split(line.replace("=", " ", 1), comments=True)
            if len(parts) == 2 and parts[0].lower() == "identityfile":
                name = parts[1].replace("%d", str(home))
                if name.startswith("~/"):
                    name = str(home / name[2:])
                if "%" not in name and name.lower() != "none":
                    path = Path(name)
                    candidates.add(path if path.is_absolute() else home / path)
    except (OSError, ValueError):
        pass
    return tuple(
        sorted(
            str(path.absolute())
            for path in candidates
            if path.is_file()
            and not path.name.endswith(".pub")
            and path.name not in {"config", "known_hosts"}
        )
    )


class BackendSSHAgent:
    """One advisory-lock owner; never changes the user's SSH_AUTH_SOCK."""

    def __init__(self, credentials_root: Path, *, home: Path | None = None):
        self.home = home or Path.home()
        self.credentials_root = credentials_root
        self.socket = agent_socket_path(self.home)
        self._lock: int | None = None
        self._pid: int | None = None
        self._process: subprocess.Popen | None = None
        self._lifecycle_lock = threading.RLock()

    def _process_identity(self, pid: int) -> str | None:
        try:
            with socket.socket(socket.AF_UNIX) as peer:
                peer.settimeout(AGENT_COMMAND_TIMEOUT_SECONDS)
                peer.connect(str(self.socket))
                if sys.platform == "darwin":
                    # Darwin LOCAL_PEERPID; the agent binds in foreground mode.
                    peer_pid = peer.getsockopt(0, 2)
                elif sys.platform == "linux":
                    peer_pid, uid, _ = struct.unpack(
                        "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    )
                    if uid != os.getuid():
                        return None
                else:
                    return None
            info = self.socket.stat()
            if peer_pid != pid or info.st_uid != os.getuid():
                return None
            if agent_status(self.home) != "running":
                return None
            return f"{pid}:{info.st_dev}:{info.st_ino}:{info.st_uid}"
        except OSError:
            return None

    def _wait_for_socket_removal(self) -> None:
        deadline = time.monotonic() + AGENT_COMMAND_TIMEOUT_SECONDS
        while self.socket.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("previous SSH agent did not release its socket")
            time.sleep(AGENT_POLL_SECONDS)

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start()

    def _start(self) -> None:
        if self._lock is not None:
            return
        try:
            private_directory(self.socket.parent, "SSH agent directory")
            # Never use /tmp's short_socket_root fallback: PrivateTmp would make
            # the same name refer to a different socket outside the service.
            if len(os.fsencode(self.socket)) >= AGENT_SOCKET_PATH_MAX_BYTES:
                raise RuntimeError("account SSH agent socket path is too long")
            self._lock = os.open(self.socket.parent / "owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = self.socket.parent / "owner.json"
            if receipt.exists():
                old = json.loads(receipt.read_text())
                identity = self._process_identity(old["pid"])
                if identity is not None:
                    if identity != old["identity"]:
                        raise RuntimeError("SSH agent orphan identity changed")
                    os.kill(old["pid"], signal.SIGTERM)
                    self._wait_for_socket_removal()
                elif agent_status(self.home) == "running":
                    raise RuntimeError("SSH agent orphan cannot be verified")
            elif agent_status(self.home) == "running":
                raise RuntimeError("SSH agent socket has no ownership receipt")
            self.socket.unlink(missing_ok=True)
            self._process = subprocess.Popen(
                ["ssh-agent", "-D", "-a", str(self.socket)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._pid = self._process.pid
            deadline = time.monotonic() + AGENT_COMMAND_TIMEOUT_SECONDS
            identity = None
            while self._process.poll() is None:
                identity = self._process_identity(self._pid)
                if identity is not None:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(AGENT_POLL_SECONDS)
            if identity is None:
                raise RuntimeError("SSH agent process identity cannot be verified")
            temporary = receipt.with_suffix(".tmp")
            temporary.write_text(json.dumps({"pid": self._pid, "identity": identity}))
            os.replace(temporary, receipt)
            self.load_keys()
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            subprocess.TimeoutExpired,
        ) as exc:
            logger.warning("Deploy keys remain readable: backend SSH agent unavailable (%s)", exc)
            self.stop()

    def load_keys(self) -> None:
        for private in self.credentials_root.glob("projects/*/*/id_ed25519"):
            try:
                _public_material(
                    pwd.getpwuid(os.getuid()),
                    self.home,
                    self.credentials_root,
                    private,
                    Path(str(private) + ".pub"),
                    created=False,
                )
                if _run(["ssh-add", str(private)], str(self.socket)).returncode:
                    raise ValueError("agent refused deploy key")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                logger.warning("Deploy key remains readable: could not load %s", private)

    def stop_after_workers_drained(self, is_idle: Callable[[], bool]) -> None:
        if is_idle():
            self.stop()
            return
        # Shutdown has a bounded join. Preserve signing for remaining workers;
        # if the backend exits first, the next owner verifies this orphan.
        logger.warning("Keeping the SSH agent available until provider workers drain.")

        def finish() -> None:
            while not is_idle():
                time.sleep(AGENT_WORKER_DRAIN_POLL_SECONDS)
            self.stop()

        threading.Thread(target=finish, name="rcp-ssh-agent-drain", daemon=True).start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop()

    def _stop(self) -> None:
        if self._process is not None:
            with suppress(ProcessLookupError):
                self._process.terminate()
            try:
                self._process.wait(timeout=AGENT_COMMAND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None
        self._pid = None
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None
