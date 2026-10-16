"""The live status of one agent task, folded from provider status deltas.

Display only: the chat's agent panel reads it, and nothing else may
(invariant 4b). A later delta replaces what it observed and leaves the rest.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from rcp.limits import AGENT_STATUS_COMPACTION_LIMIT, AGENT_STATUS_SUBAGENT_LIMIT
from rcp.providers.base import AgentCompaction, AgentStatusDelta, AgentSubagent


class AgentStatus(BaseModel):
    context_tokens: int | None = Field(default=None, ge=0)
    context_window: int | None = Field(default=None, ge=0)
    autocompact_tokens: int | None = Field(default=None, ge=0)
    compacting: bool = False
    compactions: list[AgentCompaction] = Field(default_factory=list)
    subagents: list[AgentSubagent] = Field(default_factory=list)


def fold_agent_status(current: AgentStatus | None, delta: AgentStatusDelta) -> AgentStatus:
    status = current.model_copy(deep=True) if current is not None else AgentStatus()
    last = status.compactions[-1] if status.compactions else None
    if delta.context_tokens is not None and last is not None and last.after_tokens is None:
        # Codex reports no post-compaction size; its next model call shows it.
        last.after_tokens = delta.context_tokens
    for field in ("context_tokens", "context_window", "autocompact_tokens", "compacting"):
        value = getattr(delta, field)
        if value is not None:
            setattr(status, field, value)
    if delta.compaction is not None:
        compaction = delta.compaction
        if compaction.before_tokens is None and status.context_tokens is not None:
            compaction = compaction.model_copy(update={"before_tokens": status.context_tokens})
        status.compactions = [*status.compactions, compaction][-AGENT_STATUS_COMPACTION_LIMIT:]
        status.compacting = False
        if compaction.after_tokens is not None:
            status.context_tokens = compaction.after_tokens
    if delta.subagent is not None:
        status.subagents = _upsert(status.subagents, delta.subagent)
    return status


def _upsert(subagents: list[AgentSubagent], update: AgentSubagent) -> list[AgentSubagent]:
    for index, existing in enumerate(subagents):
        if existing.id == update.id:
            merged = existing.model_copy(
                update={
                    "kind": update.kind or existing.kind,
                    "label": update.label or existing.label,
                    "state": update.state,
                    "tokens": update.tokens if update.tokens is not None else existing.tokens,
                }
            )
            return [*subagents[:index], merged, *subagents[index + 1 :]]
    return [*subagents, update][-AGENT_STATUS_SUBAGENT_LIMIT:]
