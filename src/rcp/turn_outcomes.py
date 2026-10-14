"""File-backed Work turn outcomes and their persisted projection."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.limits import TURN_OUTCOME_REASON_MAX_CHARS

OutcomeReason = Annotated[
    str,
    Field(max_length=TURN_OUTCOME_REASON_MAX_CHARS, pattern=r"^[^\r\n]*$"),
]
_REASON_RULE = "reason is required and non-blank for partial and could_not; optional for done."


class WorkTurnOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"description": _REASON_RULE})

    status: Literal["done", "partial", "could_not"]
    reason: OutcomeReason | None = None

    @model_validator(mode="after")
    def require_reason(self) -> Self:
        if self.status != "done" and (self.reason is None or not self.reason.strip()):
            raise ValueError(_REASON_RULE)
        return self


class TurnOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["done", "partial", "could_not", "not_stated"]
    reason: OutcomeReason | None = None


def work_turn_outcome_contract(path: str) -> str:
    """Render the same schema and conditional rule that validate the stage file."""
    return (
        f"For ordinary human Work chat and nightly consolidation only, write `{path}` before "
        "finishing. Child Work and Experiment loops do not require this output. JSON schema:\n"
        + json.dumps(WorkTurnOutcome.model_json_schema(), ensure_ascii=False)
    )
