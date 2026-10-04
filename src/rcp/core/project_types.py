"""Project types: the node-type questions kernel code asks a project.

Authority, validation, materialization, history, and run code ask the
project's type which node types play which part, instead of naming node types
or node classes themselves. The research type answers today
(``rcp.core.research_type``); another type would answer the same questions
differently, and the code asking them would not change. See
``docs/decisions/2026-10-04-the-kernel-asks-node-type-questions.md``.

Custom ontology types are stored as their base type, so every answer here is
keyed by the base ``node.type``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rcp.core.models import GraphState


@dataclass(frozen=True)
class ProjectType:
    """The answers one kind of project gives about its node types."""

    name: str
    # Base node types in their canonical order, which also orders rendered prose.
    node_types: tuple[str, ...]
    # Reader-facing singular and plural names, as agent contracts spell them.
    labels: Mapping[str, str]
    plural_labels: Mapping[str, str]
    # Existing nodes of these types are protected beliefs (invariant 3b): agents
    # may create them, but structural or semantic changes need a Proposal.
    protected_belief_types: frozenset[str]
    # Beliefs that outcomes bear on. A status change on one records a belief
    # transition and needs a cause.
    belief_types: frozenset[str]
    # Nodes that record an observation bearing on a belief.
    outcome_types: frozenset[str]
    # Relations through which an outcome bears on a belief. They carry an
    # assessment and may be cited as the cause of a belief status change.
    belief_outcome_relations: frozenset[str]
    # The node a bounded loop drives.
    control_node_types: frozenset[str]
    # Nodes whose outcome is a human (or orchestrator) choice among options.
    chooser_types: frozenset[str]
    # Nodes that record an obstacle gating action.
    blocker_types: frozenset[str]
    # Nodes that frame the project's open questions.
    question_types: frozenset[str]
    # Structural relations into an existing protected belief; restructuring one
    # needs a Proposal, like editing the belief itself.
    protected_relations: frozenset[str]
    # Relations from an action node to a blocker that gates it.
    blocking_relations: frozenset[str]
    # The field supersede and merge set to ``retired_value``, where it is not
    # ``default_lifecycle_field``.
    lifecycle_fields: Mapping[str, str]
    default_lifecycle_field: str = "status"
    retired_value: str = "superseded"

    def is_protected_belief(self, node_type: str | None) -> bool:
        return node_type in self.protected_belief_types

    def is_belief(self, node_type: str | None) -> bool:
        return node_type in self.belief_types

    def is_outcome(self, node_type: str | None) -> bool:
        return node_type in self.outcome_types

    def is_control_node(self, node_type: str | None) -> bool:
        return node_type in self.control_node_types

    def is_chooser(self, node_type: str | None) -> bool:
        return node_type in self.chooser_types

    def is_blocker(self, node_type: str | None) -> bool:
        return node_type in self.blocker_types

    def is_question(self, node_type: str | None) -> bool:
        return node_type in self.question_types

    def lifecycle_field(self, node_type: str) -> str:
        """The field supersede and merge set to ``retired_value`` on ``node_type``."""

        return self.lifecycle_fields.get(node_type, self.default_lifecycle_field)

    def ordered(self, node_types: Iterable[str]) -> list[str]:
        """``node_types`` in canonical order."""

        wanted = set(node_types)
        return [node_type for node_type in self.node_types if node_type in wanted]

    def label(self, node_type: str) -> str:
        return self.labels[node_type]

    def label_list(
        self, node_types: Iterable[str], *, conjunction: str = "or", plural: bool = False
    ) -> str:
        """Join type names for prose, e.g. ``ResearchQuestion or Hypothesis``."""

        names = self.plural_labels if plural else self.labels
        words = [names[node_type] for node_type in self.ordered(node_types)]
        if len(words) <= 2:
            return f" {conjunction} ".join(words)
        return f"{', '.join(words[:-1])}, {conjunction} {words[-1]}"


@cache
def _research() -> ProjectType:
    from rcp.core.research_type import RESEARCH

    return RESEARCH


def project_type_of(state: GraphState | None = None) -> ProjectType:
    """The type of the project ``state`` belongs to.

    Every project is a research project today, so the answer does not yet
    depend on ``state``. When a second type exists, a project's type will be
    recorded in its canonical identity, and a project without that record is a
    research project, so replay stays deterministic. Callers that hold a graph
    pass it so that change needs no call-site edits.
    """

    return _research()
