"""Opaque stopped-tree checkpoints and pre-admission filesystem recovery.

RCP owns root discovery and application validation. This module owns stopped
filesystem inventories and retains prepared checkpoints for legacy recovery.
Its caller must keep the service and every writer stopped throughout replacement;
it never grants work admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import (
    CHECKPOINT_COPY_BYTES,
    MAX_CHECKPOINT_BYTES,
    MAX_CHECKPOINT_DIFFERENCE_PATH,
    MAX_CHECKPOINT_DIFFERENCES,
    MAX_CHECKPOINT_ENTRIES,
    MAX_CHECKPOINT_MANIFEST_BYTES,
    MAX_CHECKPOINT_ROOTS,
)


@dataclass(frozen=True)
class SnapshotRoot:
    live: Path
    payload: Path


@dataclass(frozen=True)
class Checkpoint:
    directory: Path
    sha256: str
    boundary_sha256: str


def _fail(message: str) -> None:
    raise SupervisorError(message)


def _digest(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _keys(value: object, names: set[str]) -> bool:
    return isinstance(value, dict) and value.keys() == names


def _path(value: object) -> Path:
    if (
        not isinstance(value, str)
        or len(value.encode()) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        _fail("Checkpoint paths must be bounded absolute directory names.")
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts or str(path) != value:
        _fail("Checkpoint paths must be normalized absolute directories.")
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink():
            _fail("Checkpoint paths must not traverse symbolic links.")
        if ancestor != path:
            info = ancestor.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
            ):
                _fail("Checkpoint paths must not traverse directories controlled by other writers.")
    return path


def _directory(path: Path, *, private: bool = False) -> None:
    _path(str(path))
    try:
        info = path.lstat()
    except OSError as exc:
        raise SupervisorError("A required checkpoint directory is unavailable.") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or info.st_mode & 0o500 != 0o500
    ):
        _fail("Checkpoint directories must belong to this account and exclude other writers.")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        _fail("Checkpoint storage directories must have mode 0700.")


def _disjoint(paths: list[Path]) -> None:
    for index, first in enumerate(paths):
        for second in paths[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                _fail("Checkpoint storage, payloads, and live roots must not overlap.")


def _file_hash(
    path: Path, *, max_bytes: int | None = None, exact: bool = False
) -> tuple[str, int, int]:
    """Hash one owned file; ``exact`` snapshots also admit hardlinks and group/other write bits.

    Agent tools such as uv hardlink installed packages from their cache and create
    world-writable lock files inside retained scratch; a stopped exact snapshot copies
    their bytes and mode, and restore yields ordinary single-link files.
    """
    max_bytes = MAX_CHECKPOINT_BYTES if max_bytes is None else max_bytes
    before = path.lstat()
    if before.st_size > max_bytes:
        _fail("checkpoint_capacity: A checkpoint file exceeds the size limit.")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or (before.st_nlink != 1 and not exact)
        or before.st_mode & (0o7000 if exact else 0o7022)
        or not before.st_mode & 0o400
    ):
        _fail("Checkpoint files must be owned regular files without links or unsafe permissions.")
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()) != before:
            _fail("A checkpoint file changed while opening it.")
        while chunk := stream.read(CHECKPOINT_COPY_BYTES):
            size += len(chunk)
            if size > max_bytes:
                _fail("checkpoint_capacity: A checkpoint file exceeds the size limit.")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    # atime may change through this read; every content/identity field must agree.
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        any(getattr(before, name) != getattr(after, name) for name in fields)
        or size != before.st_size
    ):
        _fail("A checkpoint file changed while reading it.")
    return digest.hexdigest(), size, stat.S_IMODE(before.st_mode)


def _inventory(
    root: Path,
    *,
    max_entries: int | None = None,
    max_bytes: int | None = None,
    links: bool = True,
    exact: bool = False,
) -> list[dict]:
    """Inventory a tree; ``links`` records symbolic links by text, else refuses them.

    Prepared payloads retain links; exact snapshots admit only links below
    retained run stages. Legacy offline adoption keeps its no-links contract.
    """
    max_entries = MAX_CHECKPOINT_ENTRIES if max_entries is None else max_entries
    max_bytes = MAX_CHECKPOINT_BYTES if max_bytes is None else max_bytes
    _directory(root)
    owner = _access_metadata(root) if exact else None
    result: list[dict] = []
    size = 0
    pending = [root]
    while pending:
        parent = pending.pop()
        _directory(parent)
        # Path.iterdir uses listdir on Python 3.11; scandir lets the entry bound
        # stop even a single oversized directory before materializing its names.
        with os.scandir(parent) as children:
            for entry in children:
                if len(result) >= max_entries:
                    _fail("checkpoint_capacity: Checkpoint inventory exceeds its entry limit.")
                child = Path(entry.path)
                relative = child.relative_to(root).as_posix()
                if len(relative.encode()) > 4096:
                    _fail("A checkpoint relative path exceeds the limit.")
                try:
                    info = child.lstat()
                    metadata = _access_metadata(child, root=root, owner=owner) if exact else {}
                    if stat.S_ISDIR(info.st_mode):
                        _directory(child)
                        result.append({"path": relative, "kind": "directory", **metadata})
                        pending.append(child)
                    elif stat.S_ISLNK(info.st_mode) and links:
                        if exact and (
                            Path(relative).parts[0] != "run-stage" or len(Path(relative).parts) < 2
                        ):
                            _fail(
                                "checkpoint_unsafe_entry: symbolic link outside retained scratch."
                            )
                        # A link is inventory by its text alone; nothing here follows it.
                        target = os.readlink(child)
                        if not _valid_link_target(target):
                            _fail("A checkpoint link target is invalid.")
                        result.append(
                            {"path": relative, "kind": "symlink", "target": target, **metadata}
                        )
                    else:
                        digest, length, mode = _file_hash(
                            child, max_bytes=max_bytes - size, exact=exact
                        )
                        size += length
                        result.append(
                            {
                                "path": relative,
                                "kind": "file",
                                "sha256": digest,
                                "size": length,
                                "mode": mode,
                                **metadata,
                            }
                        )
                except (SupervisorError, OSError) as exc:
                    raise SupervisorError(
                        f"{exc if isinstance(exc, SupervisorError) else 'Checkpoint entry unreadable.'} "
                        f"{_entry_name(relative)}: changed."
                    ) from exc
    return sorted(result, key=lambda item: item["path"])


def _entry_name(relative: str) -> str:
    return json.dumps(relative, ensure_ascii=False)[:MAX_CHECKPOINT_DIFFERENCE_PATH]


def _access_metadata(path: Path, *, root: Path | None = None, owner: dict | None = None) -> dict:
    info = path.lstat()
    relative = path.relative_to(root).as_posix() if root is not None else "."
    try:
        attributes = os.listxattr(path, follow_symlinks=False) if hasattr(os, "listxattr") else []
    except OSError as exc:
        raise SupervisorError(
            f"checkpoint_unsafe_entry: cannot inspect xattrs on {_entry_name(relative)}."
        ) from exc
    if (
        attributes
        or owner is not None
        and (info.st_uid, info.st_gid) != (owner["uid"], owner["gid"])
    ):
        _fail(
            "checkpoint_unsafe_entry: xattrs or ownership differing from the root on "
            f"{_entry_name(relative)}."
        )
    metadata = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
    _validate_metadata(metadata, symlink=path.is_symlink(), writable=stat.S_ISREG(info.st_mode))
    return metadata


def _validate_metadata(value: object, *, symlink: bool = False, writable: bool = False) -> None:
    if (
        not _keys(value, {"mode", "uid", "gid"})
        or any(type(value[key]) is not int or value[key] < 0 for key in ("uid", "gid"))
        or type(value["mode"]) is not int
        or not 0 <= value["mode"] <= 0o777
        or (not symlink and not writable and value["mode"] & 0o022)
    ):
        _fail("Checkpoint access metadata is unsupported.")


def _apply_metadata(path: Path, metadata: dict) -> None:
    info = path.lstat()
    if (info.st_uid, info.st_gid) != (metadata["uid"], metadata["gid"]):
        os.chown(path, metadata["uid"], metadata["gid"], follow_symlinks=False)
    if not path.is_symlink():
        os.chmod(path, metadata["mode"])
    elif stat.S_IMODE(path.lstat().st_mode) != metadata["mode"]:
        if not hasattr(os, "lchmod"):
            _fail("checkpoint_unsafe_entry: symbolic link mode is unsupported.")
        os.lchmod(path, metadata["mode"])


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, document: dict) -> None:
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        + b"\n"
    )
    if len(payload) > MAX_CHECKPOINT_MANIFEST_BYTES:
        _fail("checkpoint_capacity: Checkpoint metadata exceeds the size limit.")
    temporary = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temporary):
        _file_hash(temporary, max_bytes=MAX_CHECKPOINT_MANIFEST_BYTES)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_json(path: Path) -> tuple[dict, str]:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > MAX_CHECKPOINT_MANIFEST_BYTES
    ):
        _fail("Checkpoint metadata must be a bounded private regular file.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()) != info:
            _fail("Checkpoint metadata changed while opening it.")
        payload = stream.read(MAX_CHECKPOINT_MANIFEST_BYTES + 1)
    if len(payload) > MAX_CHECKPOINT_MANIFEST_BYTES:
        _fail("checkpoint_capacity: Checkpoint metadata exceeds the size limit.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("Checkpoint metadata has duplicate fields.")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SupervisorError("Checkpoint metadata is not valid JSON.") from exc
    return value, hashlib.sha256(payload).hexdigest()


def _valid_link_target(target: object) -> bool:
    return (
        isinstance(target, str)
        and 0 < len(target.encode()) <= 4096
        and not any(ord(c) < 32 for c in target)
    )


def _validate_entries(entries: object, *, exact: bool = False) -> None:
    if not isinstance(entries, list) or len(entries) > MAX_CHECKPOINT_ENTRIES:
        _fail("Checkpoint entries are missing or exceed the limit.")
    previous = ""
    directories = {"."}
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") not in (
            "file",
            "directory",
            "symlink",
        ):
            _fail("Checkpoint entry type is unsupported.")
        names = {"path", "kind"}
        if entry["kind"] == "file":
            names |= {"sha256", "size", "mode"}
        elif entry["kind"] == "symlink":
            names |= {"target"}
        if exact:
            names |= {"mode", "uid", "gid"}
            _validate_metadata(
                {key: entry.get(key) for key in ("mode", "uid", "gid")},
                symlink=entry["kind"] == "symlink",
                writable=entry["kind"] == "file",
            )
        if not _keys(entry, names):
            _fail("Checkpoint entry fields are unsupported.")
        relative = entry["path"]
        if (
            not isinstance(relative, str)
            or not relative
            or len(relative.encode()) > 4096
            or any(ord(c) < 32 for c in relative)
            or "\\" in relative
        ):
            _fail("Checkpoint entry paths are invalid.")
        path = Path(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or str(path) != relative
            or relative <= previous
        ):
            _fail("Checkpoint entry paths must be normalized, sorted, and unique.")
        if str(path.parent) not in directories:
            _fail("Checkpoint entry has no declared parent directory.")
        previous = relative
        if entry["kind"] == "directory":
            directories.add(relative)
        elif entry["kind"] == "symlink":
            if not _valid_link_target(entry["target"]):
                _fail("Checkpoint link target is invalid.")
        else:
            if (
                not _digest(entry["sha256"])
                or type(entry["size"]) is not int
                or not 0 <= entry["size"] <= MAX_CHECKPOINT_BYTES
                or type(entry["mode"]) is not int
                or not 0 <= entry["mode"] <= 0o777
                or (not exact and entry["mode"] & 0o022)
                or not entry["mode"] & 0o400
            ):
                _fail("Checkpoint file identity or permissions are invalid.")
            total += entry["size"]
    if total > MAX_CHECKPOINT_BYTES:
        _fail("checkpoint_capacity: Checkpoint payload exceeds the size limit.")


def _verify_tree(
    root: Path, entries: list[dict], *, stored: bool, metadata: dict | None = None
) -> None:
    try:
        _directory(root, private=stored or metadata is None)
    except (SupervisorError, OSError) as exc:
        status = "changed" if os.path.lexists(root) else "missing"
        raise SupervisorError(f"Checkpoint tree differs: .: {status}.") from exc
    actual = _inventory(root, exact=metadata is not None)
    expected = [
        dict(entry, mode=0o400 if entry["kind"] == "file" else 0o700)
        if stored and (entry["kind"] == "file" or metadata and entry["kind"] == "directory")
        else entry
        for entry in entries
    ]
    if metadata is not None and not stored:
        try:
            if _access_metadata(root) != metadata:
                _fail("Checkpoint root access metadata differs.")
        except (SupervisorError, OSError) as exc:
            raise SupervisorError("Checkpoint tree differs: .: changed.") from exc
    if actual != expected:
        expected_by_path = {entry["path"]: entry for entry in expected}
        actual_by_path = {entry["path"]: entry for entry in actual}
        differences = []
        for path in sorted(expected_by_path.keys() | actual_by_path.keys()):
            if path not in actual_by_path:
                status = "missing"
            elif path not in expected_by_path:
                status = "extra"
            elif expected_by_path[path] != actual_by_path[path]:
                status = "changed"
            else:
                continue
            differences.append(f"{_entry_name(path)}: {status}")
            if len(differences) == MAX_CHECKPOINT_DIFFERENCES:
                break
        _fail("Checkpoint tree differs: " + "; ".join(differences) + ".")
    for entry in entries:
        if entry["kind"] == "directory":
            _directory(root / entry["path"], private=stored or metadata is None)


def _copy_tree(
    source: Path,
    destination: Path,
    entries: list[dict],
    *,
    stored: bool,
    metadata: dict | None = None,
) -> None:
    destination.mkdir(mode=0o700)
    for entry in entries:
        target = destination / entry["path"]
        if entry["kind"] == "directory":
            target.mkdir(mode=0o700)
            continue
        origin = source / entry["path"]
        if entry["kind"] == "symlink":
            if not os.path.islink(origin) or os.readlink(origin) != entry["target"]:
                _fail("A checkpoint copy source differs from its verified inventory.")
            os.symlink(entry["target"], target)
            continue
        source_fd = os.open(origin, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(source_fd, "rb") as reader, target.open("xb") as writer:
            info = os.fstat(reader.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or (info.st_nlink != 1 and metadata is None)
                or info.st_size != entry["size"]
            ):
                _fail("A checkpoint copy source differs from its verified inventory.")
            digest = hashlib.sha256()
            remaining = entry["size"]
            while remaining:
                chunk = reader.read(min(CHECKPOINT_COPY_BYTES, remaining))
                if not chunk:
                    _fail("A checkpoint source became shorter during copy.")
                writer.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if reader.read(1) or digest.hexdigest() != entry["sha256"]:
                _fail("Checkpoint copy SHA-256 mismatch.")
            writer.flush()
            os.fchmod(writer.fileno(), 0o400 if stored else entry["mode"])
            if metadata is not None:
                _apply_metadata(target, dict(entry, mode=0o400) if stored else entry)
            os.fsync(writer.fileno())
    if metadata is not None:
        for entry in reversed(entries):
            if entry["kind"] == "file":
                continue
            access = dict(entry) if stored else entry
            if stored:
                access["mode"] = 0o700 if entry["kind"] == "directory" else entry["mode"]
            _apply_metadata(destination / entry["path"], access)
        _apply_metadata(
            destination,
            dict(metadata, mode=0o700) if stored else metadata,
        )
    for directory in reversed(
        [
            destination,
            *(destination / item["path"] for item in entries if item["kind"] == "directory"),
        ]
    ):
        _fsync_directory(directory)
    _fsync_directory(destination.parent)
    _verify_tree(destination, entries, stored=stored, metadata=metadata)


def _document(directory: Path, expected_sha256: str) -> dict:
    _directory(directory, private=True)
    if not _digest(expected_sha256):
        _fail("A checkpoint requires its exact SHA-256 identity.")
    document, digest = _read_json(directory / "checkpoint.json")
    if digest != expected_sha256:
        _fail("Checkpoint manifest SHA-256 mismatch.")
    if (
        not _keys(document, {"version", "checkpoint_id", "boundary_sha256", "roots"})
        or type(document["version"]) is not int
        or document["version"] not in (1, 2)
    ):
        _fail("Checkpoint manifest format is unsupported.")
    try:
        identity = uuid.UUID(hex=document["checkpoint_id"])
        if identity.version != 4 or identity.hex != document["checkpoint_id"]:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise SupervisorError("Checkpoint identity is invalid.") from exc
    if not _digest(document["boundary_sha256"]):
        _fail("Checkpoint boundary identity is invalid.")
    roots = document["roots"]
    if not isinstance(roots, list) or not roots or len(roots) > MAX_CHECKPOINT_ROOTS:
        _fail("Checkpoint replacement roots are invalid.")
    live_paths = []
    total_entries = 0
    total_bytes = 0
    for index, root in enumerate(roots):
        fields = {"live", "payload", "entries"}
        if document["version"] == 2:
            fields.add("metadata")
        if not _keys(root, fields) or root["payload"] != f"payload/{index}":
            _fail("Checkpoint replacement fields are unsupported.")
        live = _path(root["live"])
        _directory(live.parent)
        live_paths.append(live)
        if document["version"] == 2:
            _validate_metadata(root["metadata"])
        _validate_entries(root["entries"], exact=document["version"] == 2)
        total_entries += len(root["entries"])
        total_bytes += sum(entry.get("size", 0) for entry in root["entries"])
        if total_entries > MAX_CHECKPOINT_ENTRIES or total_bytes > MAX_CHECKPOINT_BYTES:
            _fail("checkpoint_capacity: Checkpoint inventory exceeds its resource limits.")
        _verify_tree(
            directory / root["payload"],
            root["entries"],
            stored=True,
            metadata=root.get("metadata"),
        )
    _disjoint([directory, *live_paths])
    return document


def create_checkpoint(
    destination: Path, roots: tuple[SnapshotRoot, ...], *, boundary_sha256: str
) -> Checkpoint:
    """Seal app-prepared trees after application quiescence; never capture live state."""
    return _create_checkpoint(
        destination, roots, boundary_sha256=boundary_sha256, offline_snapshot=False
    )


def create_offline_snapshot(destination: Path, live: Path, *, boundary_sha256: str) -> Checkpoint:
    """Retain stopped legacy bytes before any newer app interprets them.

    Only source adoption uses this initial opaque snapshot. Its caller must have
    proved every writer stopped; updates use a separate multi-root exact snapshot.
    """
    return _create_checkpoint(
        destination,
        (SnapshotRoot(live, live),),
        boundary_sha256=boundary_sha256,
        offline_snapshot=True,
    )


def check_snapshot_space(directory: Path, roots: tuple[Path, ...]) -> None:
    """Refuse before service stop unless the checkpoint and a rollback both fit.

    The checkpoint holds one full copy of every root; a rollback then builds another
    full sibling copy beside each live root before swapping it in. Both land on
    whichever filesystems hold them, often the same one.
    """
    _directory(directory)
    need: dict[int, list[int]] = {}  # st_dev -> [bytes, inodes]
    paths: dict[int, Path] = {}

    def reserve(path: Path, size: int, inodes: int) -> None:
        device = os.stat(path).st_dev
        paths.setdefault(device, path)
        entry = need.setdefault(device, [0, 0])
        entry[0] += size
        entry[1] += inodes

    reserve(directory, MAX_CHECKPOINT_MANIFEST_BYTES, 3)  # destination, payload, manifest
    total_entries = total_bytes = 0
    for root in roots:
        entries = _inventory(
            root,
            max_entries=MAX_CHECKPOINT_ENTRIES - total_entries,
            max_bytes=MAX_CHECKPOINT_BYTES - total_bytes,
            exact=True,
        )
        total_entries += len(entries)
        total_bytes += sum(entry.get("size", 0) for entry in entries)
        block = os.statvfs(directory).f_frsize or 4096
        size = block + sum(
            max(1, (entry.get("size", 0) + block - 1) // block) * block for entry in entries
        )
        reserve(directory, size, len(entries) + 1)
        reserve(root.parent, size, len(entries) + 1)
    for device, (required, required_inodes) in need.items():
        filesystem = os.statvfs(paths[device])
        if filesystem.f_favail < required_inodes:
            _fail(
                f"checkpoint_capacity: {paths[device]} needs {required_inodes} inodes for the "
                f"checkpoint and a rollback; only {filesystem.f_favail} are available."
            )
        available = shutil.disk_usage(paths[device]).free
        if available < required:
            _fail(
                f"checkpoint_capacity: {paths[device]} needs {required} bytes for the "
                f"checkpoint and a rollback; only {available} bytes are available."
            )


def create_stopped_snapshot(
    destination: Path, roots: tuple[Path, ...], *, boundary_sha256: str
) -> Checkpoint:
    """Seal every stopped entry before old preparation can mutate any live root."""
    return _create_checkpoint(
        destination,
        tuple(SnapshotRoot(root, root) for root in roots),
        boundary_sha256=boundary_sha256,
        offline_snapshot=True,
        exact=True,
    )


def _create_checkpoint(
    destination: Path,
    roots: tuple[SnapshotRoot, ...],
    *,
    boundary_sha256: str,
    offline_snapshot: bool,
    exact: bool = False,
) -> Checkpoint:
    _path(str(destination))
    _directory(destination.parent)
    if not roots or len(roots) > MAX_CHECKPOINT_ROOTS or not _digest(boundary_sha256):
        _fail("Checkpoint creation requires roots and an application boundary digest.")
    paths = [destination]
    inventories = []
    metadata_inventory = []
    total_entries = 0
    total_bytes = 0
    for root in roots:
        _directory(root.live)
        _directory(root.live.parent)
        _directory(root.payload)
        metadata_inventory.append(_access_metadata(root.live) if exact else None)
        paths.append(root.live)
        if not offline_snapshot:
            paths.append(root.payload)
        entries = _inventory(
            root.payload,
            max_entries=MAX_CHECKPOINT_ENTRIES - total_entries,
            max_bytes=MAX_CHECKPOINT_BYTES - total_bytes,
            links=exact or not offline_snapshot,
            exact=exact,
        )
        inventories.append(entries)
        total_entries += len(entries)
        total_bytes += sum(entry.get("size", 0) for entry in entries)
    _disjoint(paths)
    if (
        sum(len(entries) for entries in inventories) > MAX_CHECKPOINT_ENTRIES
        or sum(entry.get("size", 0) for entries in inventories for entry in entries)
        > MAX_CHECKPOINT_BYTES
    ):
        _fail("checkpoint_capacity: Checkpoint inventory exceeds its resource limits.")
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SupervisorError(
            "Checkpoint creation never overwrites an existing destination."
        ) from exc
    (destination / "payload").mkdir(mode=0o700)
    records = []
    for index, (root, entries) in enumerate(zip(roots, inventories, strict=True)):
        _validate_entries(entries, exact=exact)
        metadata = metadata_inventory[index]
        relative = f"payload/{index}"
        _copy_tree(root.payload, destination / relative, entries, stored=True, metadata=metadata)
        if _inventory(root.payload, links=exact or not offline_snapshot, exact=exact) != entries:
            _fail("An application checkpoint payload changed during capture.")
        record = {"live": str(root.live), "payload": relative, "entries": entries}
        if exact:
            if _access_metadata(root.live) != metadata:
                _fail("A stopped root changed access metadata during capture.")
            record["metadata"] = metadata
        records.append(record)
    if exact:
        for root in records:
            try:
                _verify_tree(
                    Path(root["live"]), root["entries"], stored=False, metadata=root["metadata"]
                )
            except (SupervisorError, OSError) as exc:
                raise SupervisorError(
                    "checkpoint_unsafe_entry: stopped roots changed during capture."
                ) from exc
    document = {
        "version": 2 if exact else 1,
        "checkpoint_id": uuid.uuid4().hex,
        "boundary_sha256": boundary_sha256,
        "roots": records,
    }
    _write_json(destination / "checkpoint.json", document)
    _fsync_directory(destination.parent)
    _value, digest = _read_json(destination / "checkpoint.json")
    return read_checkpoint(destination, expected_sha256=digest)


def read_checkpoint(directory: Path, *, expected_sha256: str) -> Checkpoint:
    document = _document(directory, expected_sha256)
    return Checkpoint(directory, expected_sha256, document["boundary_sha256"])


def restore_checkpoint(checkpoint: Checkpoint) -> None:
    """Resume exact pre-admission replacement; preserve every candidate quarantine.

    The caller owns the service stop and admission guard. Calling this after
    admission reopens is forbidden: selected-release recovery must preserve new
    work. A completed restoration is verified, never reapplied over changed data.
    """
    document = _document(checkpoint.directory, checkpoint.sha256)
    if document["boundary_sha256"] != checkpoint.boundary_sha256:
        _fail("Checkpoint application boundary changed.")
    journal = checkpoint.directory / "restore.json"
    paths = []
    for index, root in enumerate(document["roots"]):
        live = Path(root["live"])
        suffix = f"{document['checkpoint_id']}-{index}"
        partial = live.with_name(f".rcp-restore-{suffix}")
        quarantine = live.with_name(f".rcp-quarantine-{suffix}")
        paths.append((live, partial, quarantine, root))
    _disjoint(
        [
            checkpoint.directory,
            *(
                path
                for live, partial, quarantine, _root in paths
                for path in (live, partial, quarantine)
            ),
        ]
    )
    state = {
        "version": document["version"],
        "checkpoint_sha256": checkpoint.sha256,
        "status": "restoring",
    }
    if document["version"] == 2:
        state["absent_roots"] = [
            index for index, (live, _, _, _) in enumerate(paths) if not os.path.lexists(live)
        ]
    if os.path.lexists(journal):
        state, _digest_value = _read_json(journal)
        if (
            not _keys(
                state,
                {"version", "checkpoint_sha256", "status"}
                | ({"absent_roots"} if document["version"] == 2 else set()),
            )
            or type(state["version"]) is not int
            or state["version"] != document["version"]
            or state["checkpoint_sha256"] != checkpoint.sha256
            or state["status"] not in ("restoring", "complete")
        ):
            _fail("Restore journal is unsupported or differs from its checkpoint.")
        if document["version"] == 2:
            _validate_absent_roots(state["absent_roots"], len(paths))
    else:
        if any(
            os.path.lexists(path)
            for _live, partial, quarantine, _root in paths
            for path in (partial, quarantine)
        ):
            _fail("Restore staging exists without its owning journal.")
        _write_json(journal, state)
    if state["status"] == "complete":
        for live, _partial, _quarantine, root in paths:
            _verify_live_root(live, root)
        return
    for index, (live, partial, quarantine, root) in enumerate(paths):
        absent = index in state.get("absent_roots", [])
        if absent and os.path.lexists(quarantine):
            _fail("An absent root has an unexpected quarantine.")
        if absent and os.path.lexists(live):
            if os.path.lexists(partial):
                _fail("Restore has ambiguous live and staged roots.")
            _verify_live_root(live, root)
            continue
        if os.path.lexists(quarantine):
            _directory(quarantine)
            if os.path.lexists(live):
                if os.path.lexists(partial):
                    _fail("Restore has ambiguous live and staged roots.")
                _verify_live_root(live, root)
                continue
        elif not absent:
            _directory(live)
        if os.path.lexists(partial):
            # Only this journal's disposable staging tree may be rebuilt. Live
            # roots and quarantines are never deleted or recursively overlaid.
            _directory(partial)
            # Restore copies can already carry read-only directory modes.
            for parent, directories, _files in os.walk(partial, followlinks=False):
                os.chmod(parent, 0o700)
                for name in directories:
                    child = Path(parent) / name
                    if not child.is_symlink():
                        os.chmod(child, 0o700)
            shutil.rmtree(partial)
            _fsync_directory(partial.parent)
        _copy_tree(
            checkpoint.directory / root["payload"],
            partial,
            root["entries"],
            stored=False,
            metadata=root.get("metadata"),
        )
        if not absent and not os.path.lexists(quarantine):
            os.replace(live, quarantine)
            _fsync_directory(live.parent)
        os.replace(partial, live)
        _fsync_directory(live.parent)
    for live, _partial, _quarantine, root in paths:
        _verify_live_root(live, root)
    _write_json(journal, dict(state, status="complete"))


def _verify_live_root(live: Path, root: dict) -> None:
    try:
        _verify_tree(live, root["entries"], stored=False, metadata=root.get("metadata"))
    except (SupervisorError, OSError) as exc:
        raise SupervisorError(f"rollback_tree_mismatch: {exc}") from exc


def _exact_document(checkpoint: Checkpoint) -> dict:
    document = _document(checkpoint.directory, checkpoint.sha256)
    if document["version"] != 2:
        _fail("rollback_tree_mismatch: legacy checkpoint has no exact stopped-tree proof.")
    if document["boundary_sha256"] != checkpoint.boundary_sha256:
        _fail("rollback_tree_mismatch: checkpoint boundary identity changed.")
    return document


def verify_checkpoint(checkpoint: Checkpoint) -> None:
    """Independently rescan restored roots before old ordinary startup."""
    try:
        for root in _exact_document(checkpoint)["roots"]:
            _verify_live_root(Path(root["live"]), root)
    except SupervisorError as exc:
        if str(exc).startswith("rollback_tree_mismatch:"):
            raise
        raise SupervisorError(f"rollback_tree_mismatch: {exc}") from exc
    except OSError as exc:
        raise SupervisorError("rollback_tree_mismatch: checkpoint could not be read.") from exc


def check_checkpoint_roots(checkpoint: Checkpoint, roots: tuple[Path, ...]) -> None:
    document = _exact_document(checkpoint)
    expected = [root["live"] for root in document["roots"]]
    actual = [str(_path(str(root))) for root in roots]
    if len(set(actual)) != len(actual) or set(expected) != set(actual):
        _fail("checkpoint_root_mismatch: prepared roots differ from stopped inventory.")


def _validate_absent_roots(indices: object, count: int) -> None:
    if (
        not isinstance(indices, list)
        or any(type(index) is not int or not 0 <= index < count for index in indices)
        or indices != sorted(set(indices))
    ):
        _fail("Restore journal has invalid absent roots.")
