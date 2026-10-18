from __future__ import annotations

import json
from typing import Annotated, Any, get_args

import pytest
from pydantic import BaseModel

from rcp.core.models import (
    PROPOSAL_FILING_STAMPS,
    PROPOSAL_RESOLUTION_FIELDS,
    BaseNode,
    Edge,
    Evidence,
    EvidenceAssessment,
    ExperimentAttempt,
    GraphField,
    GraphState,
    Proposal,
    graph_field,
)
from rcp.core.validation.constants import (
    IMMUTABLE_NODE_UPDATE_FIELDS,
    LEGACY_COMPATIBILITY_UPDATE_FIELDS,
)
from rcp.history.delta import (
    _SEMANTIC_AMBIGUITY_BOOKKEEPING,
    _SEMANTIC_EDGE_BOOKKEEPING,
    _SEMANTIC_GLOSSARY_BOOKKEEPING,
    _SEMANTIC_NODE_BOOKKEEPING,
    _SEMANTIC_PROPOSAL_BOOKKEEPING,
)


def _graph_models() -> set[type[BaseModel]]:
    """Walk actual field types and bases, including nested operation unions."""

    seen: set[type[BaseModel]] = set()
    pending: list[Any] = [GraphState]
    while pending:
        annotation = pending.pop()
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation in seen or annotation is BaseModel:
                continue
            seen.add(annotation)
            pending.extend(annotation.__bases__)
            pending.extend(field.annotation for field in annotation.model_fields.values())
        else:
            pending.extend(get_args(annotation))
    return seen


def test_every_reachable_graph_field_has_one_explicit_semantic_role() -> None:
    models = _graph_models()
    assert {BaseNode, ExperimentAttempt, EvidenceAssessment, Proposal} <= models
    for model in models:
        for name, field in model.model_fields.items():
            roles = [item for item in field.metadata if isinstance(item, GraphField)]
            assert len(roles) == 1, f"{model.__name__}.{name}"
            assert graph_field(field).role in {"content", "engine", "identity"}

    class Unclassified(BaseModel):
        value: str

    class MultiplyClassified(BaseModel):
        value: Annotated[str, GraphField("content"), GraphField("engine")]

    for model in (Unclassified, MultiplyClassified):
        with pytest.raises(ValueError):
            graph_field(model.model_fields["value"])


def test_metadata_preserves_existing_owner_policies_and_semantic_exclusions() -> None:
    # These are the old literals, retained here once as the migration contract.
    assert {
        "related_node_ids",
        "related_edge_ids",
        "related_config_keys",
        "dependency_rule",
        "base_rev",
        "created_by",
        "created_by_operation_id",
        "raised_rev",
    } == PROPOSAL_FILING_STAMPS
    assert {
        "status",
        "resolved_rev",
        "resolved_by",
        "resolved_by_operation_id",
        "resolution_reason",
        "rejection_reason",
    } == PROPOSAL_RESOLUTION_FIELDS
    assert {
        "id",
        "type",
        "extension_type",
        "standing",
        "created_rev",
        "updated_rev",
        "legacy_strength",
        "current_summary_stale",
        "next_action_stale",
    } == IMMUTABLE_NODE_UPDATE_FIELDS
    assert {
        "legacy_strength",
        "current_summary_stale",
        "next_action_stale",
    } == LEGACY_COMPATIBILITY_UPDATE_FIELDS
    assert {
        "created_rev",
        "updated_rev",
        "current_summary_stale",
        "next_action_stale",
    } == _SEMANTIC_NODE_BOOKKEEPING
    assert {"created_rev"} == _SEMANTIC_EDGE_BOOKKEEPING
    assert (
        PROPOSAL_FILING_STAMPS
        | {
            "resolved_rev",
            "resolved_by_operation_id",
        }
        == _SEMANTIC_PROPOSAL_BOOKKEEPING
    )
    assert {"raised_rev"} == _SEMANTIC_AMBIGUITY_BOOKKEEPING
    assert {"updated_rev"} == _SEMANTIC_GLOSSARY_BOOKKEEPING
    assert graph_field(BaseNode.model_fields["standing"]) == GraphField(
        "content", node_update="immutable"
    )
    assert graph_field(Evidence.model_fields["legacy_strength"]) == GraphField(
        "content", node_update="replay_only"
    )
    assert graph_field(Proposal.model_fields["status"]) == GraphField(
        "content", proposal_owner="resolution"
    )
    assert graph_field(Proposal.model_fields["resolved_rev"]) == GraphField(
        "engine", proposal_owner="resolution"
    )
    # Derived ownership does not remove semantically meaningful edge layers.
    assert graph_field(Edge.model_fields["layer"]).role == "content"


def test_old_writer_graph_roundtrip_retains_values_without_field_metadata() -> None:
    # Pre-redesign records explicitly retain their dependency rule and the retired
    # Evidence label. New annotation metadata is never a persisted field.
    old_document = {
        "revision": 7,
        "nodes": {
            "ev/legacy": {
                "id": "ev/legacy",
                "type": "evidence",
                "title": "Prior observation",
                "observation": "The measured effect occurred.",
                "role": "result",
                "legacy_strength": "supporting",
                "standing": "accepted",
                "created_rev": 1,
                "updated_rev": 3,
            }
        },
        "proposals": {
            "prop/legacy": {
                "id": "prop/legacy",
                "title": "Revise the prior observation",
                "card": {},
                "ops": [
                    {
                        "op": "update_nodes",
                        "intent": "content_change",
                        "nodes": [
                            {
                                "id": "ev/legacy",
                                "changes": {"observation": "The measured effect occurred twice."},
                            }
                        ],
                    }
                ],
                "dependency_rule": 1,
                "base_rev": 3,
                "raised_rev": 4,
                "related_node_ids": ["ev/legacy"],
            }
        },
    }
    old_bytes = json.dumps(old_document, sort_keys=True)
    restored = GraphState.model_validate_json(old_bytes)
    assert (
        json.dumps(restored.model_dump(mode="json", exclude_unset=True), sort_keys=True)
        == old_bytes
    )


def test_merge_contract_binds_values_heads_and_answers_without_patch_authority() -> None:
    from uuid import uuid4

    from pydantic import ValidationError

    from rcp.core.authority import CapturedChangeAuthority
    from rcp.core.models import AuthorizedHuman, BranchMergeProvenance, Patch
    from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
    from rcp.runs.merge_contract import (
        FieldConflict,
        MergeAnswers,
        MergeCheckReceipt,
        MergePlan,
        MergePreparationContract,
        MissingValue,
        PresentValue,
    )

    missing, null = MissingValue(), PresentValue(value=None)
    assert missing.model_dump() != null.model_dump()
    field = dict(
        conflict_id="node:status",
        collection="nodes",
        entity_id="hyp/claim",
        field_name="status",
        base=PresentValue(value="proposed"),
        branch=PresentValue(value="supported"),
        destination=PresentValue(value="refuted"),
    )
    with pytest.raises(ValidationError):
        FieldConflict(**field, allowed_answer_kinds=("branch", "destination", "merged"))
    conflict = FieldConflict(**field, allowed_answer_kinds=("branch", "destination"))
    with pytest.raises(ValidationError):
        conflict.field_name = "title"
    with pytest.raises(ValidationError):
        MergeAnswers.model_validate({"answers": {}, "cards": {}, "ops": []})
    with pytest.raises(ValidationError):
        MergeAnswers.model_validate({"answers": {"node:status": {"kind": "merged"}}, "cards": {}})
    answers = MergeAnswers.model_validate(
        {"answers": {"node:status": {"kind": "branch"}}, "cards": {}}
    )
    assert MergeAnswers.model_validate_json(answers.model_dump_json()) == answers

    branch_id = str(uuid4())
    base_head = GraphHeadRef(target=GraphTargetRef(), revision=0)
    source_head = GraphHeadRef(
        target=GraphTargetRef(kind="branch", branch_id=branch_id), revision=0
    )
    provenance = BranchMergeProvenance(
        merge_id="a" * 64,
        branch_id=branch_id,
        episode_id=None,
        branch_base_head=base_head,
        branch_head=source_head,
        rebased_main_head=base_head,
        merge_task_id="merge-task",
    )
    contract = MergePreparationContract(
        ruleset_tag="test",
        trigger_manifest={},
        transition_trace_schema={},
        orchestrator_patch_schema={},
    )
    authorizer = AuthorizedHuman(space_id=str(uuid4()), user_id=str(uuid4()), display_name="Member")
    template = Patch(
        kind="work",
        author="agent",
        summary="Merge source",
        ops=[],
        profile="orchestrator",
        task_id="merge-task",
        source_operation_id="merge-task",
        authorized_by=authorizer,
        branch_merge=provenance,
        run_truth_scope=["repository"],
    )
    plan = MergePlan(
        captured_authority=CapturedChangeAuthority(
            project_id="project", apply_target=base_head.target, patch_template=template
        ),
        context_id="b" * 64,
        merge_task_id="merge-task",
        authorized_by=authorizer,
        provenance=provenance,
        base_head=base_head,
        source_head=source_head,
        destination_head=base_head,
        base_graph=GraphState(),
        source_graph=GraphState(),
        destination_graph=GraphState(),
        run_truth_scope=("repository",),
        conflicts=(conflict,),
        preparation_contract=contract,
    )
    assert len(plan.plan_id) == 64
    assert MergePlan.model_validate_json(plan.model_dump_json()) == plan
    changed = plan.model_dump(mode="json")
    changed["context_id"] = "c" * 64
    with pytest.raises(ValidationError):
        MergePlan.model_validate(changed)
    changed.pop("plan_id")
    assert MergePlan.model_validate(changed).plan_id != plan.plan_id
    receipt = MergeCheckReceipt(
        task_id="merge-task",
        turn_id="turn-1",
        plan_id=plan.plan_id,
        answers_digest="d" * 64,
        source_head=source_head,
        destination_head=base_head,
        preparation_contract=contract,
    )
    assert MergeCheckReceipt.model_validate_json(receipt.model_dump_json()) == receipt
