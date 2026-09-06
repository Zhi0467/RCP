"""Resolve the fixed root toolchain without trusting service-account PATH entries."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import ROOT_EXECUTABLE_PATH


def root_executable(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise SupervisorError("A root tool must be named within the fixed executable path.")
    located = shutil.which(name, path=ROOT_EXECUTABLE_PATH)
    if located is None:
        raise SupervisorError(f"Required root-owned executable {name} is unavailable.")
    try:
        executable = Path(located).resolve(strict=True)
        for item in (executable, *executable.parents):
            info = item.lstat()
            expected = (
                stat.S_ISREG(info.st_mode) if item == executable else stat.S_ISDIR(info.st_mode)
            )
            if not expected or info.st_uid != 0 or info.st_mode & 0o022:
                raise SupervisorError(f"Executable {name} has an unsafe owner or path.")
    except SupervisorError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SupervisorError(f"Executable {name} cannot be safely resolved.") from exc
    return str(executable)
