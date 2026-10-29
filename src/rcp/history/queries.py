"""The shared contract for graph history reads.

Head references, step records, and the paged answers every history consumer
reads: the branch Changes view, merge context, refresh routing, revision
summaries, digest, consolidation, and the agent ``graph_diff`` / ``graph_log``
verbs. The engine that produces them lives in ``rcp.history.engine``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from rcp.core.models import GraphState, Patch
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history.delta import SemanticDelta

_HEAD = re.compile(r"^(?:(main)|branch:([A-Za-z0-9._-]{1,200}))?@(head|0|[1-9][0-9]{0,11})$")


class HeadSpec(BaseModel):
    """An unresolved head reference: a target plus a revision or ``head``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: GraphTargetRef | None = None  # None: the run's own staged target.
    revision: int | Literal["head"]


def parse_head(text: str, names: Mapping[str, str] | None = None) -> HeadSpec:
    """Parse ``main@R``, ``branch:<id>@R``, ``main@head``, or a bare ``@R``/``@head``."""

    match = _HEAD.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a graph head reference: {text!r}")
    main, branch_id, revision = match.groups()
    if branch_id and names is not None and branch_id not in names:
        branch_id = next((key for key, name in names.items() if name == branch_id), branch_id)
    target = (
        GraphTargetRef()
        if main
        else GraphTargetRef(kind="branch", branch_id=branch_id)
        if branch_id
        else None
    )
    return HeadSpec(target=target, revision="head" if revision == "head" else int(revision))


def format_head(head: GraphHeadRef) -> str:
    return f"{head.target.key}@{head.revision}"


@dataclass(frozen=True)
class StepRecord:
    """One retained Patch on a graph and what it did.

    ``inherited`` marks main steps through a branch's base when read on that
    branch. A retained rejected receipt has ``accepted`` False and an empty
    delta; its after-head carries the previous transition id.
    """

    before_head: GraphHeadRef
    after_head: GraphHeadRef
    before_state: GraphState
    after_state: GraphState
    patch: Patch
    delta: SemanticDelta
    changed_node_ids: frozenset[str]
    changed_edge_ids: frozenset[str]
    accepted: bool
    inherited: bool = False


class _AnswerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldChange(_AnswerModel):
    field: str
    before: JsonValue = None
    after: JsonValue = None
    # Set when a long value continues on the next page.
    continued: bool = False


class EntityChange(_AnswerModel):
    kind: Literal["node", "edge", "proposal", "ambiguity", "glossary", "global"]
    id: str
    type: str | None = None
    title: str | None = None
    change: Literal["created", "updated", "removed"]
    fields: list[FieldChange] = Field(default_factory=list)


class StepAttribution(_AnswerModel):
    head: str
    producer: Literal["human", "agent", "system"]
    task_id: str | None = None
    episode_id: str | None = None
    summary: str = ""
    inherited: bool = False


class GraphDiffPage(_AnswerModel):
    """Net change from ``from_head`` to ``to_head``, with the steps behind it."""

    from_head: str
    to_head: str
    changes: list[EntityChange]
    steps: list[StepAttribution]
    cursor: str | None = None


class GraphLogEntry(_AnswerModel):
    step: StepAttribution
    change: EntityChange


class GraphLogPage(_AnswerModel):
    """Every step that touched one id, newest first, read at ``at_head``."""

    id: str
    at_head: str
    entries: list[GraphLogEntry]
    cursor: str | None = None


class HistoryEngine(Protocol):
    """One project's history, shared by every caller of every graph target."""

    def resolve(self, spec: HeadSpec, *, default_target: GraphTargetRef) -> GraphHeadRef:
        """Return the exact head, refusing nonexistent, out-of-range, or pre-base revisions."""
        ...

    def state_at(self, head: GraphHeadRef) -> GraphState: ...

    def steps(self, head: GraphHeadRef, *, include_inherited: bool = False) -> list[StepRecord]:
        """Every retained step through ``head``, oldest first."""
        ...

    def diff(
        self,
        from_head: GraphHeadRef,
        to_head: GraphHeadRef,
        *,
        ids: frozenset[str] = frozenset(),
        cursor: str | None = None,
    ) -> GraphDiffPage: ...

    def log(
        self, entity_id: str, at_head: GraphHeadRef, *, cursor: str | None = None
    ) -> GraphLogPage: ...
