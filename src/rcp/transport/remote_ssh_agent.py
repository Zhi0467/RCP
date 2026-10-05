"""Shipped stdlib key inventory, signing checks, and account-agent preparation."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal


def _run(arguments: list[str], socket: str | None = None, *, text: str = "", timeout: float):
    environment = dict(os.environ, SSH_ASKPASS_REQUIRE="never")
    if socket is not None:
        environment["SSH_AUTH_SOCK"] = socket
    return subprocess.run(
        arguments,
        input=text,
        capture_output=True,
        text=True,
        env=environment,
        timeout=timeout,
        check=False,
    )


def agent_socket_path(home: Path) -> Path:
    return home / ".rcp" / "ssh-agent" / "agent.sock"


def agent_running(home: Path, timeout: float) -> bool:
    try:
        return _run(
            ["ssh-add", "-l"], str(agent_socket_path(home)), timeout=timeout
        ).returncode in (0, 1)
    except (OSError, subprocess.TimeoutExpired):
        return False


def _fingerprint(public: str) -> str:
    blob = base64.b64decode(public.split()[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def confirm_keys(
    *,
    private_key_paths: tuple[str, ...],
    kind: Literal["deploy_key", "ssh_identity"],
    agent_socket: str | None,
    timeout: float,
) -> tuple[dict, ...]:
    """Only a matching private/public pair, listed identity and signature may hide a key."""
    listed: set[str] = set()
    if agent_socket:
        try:
            result = _run(["ssh-add", "-L"], agent_socket, timeout=timeout)
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
                derived = _run(["ssh-keygen", "-y", "-P", "", "-f", str(path)], timeout=timeout)
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
                        timeout=timeout,
                    )
                    confirmed = signed.returncode == 0
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass
        evidence.append(
            dict(
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


def inventory(home: Path, timeout: float) -> dict:
    deploy_keys = sorted(
        str(p)
        for p in (home / ".local/share/rcp/credentials").glob("projects/*/*/id_ed25519")
        if p.is_file()
    )
    return {
        "home": str(home),
        "uid": str(os.getuid()),
        "platform": sys.platform,
        "running": agent_running(home, timeout),
        "deploy_keys": deploy_keys,
        "user_keys": tuple(
            path for path in user_ssh_identity_candidates(home) if path not in deploy_keys
        ),
    }


def prepare(home: Path, source: str) -> str:
    root = agent_socket_path(home).parent
    for path in (home / ".rcp", root):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (
            path.is_symlink()
            or not path.is_dir()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("account SSH-agent directory must be private and owned")
    # Content-addressed code survives the stage and concurrent launches never
    # rewrite a helper that a service is about to execute.
    helper = root / ("owner-" + hashlib.sha256(source.encode()).hexdigest() + ".py")
    try:
        with helper.open("x") as stream:
            stream.write(source)
    except FileExistsError:
        if helper.read_text() != source:
            raise ValueError("account SSH-agent helper changed") from None
    return str(helper)


def serve(home: Path, timeout: float) -> None:
    root = agent_socket_path(home).parent
    descriptor = os.open(root / "owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(descriptor, True)
    if agent_running(home, timeout):
        return
    agent_socket_path(home).unlink(missing_ok=True)
    os.execvp("ssh-agent", ["ssh-agent", "-D", "-a", str(agent_socket_path(home))])


def confirm(home: Path, timeout: float, wait: float, poll: float) -> tuple[dict, ...]:
    deadline = time.monotonic() + wait
    while not agent_running(home, timeout) and time.monotonic() < deadline:
        time.sleep(poll)
    facts = inventory(home, timeout)
    socket = str(agent_socket_path(home))
    if facts["running"]:
        for key in facts["deploy_keys"]:
            # The evidence below records the readable fallback.
            with suppress(OSError, subprocess.TimeoutExpired):
                _run(["ssh-add", key], socket, timeout=timeout)
    return (
        *confirm_keys(
            private_key_paths=tuple(facts["deploy_keys"]),
            kind="deploy_key",
            agent_socket=socket if facts["running"] else None,
            timeout=timeout,
        ),
        *confirm_keys(
            private_key_paths=tuple(facts["user_keys"]),
            kind="ssh_identity",
            agent_socket=os.environ.get("SSH_AUTH_SOCK"),
            timeout=timeout,
        ),
    )


def main() -> None:
    action, raw_home, raw_timeout = sys.argv[1:4]
    home, timeout = Path(raw_home), float(raw_timeout)
    if not home.is_absolute() or ".." in home.parts:
        raise ValueError("account home must be absolute")
    if action == "inventory":
        result = inventory(home, timeout)
    elif action == "prepare":
        result = prepare(home, sys.stdin.read())
    elif action == "serve":
        serve(home, timeout)
        return
    elif action == "confirm":
        result = confirm(home, timeout, float(sys.argv[4]), float(sys.argv[5]))
    else:
        raise ValueError("unknown SSH-agent operation")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
