"""The per-account RCP root, `~/.rcp`, where RCP keeps its own files.

RCP keeps nothing of its own in `/tmp` or the system temp directory, so a
human can grant those to agents like any other path.
"""

from __future__ import annotations

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
