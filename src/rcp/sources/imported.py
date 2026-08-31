"""Durable project-owned provider histories imported by project transfer."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rcp.limits import (
    PROJECT_TRANSFER_COPY_BUFFER_BYTES,
    PROJECT_TRANSFER_INVENTORY_MAX_ENTRIES,
    PROJECT_TRANSFER_MANIFEST_MAX_BYTES,
)
from rcp.providers import PROVIDERS, ProviderId
from rcp.transfer.archive import TransferArchiveEntry

_MANIFEST_NAME = "manifest.json"


class _StrictImportedSourceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )


class ImportedProviderSourceFile(_StrictImportedSourceModel):
    provider: ProviderId
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ImportedProviderSourceInventory(_StrictImportedSourceModel):
    project_id: str
    files: tuple[ImportedProviderSourceFile, ...]
    payload_size_bytes: int = Field(ge=0)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("imported provider source project id must be a canonical UUID4")
        return value

    @model_validator(mode="after")
    def validate_inventory(self) -> ImportedProviderSourceInventory:
        keys = [(item.provider, item.sha256) for item in self.files]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("imported provider source files must be sorted and unique")
        if sum(item.size_bytes for item in self.files) != self.payload_size_bytes:
            raise ValueError("imported provider source byte total differs from its files")
        if self.fingerprint != _inventory_fingerprint(self.project_id, self.files):
            raise ValueError("imported provider source fingerprint differs from its files")
        return self

    def roots(self, root: Path) -> dict[ProviderId, list[str]]:
        providers = sorted({item.provider for item in self.files})
        return {provider: [str(root / provider)] for provider in providers}


class ImportedProviderSourceStore:
    """Publish and validate one project's immutable imported native histories."""

    def __init__(self, data_dir: Path, project_id: str) -> None:
        parsed = uuid.UUID(project_id)
        if parsed.version != 4 or str(parsed) != project_id:
            raise ValueError("imported provider source project id must be a canonical UUID4")
        self.project_id = project_id
        self.project_root = data_dir / "project-sources" / project_id
        self.root = self.project_root / "provider-history"

    def publish(
        self,
        capture_root: Path,
        entries: tuple[TransferArchiveEntry, ...],
    ) -> ImportedProviderSourceInventory:
        """Atomically publish one exact provider-history inventory or verify idempotence."""

        inventory = _inventory_from_entries(self.project_id, entries)
        if os.path.lexists(self.root):
            current = self.inventory()
            if current != inventory:
                raise ValueError("imported provider source root already contains another inventory")
            return current
        _require_directory(capture_root, label="project transfer capture root")
        _ensure_private_directory(self.project_root.parent)
        _ensure_private_directory(self.project_root)
        staging = self.project_root / f".provider-history-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            for item in inventory.files:
                source = capture_root / "provider-history" / item.provider / item.sha256
                destination = staging / item.provider / item.sha256
                destination.parent.mkdir(mode=0o700, exist_ok=True)
                _copy_exact_regular_file(
                    source,
                    destination,
                    expected_sha256=item.sha256,
                    expected_size=item.size_bytes,
                )
            _write_manifest(staging / _MANIFEST_NAME, inventory)
            _fsync_tree(staging)
            try:
                os.rename(staging, self.root)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                current = self.inventory()
                if current != inventory:
                    raise ValueError(
                        "imported provider source root appeared with another inventory"
                    ) from exc
                shutil.rmtree(staging)
                _fsync_directory(self.project_root)
            _fsync_directory(self.project_root)
            return self.inventory()
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def inventory(self) -> ImportedProviderSourceInventory:
        """Validate the sealed receipt and every owned byte before returning roots."""

        if not os.path.lexists(self.root):
            return _empty_inventory(self.project_id)
        _require_private_directory(
            self.project_root.parent,
            label="imported provider source collection",
        )
        _require_private_directory(self.project_root, label="imported provider project root")
        _require_private_directory(self.root, label="imported provider source root")
        manifest_path = self.root / _MANIFEST_NAME
        payload = _read_small_regular_file(manifest_path)
        try:
            expected = ImportedProviderSourceInventory.model_validate_json(payload)
        except ValueError as exc:
            raise ValueError("imported provider source manifest is invalid") from exc
        if expected.project_id != self.project_id:
            raise ValueError("imported provider source manifest names another project")

        expected_paths = {_MANIFEST_NAME}
        for item in expected.files:
            provider_root = self.root / item.provider
            _require_private_directory(
                provider_root,
                label="imported provider source provider root",
            )
            path = provider_root / item.sha256
            _verify_regular_file(path, item.sha256, item.size_bytes)
            expected_paths.add(f"{item.provider}/{item.sha256}")
        observed = _relative_inventory(self.root)
        if observed != expected_paths:
            raise ValueError("imported provider source root differs from its sealed inventory")
        return expected

    def source_roots(self) -> dict[ProviderId, list[str]]:
        return self.inventory().roots(self.root)


def _inventory_from_entries(
    project_id: str,
    entries: tuple[TransferArchiveEntry, ...],
) -> ImportedProviderSourceInventory:
    files: list[ImportedProviderSourceFile] = []
    for entry in entries:
        if entry.group != "provider_history":
            raise ValueError("imported provider source publication received another file group")
        path = PurePosixPath(entry.archive_path)
        provider = path.parts[1]
        if provider not in PROVIDERS:
            raise ValueError("imported provider source names an unavailable provider")
        files.append(
            ImportedProviderSourceFile(
                provider=provider,
                sha256=entry.sha256,
                size_bytes=entry.size_bytes,
            )
        )
    ordered = tuple(sorted(files, key=lambda item: (item.provider, item.sha256)))
    if len(ordered) > PROJECT_TRANSFER_INVENTORY_MAX_ENTRIES:
        raise ValueError("imported provider source inventory exceeds its entry bound")
    if len({(item.provider, item.sha256) for item in ordered}) != len(ordered):
        raise ValueError("imported provider source entries repeat one file")
    return ImportedProviderSourceInventory(
        project_id=project_id,
        files=ordered,
        payload_size_bytes=sum(item.size_bytes for item in ordered),
        fingerprint=_inventory_fingerprint(project_id, ordered),
    )


def _empty_inventory(project_id: str) -> ImportedProviderSourceInventory:
    return ImportedProviderSourceInventory(
        project_id=project_id,
        files=(),
        payload_size_bytes=0,
        fingerprint=_inventory_fingerprint(project_id, ()),
    )


def _inventory_fingerprint(
    project_id: str,
    files: tuple[ImportedProviderSourceFile, ...],
) -> str:
    payload = {
        "project_id": project_id,
        "files": [item.model_dump(mode="json") for item in files],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    with suppress(FileExistsError):
        path.mkdir(mode=0o700)
    _require_private_directory(path, label="imported provider source parent")


def _require_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is not a safe directory")


def _require_private_directory(path: Path, *, label: str) -> None:
    _require_directory(path, label=label)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        raise ValueError(f"{label} must have mode 0700")


def _copy_exact_regular_file(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    _require_directory(source.parent.parent, label="provider-history capture root")
    _require_directory(source.parent, label="provider-history capture directory")
    source_descriptor = os.open(
        source,
        os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    destination_descriptor = -1
    digest = hashlib.sha256()
    size = 0
    try:
        if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
            raise ValueError("provider-history capture source is not a regular file")
        destination_descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        while True:
            chunk = os.read(source_descriptor, PROJECT_TRANSFER_COPY_BUFFER_BYTES)
            if not chunk:
                break
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                if written <= 0:
                    raise OSError("short imported provider source write")
                remaining = remaining[written:]
            digest.update(chunk)
            size += len(chunk)
        if (digest.hexdigest(), size) != (expected_sha256, expected_size):
            raise ValueError("provider-history capture bytes differ from their archive entry")
        os.fchmod(destination_descriptor, 0o400)
        os.fsync(destination_descriptor)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


def _write_manifest(path: Path, inventory: ImportedProviderSourceInventory) -> None:
    payload = inventory.model_dump_json(indent=2).encode() + b"\n"
    if len(payload) > PROJECT_TRANSFER_MANIFEST_MAX_BYTES:
        raise ValueError("imported provider source manifest exceeds its byte bound")
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short imported provider source manifest write")
            view = view[written:]
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_small_regular_file(path: Path) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_size > PROJECT_TRANSFER_MANIFEST_MAX_BYTES
        ):
            raise ValueError("imported provider source manifest is not one bounded regular file")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, PROJECT_TRANSFER_COPY_BUFFER_BYTES)
            if not chunk:
                break
            payload.extend(chunk)
        return bytes(payload)
    finally:
        os.close(descriptor)


def _verify_regular_file(path: Path, expected_sha256: str, expected_size: int) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o400:
            raise ValueError("imported provider source is not a read-only regular file")
        while True:
            chunk = os.read(descriptor, PROJECT_TRANSFER_COPY_BUFFER_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(descriptor)
    if (digest.hexdigest(), size) != (expected_sha256, expected_size):
        raise ValueError("imported provider source content does not match its sealed inventory")


def _relative_inventory(root: Path) -> set[str]:
    values: set[str] = set()
    for direct in root.iterdir():
        metadata = direct.lstat()
        if direct.name == _MANIFEST_NAME:
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("imported provider source manifest is unsafe")
            values.add(direct.name)
            continue
        if not stat.S_ISDIR(metadata.st_mode) or direct.name not in PROVIDERS:
            raise ValueError("imported provider source root contains an unsafe entry")
        for child in direct.iterdir():
            child_metadata = child.lstat()
            if not stat.S_ISREG(child_metadata.st_mode):
                raise ValueError("imported provider source provider root contains an unsafe entry")
            values.add(f"{direct.name}/{child.name}")
    if len(values) > PROJECT_TRANSFER_INVENTORY_MAX_ENTRIES + 1:
        raise ValueError("imported provider source root exceeds its inventory bound")
    return values


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in (*directories, root):
        _fsync_directory(directory)


__all__ = [
    "ImportedProviderSourceFile",
    "ImportedProviderSourceInventory",
    "ImportedProviderSourceStore",
]
