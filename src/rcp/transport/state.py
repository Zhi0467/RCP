from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel

from rcp.config import Manifest, load_manifest
from rcp.transport.ssh import rsync_ssh_arguments, ssh_arguments

_SNAPSHOT_LOCKS_GUARD = threading.Lock()
_SNAPSHOT_LOCKS: dict[str, threading.RLock] = {}


def _snapshot_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root.resolve()))
    with _SNAPSHOT_LOCKS_GUARD:
        return _SNAPSHOT_LOCKS.setdefault(key, threading.RLock())


class StateUnavailable(RuntimeError):
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
    def run_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".agent-run.lock"
        with path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateUnavailable(
                    "Another seed, refresh, or node-chat run is already in progress."
                ) from exc
            try:
                yield
            finally:
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

    def _refresh_snapshot(self) -> bool:
        if not self._remote_manifest_exists():
            return False
        if not self._acquire_refresh_lock():
            raise StateUnavailable(self.error or "canonical state is busy")
        try:
            return self._sync_remote_tree()
        finally:
            self._release_refresh_lock()

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
        with self.snapshot_lock:
            prepare = self._ssh(["mkdir", "-p", str(self.remote_root)])
            if prepare.returncode:
                self._mark_unreachable(prepare.stderr)
                raise StateUnavailable(self.error or "canonical state is unreachable")
            if not self._acquire_refresh_lock():
                raise StateUnavailable(self.error or "canonical state is busy")
            try:
                if self._remote_manifest_exists():
                    self._sync_remote_tree()
                yield
            finally:
                self._release_refresh_lock()

    def _acquire_refresh_lock(self) -> bool:
        acquired = self._ssh(["mkdir", str(self.lock_dir)])
        if acquired.returncode:
            self._mark_unreachable(
                "Another control-panel operation is reading or writing canonical state. "
                "If it crashed, remove .research/.refresh.lock manually."
            )
            return False
        self._mark_reachable()
        return True

    def _release_refresh_lock(self) -> None:
        released = self._ssh(["rmdir", str(self.lock_dir)])
        if released.returncode:
            self._mark_unreachable(released.stderr)

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        prepared = self._ssh(["mkdir", "-p", str(self.remote_root)])
        if prepared.returncode:
            raise StateUnavailable(prepared.stderr.strip() or "canonical state is unreachable")
        lock_dir = self.remote_root / ".agent-run.lock"
        acquired = self._ssh(["mkdir", str(lock_dir)])
        if acquired.returncode:
            raise StateUnavailable(
                "Another seed, refresh, or node-chat run is already in progress. "
                "If it crashed, remove .research/.agent-run.lock manually."
            )
        try:
            yield
        finally:
            self._ssh(["rmdir", str(lock_dir)])

    def publish(self, relative_paths: list[Path | str]) -> None:
        with self.snapshot_lock:
            self._publish(relative_paths)

    def _publish(self, relative_paths: list[Path | str]) -> None:
        sources: list[str] = []
        remote_parents: set[str] = set()
        for raw_relative in relative_paths:
            relative = Path(raw_relative)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"state publish path must be relative: {relative}")
            source = self.root / relative
            if not source.is_file():
                continue
            sources.append(str(relative))
            remote_parents.add(str(self.remote_root.joinpath(*relative.parent.parts)))
        if not sources:
            return
        prepared = self._ssh(["mkdir", "-p", *sorted(remote_parents)])
        if prepared.returncode:
            self._mark_unreachable(prepared.stderr)
            raise StateUnavailable(self.error or "canonical state is unreachable")
        destination = f"{self.host}:{shlex.quote(str(self.remote_root))}/"
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
        self._mark_reachable(synced=True)

    def publish_committed_batch(
        self,
        relative_paths: list[Path | str],
        batch_directory: Path | str,
    ) -> None:
        """Commit history first, then idempotently publish its derived files."""

        with self.snapshot_lock:
            batch = _validated_batch_directory(batch_directory)
            self._publish_committed_history(relative_paths, batch, commit_is_directory=True)

    def publish_committed_patch(
        self,
        relative_paths: list[Path | str],
        patch_path: Path | str,
    ) -> None:
        """Commit one patch first, then idempotently publish its derived files."""

        with self.snapshot_lock:
            patch = _validated_patch_path(patch_path)
            self._publish_committed_history(relative_paths, patch, commit_is_directory=False)

    def _publish_committed_history(
        self,
        relative_paths: list[Path | str],
        commit_path: Path,
        *,
        commit_is_directory: bool,
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

        apply_script = """\
import json
import os
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
stage = Path(sys.argv[2])
commit = Path(sys.argv[3])
paths = [Path(value) for value in json.loads(sys.argv[4])]
commit_is_directory = sys.argv[5] == "directory"
ordinary = [
    path
    for path in paths
    if path != commit and not (commit_is_directory and commit in path.parents)
]
commit_source = stage / commit
commit_target = root / commit
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
"""
        apply_arguments = [
            "python3",
            "-c",
            apply_script,
            str(self.remote_root),
            str(stage),
            commit_path.as_posix(),
            json.dumps(sources, separators=(",", ":")),
            "directory" if commit_is_directory else "file",
        ]
        applied = self._ssh(apply_arguments)
        if applied.returncode:
            marker = self._ssh(
                [
                    "test",
                    "-d" if commit_is_directory else "-f",
                    str(self.remote_root / commit_path),
                ]
            )
            if marker.returncode == 0:
                # The log is authoritative. A second idempotent pass skips
                # already-moved files and repairs only what remains staged.
                repaired = self._ssh(apply_arguments)
                if repaired.returncode == 0:
                    self._mark_reachable(synced=True)
                    return
                status: Literal["absent", "present", "unknown"] = "present"
                message = repaired.stderr or applied.stderr
            elif marker.returncode == 1:
                status = "absent"
                message = applied.stderr
            else:
                status = "unknown"
                message = marker.stderr or applied.stderr
            self._mark_unreachable(message)
            raise BatchPublishFailed(
                self.error or "canonical state commit failed",
                commit_status=status,
            )
        self._mark_reachable(synced=True)

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
    if (
        relative.parent != Path("patches")
        or not re.fullmatch(r"[0-9]{6}\.json", relative.name)
    ):
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
