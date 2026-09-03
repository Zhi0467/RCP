from __future__ import annotations

import os
import shlex
import stat
from contextlib import suppress
from pathlib import Path

from rcp.ssh_validation import validate_ssh_destination

# Options that do not require local filesystem preparation. A few strict
# provisioning paths intentionally consume these directly without multiplexing.
SSH_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
]


def ssh_arguments(
    host: str,
    command: str,
    *,
    strict_host_key_checking: bool = False,
) -> list[str]:
    validate_ssh_destination(host)
    if strict_host_key_checking:
        # A pre-existing multiplexed master has already completed host-key
        # negotiation and can bypass the strict policy on this invocation.
        # OpenSSH documents ``-S none`` as disabling connection sharing.
        options = [*SSH_OPTIONS, "-o", "StrictHostKeyChecking=yes", "-S", "none"]
    else:
        options = _multiplexed_ssh_options()
    return ["ssh", *options, host, command]


def rsync_ssh_arguments() -> list[str]:
    return ["-e", shlex.join(["ssh", *_multiplexed_ssh_options()])]


def _multiplexed_ssh_options() -> list[str]:
    control_directory = _require_control_directory()
    return [
        *SSH_OPTIONS,
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=60",
        "-o",
        f"ControlPath={control_directory}/%C",
    ]


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


def _control_directory_path() -> Path:
    return Path("/tmp") / f"rcp-ssh-{os.geteuid()}"
