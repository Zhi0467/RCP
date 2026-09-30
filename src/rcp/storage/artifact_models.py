from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from rcp.artifacts import ArtifactMediaType
from rcp.live_artifacts import ResolvedLiveVersion


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    project_id: str
    supplier: Literal["turn", "episode_ending"]
    supplier_id: str
    source_name: str
    media_type: ArtifactMediaType
    created_at: str
    expires_at: str | None = None
    kept_at: str | None = None
    current_version: str = ""
    origin_operation_id: str | None = None
    episode_id: str | None = None
    chat_id: str | None = None
    display_title: str | None = None
    live_data_allowed: bool = Field(default=True, exclude_if=lambda value: value is True)

    @field_validator("created_at", "expires_at", "kept_at")
    @classmethod
    def timestamp_is_aware(cls, value: str | None) -> str | None:
        if value is None:
            return None
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("artifact timestamps must have a timezone")
        return timestamp.astimezone(UTC).isoformat()


class ArtifactFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    file_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)


class ArtifactVersion(ArtifactFile):
    version_id: str
    operation_id: str
    created_at: str
    sequence: int = Field(ge=0)
    live: ResolvedLiveVersion | None = None
    live_snapshot: ArtifactFile | None = None

    @model_validator(mode="after")
    def validate_snapshot_owner(self) -> ArtifactVersion:
        if self.live_snapshot is not None and self.live_snapshot.artifact_id != self.artifact_id:
            raise ValueError("live snapshot belongs to another artifact")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_shape(self, handler):
        data = handler(self)
        for field in ("live", "live_snapshot"):
            if field not in self.model_fields_set:
                data.pop(field, None)
        return data


def artifact_version_files(versions: Iterable[ArtifactVersion]) -> list[ArtifactFile]:
    """The shared inventory of version bytes and saved live snapshots."""
    entries: dict[tuple[str, str], ArtifactFile] = {}
    for version in versions:
        files = [ArtifactFile(**version.model_dump(include=set(ArtifactFile.model_fields)))]
        if version.live_snapshot is not None:
            files.append(version.live_snapshot)
        for entry in files:
            key = (entry.artifact_id, entry.file_id)
            if key in entries and entries[key] != entry:
                raise ValueError("artifact file has inconsistent inventory metadata")
            entries[key] = entry
    return list(entries.values())


class ArtifactVersionConflict(ValueError):
    pass
