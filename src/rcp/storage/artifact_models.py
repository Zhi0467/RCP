from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.artifacts import ArtifactMediaType


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


class ArtifactVersionConflict(ValueError):
    pass
