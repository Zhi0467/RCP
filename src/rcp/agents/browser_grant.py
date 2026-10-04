"""Immutable browser intent resolution passed to one provider invocation."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BrowserOwnerKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    space_id: str
    project_id: str
    stage_name: str
    host_key: str

    def token(self) -> str:
        return "browser-" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class BrowserGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requested: bool = False
    status: Literal["not_requested", "granted", "unavailable"] = "not_requested"
    reason_code: str | None = None
    detail: str | None = None
    owner: BrowserOwnerKey | None = None
    session_name: str | None = None
    invocation_dir: str | None = None
    path_prefix: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class BrowserTurnStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["not_requested", "granted", "unavailable", "lost"] = "not_requested"
    reason_code: str | None = None
    detail: str | None = None


def browser_prompt_line(grant: BrowserGrant) -> str:
    """An explicit value each turn also revokes a prior native-session instruction."""
    if grant.status == "granted":
        return (
            f"Browser: session {grant.session_name}; run playwright-cli from "
            f"{grant.invocation_dir}; playwright-cli --help lists commands."
        )
    if grant.status == "unavailable":
        return f"Browser: unavailable ({grant.reason_code}); no browser grant for this turn."
    return "Browser: off; no browser grant for this turn."
