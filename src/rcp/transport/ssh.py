from __future__ import annotations

import errno
import hashlib
import os
import shlex
import socket
import stat
from contextlib import suppress
from pathlib import Path

from rcp.limits import (
    SSH_CONTROL_PERSIST_SECONDS,
    SSH_CONTROL_PROBE_TIMEOUT_SECONDS,
    SSH_SERVER_ALIVE_COUNT_MAX,
    SSH_SERVER_ALIVE_INTERVAL_SECONDS,
)
from rcp.ssh_validation import validate_ssh_destination

# Callers with no work of their own to lose share one master.
SHARED_CONTROL_PARTITION = "shared"

# Options that do not require local filesystem preparation. A few strict
# provisioning paths intentionally consume these directly without multiplexing.
SSH_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    f"ServerAliveInterval={SSH_SERVER_ALIVE_INTERVAL_SECONDS}",
    "-o",
    f"ServerAliveCountMax={SSH_SERVER_ALIVE_COUNT_MAX}",
]


def ssh_arguments(
    host: str,
    command: str,
    *,
    strict_host_key_checking: bool = False,
    partition: str | None = None,
) -> list[str]:
    validate_ssh_destination(host)
    if strict_host_key_checking:
        # A pre-existing multiplexed master has already completed host-key
        # negotiation and can bypass the strict policy on this invocation.
        # OpenSSH documents ``-S none`` as disabling connection sharing.
        # An unshared connection already isolates this call more strictly than
        # any partition would, so a partition asked for here has nothing to add.
        options = [*SSH_OPTIONS, "-o", "StrictHostKeyChecking=yes", "-S", "none"]
    else:
        options = _multiplexed_ssh_options(partition)
    return ["ssh", *options, host, command]


def rsync_ssh_arguments(*, partition: str | None = None) -> list[str]:
    return ["-e", shlex.join(["ssh", *_multiplexed_ssh_options(partition)])]


def _multiplexed_ssh_options(partition: str | None = None) -> list[str]:
    control_directory = _require_control_directory()
    return [
        *SSH_OPTIONS,
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPersist={SSH_CONTROL_PERSIST_SECONDS}",
        "-o",
        f"ControlPath={control_directory}/{control_partition_token(partition)}-%C",
    ]


def control_partition_token(partition: str | None) -> str:
    """Name the master a caller shares, so one lost link ends one unit of work.

    OpenSSH derives ``%C`` from the destination alone, so without this every
    caller of one host lands on one master and dies with it. A caller that owns
    work of its own names that work here and gets its own master; the callers
    that pass nothing keep sharing one, which is what short control traffic
    wants. The name is digested because it is a socket path component: the
    identities callers hold are remote paths, and a socket path has a hard
    length ceiling well below theirs.
    """

    if partition is None:
        return SHARED_CONTROL_PARTITION
    return hashlib.sha256(partition.encode("utf-8")).hexdigest()[:12]


def sweep_control_sockets() -> None:
    """Unlink partitioned mux sockets whose master is gone.

    OpenSSH clears a dead socket only when a later connection asks for the same
    path. A partitioned path names work that never happens twice, so nothing
    ever asks again and the socket stays. The shared path is the opposite and is
    skipped for that reason: OpenSSH already heals it on the next connection,
    and sweeping a path in use is the one way to delete a socket a master bound
    between the check and the unlink.

    What is checked is whether a socket still answers, never whose it looks
    like. The directory is keyed by user account alone, so a second RCP with its
    own data directory keeps its live masters in here too, and deleting one
    would cause exactly the lost link this partitioning prevents.

    Best effort: leftover sockets are not worth failing a start over.
    """

    with suppress(OSError, RuntimeError):
        for entry in _require_control_directory().iterdir():
            if entry.name.startswith(f"{SHARED_CONTROL_PARTITION}-"):
                continue
            with suppress(OSError):
                if stat.S_ISSOCK(entry.lstat().st_mode) and not _socket_answers(entry):
                    entry.unlink()


def _socket_answers(path: Path) -> bool:
    """Whether a master is still listening. A master listens before it is named.

    A refusal is read as death, which is right except when a live master's
    accept queue is full. That costs one extra master for one run rather than a
    lost link, because unlinking a socket does not disturb the sessions already
    attached to it.
    """

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # A full accept queue refuses on macOS but blocks forever on Linux, and a
    # sweep that hangs would hold up the start it runs in.
    probe.settimeout(SSH_CONTROL_PROBE_TIMEOUT_SECONDS)
    try:
        probe.connect(os.fspath(path))
    except OSError as exc:
        # Anything other than a refusal is a question this sweep cannot answer,
        # so it leaves the socket alone.
        return exc.errno not in (errno.ECONNREFUSED, errno.ENOENT)
    finally:
        probe.close()
    return True


def _require_control_directory() -> Path:
    """Create and prove the private local owner of SSH mux sockets."""

    directory = _control_directory_path()
    with suppress(FileExistsError):
        directory.mkdir(mode=0o700)
    try:
        info = directory.lstat()
    except OSError as exc:
        raise RuntimeError("RCP SSH control directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("RCP SSH control directory is unsafe")
    return directory


# The shell's own "command not found". SSH returns the remote command's status,
# so this is what a host without the interpreter answers with.
_COMMAND_NOT_FOUND = 127


def missing_remote_interpreter(return_code: int, stderr: str) -> bool:
    """Whether this host answered that it has no `python3` to run RCP's helpers.

    The text is checked as well as the code, because a provider is free to exit
    127 for reasons of its own and must not be reported as a missing interpreter.
    """

    if return_code != _COMMAND_NOT_FOUND:
        return False
    text = stderr.casefold()
    return "python3" in text and "not found" in text


def missing_remote_interpreter_detail(host: str) -> str:
    """Say what is missing and what it costs, in the one sentence a human reads."""

    where = host or "this execution machine"
    return (
        f"{where} has no python3 on PATH. RCP runs its stage, journal, and process "
        "helpers there, so this machine cannot run agent turns until one exists. "
        "Any python3 from the last several years will do; nothing else is needed."
    )


def _control_directory_path() -> Path:
    return Path("/tmp") / f"rcp-ssh-{os.geteuid()}"
