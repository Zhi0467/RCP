"""Plain stopped-root copies and resumable, consumable rename publication.

The coordinator stops writers and owns admission. This module runs with the
supervisor's privilege; it imposes no policy on entries inside a copied root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import CHECKPOINT_SPACE_MARGIN


@dataclass(frozen=True)
class SnapshotRoot:
    live: Path
    payload: Path


@dataclass(frozen=True)
class Checkpoint:
    directory: Path
    # Retain the reference shape so completed historical operation journals stay
    # readable. Version 3 uses an opaque identity, not a digest of file contents.
    sha256: str
    boundary_sha256: str


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _run(argv: list[str]) -> str:
    # No elapsed-time ceiling: the outer worker owns cancellation and its
    # process group. Reap this child on ordinary interruption as well.
    executable = shutil.which(argv[0])
    if executable is None:
        raise SupervisorError(f"Filesystem command is unavailable: {argv[0]}")
    command = [executable, *argv[1:]]
    if sys.platform == "linux":
        command = [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("child_exec.py")),
            str(os.getpid()),
            *command,
        ]
    with tempfile.TemporaryFile() as error:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error)
        try:
            output, _ = process.communicate()
            if process.returncode:
                error.seek(0)
                detail = error.read(4096).decode(errors="replace")
                raise SupervisorError(f"Filesystem command failed ({argv[0]}): {detail}")
            return output.decode()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _copy_flags() -> list[str]:
    if sys.platform == "darwin":
        return ["cp", "-a"]
    return ["cp", "-a", "--preserve=xattr", "--reflink=auto", "--sparse=always"]


def _sync_filesystem(path: Path) -> None:
    _run(["sync"] if sys.platform == "darwin" else ["sync", "-f", "--", str(path)])


def copy_trees(destination: Path, sources: tuple[Path, ...]) -> dict[Path, Path]:
    """Copy a group in one GNU invocation, retaining links between its roots."""
    destination.mkdir(mode=0o700)
    targets = {source: destination / source.relative_to(source.anchor) for source in sources}
    if sources:
        if sys.platform == "darwin":
            # BSD cp lacks --parents. This is only the development fallback;
            # Linux qualification exercises GNU metadata and hardlink semantics.
            for source, target in targets.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                _run([*_copy_flags(), str(source), str(target)])
        else:
            _run([*_copy_flags(), "--parents", "--", *map(str, sources), str(destination)])
    _sync_filesystem(destination)
    return targets


def copy_contents(source: Path, destination: Path) -> None:
    """Overlay validated candidate contents using the same plain copier.

    Replace an existing destination entry, never write through a symlink there.
    """
    overlay = [] if sys.platform == "darwin" else ["--remove-destination"]
    _run([*_copy_flags(), *overlay, str(source) + "/.", str(destination)])
    _sync_filesystem(destination)


def _existing_parent(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return path


def _roots(roots: tuple[SnapshotRoot, ...]) -> list[SnapshotRoot]:
    result: list[SnapshotRoot] = []
    for root in sorted(roots, key=lambda root: len(root.live.parts)):
        if not root.live.is_absolute() or not root.payload.is_absolute() or root.live == Path("/"):
            raise SupervisorError("Checkpoint roots must be absolute non-filesystem roots.")
        covering = next((item for item in result if root.live.is_relative_to(item.live)), None)
        if covering is not None:
            if root.payload != covering.payload / root.live.relative_to(covering.live):
                raise SupervisorError("Nested replacement roots have inconsistent sources.")
            continue
        result.append(root)
    return result


def check_snapshot_space(directory: Path, roots: tuple[Path, ...]) -> list[dict]:
    """Advisory allocated-byte/inode estimates; actual copy and sync decide fit."""
    groups: dict[int, list[Path]] = {}
    for root in _roots(tuple(SnapshotRoot(path, path) for path in roots)):
        parent = _existing_parent(root.live.parent)
        groups.setdefault(parent.stat().st_dev, []).append(root.live)
    estimates = []
    for paths in groups.values():
        existing = [str(path) for path in paths if os.path.lexists(path)]
        parent = _existing_parent(paths[0].parent)
        capacity = os.statvfs(parent)
        try:
            sizes = _run(["du", "-sk", *existing]) if existing else ""
            allocated = sum(int(line.split()[0]) * 1024 for line in sizes.splitlines())
            inode_counts = (
                _run(["du", "--inodes", "-s", "--", *existing])
                if existing and sys.platform != "darwin"
                else ""
            )
            inodes = sum(int(line.split()[0]) for line in inode_counts.splitlines())
            estimate = {
                "filesystem": str(parent),
                "bytes": allocated,
                "inodes": inodes,
                "available_bytes": capacity.f_bavail * capacity.f_frsize,
                "available_inodes": capacity.f_favail,
            }
            estimates.append(estimate)
            if (
                allocated * CHECKPOINT_SPACE_MARGIN > estimate["available_bytes"]
                or inodes * CHECKPOINT_SPACE_MARGIN > capacity.f_favail
            ):
                warnings.warn(
                    f"Checkpoint space estimate exceeds available capacity: {estimate}",
                    stacklevel=2,
                )
        except (OSError, ValueError, SupervisorError) as exc:
            warnings.warn(f"Checkpoint space estimate unavailable: {exc}", stacklevel=2)
    return estimates


def create_stopped_snapshot(
    destination: Path, roots: tuple[Path, ...], *, boundary_sha256: str
) -> Checkpoint:
    return create_checkpoint(
        destination,
        tuple(SnapshotRoot(root, root) for root in roots),
        boundary_sha256=boundary_sha256,
    )


def create_checkpoint(
    destination: Path, roots: tuple[SnapshotRoot, ...], *, boundary_sha256: str
) -> Checkpoint:
    roots = _roots(roots)
    for root in roots:
        if destination.is_relative_to(root.live) or destination.is_relative_to(root.payload):
            raise SupervisorError("Checkpoint catalog must be outside copied roots.")
    destination.mkdir(mode=0o700)
    identity = uuid.uuid4().hex + uuid.uuid4().hex
    document = {
        "version": 3,
        "checkpoint_id": identity,
        "boundary_sha256": boundary_sha256,
        "ready": False,
        "workspaces": [],
        "roots": [],
    }
    groups: dict[int, list[SnapshotRoot]] = {}
    for root in roots:
        parent = _existing_parent(root.live.parent)
        groups.setdefault(parent.stat().st_dev, []).append(root)
    for group in groups.values():
        parent = _existing_parent(group[0].live.parent)
        # Climb out of *all* live/source roots, retaining this filesystem.
        while any(
            parent.is_relative_to(path) for root in roots for path in (root.live, root.payload)
        ):
            parent = parent.parent
        if parent.stat().st_dev != _existing_parent(group[0].live.parent).stat().st_dev:
            raise SupervisorError("No staging parent outside the roots shares their filesystem.")
        workspace = Path(tempfile.mkdtemp(prefix=f".rcp-checkpoint-{identity[:16]}-", dir=parent))
        document["workspaces"].append(str(workspace))
        # Publish references before copying so abandoned partial copies can be pruned.
        _write_json(destination / "checkpoint.json", document)
        sources = tuple(
            dict.fromkeys(root.payload for root in group if os.path.lexists(root.payload))
        )
        targets = copy_trees(workspace / "payload", sources)
        for root in group:
            document["roots"].append(
                {
                    "live": str(root.live),
                    "payload": str(targets[root.payload]) if root.payload in targets else None,
                    "present": root.payload in targets,
                    "quarantine": str(workspace / f"quarantine-{len(document['roots'])}"),
                }
            )
    document["ready"] = True
    _write_json(destination / "checkpoint.json", document)
    _fsync_directory(destination.parent)
    return Checkpoint(destination, identity, boundary_sha256)


def _document(checkpoint: Checkpoint) -> dict:
    document = json.loads((checkpoint.directory / "checkpoint.json").read_text())
    if document.get("version") != 3:
        raise SupervisorError(
            "Legacy subset checkpoints cannot replace whole roots; finish recovery with the previous supervisor."
        )
    if (
        document.get("checkpoint_id") != checkpoint.sha256
        or document.get("boundary_sha256") != checkpoint.boundary_sha256
        or document.get("ready") is not True
    ):
        raise SupervisorError("Checkpoint identity or readiness does not match the operation.")
    return document


def read_checkpoint(directory: Path, *, expected_sha256: str) -> Checkpoint:
    document = json.loads((directory / "checkpoint.json").read_text())
    checkpoint = Checkpoint(directory, expected_sha256, document["boundary_sha256"])
    _document(checkpoint)
    return checkpoint


def check_checkpoint_roots(checkpoint: Checkpoint, roots: tuple[Path, ...]) -> None:
    captured = [Path(root["live"]) for root in _document(checkpoint)["roots"]]
    if any(not any(root.is_relative_to(parent) for parent in captured) for root in roots):
        raise SupervisorError("Prepared replacement root lacks stopped snapshot coverage.")


def restore_checkpoint(checkpoint: Checkpoint) -> None:
    document = _document(checkpoint)
    journal = checkpoint.directory / "restore.json"
    progress = (
        json.loads(journal.read_text())
        if journal.exists()
        else {"checkpoint_id": checkpoint.sha256, "states": ["pending"] * len(document["roots"])}
    )
    if (
        progress.get("checkpoint_id") != checkpoint.sha256
        or len(progress.get("states", [])) != len(document["roots"])
        or any(
            state not in {"pending", "quarantining", "publishing", "complete"}
            for state in progress["states"]
        )
    ):
        raise SupervisorError("Checkpoint publication journal is invalid.")
    for index, root in enumerate(document["roots"]):
        if progress["states"][index] == "complete":
            continue
        live, quarantine = Path(root["live"]), Path(root["quarantine"])
        payload = Path(root["payload"]) if root["present"] else None
        if progress["states"][index] == "pending":
            if payload is not None and not os.path.lexists(payload):
                raise SupervisorError("Checkpoint payload is unavailable before publication.")
            progress["states"][index] = "quarantining"
            _write_json(journal, progress)
        if progress["states"][index] == "quarantining":
            if not os.path.lexists(quarantine) and os.path.lexists(live):
                os.replace(live, quarantine)
            _fsync_directory(quarantine.parent)
            _fsync_directory(_existing_parent(live.parent))
            progress["states"][index] = "publishing"
            _write_json(journal, progress)
        if payload is not None:
            if os.path.lexists(payload):
                live.parent.mkdir(parents=True, exist_ok=True)
                os.replace(payload, live)
            elif not os.path.lexists(live):
                raise SupervisorError("Neither checkpoint payload nor published root exists.")
            _fsync_directory(payload.parent)
        _fsync_directory(_existing_parent(live.parent))
        progress["states"][index] = "complete"
        _write_json(journal, progress)
