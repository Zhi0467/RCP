"""State-only transfer negotiation and verified tar snapshots."""

from __future__ import annotations

import contextvars
import filecmp
import importlib.resources
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from rcp.limits import (
    STATE_TRANSFER_ATTEMPTS,
    STATE_TRANSFER_PROBE_TIMEOUT_SECONDS,
    STATE_TRANSFER_RETRY_INITIAL_SECONDS,
    STATE_TRANSFER_RETRY_MAX_SECONDS,
    STATE_TRANSFER_STDERR_BYTES,
    STATE_TRANSFER_TIMEOUT_SECONDS,
)
from rcp.transport.ssh import ssh_arguments

_LOG = logging.getLogger(__name__)
_CACHE: dict[str, TransferEngine] = {}
_WARNED: set[str] = set()
_LOCK = threading.RLock()
_HOST_LOCKS: dict[str, threading.Lock] = {}
_WARNING = contextvars.ContextVar[Callable[[str], None] | None](
    "state_transfer_warning", default=None
)


@dataclass(frozen=True)
class TransferEngine:
    engine: str
    local_path: str | None
    local_version: str
    remote_version: str

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


# Parse the actual transfer options before --version, without starting a transfer.
_RSYNC_PROBE_OPTIONS = (
    "-a",
    "--delete",
    "--exclude=.rcp-contract-probe",
    "-R",
    "-e",
    "ssh",
    "--version",
)


def parse_version(output: str) -> bool:
    """Accept GNU rsync and openrsync advertising protocol 29 or newer."""
    match = re.search(
        r"^(?:rsync\s+version\s+\d+\.\d+\.\d+[^\n]*?|openrsync:)\s*protocol version\s+(\d+)",
        output,
        re.MULTILINE,
    )
    return bool(match and int(match[1]) >= 29)


def _probe(argv: list[str], *, remote: bool = False) -> tuple[bool, str]:
    # Lazy import: run_stage owns the existing SSH failure classification and
    # imports the state workspace, which in turn imports this transfer owner.
    from rcp.transport.run_stage import _ssh_failure, _SshNoVerdict

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=STATE_TRANSFER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _ssh_failure(
            _SshNoVerdict(argv, 255, "", str(exc)), "rsync probe gave no verdict"
        ) from exc
    if remote and result.returncode == 255:
        raise _ssh_failure(result, "rsync probe host is unreachable")
    output = result.stdout.strip() or result.stderr.strip()
    return result.returncode == 0 and parse_version(output), output.split("\n", 1)[
        0
    ] or "no version reported"


def _local_candidate() -> tuple[str | None, str]:
    seen: list[str] = []
    for directory in os.get_exec_path():
        candidate = os.path.abspath(os.path.join(directory, "rsync"))
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        passed, version = _probe([candidate, *_RSYNC_PROBE_OPTIONS])
        if passed:
            return candidate, version
        seen.append(f"{candidate}: {version}")
    return None, "; ".join(seen) or "rsync not found on PATH"


@contextmanager
def warning_context(callback: Callable[[str], None]) -> Iterator[None]:
    token = _WARNING.set(callback)
    try:
        yield
    finally:
        _WARNING.reset(token)


def get_engine(host: str) -> TransferEngine:
    with _LOCK:
        cached = _CACHE.get(host)
        if cached is not None:
            return cached
        host_lock = _HOST_LOCKS.setdefault(host, threading.Lock())
    # Probe under a per-host lock, so a hung host never stalls another host.
    with host_lock:
        with _LOCK:
            cached = _CACHE.get(host)
        if cached is not None:
            return cached
        local_path, local_version = _local_candidate()
        remote_ok, remote_version = _probe(
            ssh_arguments(host, shlex.join(["rsync", *_RSYNC_PROBE_OPTIONS])), remote=True
        )
        engine = TransferEngine(
            "rsync" if local_path and remote_ok else "tar",
            local_path,
            local_version,
            remote_version,
        )
        with _LOCK:
            _CACHE[host] = engine
            warn = engine.engine == "tar" and host not in _WARNED
            if warn:
                _WARNED.add(host)
        if warn:
            failed = []
            if not local_path:
                failed.append(f"backend ({local_version})")
            if not remote_ok:
                failed.append(f"execution host ({remote_version})")
            message = f"State transfers for {host} use tar over SSH: {'; '.join(failed)} failed the rsync contract. Install rsync with protocol >= 29 and support for -a, --delete, --exclude, -R and -e ssh on the failed end(s)."
            _LOG.warning(message)
            callback = _WARNING.get()
            if callback:
                try:
                    callback(message)
                except Exception:
                    _LOG.exception("Could not record state transfer warning in task events")
        return engine


def diagnostics(host: str) -> dict[str, str | None] | None:
    with _LOCK:
        engine = _CACHE.get(host)
        return engine.as_dict() if engine else None


def transfer_result(
    host: str, result: subprocess.CompletedProcess[str]
) -> subprocess.CompletedProcess[str]:
    if (
        result.returncode == 127
        or "protocol" in (result.stderr or "").lower()
        and any(
            word in (result.stderr or "").lower()
            for word in ("mismatch", "incompatib", "not match")
        )
    ):
        # Only forget the verdict: the next transfer probes again. Probing here
        # could raise a transport error that hides this transfer's own result.
        with _LOCK:
            _CACHE.pop(host, None)
    return result


def _remote_arguments(
    host: str, operation: str, root: str | Path, excludes: Sequence[str] = ()
) -> list[str]:
    source = (
        importlib.resources.files("rcp.transport")
        .joinpath("remote_state_transfer.py")
        .read_text(encoding="utf-8")
    )
    return ssh_arguments(
        host,
        shlex.join(["python3", "-c", source, operation, str(root), json.dumps(list(excludes))]),
    )


def _failure(argv: list[str], error: Exception) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        argv, 127 if isinstance(error, FileNotFoundError) else 1, "", str(error)
    )


def _contains_excluded(root: Path, excludes: set[str]) -> bool:
    if not root.is_dir() or root.is_symlink():
        return False
    return any(
        entry.name in excludes or _contains_excluded(entry, excludes) for entry in root.iterdir()
    )


def _check_tree_conflicts(staged: Path, target: Path, excludes: set[str]) -> None:
    for source in staged.iterdir():
        destination = target / source.name
        source_directory = source.is_dir() and not source.is_symlink()
        destination_directory = destination.is_dir() and not destination.is_symlink()
        if not source_directory and destination_directory:
            if _contains_excluded(destination, excludes):
                raise ValueError("remote entry conflicts with a preserved local exclusion")
        elif source_directory and destination_directory:
            _check_tree_conflicts(source, destination, excludes)


def _publish_tree_entries(staged: Path, target: Path) -> None:
    for source in sorted(staged.iterdir()):
        destination = target / source.name
        if source.is_dir() and not source.is_symlink():
            if destination.is_symlink() or destination.exists() and not destination.is_dir():
                destination.unlink()
            destination.mkdir(exist_ok=True)
            _publish_tree_entries(source, destination)
            continue
        if source.is_symlink() and destination.is_symlink():
            if os.readlink(source) == os.readlink(destination):
                continue
        elif source.is_file() and destination.is_file() and not destination.is_symlink():
            source_stat = source.stat()
            destination_stat = destination.stat()
            if (
                source_stat.st_mode == destination_stat.st_mode
                and source_stat.st_mtime_ns == destination_stat.st_mtime_ns
                and filecmp.cmp(source, destination, shallow=False)
            ):
                continue
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{source.name}-", dir=target)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            if source.is_symlink():
                temporary.unlink()
                temporary.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, temporary)
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
        finally:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()


def _delete_obsolete(target: Path, staged: Path | None, excludes: set[str]) -> None:
    for destination in target.iterdir():
        if destination.name in excludes:
            continue
        source = staged / destination.name if staged is not None else None
        exists_remotely = source is not None and (source.exists() or source.is_symlink())
        if destination.is_dir() and not destination.is_symlink():
            _delete_obsolete(destination, source if exists_remotely else None, excludes)
            if not exists_remotely and not any(destination.iterdir()):
                destination.rmdir()
            elif exists_remotely:
                shutil.copystat(source, destination)
        elif not exists_remotely:
            destination.unlink()


def _apply_tree(staged: Path, target: Path, excludes: set[str]) -> None:
    """Publish verified entries atomically, keeping the root and excluded inodes.

    A failure can leave complete old and new files together. Repeating the pull
    converges; deletion starts only after every incoming entry was published.
    """
    if target.is_symlink():
        raise ValueError("state mirror root must not be a symlink")
    target.mkdir(exist_ok=True)
    _check_tree_conflicts(staged, target, excludes)
    _publish_tree_entries(staged, target)
    _delete_obsolete(target, staged, excludes)
    shutil.copystat(staged, target)


def pull_tar(
    host: str, remote_root: str | Path, local_root: Path, excludes: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    return _retrying(host, "pull", lambda: _pull_tar_once(host, remote_root, local_root, excludes))


def _pull_tar_once(
    host: str, remote_root: str | Path, local_root: Path, excludes: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    argv = _remote_arguments(host, "pull", remote_root, excludes)
    local_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{local_root.name}-transfer-", dir=local_root.parent
        ) as temporary:
            working = Path(temporary)
            staged = working / "next"
            staged.mkdir()
            with (working / "snapshot.tar").open("w+b") as stream:
                result = subprocess.run(
                    argv,
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=STATE_TRANSFER_TIMEOUT_SECONDS,
                )
                stderr = result.stderr.decode("utf-8", errors="replace")
                if result.returncode:
                    return transfer_result(
                        host, subprocess.CompletedProcess(argv, result.returncode, "", stderr)
                    )
                stream.seek(0)
                with tarfile.open(fileobj=stream, mode="r:") as archive:
                    all_members = archive.getmembers()
                    stream.seek(archive.offset)
                    if stream.read(1024) != bytes(1024):
                        raise tarfile.ReadError("state archive is missing its complete end marker")
                    members = [
                        member
                        for member in all_members
                        if not set(Path(member.name).parts) & set(excludes)
                    ]
                    archive.extractall(staged, members=members, filter=_state_tar_filter)
            _apply_tree(staged, local_root, set(excludes))
        return subprocess.CompletedProcess(argv, 0, "", stderr)
    except (OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        return transfer_result(host, _failure(argv, exc))


def push_tar(
    host: str, remote_stage: str | Path, local_root: Path, relative_paths: Sequence[str | Path]
) -> subprocess.CompletedProcess[str]:
    return _retrying(
        host, "push", lambda: _push_tar_once(host, remote_stage, local_root, relative_paths)
    )


def _push_tar_once(
    host: str, remote_stage: str | Path, local_root: Path, relative_paths: Sequence[str | Path]
) -> subprocess.CompletedProcess[str]:
    argv = _remote_arguments(host, "push", remote_stage)
    try:
        with tempfile.TemporaryFile() as stream:
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for relative in relative_paths:
                    path = Path(relative)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("state publication requires relative paths")
                    archive.add(local_root / path, arcname=path.as_posix(), recursive=False)
            stream.seek(0)
            result = subprocess.run(
                argv,
                stdin=stream,
                capture_output=True,
                check=False,
                timeout=STATE_TRANSFER_TIMEOUT_SECONDS,
            )
            return transfer_result(
                host,
                subprocess.CompletedProcess(
                    argv,
                    result.returncode,
                    result.stdout.decode("utf-8", errors="replace"),
                    result.stderr.decode("utf-8", errors="replace"),
                ),
            )
    except (OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        return transfer_result(host, _failure(argv, exc))


def run_rsync(
    host: str, arguments: list[str], *, phase: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return _retrying(host, phase, lambda: _run_rsync_once(host, arguments, cwd))


def _run_rsync_once(
    host: str, arguments: list[str], cwd: Path | None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=STATE_TRANSFER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = _failure(arguments, exc)
    transfer_result(host, result)
    return result


# rsync exit codes for a stream or connection that died, not a refused transfer:
# 10 socket I/O, 12 protocol data stream, 20 signal, 30 data timeout,
# 35 daemon connection timeout; 255 is ssh losing the connection.
_TRANSIENT_EXIT_CODES = frozenset({10, 12, 20, 30, 35, 255})
_TRANSIENT_STDERR = re.compile(
    "|".join(
        (
            "unexpected end of file",
            "connection reset",
            "connection closed",
            "broken pipe",
            "message authentication code incorrect",
            "kex_exchange_identification",
            "timed out",
        )
    ),
    re.IGNORECASE,
)


def _transient(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0 or result.returncode == 127:
        return False
    return result.returncode in _TRANSIENT_EXIT_CODES or bool(
        _TRANSIENT_STDERR.search(result.stderr or "")
    )


def _retrying(
    host: str, phase: str, attempt: Callable[[], subprocess.CompletedProcess[str]]
) -> subprocess.CompletedProcess[str]:
    """Rerun one whole transfer after a dropped stream; every attempt is idempotent."""
    delay = STATE_TRANSFER_RETRY_INITIAL_SECONDS
    failures: list[str] = []
    for number in range(1, STATE_TRANSFER_ATTEMPTS + 1):
        result = attempt()
        stderr = (result.stderr or "").strip()[-STATE_TRANSFER_STDERR_BYTES:]
        if result.returncode:
            failures.append(f"attempt {number} exit {result.returncode}: {stderr}")
        if not _transient(result):
            break
        if number == STATE_TRANSFER_ATTEMPTS:
            break
        _LOG.warning(
            "State %s with %s failed (exit %s), retrying in %.1fs: %s",
            phase,
            host,
            result.returncode,
            delay,
            stderr,
        )
        time.sleep(delay)
        delay = min(delay * 2, STATE_TRANSFER_RETRY_MAX_SECONDS)
    if len(failures) > 1:
        result = subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout,
            f"state {phase} with {host} failed after {len(failures)} attempts\n"
            + "\n".join(failures),
        )
    return result


def _state_tar_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    """Keep rsync-style symlinks, while refusing any archive write through one."""
    try:
        filtered = tarfile.data_filter(member, destination)
    except (tarfile.LinkOutsideDestinationError, tarfile.AbsoluteLinkError):
        if not member.issym():
            raise
        # Symlink targets are opaque state, and need not resolve in the mirror.
        # tar_filter still proves that the symlink itself stays in destination;
        # data_filter rejects any later member that would write through it.
        filtered = tarfile.tar_filter(member, destination)
    return filtered.replace(
        mode=member.mode, uid=member.uid, gid=member.gid, uname=member.uname, gname=member.gname
    )
