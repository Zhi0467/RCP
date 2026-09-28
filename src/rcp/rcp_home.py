"""The per-account RCP root, `~/.rcp`, where RCP keeps its own files.

RCP keeps nothing of its own in `/tmp` or the system temp directory, so a
human can grant those to agents like any other path.
"""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path


def rcp_home() -> Path:
    return Path.home() / ".rcp"


def private_directory(directory: Path, label: str) -> Path:
    """Create `directory` (and missing parents) mode 700 and prove it is ours."""

    for parent in reversed(directory.parents):
        if not parent.exists():
            with suppress(FileExistsError):
                parent.mkdir(mode=0o700)
    with suppress(FileExistsError):
        directory.mkdir(mode=0o700)
    try:
        info = directory.lstat()
    except OSError as exc:
        raise RuntimeError(f"RCP {label} is unavailable: {directory}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError(f"RCP {label} is unsafe: {directory}")
    return directory


def rcp_temp_dir() -> Path:
    """The `dir=` for RCP's short-lived temporary files and folders."""

    return private_directory(rcp_home() / "tmp", "temporary directory")


#: `rcp-command-<32 hex>.sock`, the only socket name the command broker binds.
_COMMAND_SOCKET_NAME_BYTES = len("rcp-command-") + 32 + len(".sock")
#: Below both sun_path sizes (104 macOS, 108 Linux) with room for the terminator.
_SOCKET_PATH_LIMIT = 100


def short_socket_root(home: str) -> str:
    """The short private folder sockets use when `~/.rcp` is too deep for them.

    Named from the account home so every side computes it without asking. The
    staged command broker and client repeat this rule; a test holds them equal.
    """

    return f"/tmp/rcp-{hashlib.sha256(home.encode('utf-8')).hexdigest()[:12]}"


def command_socket_directory(home: str) -> str:
    """Where the command broker binds, for an account whose home is `home`."""

    default = os.path.join(home, ".rcp", "sockets")
    if len(os.fsencode(default)) + 1 + _COMMAND_SOCKET_NAME_BYTES < _SOCKET_PATH_LIMIT:
        return default
    return os.path.join(short_socket_root(home), "sockets")
