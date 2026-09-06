"""Stdlib ownership checks shared by RCP and a helper job's shell watcher."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def owner_command(backend: str, handle: str, uid: str, *, cancel: bool = False) -> list[str]:
    if backend == "systemd_user":
        arguments = ["stop", handle] if cancel else ["show", "-p", "ActiveState", handle]
        return ["env", f"XDG_RUNTIME_DIR=/run/user/{uid}", "systemctl", "--user", *arguments]
    raise ValueError(f"Unsupported process owner: {backend}")


def owner_alive(backend: str, result: subprocess.CompletedProcess[str]) -> bool | None:
    if backend == "systemd_user":
        if result.returncode:
            return False if "could not be found" in result.stderr.casefold() else None
        state = result.stdout.strip().removeprefix("ActiveState=")
        if state in {"active", "activating", "reloading", "deactivating", "refreshing"}:
            return True
        return False if state in {"inactive", "failed"} else None
    raise ValueError(f"Unsupported process owner: {backend}")


def require_cancel_success(backend: str, result: subprocess.CompletedProcess[str]) -> None:
    absent = {
        "systemd_user": ("not loaded", "could not be found", "does not exist"),
    }[backend]
    if result.returncode and not any(marker in result.stderr.casefold() for marker in absent):
        raise RuntimeError(result.stderr or f"{backend} cancellation failed")


def observe_job(root: Path, backend: str, handle: str, *, timeout: float) -> int:
    result = subprocess.run(
        owner_command(backend, handle, str(os.getuid())),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    alive = owner_alive(backend, result)
    if alive is None:
        raise RuntimeError(result.stderr or "The process owner could not report job state.")
    if alive:
        return 1
    if (root / "cancelled").is_file():
        return 0
    try:
        status, ended = (root / "exit").read_text().split()
        if not 0 <= int(status) <= 255 or int(ended) < 0:
            raise ValueError("Invalid exit receipt")
    except (OSError, ValueError) as exc:
        raise RuntimeError("The job disappeared without a valid exit receipt.") from exc
    return 0


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 5 or argv[1] not in {"check", "cancel"}:
            raise ValueError("Expected action, backend, handle and an operation timeout.")
        timeout = float(argv[4])
        if timeout <= 0:
            raise ValueError("Timeout must be positive.")
        root = Path(argv[0]).resolve().parent
        backend, handle = argv[2:4]
        if argv[1] == "check":
            return observe_job(root, backend, handle, timeout=timeout)
        result = subprocess.run(
            owner_command(backend, handle, str(os.getuid()), cancel=True),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        require_cancel_success(backend, result)
        temporary = root / ".cancelled.tmp"
        temporary.write_text(str(int(time.time())))
        os.replace(temporary, root / "cancelled")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a shipped script
    raise SystemExit(main(sys.argv))
