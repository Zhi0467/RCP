from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from rcp.core.models import GraphState, Patch
from rcp.core.validation.report import ValidationReport


@dataclass
class OpContext:
    """Everything an operation rule needs to judge one operation of a patch.

    ``repositories`` is deliberately mutable and shared across the operations of
    a single patch: a ``set_project_truth_scope`` operation that introduces a new
    repository descriptor makes that alias available to the operations after it.
    """

    state: GraphState
    patch: Patch
    report: ValidationReport
    revision: int | None
    project_truth_scope: set[str]
    repositories: set[str]
    machines: set[str] | None
    default_run_truth_scope: set[str]
    state_repository: str | None
    mode: Literal["admission", "replay"]


#: Validates one operation, reporting into ``ctx.report``. Returns the oldest
#: source-reference timestamp the operation cited, or ``None`` when it cites none.
OpValidator = Callable[[dict[str, Any], OpContext], Any]

#: Returns the graph nodes and project-config keys one operation depends on, as
#: ``(candidate node ids, config keys)``. Candidates are filtered against the
#: graph by the caller, so a rule may return ids that do not exist.
OpDependencies = Callable[[dict[str, Any], GraphState], tuple[list[Any], list[str]]]


@dataclass(frozen=True)
class OpRule:
    """One entry of the operation vocabulary.

    An operation with no ``validate`` carries no operation-level checks; one with
    no ``dependencies`` cannot make a proposal depend on existing graph state.
    Per-operation metadata belongs here as further fields.
    """

    structural_validate: OpValidator | None = None
    authoring_validate: OpValidator | None = None
    dependencies: OpDependencies | None = None
