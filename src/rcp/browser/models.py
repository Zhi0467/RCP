"""Immutable host-runtime results, independent of launch grants."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SessionLease(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_name: str
    invocation_dir: str
    path_prefix: str
    env: dict[str, str]
    owner_token: str = Field(exclude=True)
    lease_id: str = Field(exclude=True)
    host: str = Field(default="", exclude=True)
    partition: str | None = Field(default=None, exclude=True)
    data_dir: str = Field(exclude=True)


class Unavailable(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason_code: str
    detail: str


class SessionCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    alive: bool
    reason_code: str | None = None
    detail: str | None = None


class BrowserReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    detail: str | None = None
    apt_command: str | None = None
