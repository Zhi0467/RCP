"""Reclaim retained update artifacts that no recovery path can reach any more.

Updates stage rollback checkpoints and installed release trees.
A committed update never restores old data, so no finished checkpoint serves a
rollback; only the live release and the newest trees are kept. The
decision is made here from the journal, the release pointer, and the selected
receipt; the privileged filesystem worker also removes referenced per-filesystem
workspaces, including consumed snapshots and candidate quarantines.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rcp_supervisor.checkpoint import _fsync_directory
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import (
    MAX_OPERATION_BYTES,
    RETAINED_CHECKPOINTS,
    RETAINED_OPERATION_JOURNALS,
    RETAINED_RELEASES,
    RETAINED_ROOT_ENVIRONMENTS,
    RETAINED_SUPERVISOR_LOGS,
)
from rcp_supervisor.operations import TERMINAL, OperationStore

if TYPE_CHECKING:
    from rcp_supervisor.runtime import Paths, SystemRuntime

# Resolve while importing: a self-update later repoints `current`, and resolving
# __file__ then could name the new environment rather than this running one.
_RUNNING_MODULE = Path(__file__).resolve()


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
    if path.parent != checkpoints_root / record["operation_id"] or not path.is_dir():
        return False
    publication = path / "restore.json"
    if publication.exists():
        progress = json.loads(publication.read_text())
        if "states" in progress and all(state == "complete" for state in progress["states"]):
            return False  # Consumed snapshots no longer reserve a rollback slot.
    return True


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
        else:
            # Preparation and retention share a lock, so even a workspace whose
            # first journal never published is no longer in use.
            remove_checkpoints.append(path)

    builds: list[tuple[int, Path]] = []
    for entry in _children(releases_root):
        path = Path(entry.path)
        if not entry.is_dir(follow_symlinks=False) or not entry.name.isdigit():
            left_alone.append(f"{path}: not a release build directory")
            continue
        builds.append((int(entry.name), path))
    builds.sort(reverse=True)
    installed = [(build, path) for build, path in builds if (path / "installed.json").is_file()]
    newest_builds = {path for _, path in installed[:RETAINED_RELEASES]}
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
        )
        for directory in plan.remove_checkpoints:
            runtime.remove_retained(directory, runtime.paths.checkpoints_root)
        for directory in plan.remove_releases:
            runtime.remove_retained(directory, runtime.paths.releases_root)
        runtime.prune_backup_captures()
        prune_supervisor_metadata(runtime.paths, store)

    return plan


def remove_retained_tree(directory: Path, root: Path) -> None:
    """Delete one retained artifact and its referenced filesystem workspaces.

    The coordinator decided; this only checks the shape of what it was handed:
    an absolute direct child of the requested root, itself a directory and not
    a link. Read-only trees (protected staged inputs,
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
    if directory.is_symlink() or not directory.is_dir():
        raise SupervisorError("A retained artifact must be a directory, not a link.")
    # New catalogs point outside the central workspace, onto each live filesystem.
    # Old completed catalogs remain removable without interpreting their entries.
    catalogs = [directory / "checkpoint.json", *directory.glob("*/checkpoint.json")]
    for catalog in catalogs:
        if not catalog.is_file():
            continue
        document = json.loads(catalog.read_text())
        if document.get("version") != 3:
            continue
        for name in document["workspaces"]:
            workspace = Path(name)
            if (
                not workspace.is_absolute()
                or not workspace.name.startswith(
                    f".rcp-checkpoint-{document['checkpoint_id'][:16]}-"
                )
                or workspace.is_symlink()
            ):
                raise SupervisorError("Checkpoint workspace reference is invalid.")
            if workspace.exists():
                _remove_tree(workspace)
    _remove_tree(directory)


def _remove_tree(directory: Path) -> None:
    # Preserved directory modes may be 000, including in development without root.
    os.chmod(directory, 0o700)
    for current, names, _files in os.walk(directory, followlinks=False):
        for name in names:
            child = Path(current) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
    shutil.rmtree(directory)
    _fsync_directory(directory.parent)


def prune_logs(directory: Path, *, keep: int = RETAINED_SUPERVISOR_LOGS) -> None:
    """Bound diagnostic files, including repeated failed attempts."""
    if not directory.exists():
        return
    from rcp_supervisor.install import _require_directory

    _require_directory(directory)
    logs = [
        path
        for path in directory.iterdir()
        if path.name.startswith(("output-", "error-", "probe-"))
        and path.suffix == ".log"
        and stat.S_ISREG(path.lstat().st_mode)
    ]
    for path in sorted(logs, key=lambda path: path.stat().st_mtime_ns, reverse=True)[keep:]:
        path.unlink()


def prune_supervisor_metadata(paths: Paths, store: OperationStore) -> None:
    """Reclaim only supervisor-owned derived artifacts after a successful run.

    Call with both preparation and operation locks, and no unfinished operation.
    Release trees/checkpoint catalogs must be removed before their journals.
    """
    from rcp_supervisor.install import _require_directory

    records = sorted(store.records(), key=lambda item: item[1], reverse=True)
    for record, _ in records[RETAINED_OPERATION_JOURNALS:]:
        (store.directory / f"{record['operation_id']}.json").unlink()
    for path in store.directory.glob("*.tmp"):
        path.unlink()  # records() already validated every interrupted journal write.
    _fsync_directory(store.directory)
    prune_logs(paths.supervisor / "logs", keep=0)
    _prune_atomic_staging(paths.supervisor)
    _prune_pointer_staging(paths.current.parent, "current")
    _prune_pointer_staging(paths.supervisor, "supervisor")

    receipts = paths.supervisor / "release-receipts"
    if receipts.exists():
        _require_directory(receipts)
        _prune_atomic_staging(receipts, receipt=True)
        for path in receipts.iterdir():
            if (
                path.suffix == ".json"
                and path.stem.isdigit()
                and stat.S_ISREG(path.lstat().st_mode)
                and not (paths.releases_root / path.stem).is_dir()
            ):
                path.unlink()
    bundles = paths.supervisor / "bundles"
    if bundles.exists():
        _require_directory(bundles)
        for path in bundles.iterdir():
            if (
                path.is_dir()
                and not path.is_symlink()
                and (
                    _is_operation_id(path.name)
                    or (
                        path.name.startswith(".")
                        and ".fetch-" in path.name
                        and _is_operation_id(path.name[1:].split(".fetch-", 1)[0])
                    )
                )
            ):
                remove_retained_tree(path, bundles)
            elif (
                path.name.startswith(".")
                and path.name.endswith(".fetch.lock")
                and _is_operation_id(path.name[1 : -len(".fetch.lock")])
                and stat.S_ISREG(path.lstat().st_mode)
            ):
                path.unlink()

    protected = {
        Path(sys.executable).absolute(),
        Path(sys.executable).resolve(),
        _RUNNING_MODULE,
    }
    pointer = paths.supervisor / "current"
    if pointer.is_symlink():
        protected.add(pointer.resolve())
    # The public operator console follows the selected application's build.
    current = paths.current
    if current.is_symlink():
        protected.add(paths.supervisor / "operator" / current.resolve().name)
    for storage in ("operator", "versions"):
        root = paths.supervisor / storage
        if not root.exists():
            continue
        _require_directory(root)
        children = [
            p
            for p in root.iterdir()
            if p.is_dir()
            and not p.is_symlink()
            and (
                p.name.isdigit()
                if storage == "operator"
                else len(p.name.split(".")) == 3 and all(n.isdigit() for n in p.name.split("."))
            )
        ]
        complete = sorted(
            (p for p in children if (p / "installed.json").is_file()),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        kept = set(complete[:RETAINED_ROOT_ENVIRONMENTS])
        for child in children:
            if child not in kept and not any(p == child or child in p.parents for p in protected):
                remove_retained_tree(child, root)
    # Old root uv caches are reproducible, never a Python runtime or a receipt.
    cache = paths.supervisor / "cache"
    if cache.exists():
        _require_directory(cache)
        for path in cache.iterdir():
            if path.is_dir() and not path.is_symlink():
                remove_retained_tree(path, cache)
            else:
                path.unlink()
    python = paths.supervisor / "python"
    if python.exists():
        _require_directory(python)
        staging = python / ".temp"
        if staging.exists():
            _require_directory(staging)
            remove_retained_tree(staging, python)
        referenced = set(protected)
        for storage in ("operator", "versions"):
            for executable in (paths.supervisor / storage).glob("*/.venv/bin/python"):
                referenced.add(executable.resolve())
        for path in python.iterdir():
            if (
                path.name.startswith("cpython-")
                and path.is_dir()
                and not path.is_symlink()
                and not any(p == path or path in p.parents for p in referenced)
            ):
                remove_retained_tree(path, python)


def prune_backup_captures(data_dir: Path) -> None:
    """Clear backup-only stages as their service-account owner after an update."""
    if os.geteuid() == 0:
        raise SupervisorError("Backup captures must be pruned as the service account.")
    stages = data_dir.resolve() / "run-stage"
    if not stages.exists():
        return
    info = stages.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise SupervisorError("Backup staging root is unsafe.")
    for path in stages.iterdir():
        if not path.name.startswith("backup-") or not _is_operation_id(path.name[7:]):
            continue
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SupervisorError("Backup capture stage is unsafe.")
        _remove_tree(path)


def _prune_atomic_staging(directory: Path, *, receipt: bool = False) -> None:
    # mkstemp uses an eight-character random suffix. Only our specific atomic
    # metadata publications are candidates; ordinary operator files are not.
    pattern = (
        r"\.(?:[0-9]+)\.json\.[a-z0-9_]{8}"
        if receipt
        else r"\.(?:selected|status|adoption)\.json\.[a-z0-9_]{8}"
    )
    for path in directory.iterdir():
        if not re.fullmatch(pattern, path.name):
            continue
        info = path.lstat()
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o022
        ):
            path.unlink()


def _prune_pointer_staging(directory: Path, label: str) -> None:
    for path in directory.iterdir():
        if not re.fullmatch(rf"\.{label}-(?:adoption-)?[0-9a-f]{{32}}", path.name):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) and info.st_uid == os.geteuid():
            path.unlink()
