from __future__ import annotations

import fcntl
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel

from rcp.config import Manifest, load_manifest
from rcp.limits import (
    STATE_LOCK_ATTEMPT_TIMEOUT_SECONDS,
    STATE_LOCK_HOLDER_STOP_TIMEOUT_SECONDS,
    STATE_LOCK_POLL_INTERVAL_SECONDS,
)
from rcp.transport.ssh import rsync_ssh_arguments, ssh_arguments

_SNAPSHOT_LOCKS_GUARD = threading.Lock()
_SNAPSHOT_LOCKS: dict[str, threading.RLock] = {}

_LOCK_ACQUIRED = "acquired"
_LOCK_CONTENDED = "contended"
_LOCK_LEGACY_DIRECTORY = "legacy-directory"
_LOCK_UNSAFE_ENTRY = "unsafe-entry"
_REMOTE_ADVISORY_LOCK_SCRIPT = """\
import fcntl
import json
import os
import shutil
import stat
import sys
from pathlib import Path


def relative_path(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {value}")
    return path


def apply_staged(command):
    root = Path(command["root"])
    stage = Path(command["stage"])
    if (
        Path(lock_path).name != ".refresh.lock"
        or root != Path(lock_path).parent
        or not root.is_absolute()
        or stage.parent != root / ".publish"
    ):
        raise ValueError("invalid canonical root or staging directory")
    paths = [relative_path(value) for value in command["paths"]]
    commit_value = command.get("commit")
    commit = relative_path(commit_value) if commit_value is not None else None
    commit_is_directory = bool(command.get("commit_is_directory"))
    ordinary = [
        path
        for path in paths
        if commit is None
        or (path != commit and not (commit_is_directory and commit in path.parents))
    ]
    commit_target = root / commit if commit is not None else None
    try:
        if commit is not None:
            commit_source = stage / commit
            commit_target.parent.mkdir(parents=True, exist_ok=True)
            if commit_target.exists():
                if commit_source.exists():
                    if commit_is_directory:
                        shutil.rmtree(commit_source)
                    else:
                        commit_source.unlink()
            elif commit_source.exists():
                os.replace(commit_source, commit_target)
            else:
                raise FileNotFoundError(f"missing staged history commit: {commit_source}")
        for path in ordinary:
            source = stage / path
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                os.replace(source, target)
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": True, "commit_status": "present" if commit is not None else None}
    except Exception as exc:
        if commit_target is None:
            commit_status = None
        elif commit_is_directory:
            commit_status = "present" if commit_target.is_dir() else "absent"
        else:
            commit_status = "present" if commit_target.is_file() else "absent"
        return {"ok": False, "commit_status": commit_status, "error": str(exc)[:1000]}

lock_path = sys.argv[1]
try:
    mode = os.lstat(lock_path).st_mode
except FileNotFoundError:
    mode = None
if mode is not None:
    if stat.S_ISDIR(mode):
        # A crashed mkdir-era run leaves its lock directory behind empty, and
        # that artifact is RCP's to clear. rmdir reclaims exactly that case:
        # anything with contents is somebody's state and still refuses.
        try:
            os.rmdir(lock_path)
        except OSError:
            print("legacy-directory", flush=True)
            raise SystemExit(0)
    elif not stat.S_ISREG(mode):
        print("unsafe-entry", flush=True)
        raise SystemExit(0)
try:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
except IsADirectoryError:
    print("legacy-directory", flush=True)
    raise SystemExit(0)
except OSError as exc:
    if os.path.lexists(lock_path) and os.path.islink(lock_path):
        print("unsafe-entry", flush=True)
        raise SystemExit(0)
    print("error", flush=True)
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    raise SystemExit(1)
if not stat.S_ISREG(os.fstat(descriptor).st_mode):
    os.close(descriptor)
    print("unsafe-entry", flush=True)
    raise SystemExit(0)
with os.fdopen(descriptor, "a+") as handle:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("contended", flush=True)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    print("acquired", flush=True)
    for line in sys.stdin:
        try:
            command = json.loads(line)
            if command.get("op") != "apply":
                raise ValueError("unsupported lock-holder command")
            response = apply_staged(command)
        except Exception as exc:
            response = {"ok": False, "commit_status": None, "error": str(exc)[:1000]}
        print(json.dumps(response, separators=(",", ":")), flush=True)
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
"""


def _snapshot_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root.resolve()))
    with _SNAPSHOT_LOCKS_GUARD:
        return _SNAPSHOT_LOCKS.setdefault(key, threading.RLock())


class StateUnavailable(RuntimeError):
    pass


class RunLockCancelled(RuntimeError):
    """Run-lock acquisition stopped because its owning task was cancelled."""


class RunLockOwnershipLost(StateUnavailable):
    """A previously acquired run lock is no longer owned."""


class RunLockLease:
    """Observable ownership for one held canonical-state run lock."""

    def __init__(
        self,
        location: str,
        *,
        on_lost: Callable[[str], None] | None = None,
        owned: Callable[[], bool] | None = None,
        command: Callable[[dict[str, object]], dict[str, object]] | None = None,
    ) -> None:
        self.location = location
        self._on_lost = on_lost
        self._owned = owned
        self._command = command
        self._guard = threading.Lock()
        self._command_guard = threading.Lock()
        self._lost: str | None = None
        self._releasing = False

    def assert_owned(self) -> None:
        if self._owned is not None and not self._owned():
            self._mark_lost(f"Canonical-state lock ownership was lost at {self.location}.")
        with self._guard:
            message = self._lost
            releasing = self._releasing
        if message is not None:
            raise RunLockOwnershipLost(message)
        if releasing:
            raise RunLockOwnershipLost(
                f"Canonical-state lock lease at {self.location} is no longer active."
            )

    def _mark_lost(self, message: str) -> None:
        callback: Callable[[str], None] | None = None
        with self._guard:
            if self._lost is not None or self._releasing:
                return
            self._lost = message
            callback = self._on_lost
        if callback is not None:
            try:
                callback(message)
            except Exception as exc:  # The typed ownership signal must remain authoritative.
                with self._guard:
                    self._lost = f"{message} Ownership-loss callback failed: {str(exc)[:200]}"

    def _begin_release(self) -> None:
        with self._guard:
            self._releasing = True

    def _run_owned_command(self, command: dict[str, object]) -> dict[str, object]:
        with self._command_guard:
            self.assert_owned()
            if self._command is None:
                raise StateUnavailable(
                    f"Canonical-state lock at {self.location} cannot apply remote commands."
                )
            response = self._command(command)
            self.assert_owned()
            return response


class _LegacyLockDirectory(StateUnavailable):
    pass


class _UnsafeLockEntry(StateUnavailable):
    pass


class BatchPublishFailed(StateUnavailable):
    """A remote batch failed with an explicit commit-point observation."""

    def __init__(self, message: str, *, commit_status: Literal["absent", "present", "unknown"]):
        self.commit_status = commit_status
        super().__init__(message)


class StateWorkspaceStatus(BaseModel):
    remote: bool
    reachable: bool
    location: str
    last_synced_at: datetime | None = None
    error: str | None = None


class StateWorkspace:
    def __init__(self, root: Path, location: str) -> None:
        self.root = root
        self.location = location
        self.remote = False
        self.reachable = True
        self.last_synced_at: datetime | None = None
        self.error: str | None = None
        self.materialization_repair_required = False
        self.snapshot_lock = _snapshot_lock(root)

    def refresh(self) -> bool:
        with self.snapshot_lock:
            return self._refresh_snapshot()

    def _refresh_snapshot(self) -> bool:
        self.reachable = True
        return True

    def refresh_if_stale(self, max_age_seconds: float = 2.0) -> bool:
        return self.refresh()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.snapshot_lock:
            yield

    @contextmanager
    def run_lock(
        self,
        *,
        on_wait: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        on_lost: Callable[[str], None] | None = None,
    ) -> Iterator[RunLockLease]:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".agent-run.lock"
        with path.open("a+", encoding="utf-8") as handle:
            waiting_reported = False
            while True:
                if cancelled is not None and cancelled():
                    raise RunLockCancelled("Run-lock acquisition was cancelled while waiting.")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting_reported and on_wait is not None:
                        on_wait("Waiting for another graph-writing run to release canonical state.")
                        waiting_reported = True
                    time.sleep(STATE_LOCK_POLL_INTERVAL_SECONDS)
            lease = RunLockLease(str(path), on_lost=on_lost)
            try:
                if cancelled is not None and cancelled():
                    raise RunLockCancelled("Run-lock acquisition was cancelled after acquiring.")
                yield lease
            finally:
                lease._begin_release()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def publish(self, relative_paths: list[Path | str]) -> None:
        del relative_paths

    def publish_committed_batch(
        self,
        relative_paths: list[Path | str],
        batch_directory: Path | str,
    ) -> None:
        """Publish a locally committed patch batch as one visible history unit."""

        del batch_directory
        self.publish(relative_paths)

    def publish_committed_patch(
        self,
        relative_paths: list[Path | str],
        patch_path: Path | str,
    ) -> None:
        """Publish one patch as the visible history commit point."""

        del patch_path
        self.publish(relative_paths)

    def require_materialization_repair(self) -> None:
        self.materialization_repair_required = True

    def complete_materialization_repair(self) -> None:
        self.materialization_repair_required = False

    def status(self) -> StateWorkspaceStatus:
        return StateWorkspaceStatus(
            remote=self.remote,
            reachable=self.reachable,
            location=self.location,
            last_synced_at=self.last_synced_at,
            error=self.error,
        )


class LocalStateWorkspace(StateWorkspace):
    pass


def _advisory_lock_holder_arguments(
    lock_path: str | os.PathLike[str],
    *,
    python_executable: str = "python3",
) -> list[str]:
    return [python_executable, "-c", _REMOTE_ADVISORY_LOCK_SCRIPT, os.fspath(lock_path)]


def _remote_advisory_lock_command(host: str, lock_path: str | os.PathLike[str]) -> list[str]:
    command = shlex.join(_advisory_lock_holder_arguments(lock_path))
    return ssh_arguments(host, command)


def _stop_lock_holder(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    try:
        process.wait(timeout=STATE_LOCK_HOLDER_STOP_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=STATE_LOCK_HOLDER_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=STATE_LOCK_HOLDER_STOP_TIMEOUT_SECONDS)


def _terminate_lock_holder(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=STATE_LOCK_HOLDER_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=STATE_LOCK_HOLDER_STOP_TIMEOUT_SECONDS)


def _lock_holder_error(process: subprocess.Popen[str], status: str) -> str:
    _terminate_lock_holder(process)
    stderr = process.stderr.read().strip() if process.stderr is not None else ""
    return stderr[:1000] or f"unexpected holder status {status!r}"


def _raise_lock_cancelled(process: subprocess.Popen[str], *, acquired: bool = False) -> None:
    _terminate_lock_holder(process)
    timing = "after acquiring" if acquired else "while waiting"
    raise RunLockCancelled(f"Run-lock acquisition was cancelled {timing}.")


class _HolderLines:
    """Whole-line reader for a lock holder's stdout.

    Polling the descriptor with ``select`` cannot see bytes already sitting in
    the text wrapper's buffer, so a contention that resolves inside one poll
    interval delivers ``contended`` and ``acquired`` in a single read and the
    second status becomes invisible. A reader thread owns ``readline`` and hands
    complete lines over a queue instead.
    """

    _CLOSED = object()

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._lines: queue.Queue[str | object] = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        stream = self._process.stdout
        try:
            if stream is not None:
                for line in stream:
                    status = line.strip()
                    if status:
                        self._lines.put(status)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(self._CLOSED)

    def next_line(self, timeout: float) -> str | None:
        """Return the next status, ``""`` once the stream ends, ``None`` on timeout."""

        try:
            item = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is self._CLOSED:
            self._lines.put(self._CLOSED)
            return ""
        assert isinstance(item, str)
        return item


def _wait_for_lock_holder(
    process: subprocess.Popen[str],
    lines: _HolderLines,
    location: str,
    *,
    on_wait: Callable[[str], None] | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    deadline = time.monotonic() + STATE_LOCK_ATTEMPT_TIMEOUT_SECONDS
    contended = False
    waiting_reported = False
    while True:
        if cancelled is not None and cancelled():
            _raise_lock_cancelled(process)
        if not contended and time.monotonic() >= deadline:
            _terminate_lock_holder(process)
            raise StateUnavailable(
                f"Timed out after {STATE_LOCK_ATTEMPT_TIMEOUT_SECONDS:g} seconds while checking "
                f"canonical-state lock ownership at {location}."
            )
        status = lines.next_line(STATE_LOCK_POLL_INTERVAL_SECONDS)
        if status is None:
            continue
        if status == "":
            if cancelled is not None and cancelled():
                _raise_lock_cancelled(process)
            detail = _lock_holder_error(process, "holder exited without a status")
            raise StateUnavailable(
                f"Could not establish canonical-state lock ownership at {location}: {detail}"
            )
        if cancelled is not None and cancelled():
            _raise_lock_cancelled(process, acquired=status == _LOCK_ACQUIRED)
        if status == _LOCK_CONTENDED:
            contended = True
            if not waiting_reported and on_wait is not None:
                on_wait("Waiting for another graph-writing run to release canonical state.")
                waiting_reported = True
            continue
        if status == _LOCK_ACQUIRED:
            return
        _stop_lock_holder(process)
        if status == _LOCK_LEGACY_DIRECTORY:
            raise _LegacyLockDirectory(
                f"Canonical-state lock {location} is a legacy directory RCP could not reclaim. "
                "An empty one is removed automatically, but this directory still holds contents "
                "whose owner RCP cannot identify, so RCP preserved it. Inspect what is inside it, "
                "then use Retry in RCP."
            )
        if status == _LOCK_UNSAFE_ENTRY:
            raise _UnsafeLockEntry(
                f"Canonical-state lock {location} is not a regular file. RCP preserved it "
                "because replacing a directory, symlink, or special file cannot be proved safe. "
                "Inspect the project or deployment that created it, then use Retry in RCP."
            )
        detail = _lock_holder_error(process, status)
        raise StateUnavailable(
            f"Could not establish canonical-state lock ownership at {location}: {detail}"
        )


def _supervise_lock_holder(
    process: subprocess.Popen[str],
    lease: RunLockLease,
    stopped: threading.Event,
) -> None:
    while not stopped.wait(STATE_LOCK_POLL_INTERVAL_SECONDS):
        return_code = process.poll()
        if return_code is not None:
            lease._mark_lost(
                f"Canonical-state lock holder for {lease.location} exited unexpectedly "
                f"with status {return_code}."
            )
            return


def _raise_holder_command_lost(
    process: subprocess.Popen[str],
    lease: RunLockLease,
    message: str,
) -> None:
    lease._mark_lost(message)
    _terminate_lock_holder(process)
    lease.assert_owned()
    raise RunLockOwnershipLost(message)


def _send_lock_holder_command(
    process: subprocess.Popen[str],
    lines: _HolderLines,
    lease: RunLockLease,
    command: dict[str, object],
) -> dict[str, object]:
    if process.stdin is None or process.stdout is None:
        _raise_holder_command_lost(
            process,
            lease,
            f"Canonical-state lock holder channel for {lease.location} is unavailable.",
        )
    try:
        process.stdin.write(json.dumps(command, separators=(",", ":")) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        _raise_holder_command_lost(
            process,
            lease,
            f"Canonical-state lock holder channel for {lease.location} failed before apply: {exc}",
        )
    deadline = time.monotonic() + STATE_LOCK_ATTEMPT_TIMEOUT_SECONDS
    while True:
        if time.monotonic() >= deadline:
            _raise_holder_command_lost(
                process,
                lease,
                f"Canonical-state lock holder at {lease.location} stopped responding during apply.",
            )
        line = lines.next_line(STATE_LOCK_POLL_INTERVAL_SECONDS)
        if line is None:
            continue
        if line == "":
            _raise_holder_command_lost(
                process,
                lease,
                f"Canonical-state lock holder for {lease.location} "
                + ("exited" if process.poll() is not None else "closed")
                + " during apply.",
            )
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            _raise_holder_command_lost(
                process,
                lease,
                f"Canonical-state lock holder for {lease.location} returned an invalid response: "
                f"{exc}",
            )
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            _raise_holder_command_lost(
                process,
                lease,
                f"Canonical-state lock holder for {lease.location} returned an invalid response.",
            )
        return response


@contextmanager
def _process_advisory_lock(
    process_arguments: list[str],
    location: str,
    *,
    on_wait: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    on_lost: Callable[[str], None] | None = None,
) -> Iterator[RunLockLease]:
    if cancelled is not None and cancelled():
        raise RunLockCancelled("Run-lock acquisition was cancelled while waiting.")
    try:
        holder = subprocess.Popen(  # noqa: S603 - argv is constructed without a shell.
            process_arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise StateUnavailable(
            f"Could not start canonical-state lock holder for {location}: {exc}"
        ) from exc
    if holder.stdout is None:
        _terminate_lock_holder(holder)
        raise StateUnavailable(f"Lock holder for {location} did not expose an ownership signal.")
    lines = _HolderLines(holder)
    try:
        _wait_for_lock_holder(
            holder,
            lines,
            location,
            on_wait=on_wait,
            cancelled=cancelled,
        )
    except BaseException:
        _terminate_lock_holder(holder)
        raise
    if cancelled is not None and cancelled():
        _raise_lock_cancelled(holder, acquired=True)
    lease: RunLockLease

    def owned_command(command: dict[str, object]) -> dict[str, object]:
        return _send_lock_holder_command(holder, lines, lease, command)

    lease = RunLockLease(
        location,
        on_lost=on_lost,
        owned=lambda: holder.poll() is None,
        command=owned_command,
    )
    supervisor_stop = threading.Event()
    supervisor = threading.Thread(
        target=_supervise_lock_holder,
        args=(holder, lease, supervisor_stop),
        daemon=True,
    )
    supervisor.start()
    try:
        yield lease
    finally:
        lease._begin_release()
        supervisor_stop.set()
        _stop_lock_holder(holder)
        supervisor.join(timeout=STATE_LOCK_HOLDER_STOP_TIMEOUT_SECONDS)


class SSHStateWorkspace(StateWorkspace):
    def __init__(self, root: Path, host: str, repository_path: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host):
            raise ValueError("SSH host contains unsupported characters")
        remote_repository = PurePosixPath(repository_path)
        if not remote_repository.is_absolute() or str(remote_repository) == "/":
            raise ValueError("remote state repository must use a specific absolute path")
        super().__init__(root, f"{host}:{remote_repository}/.research")
        self.remote = True
        self.host = host
        self.remote_repository = remote_repository
        self.remote_root = remote_repository / ".research"
        self.lock_dir = self.remote_root / ".refresh.lock"
        self._last_refresh_monotonic = 0.0
        self._publication_lease: RunLockLease | None = None

    def _refresh_snapshot(self) -> bool:
        if not self._remote_manifest_exists():
            return False
        with self._remote_advisory_lock(self.lock_dir) as lease:
            lease.assert_owned()
            refreshed = self._sync_remote_tree()
            lease.assert_owned()
            return refreshed

    def _remote_manifest_exists(self) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest_exists = self._ssh(["test", "-f", str(self.remote_root / "manifest.toml")])
        if manifest_exists.returncode != 0:
            if manifest_exists.returncode == 1:
                self._mark_reachable()
                return False
            self._mark_unreachable(manifest_exists.stderr)
            raise StateUnavailable(self.error or "canonical state is unreachable")
        return True

    def _sync_remote_tree(self) -> bool:
        remote = f"{self.host}:{shlex.quote(str(self.remote_root))}/"
        result = subprocess.run(
            [
                "rsync",
                "-a",
                "--delete",
                "--exclude=.refresh.lock",
                "--exclude=.agent-run.lock",
                "--exclude=.publish",
                *rsync_ssh_arguments(),
                remote,
                f"{self.root}/",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            self._mark_unreachable(result.stderr)
            raise StateUnavailable(self.error or "canonical state sync failed")
        self._mark_reachable(synced=True)
        return True

    def refresh_if_stale(self, max_age_seconds: float = 2.0) -> bool:
        with self.snapshot_lock:
            if time.monotonic() - self._last_refresh_monotonic < max_age_seconds:
                return self.reachable
            return self._refresh_snapshot()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.snapshot_lock, self._remote_advisory_lock(self.lock_dir) as lease:
            self._publication_lease = lease
            try:
                lease.assert_owned()
                if self._remote_manifest_exists():
                    self._sync_remote_tree()
                lease.assert_owned()
                yield
                lease.assert_owned()
            finally:
                self._publication_lease = None

    @contextmanager
    def _publication_lock(self) -> Iterator[RunLockLease]:
        if self._publication_lease is not None:
            self._publication_lease.assert_owned()
            yield self._publication_lease
            return
        with self._remote_advisory_lock(self.lock_dir) as lease:
            self._publication_lease = lease
            try:
                yield lease
            finally:
                self._publication_lease = None

    @contextmanager
    def run_lock(
        self,
        *,
        on_wait: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        on_lost: Callable[[str], None] | None = None,
    ) -> Iterator[RunLockLease]:
        with self._remote_advisory_lock(
            self.remote_root / ".agent-run.lock",
            on_wait=on_wait,
            cancelled=cancelled,
            on_lost=on_lost,
        ) as lease:
            yield lease

    @contextmanager
    def _remote_advisory_lock(
        self,
        lock_path: PurePosixPath,
        *,
        on_wait: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        on_lost: Callable[[str], None] | None = None,
    ) -> Iterator[RunLockLease]:
        prepared = self._ssh(["mkdir", "-p", str(self.remote_root)])
        if prepared.returncode:
            self._mark_unreachable(prepared.stderr)
            raise StateUnavailable(self.error or "canonical state is unreachable")
        location = f"{self.host}:{lock_path}"

        def ownership_lost(message: str) -> None:
            self._mark_unreachable(message)
            if on_lost is not None:
                on_lost(message)

        try:
            with _process_advisory_lock(
                _remote_advisory_lock_command(self.host, lock_path),
                location,
                on_wait=on_wait,
                cancelled=cancelled,
                on_lost=ownership_lost,
            ) as lease:
                self._mark_reachable()
                yield lease
        except (_LegacyLockDirectory, _UnsafeLockEntry):
            self._mark_reachable()
            raise
        except RunLockCancelled:
            self._mark_reachable()
            raise
        except StateUnavailable as exc:
            self._mark_unreachable(str(exc))
            raise

    def publish(self, relative_paths: list[Path | str]) -> None:
        with self.snapshot_lock, self._publication_lock() as lease:
            self._publish(relative_paths, lease)

    def _publish(self, relative_paths: list[Path | str], lease: RunLockLease) -> None:
        sources: list[str] = []
        for raw_relative in relative_paths:
            relative = _validated_relative_path(raw_relative)
            source = self.root / relative
            if not source.is_file():
                continue
            sources.append(str(relative))
        if not sources:
            return
        stage = self.remote_root / ".publish" / f"files-{os.getpid()}-{time.time_ns()}"
        prepared = self._ssh(["mkdir", "-p", str(stage)])
        if prepared.returncode:
            self._mark_unreachable(prepared.stderr)
            raise StateUnavailable(self.error or "canonical state is unreachable")
        destination = f"{self.host}:{shlex.quote(str(stage))}/"
        result = subprocess.run(
            ["rsync", "-aR", *rsync_ssh_arguments(), *sources, destination],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            self._mark_unreachable(result.stderr)
            raise StateUnavailable(self.error or "canonical state publish failed")
        try:
            response = lease._run_owned_command(
                {
                    "op": "apply",
                    "root": str(self.remote_root),
                    "stage": str(stage),
                    "paths": sources,
                }
            )
        except RunLockOwnershipLost as exc:
            message = (
                "Canonical-state ownership was lost during ordinary file apply; a prefix may "
                "have been applied while the lock was held. Retry in a new transaction to "
                "restage and idempotently apply the full requested file set."
            )
            self._mark_unreachable(message)
            raise RunLockOwnershipLost(message) from exc
        if not response["ok"]:
            message = str(response.get("error") or "canonical state apply failed")
            self._mark_unreachable(message)
            raise StateUnavailable(self.error or "canonical state apply failed")
        self._mark_reachable(synced=True)

    def publish_committed_batch(
        self,
        relative_paths: list[Path | str],
        batch_directory: Path | str,
    ) -> None:
        """Commit history first, then idempotently publish its derived files."""

        with self.snapshot_lock, self._publication_lock() as lease:
            batch = _validated_batch_directory(batch_directory)
            self._publish_committed_history(
                relative_paths,
                batch,
                commit_is_directory=True,
                lease=lease,
            )

    def publish_committed_patch(
        self,
        relative_paths: list[Path | str],
        patch_path: Path | str,
    ) -> None:
        """Commit one patch first, then idempotently publish its derived files."""

        with self.snapshot_lock, self._publication_lock() as lease:
            patch = _validated_patch_path(patch_path)
            self._publish_committed_history(
                relative_paths,
                patch,
                commit_is_directory=False,
                lease=lease,
            )

    def _publish_committed_history(
        self,
        relative_paths: list[Path | str],
        commit_path: Path,
        *,
        commit_is_directory: bool,
        lease: RunLockLease,
    ) -> None:
        sources: list[str] = []
        for raw_relative in relative_paths:
            relative = _validated_relative_path(raw_relative)
            source = self.root / relative
            if source.is_file():
                sources.append(str(relative))
        if commit_is_directory:
            commit_prefix = f"{commit_path.as_posix()}/"
            includes_commit = any(path.startswith(commit_prefix) for path in sources)
        else:
            includes_commit = commit_path.as_posix() in sources
        if not includes_commit:
            raise ValueError("committed history publication is missing its patch files")

        stage_name = commit_path.name if commit_is_directory else f"patch-{commit_path.name}"
        stage = self.remote_root / ".publish" / stage_name
        prepared = self._ssh(["mkdir", "-p", str(stage)])
        if prepared.returncode:
            self._mark_unreachable(prepared.stderr)
            raise BatchPublishFailed(
                self.error or "canonical state staging failed",
                commit_status="absent",
            )
        destination = f"{self.host}:{shlex.quote(str(stage))}/"
        result = subprocess.run(
            ["rsync", "-aR", *rsync_ssh_arguments(), *sources, destination],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            self._mark_unreachable(result.stderr)
            raise BatchPublishFailed(
                self.error or "canonical state staging failed",
                commit_status="absent",
            )

        command: dict[str, object] = {
            "op": "apply",
            "root": str(self.remote_root),
            "stage": str(stage),
            "paths": sources,
            "commit": commit_path.as_posix(),
            "commit_is_directory": commit_is_directory,
        }
        try:
            response = lease._run_owned_command(command)
        except RunLockOwnershipLost as exc:
            self._raise_reconciled_commit_failure(commit_path, commit_is_directory, exc)
        if response["ok"] and response.get("commit_status") == "present":
            self._mark_reachable(synced=True)
            return
        status = response.get("commit_status")
        message = str(response.get("error") or "canonical state commit failed")
        if status == "present":
            try:
                repaired = lease._run_owned_command(command)
            except RunLockOwnershipLost as exc:
                self._raise_reconciled_commit_failure(commit_path, commit_is_directory, exc)
            if repaired["ok"] and repaired.get("commit_status") == "present":
                self._mark_reachable(synced=True)
                return
            status = repaired.get("commit_status")
            message = str(repaired.get("error") or message)
        if status not in {"absent", "present"}:
            self._raise_reconciled_commit_failure(
                commit_path,
                commit_is_directory,
                StateUnavailable(message),
            )
        self._mark_unreachable(message)
        raise BatchPublishFailed(
            self.error or "canonical state commit failed",
            commit_status=status,
        )

    def _raise_reconciled_commit_failure(
        self,
        commit_path: Path,
        commit_is_directory: bool,
        error: Exception,
    ) -> None:
        marker_arguments = [
            "test",
            "-d" if commit_is_directory else "-f",
            str(self.remote_root / commit_path),
        ]
        marker = self._ssh(marker_arguments)
        if marker.returncode == 0:
            status: Literal["absent", "present", "unknown"] = "present"
            message = marker.stderr or str(error)
            self._mark_unreachable(message)
            raise BatchPublishFailed(
                self.error or "canonical state commit failed",
                commit_status=status,
            ) from error

        deadline = time.monotonic() + STATE_LOCK_ATTEMPT_TIMEOUT_SECONDS
        try:
            with self._remote_advisory_lock(
                self.lock_dir,
                cancelled=lambda: time.monotonic() >= deadline,
            ):
                marker = self._ssh(marker_arguments)
        except (RunLockCancelled, StateUnavailable) as reacquire_error:
            status = "unknown"
            message = str(reacquire_error) or marker.stderr or str(error)
        else:
            if marker.returncode == 0:
                status = "present"
            elif marker.returncode == 1:
                status = "absent"
            else:
                status = "unknown"
            message = marker.stderr or str(error)
        self._mark_unreachable(message)
        raise BatchPublishFailed(
            self.error or "canonical state commit failed",
            commit_status=status,
        ) from error

    def _ssh(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = " ".join(shlex.quote(argument) for argument in arguments)
        try:
            return subprocess.run(
                ssh_arguments(self.host, command),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess([], 255, "", str(exc))

    def _mark_reachable(self, *, synced: bool = False) -> None:
        self.reachable = True
        self.error = None
        self._last_refresh_monotonic = time.monotonic()
        if synced:
            self.last_synced_at = datetime.now(UTC)

    def _mark_unreachable(self, message: str) -> None:
        self.reachable = False
        self.error = message.strip() or "canonical state is unreachable"
        self._last_refresh_monotonic = time.monotonic()


def _validated_relative_path(value: Path | str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"state publish path must be relative: {relative}")
    return relative


def _validated_batch_directory(value: Path | str) -> Path:
    relative = _validated_relative_path(value)
    if relative.parent != Path("patches") or not relative.name.startswith("batch-"):
        raise ValueError(f"invalid committed patch batch directory: {relative}")
    return relative


def _validated_patch_path(value: Path | str) -> Path:
    relative = _validated_relative_path(value)
    if relative.parent != Path("patches") or not re.fullmatch(r"[0-9]{6}\.json", relative.name):
        raise ValueError(f"invalid committed patch path: {relative}")
    return relative


def prepare_state_workspace(bootstrap: Manifest, data_dir: Path) -> tuple[Manifest, StateWorkspace]:
    state_repository = bootstrap.repository_map[bootstrap.state.repository]
    machine = bootstrap.machine_map[state_repository.machine]
    if not machine.host:
        return bootstrap, LocalStateWorkspace(
            bootstrap.research_dir,
            str(bootstrap.research_dir),
        )

    cache_key = hashlib.sha256(f"{machine.host}\0{state_repository.path}".encode()).hexdigest()[:16]
    cache_root = data_dir / "state-cache" / cache_key / ".research"
    workspace = SSHStateWorkspace(cache_root, machine.host, state_repository.path)
    try:
        workspace.refresh()
    except StateUnavailable:
        if not (cache_root / "manifest.toml").is_file():
            raise
    cache_manifest = cache_root / "manifest.toml"
    if not cache_manifest.is_file():
        cache_manifest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bootstrap.path, cache_manifest)
    manifest = load_manifest(cache_manifest)
    _validate_remote_identity(bootstrap, manifest, machine.host, state_repository.path)
    return manifest, workspace


def _validate_remote_identity(
    bootstrap: Manifest,
    canonical: Manifest,
    expected_host: str,
    expected_path: str,
) -> None:
    canonical_repository = canonical.repository_map[canonical.state.repository]
    canonical_machine = canonical.machine_map[canonical_repository.machine]
    if canonical.name != bootstrap.name:
        raise ValueError("cached canonical manifest belongs to a different project")
    if canonical_machine.host != expected_host or canonical_repository.path != expected_path:
        raise ValueError("canonical manifest changed its own remote state locator")
