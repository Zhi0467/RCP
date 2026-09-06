"""Exact committed checkout transfer through a local or SSH Git bundle."""

from __future__ import annotations

import importlib.resources
import os
import re
import shlex
import stat
import subprocess
import tempfile
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path

from rcp.limits import (
    PROJECT_TRANSFER_COPY_BUFFER_BYTES,
    PROJECT_TRANSFER_DIAGNOSTIC_MAX_CHARS,
    PROJECT_TRANSFER_GIT_OUTPUT_MAX_BYTES,
    PROJECT_TRANSFER_GIT_TIMEOUT_SECONDS,
    PROJECT_TRANSFER_SOURCE_PROBE_TIMEOUT_SECONDS,
)
from rcp.transport.remote_transfer_git import run_repository_transfer
from rcp.transport.ssh import ssh_arguments


@lru_cache(maxsize=1)
def _remote_source() -> str:
    return (
        importlib.resources.files("rcp.transport")
        .joinpath("remote_transfer_git.py")
        .read_text(encoding="utf-8")
    )


def probe_repository_revision(host: str, path: str) -> str:
    """Read a transferable source HEAD without changing its branch or index."""

    return _run(host, path, "probe")


def capture_repository_bundle(host: str, path: str, expected_head: str, destination: Path) -> None:
    """Write one new self-contained bundle containing only the reviewed HEAD."""

    _run(host, path, "capture", expected_head=expected_head, destination=destination)


def install_repository_bundle(host: str, path: str, bundle: Path, expected_head: str) -> None:
    """Detach a clean target at the exact HEAD, or verify an unchanged retry.

    An already matching HEAD only validates the bundle and tracked/index state;
    it preserves branch attachment and all untracked files without Git writes.
    """

    _run(host, path, "install", expected_head=expected_head, bundle=bundle)


def _run(
    host: str,
    path: str,
    operation: str,
    *,
    expected_head: str = "",
    destination: Path | None = None,
    bundle: Path | None = None,
) -> str:
    if operation != "probe" and re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        raise ValueError("repository transfer requires one full Git commit")
    timeout = (
        PROJECT_TRANSFER_SOURCE_PROBE_TIMEOUT_SECONDS
        if operation == "probe"
        else PROJECT_TRANSFER_GIT_TIMEOUT_SECONDS
    )
    created = False
    try:
        with ExitStack() as stack:
            source = stack.enter_context(open(os.devnull, "rb"))
            if bundle is not None:
                descriptor = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW)
                source = stack.enter_context(os.fdopen(descriptor, "rb"))
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("repository transfer bundle must be a regular file")
            if destination is None:
                output = stack.enter_context(tempfile.TemporaryFile())
            else:
                output = stack.enter_context(destination.open("xb"))
                created = True
                os.fchmod(output.fileno(), 0o600)
            errors = stack.enter_context(tempfile.TemporaryFile())
            if host:
                command = [
                    "python3",
                    "-c",
                    _remote_source(),
                    operation,
                    path,
                    expected_head,
                    str(timeout),
                    str(PROJECT_TRANSFER_GIT_OUTPUT_MAX_BYTES),
                    str(PROJECT_TRANSFER_COPY_BUFFER_BYTES),
                ]
                result = subprocess.run(
                    ssh_arguments(host, shlex.join(command)),
                    stdin=source,
                    stdout=output,
                    stderr=errors,
                    timeout=timeout,
                    check=False,
                )
                returncode = result.returncode
            else:
                run_repository_transfer(
                    operation,
                    path,
                    expected_head,
                    timeout,
                    PROJECT_TRANSFER_GIT_OUTPUT_MAX_BYTES,
                    PROJECT_TRANSFER_COPY_BUFFER_BYTES,
                    source,
                    output,
                )
                returncode = 0
            errors.seek(0)
            diagnostic = errors.read(PROJECT_TRANSFER_GIT_OUTPUT_MAX_BYTES + 1)
            if returncode != 0:
                # The shipped helper emits only its own nonsecret errors; SSH
                # failures do not get to become an unbounded server diagnostic.
                detail = diagnostic.decode("utf-8", errors="replace").strip()
                if returncode != 3 or len(diagnostic) > PROJECT_TRANSFER_DIAGNOSTIC_MAX_CHARS:
                    detail = "repository Git transfer could not complete"
                raise ValueError(detail or "repository Git transfer could not complete")
            if diagnostic:
                raise ValueError("repository Git transfer returned unexpected diagnostics")
            if destination is not None:
                output.flush()
                os.fsync(output.fileno())
                if os.fstat(output.fileno()).st_size == 0:
                    raise ValueError("repository Git transfer returned an empty bundle")
                return ""
            output.seek(0)
            receipt = output.read(42)
            if re.fullmatch(rb"[0-9a-f]{40}\n", receipt) is None:
                raise ValueError("repository Git transfer returned an invalid revision")
            head = receipt[:-1].decode("ascii")
            if operation == "install" and head != expected_head:
                raise ValueError("repository Git transfer did not install the reviewed revision")
            return head
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        if created and destination is not None:
            destination.unlink(missing_ok=True)
        raise ValueError("repository Git transfer is unavailable or timed out") from exc
    except BaseException:
        if created and destination is not None:
            destination.unlink(missing_ok=True)
        raise
