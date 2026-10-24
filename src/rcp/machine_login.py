"""The server account's first SSH login to a new machine: its account and its host key.

Everything here runs as the account RCP runs as, over that account's own SSH
configuration, because that is the account every later connection uses.

A host key is never trusted automatically. `scan_host_key` shows the key SSH
would check, `trust_host_key` records it after a human compared its fingerprint,
and a host that already has a recorded key is never changed here: a changed key
needs a person at a terminal.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rcp.limits import COMPUTE_PROBE_TIMEOUT_SECONDS
from rcp.ssh_validation import validate_ssh_destination
from rcp.transport.remote_compute_probe import classify_ssh_failure
from rcp.transport.ssh import ssh_arguments

LoginFailureCode = Literal[
    "authentication_failed", "host_key_unknown", "host_key_changed", "unreachable"
]

# The key type SSH prefers, first; one key is shown and recorded, never a set.
_KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)


class MachineLoginFailure(Exception):
    def __init__(self, code: LoginFailureCode, fingerprint: str | None = None) -> None:
        super().__init__(code)
        self.code: LoginFailureCode = code
        self.fingerprint = fingerprint


@dataclass(frozen=True)
class HostKey:
    # The known_hosts line, under the name SSH looks the host up by.
    entry: str
    fingerprint: str
    #: Where a trusted key is added: the first user known-hosts file.
    known_hosts: Path
    name: str
    #: Every file SSH checks the host against, user and global.
    checked: tuple[Path, ...]


def _run(argv: list[str], runner, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=COMPUTE_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MachineLoginFailure("unreachable") from exc


def login_account(host: str, *, runner=subprocess.run) -> str:
    """The account `host` logs this account in as, or the kind of login failure."""
    result = _run(ssh_arguments(host, "id -un", strict_host_key_checking=True), runner)
    lines = result.stdout.split()
    if result.returncode == 0 and lines:
        return lines[-1]
    stderr = result.stderr or ""
    state = classify_ssh_failure(stderr)
    if state == "host_key_failed":
        if "remote host identification has changed" in stderr.casefold():
            raise MachineLoginFailure("host_key_changed")
        raise MachineLoginFailure(
            "host_key_unknown", scan_host_key(host, runner=runner).fingerprint
        )
    raise MachineLoginFailure(
        "authentication_failed" if state == "authentication_failed" else "unreachable"
    )


def scan_host_key(host: str, *, runner=subprocess.run) -> HostKey:
    """The key `host` offers now, named and filed as this account's SSH would check it."""
    validate_ssh_destination(host)
    config: dict[str, str] = {}
    for line in _run(["ssh", "-G", host], runner).stdout.splitlines():
        key, _, value = line.partition(" ")
        config.setdefault(key.lower(), value.strip())
    hostname, port = config.get("hostname", ""), config.get("port", "22")
    if not hostname or not port.isdigit():
        raise MachineLoginFailure("unreachable")
    name = config.get("hostkeyalias") or (hostname if port == "22" else f"[{hostname}]:{port}")
    scan = _run(["ssh-keyscan", "-T", "10", "-p", port, "--", hostname], runner)
    offered: dict[str, str] = {}
    for line in scan.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            offered.setdefault(parts[1], parts[2])
    key_type = next((kind for kind in _KEY_TYPES if kind in offered), None)
    if key_type is None:
        raise MachineLoginFailure("unreachable")
    entry = f"{name} {key_type} {offered[key_type]}"
    listed = _run(["ssh-keygen", "-l", "-f", "-"], runner, stdin=entry + "\n").stdout.split()
    fingerprint = next((part for part in listed if part.startswith("SHA256:")), None)
    if fingerprint is None:
        raise MachineLoginFailure("unreachable")
    files = config.get("userknownhostsfile", "~/.ssh/known_hosts").split()
    checked = [*files, *config.get("globalknownhostsfile", "").split()]
    return HostKey(
        entry,
        fingerprint,
        Path(files[0]).expanduser(),
        name,
        tuple(Path(file).expanduser() for file in checked),
    )


def trust_host_key(host: str, fingerprint: str, *, runner=subprocess.run) -> None:
    """Record the key the human compared; refuse a key that moved or one already recorded."""
    key = scan_host_key(host, runner=runner)
    if key.fingerprint != fingerprint:
        raise MachineLoginFailure("host_key_changed")
    for file in key.checked:
        if not file.exists():
            continue
        known = _run(["ssh-keygen", "-F", key.name, "-f", str(file)], runner)
        if known.returncode == 0:
            # A recorded key that SSH refused is a changed key: never replaced here.
            raise MachineLoginFailure("host_key_changed")
    key.known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key.known_hosts.touch(mode=0o600)
    with key.known_hosts.open("a+", encoding="utf-8") as stream:
        stream.seek(0)
        text = stream.read()
        stream.write(("\n" if text and not text.endswith("\n") else "") + key.entry + "\n")
