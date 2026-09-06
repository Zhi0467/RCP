"""Strict compute job inputs and durable records."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.compute_jobs.text import safe_compute_diagnostic, validate_compute_metadata
from rcp.limits import COMPUTE_JOB_LABEL_MAX_CHARS

ComputeJobStatus = Literal["running", "exited", "cancelled", "lost"]
ComputeContainment = Literal["mirrored", "cooperative"]


class ComputeJobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    project_id: str
    origin_operation_id: str
    episode_id: str | None = None
    execution_machine: str
    execution_host: str = ""
    backend_id: str
    backend_handle: str
    job_root: str
    cwd: str
    argv: list[str]
    log_path: str
    exit_path: str
    containment: ComputeContainment = "cooperative"
    status: ComputeJobStatus = "running"
    exit_status: int | None = None
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    cancel_requested_by: str | None = None
    cancel_requested_at: str | None = None
    diagnostic: str | None = None

    @field_validator("diagnostic")
    @classmethod
    def redact_diagnostic(cls, value: str | None) -> str | None:
        return safe_compute_diagnostic(value) if value is not None else None


class ComputeLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    cwd: str
    label: str | None = Field(default=None, min_length=1, max_length=COMPUTE_JOB_LABEL_MAX_CHARS)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if not value[0] or any("\x00" in argument for argument in value):
            raise ValueError("compute argv requires an executable and cannot contain NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        if not PurePosixPath(value).is_absolute() or "\x00" in value:
            raise ValueError("compute cwd must be an absolute path without NUL")
        return value

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validate_compute_metadata(value)
        if not value.strip():
            raise ValueError("compute label must not be blank")
        return value.strip()


class ComputeBackendProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_machine: str
    backend_id: str
    state: Literal["ready", "unavailable", "failed"]
    ready: bool
    diagnostic: str
    required_action: str | None = None
    containment: ComputeContainment
    cgroup_isolated: bool | None = None
    status_label: str
    status_tone: Literal["ready", "error"]

    @field_validator("diagnostic", "required_action")
    @classmethod
    def redact_diagnostic(cls, value: str | None) -> str | None:
        return safe_compute_diagnostic(value) if value is not None else None
