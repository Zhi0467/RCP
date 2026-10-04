"""The Experiment guidance-validity rule that graph transitions generate.

An Experiment's ``current_summary`` and ``next_action`` are guidance written
against the graph around it. When that surrounding graph changes, the
transition manager marks the guidance stale; an explicit edit refreshes it.
This module owns the rule's triggers, its dependency signature, the actions it
generates, the shape replay accepts from it, and the projection of guidance
validity. ``rcp.core.transitions`` runs it without naming research node types.

Every value here is replay-critical: generated operations, their order, and
the trigger manifest are hashed or persisted, so changes need a new rule id.
"""

from __future__ import annotations

from typing import Any

from rcp.control import ExperimentControlState, experiment_control_dependencies
from rcp.core.models import Evidence, Experiment, GraphState, Patch
from rcp.core.operations import GraphOperation, NodeUpdate, UpdateNodesOperation
from rcp.core.project_types import project_type_of
from rcp.core.transition_models import (
    ExperimentGuidanceValidity,
    GuidanceFieldValidity,
    TransitionTrigger,
)

GUIDANCE_RULE_ID = "experiment.guidance-validity.v2"
# v2 also invalidates guidance when Evidence on a tested belief is retired.
# Replay validates recorded traces and never reruns a rule, so traces that
# recorded either version stay valid.
GUIDANCE_RULE_IDS = frozenset({"experiment.guidance-validity.v1", GUIDANCE_RULE_ID})


def guidance_triggers() -> list[TransitionTrigger]:
    """Return the conservative browser routing triggers for the guidance rule."""

    return [
        TransitionTrigger(
            operation="update_nodes",
            node_types=["blocker", "decision", "evidence", "experiment", "hypothesis"],
            node_fields=[
                "status",
                "selected_option",
                # A Decision's recorded choice is presented against its
                # options, so editing them changes a backend-owned answer.
                # Routing the draft keeps the previewed graph and that answer
                # from disagreeing until Sync.
                "options",
                "current_summary",
                "next_action",
                # Evidence a tested Hypothesis rests on stops counting once
                # it is retired.
                "validity",
            ],
        ),
        TransitionTrigger(operation="supersede_nodes", node_types=["evidence"]),
        TransitionTrigger(operation="merge_nodes", node_types=["evidence"]),
        TransitionTrigger(
            operation="create_edges",
            relations=[
                "blocked_by",
                "governed_by",
                "tests",
                "supports",
                "weakens",
                "refutes",
                "inconclusive",
                "contradicts",
            ],
        ),
        TransitionTrigger(
            operation="remove_edges",
            relations=[
                "blocked_by",
                "governed_by",
                "tests",
                "supports",
                "weakens",
                "refutes",
                "inconclusive",
                "contradicts",
            ],
        ),
        TransitionTrigger(
            operation="create_proposals",
            node_types=["decision"],
        ),
        TransitionTrigger(
            operation="resolve_proposals",
            node_types=["decision"],
        ),
        TransitionTrigger(
            operation="withdraw_proposals",
            node_types=["decision"],
        ),
    ]


def dependency_change_causes(
    timeline: list[tuple[int, GraphState, GraphState]],
) -> dict[str, int]:
    """Map each Experiment whose guidance dependencies changed to the first cause."""

    causes: dict[str, int] = {}
    for action_index, before, after in timeline:
        experiment_ids = sorted(
            node_id
            for node_id, node in {**before.nodes, **after.nodes}.items()
            if isinstance(node, Experiment) and node_id in after.nodes
        )
        for experiment_id in experiment_ids:
            # Creating an Experiment establishes its initial guidance
            # against the graph at that action.  Absence before creation is
            # not itself a dependency change.  A later edge or governance
            # mutation still observes the Experiment on both sides and
            # invalidates that guidance normally.
            if not isinstance(before.nodes.get(experiment_id), Experiment):
                continue
            before_signature = experiment_dependency_signature(before, experiment_id)
            after_signature = experiment_dependency_signature(after, experiment_id)
            if before_signature != after_signature:
                causes.setdefault(experiment_id, action_index)
    return causes


def explicit_guidance_updates(
    actions: list[tuple[Patch, GraphOperation]],
) -> dict[tuple[str, str], int]:
    """Map each explicitly written guidance field to the action that wrote it."""

    updates: dict[tuple[str, str], int] = {}
    for action_index, (_patch, operation) in enumerate(actions):
        if not isinstance(operation, UpdateNodesOperation):
            continue
        for update in operation.nodes:
            for field in ("current_summary", "next_action"):
                if field in update.changes:
                    updates[(update.id, field)] = action_index
    return updates


def guidance_actions(
    state: GraphState,
    invalidation_causes: dict[str, int],
    explicit_updates: dict[tuple[str, str], int],
) -> list[tuple[UpdateNodesOperation, int]]:
    """Return the guidance-validity operations to generate, each with its cause."""

    generated: list[tuple[UpdateNodesOperation, int]] = []
    experiment_ids = sorted(
        node_id for node_id, node in state.nodes.items() if isinstance(node, Experiment)
    )
    for experiment_id in experiment_ids:
        experiment = state.nodes[experiment_id]
        assert isinstance(experiment, Experiment)
        changes_by_cause: dict[int, dict[str, Any]] = {}
        invalidation_cause = invalidation_causes.get(experiment_id)
        for field, stale_field in (
            ("current_summary", "current_summary_stale"),
            ("next_action", "next_action_stale"),
        ):
            value = getattr(experiment, field)
            stale = getattr(experiment, stale_field)
            explicit_cause = explicit_updates.get((experiment_id, field))
            if invalidation_cause is not None:
                desired = bool(value)
                cause = invalidation_cause
            elif explicit_cause is not None:
                desired = False
                cause = explicit_cause
            else:
                continue
            if stale != desired:
                changes_by_cause.setdefault(cause, {})[stale_field] = desired
        for cause in sorted(changes_by_cause):
            generated.append(
                (
                    UpdateNodesOperation(
                        op="update_nodes",
                        nodes=[
                            NodeUpdate(
                                id=experiment_id,
                                changes=changes_by_cause[cause],
                            )
                        ],
                    ),
                    cause,
                )
            )
    return generated


def validate_guidance_operation(operation: GraphOperation) -> None:
    """Reject a recorded guidance-rule operation outside the rule's shape."""

    if not isinstance(operation, UpdateNodesOperation) or not operation.nodes:
        raise ValueError("guidance-validity rule may generate only non-empty update_nodes")
    allowed = {"current_summary_stale", "next_action_stale"}
    for update in operation.nodes:
        if not update.changes or not set(update.changes) <= allowed:
            raise ValueError("guidance-validity action changes a non-system field")
        if not all(isinstance(value, bool) for value in update.changes.values()):
            raise ValueError("guidance-validity values must be booleans")


def experiment_projections(
    state: GraphState,
    invalidation_event_by_field: dict[tuple[str, str], str],
) -> tuple[dict[str, ExperimentControlState], dict[str, ExperimentGuidanceValidity]]:
    """Project each Experiment's control state and guidance validity."""

    from rcp.control import derive_experiment_control_state

    controls: dict[str, ExperimentControlState] = {}
    guidance: dict[str, ExperimentGuidanceValidity] = {}
    for node_id, node in sorted(state.nodes.items()):
        if not isinstance(node, Experiment):
            continue
        controls[node_id] = derive_experiment_control_state(state, node_id)
        guidance[node_id] = ExperimentGuidanceValidity(
            current_summary=_guidance_field_validity(
                node.current_summary,
                node.current_summary_stale,
                invalidation_event_by_field.get((node_id, "current_summary_stale")),
            ),
            next_action=_guidance_field_validity(
                node.next_action,
                node.next_action_stale,
                invalidation_event_by_field.get((node_id, "next_action_stale")),
            ),
        )
    return controls, guidance


def experiment_dependency_signature(state: GraphState, experiment_id: str) -> tuple[Any, ...]:
    node = state.nodes.get(experiment_id)
    if not isinstance(node, Experiment):
        return ()
    control_dependencies = experiment_control_dependencies(state, experiment_id).model_dump(
        mode="json"
    )
    tests_relations = {
        (edge.id, edge.target)
        for edge in state.edges.values()
        if edge.source == experiment_id and edge.relation == "tests"
    }
    hypothesis_ids = {target for _edge_id, target in tests_relations}
    project_type = project_type_of(state)
    assessments: list[tuple[Any, ...]] = []
    for edge in state.edges.values():
        if edge.target not in hypothesis_ids or edge.relation not in (
            project_type.belief_outcome_relations
        ):
            continue
        source = state.nodes.get(edge.source)
        if not isinstance(source, Evidence):
            continue
        assessments.append(
            (
                edge.id,
                edge.source,
                edge.target,
                edge.relation,
                edge.assessment.model_dump(mode="json") if edge.assessment else None,
                # Retiring Evidence keeps its edges, so its lifecycle is part of
                # what the guidance was written against.
                getattr(source, project_type.lifecycle_field(source.type)),
            )
        )
    return (
        control_dependencies,
        tuple(sorted(tests_relations)),
        tuple(sorted(assessments, key=lambda item: item[0])),
    )


def _guidance_field_validity(
    value: str | None,
    stale: bool,
    event_id: str | None,
) -> GuidanceFieldValidity:
    if not value:
        return GuidanceFieldValidity(status="empty")
    if stale:
        return GuidanceFieldValidity(status="stale", invalidated_by_event_id=event_id)
    return GuidanceFieldValidity(status="current")
