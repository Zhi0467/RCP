from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from rcp import __version__

SERVER_METADATA_SCHEMA_VERSION = 1
SERVER_METADATA_FILENAME = "rcp-server.json"
ServerOwnerKind = Literal["cli", "desktop", "embedded"]


class ServerMetadataError(ValueError):
    pass


@dataclass(frozen=True)
class ServerMetadata:
    schema_version: int
    instance_id: str
    pid: int
    host: str
    port: int
    app_version: str
    data_dir_id: str
    owner_kind: ServerOwnerKind

    @classmethod
    def create(
        cls,
        data_dir: Path,
        *,
        host: str,
        port: int,
        owner_kind: ServerOwnerKind,
    ) -> ServerMetadata:
        return cls(
            schema_version=SERVER_METADATA_SCHEMA_VERSION,
            instance_id=str(uuid.uuid4()),
            pid=os.getpid(),
            host=host,
            port=port,
            app_version=__version__,
            data_dir_id=data_dir_identity(data_dir),
            owner_kind=owner_kind,
        )

    @classmethod
    def from_dict(cls, raw: object) -> ServerMetadata:
        if not isinstance(raw, dict):
            raise ServerMetadataError("server metadata is not a JSON object")
        expected = {
            "schema_version",
            "instance_id",
            "pid",
            "host",
            "port",
            "app_version",
            "data_dir_id",
            "owner_kind",
        }
        if set(raw) != expected:
            raise ServerMetadataError("server metadata has an unsupported shape")
        try:
            metadata = cls(**raw)
            instance_id = str(uuid.UUID(metadata.instance_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ServerMetadataError("server metadata contains invalid values") from exc
        if metadata.schema_version != SERVER_METADATA_SCHEMA_VERSION:
            raise ServerMetadataError("server metadata has an unsupported schema version")
        if instance_id != metadata.instance_id:
            raise ServerMetadataError("server metadata has a non-canonical instance id")
        if (
            isinstance(metadata.pid, bool)
            or not isinstance(metadata.pid, int)
            or metadata.pid <= 0
            or not isinstance(metadata.host, str)
            or not metadata.host
            or isinstance(metadata.port, bool)
            or not isinstance(metadata.port, int)
            or not 1 <= metadata.port <= 65535
            or not isinstance(metadata.app_version, str)
            or not metadata.app_version
            or not isinstance(metadata.data_dir_id, str)
            or len(metadata.data_dir_id) != 64
            or not isinstance(metadata.owner_kind, str)
            or metadata.owner_kind not in {"cli", "desktop", "embedded"}
        ):
            raise ServerMetadataError("server metadata contains invalid values")
        return metadata

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def base_url(self) -> str:
        host = (
            f"[{self.host}]"
            if ":" in self.host and not self.host.startswith("[")
            else self.host
        )
        return f"http://{host}:{self.port}"


def data_dir_identity(data_dir: Path) -> str:
    canonical = str(data_dir.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def metadata_path(data_dir: Path) -> Path:
    return data_dir / SERVER_METADATA_FILENAME


def read_server_metadata(data_dir: Path) -> ServerMetadata:
    path = metadata_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServerMetadataError("server metadata is unavailable") from exc
    return ServerMetadata.from_dict(raw)


def remove_server_metadata(data_dir: Path, *, instance_id: str) -> bool:
    """Remove discoverability only when it still names this lock-owning instance."""
    try:
        current = read_server_metadata(data_dir)
    except ServerMetadataError:
        return False
    if current.instance_id != instance_id:
        return False
    metadata_path(data_dir).unlink(missing_ok=True)
    return True


@contextmanager
def published_server_metadata(
    data_dir: Path, metadata: ServerMetadata
) -> Iterator[None]:
    """Publish discoverability while an already-held lock remains authoritative."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = metadata_path(data_dir)
    temporary = path.with_name(f".{path.name}.{metadata.instance_id}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(metadata.as_dict(), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        yield
    finally:
        temporary.unlink(missing_ok=True)
        remove_server_metadata(data_dir, instance_id=metadata.instance_id)
