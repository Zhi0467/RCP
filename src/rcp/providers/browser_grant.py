"""Immutable browser intent resolution passed to one provider invocation."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator


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

    # Core models import the provider registry; validate lazily to avoid that cycle.
    hidden_read_scope: SerializeAsAny[BaseModel] | None = None
    hidden_read_enforcement: SerializeAsAny[BaseModel] | None = None
    requested: bool = False
    status: Literal["not_requested", "granted", "unavailable"] = "not_requested"
    reason_code: str | None = None
    detail: str | None = None
    owner: BrowserOwnerKey | None = None
    session_name: str | None = None
    invocation_dir: str | None = None
    path_prefix: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    # Names the runtime lease this turn must release; persisted only in turn status.
    lease_id: str | None = Field(default=None, exclude=True)

    @field_validator("hidden_read_scope", "hidden_read_enforcement", mode="before")
    @classmethod
    def validate_hidden_read(cls, value, info):
        if value is None:
            return None
        from rcp.core.models import HiddenReadScope, HiddenReadStatus

        model = HiddenReadScope if info.field_name == "hidden_read_scope" else HiddenReadStatus
        # JSON mode also accepts the contract's serialized tuple fields.
        if isinstance(value, model):
            return value
        return model.model_validate_json(json.dumps(value))


class BrowserTurnStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Retained until the provider stops, including detached remote turns. The owner
    # token routes the release durably, so a controller restart cannot strand it.
    lease_id: str | None = None
    owner_token: str | None = None
    status: Literal["not_requested", "granted", "unavailable", "lost"] = "not_requested"
    reason_code: str | None = None
    detail: str | None = None

    def public(self) -> BrowserTurnStatus:
        """Routing stays in the controller; readers see only the outcome."""
        return self.model_copy(update={"lease_id": None, "owner_token": None})


def browser_prompt_line(grant: BrowserGrant) -> str:
    """An explicit value each turn also revokes a prior native-session instruction."""
    if grant.status == "granted":
        return (
            f"Browser: session {grant.session_name} is already open; run playwright-cli "
            f"from {grant.invocation_dir}. Start with goto, not open: open, close, close-all, "
            "and kill-all end RCP's session for the rest of the turn, and delete-data also "
            "erases its logins. "
            "playwright-cli --help lists commands."
        )
    if grant.status == "unavailable":
        return f"Browser: unavailable ({grant.reason_code}); no browser grant for this turn."
    return "Browser: off; no browser grant for this turn."
