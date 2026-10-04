"""Node-type questions the kernel asks, answered for the research ontology.

Authority, validation, materialization, history, and run code ask these
questions instead of naming node types or node classes themselves. Another
domain ontology would answer the same questions differently; the kernel code
that asks them would not change. See
``docs/decisions/2026-10-04-the-kernel-asks-node-type-questions.md``.

Custom ontology types are stored as their base type, so every answer here is
keyed by the base ``node.type``.
"""

from __future__ import annotations

# Existing nodes of these types are protected beliefs (invariant 3b): agents
# may create them, but structural or semantic changes need a Proposal.
PROTECTED_BELIEF_TYPES: frozenset[str] = frozenset({"research_question", "hypothesis"})

# Relations through which an outcome node bears on a protected belief. They
# carry an assessment and may be cited as the cause of a belief status change.
BELIEF_OUTCOME_RELATIONS: frozenset[str] = frozenset(
    {"supports", "weakens", "refutes", "inconclusive", "contradicts"}
)

# The value a node's lifecycle field takes when supersede or merge retires it.
RETIRED_VALUE = "superseded"
_LIFECYCLE_FIELDS: dict[str, str] = {"evidence": "validity"}


def is_protected_belief_type(node_type: str | None) -> bool:
    """Whether an existing node of ``node_type`` is a protected belief."""

    return node_type in PROTECTED_BELIEF_TYPES


def lifecycle_field(node_type: str) -> str:
    """The field supersede and merge set to ``RETIRED_VALUE`` on ``node_type``."""

    return _LIFECYCLE_FIELDS.get(node_type, "status")
