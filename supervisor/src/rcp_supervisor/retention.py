"""Reclaim retained update artifacts that no recovery path can reach any more.

Every update leaves a rollback checkpoint under the checkpoints root and an
installed tree under the releases root, and until now nothing removed either.
Only the newest checkpoints can serve a rollback, and only the live release and
its rollback target are reachable, so everything older is dead weight. The
decision is made here from the journal, the release pointer, and the selected
receipt; the removal runs as the service account through the filesystem worker,
the same path that created the directories.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rcp_supervisor.checkpoint import _directory, _fsync_directory
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import (
    MAX_OPERATION_BYTES,
    RETAINED_CHECKPOINTS,
    RETAINED_RELEASES,
    RETENTION_ORPHAN_MIN_AGE_SECONDS,
)
from rcp_supervisor.operations import TERMINAL, OperationStore

if TYPE_CHECKING:
    from rcp_supervisor.runtime import Paths, SystemRuntime


@dataclass(frozen=True)
class RetentionPlan:
    """What one prune removes, keeps, and deliberately leaves alone."""

    remove_checkpoints: tuple[Path, ...]
    remove_releases: tuple[Path, ...]
    kept_checkpoints: tuple[str, ...]
    kept_releases: tuple[str, ...]
    #: Entries the prune never touches, each with the reason, so an operator
    #: reading the result can see what was refused rather than infer it.
    left_alone: tuple[str, ...]

    def fields(self) -> list[dict]:
        fields: list[dict] = [
            {"name": "removed_checkpoints", "value": len(self.remove_checkpoints)},
            {"name": "removed_releases", "value": len(self.remove_releases)},
            {"name": "kept_checkpoints", "value": ", ".join(self.kept_checkpoints) or "none"},
            {"name": "kept_releases", "value": ", ".join(self.kept_releases) or "none"},
        ]
        if self.left_alone:
            fields.append({"name": "left_alone", "value": "; ".join(self.left_alone)})
        return fields


def _is_operation_id(name: str) -> bool:
    try:
        identity = uuid.UUID(name)
    except ValueError:
        return False
    return identity.version == 4 and str(identity) == name


def _owns_checkpoint(record: dict, checkpoints_root: Path) -> bool:
    checkpoint = record.get("checkpoint")
    directory = checkpoint.get("directory") if isinstance(checkpoint, dict) else None
    if not isinstance(directory, str):
        return False
    path = Path(directory)
    return path.parent == checkpoints_root / record["operation_id"] and path.is_dir()


def _children(root: Path) -> list[os.DirEntry]:
    with os.scandir(root) as entries:
        return sorted(entries, key=lambda entry: entry.name)


def plan_retention(
    *,
    records: list[tuple[dict, float]],
    checkpoints_root: Path,
    releases_root: Path,
    current_release_directory: str,
    selected: dict,
    protected_operation_ids: frozenset[str],
    now: float,
) -> RetentionPlan:
    """Decide what to remove; never touch the filesystem beyond listing it.

    Fails closed as a whole when the release pointer and the selected receipt
    disagree or when any operation is unfinished. Individual entries it does not
    understand are left alone and named, never removed.
    """

    if current_release_directory != selected["release_directory"]:
        raise SupervisorError(
            "The installed release pointer and the selected receipt disagree; nothing is pruned."
        )
    if any(record["phase"] not in TERMINAL for record, _ in records):
        raise SupervisorError("A deployment operation is unfinished; nothing is pruned.")

    newest_first = sorted(records, key=lambda item: item[1], reverse=True)
    # A journal that ended before its checkpoint was recorded, or whose
    # checkpoint an earlier prune removed, owns no rollback artifact and takes
    # no slot, even when its workspace still holds a prepared candidate.
    kept_operations = [
        record for record, _ in newest_first if _owns_checkpoint(record, checkpoints_root)
    ][:RETAINED_CHECKPOINTS]
    kept_ids = {record["operation_id"] for record in kept_operations} | set(protected_operation_ids)
    recorded_ids = {record["operation_id"] for record, _ in records}
    # A kept checkpoint is only usable if the release it would roll back to,
    # and the one it was taken for, are both still installed.
    kept_release_directories = {selected["release_directory"], current_release_directory}
    for record in kept_operations:
        kept_release_directories.add(record["previous"]["release_directory"])
        kept_release_directories.add(record["target"]["release_directory"])

    remove_checkpoints: list[Path] = []
    kept_checkpoints: list[str] = []
    left_alone: list[str] = []
    for entry in _children(checkpoints_root):
        path = Path(entry.path)
        if not entry.is_dir(follow_symlinks=False):
            left_alone.append(f"{path}: not a directory")
        elif not _is_operation_id(entry.name):
            left_alone.append(f"{path}: not an operation workspace")
        elif entry.name in kept_ids:
            kept_checkpoints.append(entry.name)
        elif entry.name in recorded_ids:
            remove_checkpoints.append(path)
        elif now - entry.stat(follow_symlinks=False).st_mtime < RETENTION_ORPHAN_MIN_AGE_SECONDS:
            # An operation that has not published its journal yet owns this;
            # one that died before publishing is reclaimed on a later prune.
            left_alone.append(f"{path}: unrecorded workspace younger than the age floor")
        else:
            remove_checkpoints.append(path)

    builds: list[tuple[int, Path]] = []
    for entry in _children(releases_root):
        path = Path(entry.path)
        if not entry.is_dir(follow_symlinks=False) or not entry.name.isdigit():
            left_alone.append(f"{path}: not a release build directory")
            continue
        builds.append((int(entry.name), path))
    builds.sort(reverse=True)
    newest_builds = {path for _, path in builds[:RETAINED_RELEASES]}
    remove_releases: list[Path] = []
    kept_releases: list[str] = []
    for build, path in builds:
        if str(path) in kept_release_directories or path in newest_builds:
            kept_releases.append(str(build))
        else:
            remove_releases.append(path)
    return RetentionPlan(
        remove_checkpoints=tuple(remove_checkpoints),
        remove_releases=tuple(remove_releases),
        kept_checkpoints=tuple(kept_checkpoints),
        kept_releases=tuple(sorted(kept_releases, key=int, reverse=True)),
        left_alone=tuple(left_alone),
    )


def adoption_operation_ids(paths: Paths) -> frozenset[str]:
    """The source-adoption workspace is named by its own journal, never by this store."""

    from rcp_supervisor.runtime import _read_file

    journal = paths.supervisor / "adoption.json"
    if not os.path.lexists(journal):
        return frozenset()
    try:
        record = json.loads(_read_file(journal, uid=0, max_bytes=MAX_OPERATION_BYTES))
    except (ValueError, UnicodeError) as exc:
        raise SupervisorError("The adoption journal is unreadable; nothing is pruned.") from exc
    identity = record.get("operation_id") if isinstance(record, dict) else None
    if not isinstance(identity, str) or not _is_operation_id(identity):
        raise SupervisorError("The adoption journal names no operation; nothing is pruned.")
    return frozenset({identity})


def prune_retained(runtime: SystemRuntime, store: OperationStore) -> RetentionPlan:
    """Remove what the plan names, under the same locks a deployment holds."""

    with store.locked(), runtime.deployment_lock():
        plan = plan_retention(
            records=store.records(),
            checkpoints_root=runtime.paths.checkpoints_root,
            releases_root=runtime.paths.releases_root,
            current_release_directory=runtime.current_release_directory(),
            selected=runtime.selected_release(),
            protected_operation_ids=adoption_operation_ids(runtime.paths),
            now=time.time(),
        )
        for directory in plan.remove_checkpoints:
            runtime.remove_retained(directory, runtime.paths.checkpoints_root)
        for directory in plan.remove_releases:
            runtime.remove_retained(directory, runtime.paths.releases_root)
    return plan


def remove_retained_tree(directory: Path, root: Path) -> None:
    """Delete one retained artifact, as the service account, inside its root.

    The coordinator decided; this only checks the shape of what it was handed:
    an absolute direct child of a root this account owns, itself a directory
    this account owns and not a link. Read-only trees (protected staged inputs,
    the managed Python) are made writable first so the removal completes.
    """

    if (
        not directory.is_absolute()
        or not root.is_absolute()
        or ".." in directory.parts
        or directory.parent != root
    ):
        raise SupervisorError(
            "Retained artifacts are removed only as direct children of their root."
        )
    _directory(root)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise SupervisorError(
            "A retained artifact must be a directory owned by the service account."
        )
    os.chmod(directory, 0o700)
    for current, names, _files in os.walk(directory, followlinks=False):
        for name in names:
            child = Path(current) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
    shutil.rmtree(directory)
    _fsync_directory(root)
