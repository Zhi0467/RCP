from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from rcp.config import Manifest
from rcp.core.models import GraphState, Patch
from rcp.history import HistoryManager
from rcp.paper import PaperService
from rcp.service import (
    GraphSyncRequest,
    NodeEditConflict,
    ProjectService,
    ProposalDecisionRequest,
    ReviewRequest,
)
from rcp.storage import AppStore
from tests.helpers import fabricated_authorizer, seed_patch, unchecked_write_authority

QUESTION_ID = "rq/learning-after-shift"
HYPOTHESIS_ID = "hyp/replanning-restores-plasticity"
OTHER_HYPOTHESIS_ID = "hyp/alternate-mechanism"
STATUS_EVIDENCE_ID = "ev/replanning-status"
STATUS_EDGE_ID = "edge/replanning-status"
PROTECTED_EDGE_ID = "rq/learning-after-shift::has_hypothesis::hyp/replanning-restores-plasticity"


INTENT_OPERATIONS: dict[str, dict[str, Any]] = {
    "content_change": {
        "op": "update_nodes",
        "intent": "content_change",
        "nodes": [
            {
                "id": QUESTION_ID,
                "changes": {
                    "question": "Can adaptation remain plastic after repeated task shifts?"
                },
            }
        ],
    },
    "removal": {
        "op": "remove_nodes",
        "intent": "removal",
        "node_ids": [HYPOTHESIS_ID],
    },
    "supersede": {
        "op": "supersede_nodes",
        "intent": "supersede",
        "nodes": [{"id": HYPOTHESIS_ID, "superseded_by": OTHER_HYPOTHESIS_ID}],
    },
    "merge": {
        "op": "merge_nodes",
        "intent": "merge",
        "merges": [{"duplicate": HYPOTHESIS_ID, "canonical": OTHER_HYPOTHESIS_ID}],
    },
    "protected_relation_change": {
        "op": "remove_edges",
        "intent": "protected_relation_change",
        "edge_ids": [PROTECTED_EDGE_ID],
    },
    "status_change": {
        "op": "update_nodes",
        "intent": "status_change",
        "nodes": [
            {
                "id": HYPOTHESIS_ID,
                "changes": {"status": "supported"},
                "cause": {"kind": "evidence_edge", "ref_id": STATUS_EDGE_ID},
            }
        ],
    },
}


def _service(manifest: Manifest, tmp_path) -> ProjectService:
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    history.append(seed_patch())
    history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Prepared Proposal judgment targets.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": OTHER_HYPOTHESIS_ID,
                            "type": "hypothesis",
                            "title": "An alternate plasticity mechanism",
                            "statement": "A separate mechanism restores future learning ability.",
                            "rationale": "It provides a distinct canonical target.",
                            "predictions": ["Held-out adaptation recovers."],
                            "status": "proposed",
                        },
                        {
                            "id": STATUS_EVIDENCE_ID,
                            "type": "evidence",
                            "title": "Replanning status evidence",
                            "observation": "Held-out adaptation recovered.",
                            "origin": "analytic",
                        },
                    ],
                },
                {
                    "op": "create_edges",
                    "edges": [
                        {
                            "id": STATUS_EDGE_ID,
                            "source": STATUS_EVIDENCE_ID,
                            "target": HYPOTHESIS_ID,
                            "relation": "supports",
                            "assessment": {
                                "relevance": "direct",
                                "weight": "moderate",
                            },
                        }
                    ],
                },
            ],
        )
    )
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.sqlite3")),
    )
    for node_id in (QUESTION_ID, HYPOTHESIS_ID, OTHER_HYPOTHESIS_ID):
        service.review_node(node_id, ReviewRequest(standing="contested"))
    return service


def _append_proposal(service: ProjectService, intent: str) -> str:
    proposal_id = f"prop/{intent.replace('_', '-')}"
    service.history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary=f"Proposed one {intent} change.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": proposal_id,
                            "title": f"Judge {intent.replace('_', ' ')}",
                            "card": {"decision_needed": "Approve or reject this belief change."},
                            "ops": [deepcopy(INTENT_OPERATIONS[intent])],
                        }
                    ],
                }
            ],
        )
    )
    return proposal_id


def _standing(state: GraphState, node_id: str) -> str | None:
    node = state.nodes.get(node_id)
    return None if node is None else node.standing.value


@pytest.mark.parametrize("intent", list(INTENT_OPERATIONS))
def test_proposal_approval_uses_the_declared_intent_standing_target(
    manifest: Manifest, tmp_path, intent: str
) -> None:
    service = _service(manifest, tmp_path)
    if intent == "removal":
        service.review_node(HYPOTHESIS_ID, ReviewRequest(standing="accepted"))
    proposal_id = _append_proposal(service, intent)

    state = service.decide_proposal(
        proposal_id,
        ProposalDecisionRequest(decision="approved", reason="The proposal is sound."),
    )

    assert state.proposals[proposal_id].status == "approved"
    assert state.proposals[proposal_id].resolution_reason == "The proposal is sound."
    assert state.proposals[proposal_id].rejection_reason is None
    expected_standing = {
        "content_change": ("accepted", "contested", "contested"),
        "removal": ("contested", None, "contested"),
        "supersede": ("contested", "accepted", "contested"),
        "merge": ("contested", "accepted", "contested"),
        "protected_relation_change": ("contested", "contested", "contested"),
        "status_change": ("contested", "accepted", "contested"),
    }[intent]
    assert (
        _standing(state, QUESTION_ID),
        _standing(state, HYPOTHESIS_ID),
        _standing(state, OTHER_HYPOTHESIS_ID),
    ) == expected_standing

    if intent == "content_change":
        assert state.nodes[QUESTION_ID].question.endswith("repeated task shifts?")
    elif intent == "removal":
        assert HYPOTHESIS_ID not in state.nodes
        assert STATUS_EDGE_ID not in state.edges
    elif intent == "supersede":
        assert state.nodes[HYPOTHESIS_ID].status == "superseded"
        assert f"{HYPOTHESIS_ID}::supersedes::{OTHER_HYPOTHESIS_ID}" in state.edges
    elif intent == "merge":
        assert state.nodes[HYPOTHESIS_ID].status == "superseded"
        assert f"{HYPOTHESIS_ID}::duplicate_of::{OTHER_HYPOTHESIS_ID}" in state.edges
    elif intent == "protected_relation_change":
        assert PROTECTED_EDGE_ID not in state.edges
    else:
        assert state.nodes[HYPOTHESIS_ID].status == "supported"


@pytest.mark.parametrize("intent", list(INTENT_OPERATIONS))
def test_proposal_rejection_resolves_only_and_leaves_graph_exactly_unchanged(
    manifest: Manifest, tmp_path, intent: str
) -> None:
    service = _service(manifest, tmp_path)
    proposal_id = _append_proposal(service, intent)
    before = service.history.state()

    state = service.decide_proposal(
        proposal_id,
        ProposalDecisionRequest(decision="rejected", reason="The evidence is insufficient."),
    )

    assert state.proposals[proposal_id].status == "rejected"
    assert state.proposals[proposal_id].resolution_reason == "The evidence is insufficient."
    assert state.proposals[proposal_id].rejection_reason == "The evidence is insufficient."
    assert state.nodes == before.nodes
    assert state.edges == before.edges


def test_sync_rejects_content_proposal_without_changing_the_question(
    manifest: Manifest, tmp_path
) -> None:
    service = _service(manifest, tmp_path)
    proposal_id = _append_proposal(service, "content_change")
    before = service.history.state()

    state = service.sync_graph(
        GraphSyncRequest(
            base_revision=before.revision,
            proposals=[{"proposal_id": proposal_id, "decision": "rejected"}],
        ),
        active_control_node_ids=set(),
    )

    assert state.proposals[proposal_id].status == "rejected"
    assert state.nodes == before.nodes
    assert state.edges == before.edges


def test_sync_approves_removal_without_reviewing_the_deleted_node(
    manifest: Manifest, tmp_path
) -> None:
    service = _service(manifest, tmp_path)
    service.review_node(HYPOTHESIS_ID, ReviewRequest(standing="accepted"))
    proposal_id = _append_proposal(service, "removal")
    before = service.history.state()

    state = service.sync_graph(
        GraphSyncRequest(
            base_revision=before.revision,
            proposals=[{"proposal_id": proposal_id, "decision": "approved"}],
        ),
        active_control_node_ids=set(),
    )

    assert HYPOTHESIS_ID not in state.nodes
    assert state.proposals[proposal_id].status == "approved"


def test_removal_approval_withdraws_if_a_new_incident_relation_was_not_judged(
    manifest: Manifest,
    tmp_path,
) -> None:
    service = _service(manifest, tmp_path)
    proposal_id = _append_proposal(service, "removal")
    proposal = service.history.state().proposals[proposal_id]
    assert PROTECTED_EDGE_ID in proposal.related_edge_ids
    service.history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Added a later incident relation.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_edges",
                    "edges": [
                        {
                            "id": "edge/later-incident",
                            "source": STATUS_EVIDENCE_ID,
                            "target": HYPOTHESIS_ID,
                            "relation": "weakens",
                            "assessment": {
                                "relevance": "direct",
                                "weight": "moderate",
                            },
                        }
                    ],
                }
            ],
        )
    )

    state = service.decide_proposal(
        proposal_id,
        ProposalDecisionRequest(decision="approved"),
    )

    assert state.proposals[proposal_id].status == "withdrawn"
    assert HYPOTHESIS_ID in state.nodes
    assert "edge/later-incident" in state.edges


def test_sync_stages_multiple_proposal_judgments_against_evolving_state(
    manifest: Manifest,
    tmp_path,
) -> None:
    service = _service(manifest, tmp_path)
    first_id = "prop/first-question-wording"
    second_id = "prop/second-question-wording"
    independent_id = "prop/independent-status"
    first_operation = deepcopy(INTENT_OPERATIONS["content_change"])
    second_operation = deepcopy(INTENT_OPERATIONS["content_change"])
    second_operation["nodes"][0]["changes"]["question"] = (
        "Can a different wording survive repeated shifts?"
    )
    service.history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Raised overlapping and independent judgments.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": proposal_id,
                            "title": title,
                            "card": {"decision_needed": "Approve or reject this belief change."},
                            "ops": [operation],
                        }
                        for proposal_id, title, operation in (
                            (first_id, "Use the first wording", first_operation),
                            (second_id, "Use the second wording", second_operation),
                            (
                                independent_id,
                                "Judge the independent status",
                                deepcopy(INTENT_OPERATIONS["status_change"]),
                            ),
                        )
                    ],
                }
            ],
        )
    )
    before = service.history.state()

    state = service.sync_graph(
        GraphSyncRequest(
            base_revision=before.revision,
            proposals=[
                {"proposal_id": first_id, "decision": "approved"},
                {"proposal_id": second_id, "decision": "approved"},
                {"proposal_id": independent_id, "decision": "rejected"},
            ],
        ),
        active_control_node_ids=set(),
    )

    assert state.nodes[QUESTION_ID].question == (
        "Can adaptation remain plastic after repeated task shifts?"
    )
    assert state.nodes[QUESTION_ID].standing == "accepted"
    assert state.nodes[HYPOTHESIS_ID].standing == "contested"
    assert state.proposals[first_id].status == "approved"
    assert state.proposals[second_id].status == "withdrawn"
    assert state.proposals[independent_id].status == "rejected"


def test_sync_withdraws_the_second_proposal_that_creates_the_same_edge(
    manifest: Manifest,
    tmp_path,
) -> None:
    service = _service(manifest, tmp_path)
    first_id = "prop/first-new-protected-edge"
    second_id = "prop/second-new-protected-edge"
    operation = {
        "op": "create_edges",
        "intent": "protected_relation_change",
        "edges": [
            {
                "source": QUESTION_ID,
                "target": OTHER_HYPOTHESIS_ID,
                "relation": "has_hypothesis",
            }
        ],
    }
    service.history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Raised two proposals for the same protected edge.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": proposal_id,
                            "title": title,
                            "card": {"decision_needed": "Approve creating this relation."},
                            "ops": [deepcopy(operation)],
                        }
                        for proposal_id, title in (
                            (first_id, "Create the first relation"),
                            (second_id, "Create the second relation"),
                        )
                    ],
                }
            ],
        )
    )
    before = service.history.state()

    state = service.sync_graph(
        GraphSyncRequest(
            base_revision=before.revision,
            proposals=[
                {"proposal_id": first_id, "decision": "approved"},
                {"proposal_id": second_id, "decision": "approved"},
            ],
        ),
        active_control_node_ids=set(),
    )

    edge_id = f"{QUESTION_ID}::has_hypothesis::{OTHER_HYPOTHESIS_ID}"
    assert edge_id in state.edges
    assert state.proposals[first_id].status == "approved"
    assert state.proposals[second_id].status == "withdrawn"


def test_sync_refuses_approving_a_proposal_and_directly_editing_its_node(
    manifest: Manifest,
    tmp_path,
) -> None:
    service = _service(manifest, tmp_path)
    proposal_id = _append_proposal(service, "content_change")
    before = service.history.state()
    question = before.nodes[QUESTION_ID]

    with pytest.raises(NodeEditConflict):
        service.sync_graph(
            GraphSyncRequest(
                base_revision=before.revision,
                proposals=[{"proposal_id": proposal_id, "decision": "approved"}],
                nodes=[
                    {
                        "node_id": QUESTION_ID,
                        "base_updated_rev": question.updated_rev,
                        "changes": {"question": "A conflicting direct human wording."},
                    }
                ],
            ),
            active_control_node_ids=set(),
        )

    unchanged = service.history.state()
    assert unchanged.revision == before.revision
    assert unchanged.proposals[proposal_id].status == "pending"
    assert unchanged.nodes[QUESTION_ID] == question


@pytest.mark.parametrize(
    ("intent", "relation"),
    [("supersede", "supersedes"), ("merge", "duplicate_of")],
)
def test_lifecycle_proposal_withdraws_if_its_implicit_edge_appears_first(
    manifest: Manifest,
    tmp_path,
    intent: str,
    relation: str,
) -> None:
    service = _service(manifest, tmp_path)
    proposal_id = _append_proposal(service, intent)
    edge_id = f"{HYPOTHESIS_ID}::{relation}::{OTHER_HYPOTHESIS_ID}"
    competing_id = f"prop/competing-{intent}"
    service.history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Raised a competing lifecycle Proposal.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": competing_id,
                            "title": "Apply the competing lifecycle change",
                            "card": {"decision_needed": "Approve this lifecycle change."},
                            "ops": [deepcopy(INTENT_OPERATIONS[intent])],
                        }
                    ],
                }
            ],
        )
    )
    service.decide_proposal(
        competing_id,
        ProposalDecisionRequest(decision="approved"),
    )

    state = service.decide_proposal(
        proposal_id,
        ProposalDecisionRequest(decision="approved"),
    )

    assert state.proposals[proposal_id].status == "withdrawn"
    assert state.nodes[HYPOTHESIS_ID].status == "superseded"
    assert edge_id in state.edges


def _merge_review_patch(state: GraphState, *operations: dict[str, Any]) -> Patch:
    """A merge envelope exercises the canonical merge-only review boundary."""
    branch_id = "0171cb29-ae60-4021-9d8b-9276839048c4"
    return Patch.model_validate(
        {
            "kind": "work",
            "author": "agent",
            "profile": "orchestrator",
            "authorized_by": fabricated_authorizer().model_dump(mode="json"),
            "task_id": "task/merge-review",
            "summary": "Review connected branch changes.",
            "run_truth_scope": ["repo-a"],
            "repositories_read": ["repo-a"],
            "branch_merge": {
                "merge_id": "a" * 64,
                "branch_id": branch_id,
                "episode_id": None,
                "branch_base_head": {"revision": 1},
                "branch_head": {
                    "revision": state.revision,
                    "target": {"kind": "branch", "branch_id": branch_id},
                },
                "rebased_main_head": {"revision": state.revision},
                "merge_task_id": "task/merge-review",
            },
            "ops": [
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": "prop/merge-group",
                            "title": "Review a connected merge group",
                            "card": {"decision_needed": "Approve these changes together?"},
                            "ops": list(operations),
                        }
                    ],
                }
            ],
        }
    )


def test_atomic_protected_edge_replacement_approves_and_preserves_old_dependency_rules(
    manifest: Manifest,
    tmp_path,
) -> None:
    from rcp.core.materialize import apply_valid_patch, prepare_patch_bookkeeping
    from rcp.core.operations import CreateProposalsOperation
    from rcp.core.validation import ValidationContext, validate_patch
    from rcp.core.validation.proposals import (
        ProposalResource,
        proposal_dependencies_and_effects,
        proposal_is_stale,
    )

    service = _service(manifest, tmp_path)
    state = service.history.materialize().state
    old_edge = state.edges[PROTECTED_EDGE_ID]
    patch = _merge_review_patch(
        state,
        deepcopy(INTENT_OPERATIONS["protected_relation_change"]),
        {
            "op": "create_edges",
            "intent": "protected_relation_change",
            "edges": [
                {
                    "id": PROTECTED_EDGE_ID,
                    "source": old_edge.source,
                    "target": old_edge.target,
                    "relation": old_edge.relation,
                    "explanation": "Revised relationship explanation.",
                }
            ],
        },
    )
    patch = prepare_patch_bookkeeping(state, patch).model_copy(
        update={"revision": state.revision + 1}
    )
    report = validate_patch(state, patch, ValidationContext(["repo-a", "repo-b"]))
    assert not report.rejected, report.messages
    filed = apply_valid_patch(state, patch)
    proposal = filed.proposals["prop/merge-group"]
    assert not proposal_is_stale(filed, proposal)
    footprint = proposal_dependencies_and_effects(filed, proposal)
    edge_resource = ProposalResource(kind="edge", id=PROTECTED_EDGE_ID)
    assert edge_resource in footprint.dependencies & footprint.effects
    assert PROTECTED_EDGE_ID not in footprint.required_absent_edge_ids
    # Rule 1 and rule 2 records retain the old required-absence behavior, and
    # replay leaves their persisted input document and recorded rule untouched.
    operation = patch.ops[0]
    assert isinstance(operation, CreateProposalsOperation)
    for rule in (1, 2):
        old_document = patch.model_dump(mode="json")
        old_document["ops"][0]["proposals"][0]["dependency_rule"] = rule
        if rule == 1:
            del old_document["ops"][0]["proposals"][0]["dependency_rule"]
        old_patch = Patch.model_validate(old_document)
        before_replay = old_patch.model_dump_json()
        replayed = apply_valid_patch(state, old_patch)
        old_proposal = replayed.proposals[proposal.id]
        assert old_proposal.dependency_rule == rule
        assert proposal_is_stale(replayed, old_proposal)
        assert old_patch.model_dump_json() == before_replay
    from rcp.service import _proposal_judgment_patch

    approval = _proposal_judgment_patch(
        filed, proposal, decision="approved", reason="Reviewed the replacement."
    ).model_copy(update={"revision": filed.revision + 1})
    report = validate_patch(filed, approval, ValidationContext(["repo-a", "repo-b"]))
    assert not report.rejected, report.messages
    approved = apply_valid_patch(filed, approval)
    assert approved.edges[PROTECTED_EDGE_ID].explanation == "Revised relationship explanation."
    assert approved.nodes[QUESTION_ID].standing == "contested"
    assert approved.nodes[HYPOTHESIS_ID].standing == "contested"


def test_review_footprints_separate_shared_reads_from_approval_writes(
    manifest: Manifest,
    tmp_path,
) -> None:
    from rcp.core.models import CURRENT_PROPOSAL_DEPENDENCY_RULE, Proposal
    from rcp.core.validation.proposals import ProposalResource, proposal_dependencies_and_effects

    service = _service(manifest, tmp_path)
    state = service.history.materialize().state

    def footprint(identity, operation):
        proposal = Proposal.model_validate(
            {
                "id": identity,
                "title": "Review one effect",
                "card": {"decision_needed": "Approve?"},
                "dependency_rule": CURRENT_PROPOSAL_DEPENDENCY_RULE,
                "ops": [operation],
            }
        )
        return proposal_dependencies_and_effects(state, proposal)

    left = footprint(
        "prop/left",
        {
            "op": "supersede_nodes",
            "intent": "supersede",
            "nodes": [{"id": HYPOTHESIS_ID, "superseded_by": OTHER_HYPOTHESIS_ID}],
        },
    )
    # Both independent changes read the successor. Reading it cannot accept its
    # standing or make the other review stale.
    right = footprint(
        "prop/right",
        {
            "op": "create_edges",
            "intent": "protected_relation_change",
            "edges": [
                {
                    "source": QUESTION_ID,
                    "target": OTHER_HYPOTHESIS_ID,
                    "relation": "has_hypothesis",
                }
            ],
        },
    )
    successor = ProposalResource(kind="node", id=OTHER_HYPOTHESIS_ID)
    assert successor in left.dependencies & right.dependencies
    assert successor not in left.effects | right.effects
    assert not (left.effects & right.dependencies or right.effects & left.dependencies)
    assert ProposalResource(kind="node", id=HYPOTHESIS_ID) in left.effects
    assert (
        ProposalResource(kind="edge", id=f"{HYPOTHESIS_ID}::supersedes::{OTHER_HYPOTHESIS_ID}")
        in left.effects
    )
    # Removing either connected endpoint writes the incident edge the other
    # removal reads, even though neither approval edits the other node.
    first = footprint(
        "prop/remove-question",
        {
            "op": "remove_nodes",
            "intent": "removal",
            "node_ids": [QUESTION_ID],
        },
    )
    second = footprint("prop/remove-hypothesis", deepcopy(INTENT_OPERATIONS["removal"]))
    assert first.effects & second.dependencies
    assert second.effects & first.dependencies


def test_connected_removal_review_has_current_incident_snapshot(
    manifest: Manifest,
    tmp_path,
) -> None:
    from rcp.core.materialize import apply_valid_patch, prepare_patch_bookkeeping
    from rcp.core.validation import ValidationContext, validate_patch
    from rcp.core.validation.proposals import proposal_is_stale

    service = _service(manifest, tmp_path)
    state = service.history.materialize().state
    patch = _merge_review_patch(
        state,
        {"op": "remove_nodes", "intent": "removal", "node_ids": [QUESTION_ID]},
        deepcopy(INTENT_OPERATIONS["removal"]),
    )
    patch = prepare_patch_bookkeeping(state, patch).model_copy(
        update={"revision": state.revision + 1}
    )
    report = validate_patch(state, patch, ValidationContext(["repo-a", "repo-b"]))
    assert not report.rejected, report.messages
    filed = apply_valid_patch(state, patch)
    assert not proposal_is_stale(filed, filed.proposals["prop/merge-group"])


def test_rule_two_approval_preserves_historical_standing(
    manifest: Manifest,
    tmp_path,
) -> None:
    from rcp.core.operations import CreateProposalsOperation, SetStandingOperation
    from rcp.core.validation.proposals import proposal_approval_operations

    service = _service(manifest, tmp_path)
    state = service.history.materialize().state
    content = {
        "op": "update_nodes",
        "intent": "content_change",
        "nodes": [{"id": HYPOTHESIS_ID, "changes": {"statement": "Updated claim."}}],
    }
    patch = _merge_review_patch(state, content, deepcopy(INTENT_OPERATIONS["status_change"]))
    create = patch.ops[0]
    assert isinstance(create, CreateProposalsOperation)
    source = create.proposals[0].model_copy(
        update={
            "dependency_rule": 2,
            "raised_rev": state.revision,
            "related_node_ids": [HYPOTHESIS_ID, STATUS_EVIDENCE_ID],
        }
    )
    # Old multi-intent reviews accept every recorded related node, not merely
    # the written subject. The shared constructor must retain that exact rule.
    accepted = [
        operation.node_id
        for operation in proposal_approval_operations(state, source)
        if isinstance(operation, SetStandingOperation)
    ]
    assert accepted == [HYPOTHESIS_ID, STATUS_EVIDENCE_ID]


@pytest.mark.parametrize("recorded_rule", [1, 2, 3])
@pytest.mark.parametrize("grouped", [False, True])
def test_carried_proposal_receives_destination_filing_rule(
    manifest: Manifest, tmp_path, recorded_rule: int, grouped: bool
) -> None:
    from rcp.core.materialize import prepare_patch_bookkeeping
    from rcp.core.operations import CreateProposalsOperation

    state = _service(manifest, tmp_path).history.materialize().state
    operations = [
        {
            "op": "update_nodes",
            "intent": "content_change",
            "nodes": [{"id": HYPOTHESIS_ID, "changes": {"statement": "Updated claim."}}],
        }
    ]
    if grouped:
        operations.append(deepcopy(INTENT_OPERATIONS["status_change"]))
    raw = _merge_review_patch(state, *operations).model_dump(mode="json")
    raw["ops"][0]["proposals"][0].update(dependency_rule=recorded_rule, base_rev=1, raised_rev=1)
    carried = Patch.model_validate(raw)
    before = carried.model_dump_json()

    filed = prepare_patch_bookkeeping(state, carried)

    assert isinstance(filed.ops[0], CreateProposalsOperation)
    proposal = filed.ops[0].proposals[0]
    assert proposal.dependency_rule == (3 if grouped else 2)
    assert proposal.base_rev == state.revision
    assert proposal.raised_rev == 0
    assert proposal.id == "prop/merge-group"
    assert carried.model_dump_json() == before


@pytest.mark.parametrize("invalid_changes", [{"statement": 7}, {"predictions": 7}])
def test_invalid_review_footprints_return_structured_rejection_without_mutation(
    manifest: Manifest,
    tmp_path,
    invalid_changes,
) -> None:
    from rcp.core.materialize import prepare_patch_bookkeeping
    from rcp.core.operations import CreateProposalsOperation
    from rcp.core.validation import ValidationContext, validate_patch
    from rcp.core.validation.proposals import (
        ProposalEffectRejected,
        proposal_dependencies_and_effects,
    )

    state = _service(manifest, tmp_path).history.materialize().state
    patch = _merge_review_patch(
        state,
        {
            "op": "update_nodes",
            "intent": "content_change",
            "nodes": [{"id": HYPOTHESIS_ID, "changes": invalid_changes}],
        },
        deepcopy(INTENT_OPERATIONS["status_change"]),
    )
    patch = prepare_patch_bookkeeping(state, patch)
    create = patch.ops[0]
    assert isinstance(create, CreateProposalsOperation)
    before = state.model_dump_json()
    with pytest.raises(ProposalEffectRejected) as rejected:
        proposal_dependencies_and_effects(state, create.proposals[0])
    assert rejected.value.diagnostics
    assert all(item.level == "reject" for item in rejected.value.diagnostics)
    report = validate_patch(state, patch, ValidationContext(["repo-a", "repo-b"]))
    assert report.rejected
    assert state.model_dump_json() == before
