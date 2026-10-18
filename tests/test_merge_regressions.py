"""Real planner-to-history regressions for the production merge failure classes."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from functools import cache

import pytest

from rcp.agents import agent_output_schema
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import MergeAnswersArguments, MergeAnswersCommandRequest
from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    WriteAuthority,
    capture_change_authority,
)
from rcp.core.models import AuthorizedHuman, BranchMergeProvenance, GraphBranchMetadata, Patch
from rcp.core.transition_models import GraphTargetRef, TransitionTrace
from rcp.core.transitions import TRANSITION_RULESET_TAG, transition_trigger_manifest
from rcp.core.validation.proposals import proposal_approval_operations, proposal_is_stale
from rcp.history import BranchMergeAlreadyCommitted, HistoryManager
from rcp.runs.branch_merge import (
    branch_human_review_changes,
    branch_merge_id,
    branch_merge_receipt_from_committed_patch,
    commit_branch_merge_with_history,
)
from rcp.runs.branch_merge_context import _prior_merge_reviews
from rcp.runs.merge_contract import (
    MergeAnswers,
    MergePlanningContext,
    MergePreparationContract,
    ReviewCard,
    SourceHumanFact,
    merge_contract_digest,
)
from rcp.runs.merge_patch import build_merge_patch, prepare_merge_candidate
from rcp.runs.merge_plan import plan_merge
from rcp.runs.patch_validator import MergeAnswersCheck
from rcp.transport import StateUnavailable

_BRANCH_ID = "12b3d806-f989-4db0-8a04-413b3abde287"
_HUMAN = AuthorizedHuman(
    space_id="42ff4a99-7e19-4444-aaad-1b841f30f969",
    user_id="7fc084b7-7a43-4d7f-b002-156b78ad722e",
    display_name="Member",
)


def _append(history, *ops):
    """Record actual human fixture edits, including their optimistic concurrency base."""
    operations = deepcopy(list(ops))
    current = history.state()
    for operation in operations:
        if operation["op"] == "update_nodes":
            for node in operation["nodes"]:
                node["base_updated_rev"] = current.nodes[node["id"]].updated_rev
    patch, _ = history.append(
        Patch(
            kind="approval",
            author="human",
            summary="Record fixture change",
            ops=operations,
            human_action="decision_choice"
            if any(
                operation["op"] == "update_nodes"
                and any("selected_option" in node["changes"] for node in operation["nodes"])
                for operation in operations
            )
            else None,
        ),
        authorized_by=_HUMAN,
        discard_on_reject=True,
    )
    return patch


def _fork_history(manifest, nodes, edges=()):
    """Create canonical base/branch logs and a real, exact member-merge task binding."""
    source_target = GraphTargetRef(kind="branch", branch_id=_BRANCH_ID)
    scope = sorted(manifest.project.truth_scope)
    task = AgentTaskAuthority(
        operation_id="merge-regression",
        project_id="project",
        apply_target=GraphTargetRef(),
        authorized_by=_HUMAN,
        dispatch_authority=AgentDispatchAuthority(
            profile="orchestrator",
            task_contract="orchestrate",
            scope=AgentDispatchScope(run_truth_scope=scope, patch_kind="work"),
        ),
        merge_source=source_target,
        merge_dispatcher="member",
    )
    seed_task = task.model_copy(
        update={
            "operation_id": "fixture-seed",
            "merge_source": None,
            "merge_dispatcher": None,
            "dispatch_authority": AgentDispatchAuthority(
                profile="ordinary",
                task_contract="work_auto",
                scope=AgentDispatchScope(
                    run_truth_scope=scope,
                    patch_kind="work",
                    chat_scope="project",
                    chat_id="fixture-chat",
                ),
            ),
        }
    )
    branch_task = seed_task.model_copy(
        update={
            "operation_id": "fixture-branch",
            "apply_target": source_target,
        }
    )
    authority = WriteAuthority(
        agent_authority_resolver=lambda project, operation: {
            "fixture-seed": seed_task,
            "fixture-branch": branch_task,
            "merge-regression": task,
        }[operation],
        project_membership_check=lambda project, user: (
            project == "project" and user == _HUMAN.user_id
        ),
    )
    history = HistoryManager(manifest, project_id="project", write_authority=authority)
    operations = [{"op": "create_nodes", "nodes": list(nodes)}]
    if edges:
        operations.append({"op": "create_edges", "edges": list(edges)})
    history.append(
        Patch(
            kind="work",
            author="agent",
            summary="Create base graph",
            source_operation_id="fixture-seed",
            run_truth_scope=scope,
            ops=operations,
        ),
        discard_on_reject=True,
    )
    base = history.head_ref()
    branch = history.create_branch(
        GraphBranchMetadata(
            branch_id=_BRANCH_ID,
            project_id="project",
            episode_id=None,
            kind="fork",
            base_head=base,
            parent_head=base,
            created_by="fixture-fork",
            head=base.model_copy(update={"target": source_target}),
            authorized_by=_HUMAN,
        )
    )
    return history, branch


@cache
def _preparation_contract():
    return MergePreparationContract(
        ruleset_tag=TRANSITION_RULESET_TAG,
        trigger_manifest=transition_trigger_manifest().model_dump(mode="json"),
        transition_trace_schema=TransitionTrace.model_json_schema(),
        orchestrator_patch_schema=agent_output_schema(profile="orchestrator"),
    )


def _plan(history, branch):
    """Capture the loader's exact heads, source human facts and live admission envelope."""
    metadata = branch.branch_metadata()
    destination = history.state()
    provenance = BranchMergeProvenance(
        merge_id=branch_merge_id(metadata),
        branch_id=metadata.branch_id,
        episode_id=metadata.episode_id,
        branch_base_head=metadata.effective_parent_head,
        branch_head=metadata.head,
        rebased_main_head=history.head_ref(),
        merge_task_id="merge-regression",
    )
    template = Patch(
        kind="work",
        author="agent",
        summary="Merge branch",
        source_operation_id="merge-regression",
        authorized_by=_HUMAN,
        profile="orchestrator",
        task_id="merge-regression",
        run_truth_scope=sorted(destination.project_truth_scope),
        branch_merge=provenance,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ops=[],
    )
    captured = capture_change_authority(
        destination,
        template,
        authority=history.write_authority,
        project_id=history.project_id,
        apply_target=history.graph_target,
    )
    base, source = branch.base_state(), branch.state()
    engine = history.history_engine
    receipts = branch.validated_merge_receipts()
    previous_receipt = max(
        receipts, key=lambda receipt: receipt.provenance.branch_head.revision, default=None
    )
    previous_source = (
        engine.state_at(previous_receipt.provenance.branch_head) if previous_receipt else None
    )
    facts = branch_human_review_changes(
        engine.steps(metadata.head), previous_source or base, source
    )
    context = MergePlanningContext(
        context_id=merge_contract_digest(provenance.model_dump(mode="json")),
        captured_authority=captured,
        merge_task_id=provenance.merge_task_id,
        authorized_by=_HUMAN,
        provenance=provenance,
        base_head=metadata.effective_parent_head,
        source_head=metadata.head,
        destination_head=history.head_ref(),
        base_graph=base,
        source_graph=source,
        destination_graph=destination,
        run_truth_scope=tuple(sorted(destination.project_truth_scope)),
        source_human_facts=tuple(
            SourceHumanFact.model_validate(fact.model_dump(mode="python")) for fact in facts
        ),
        preparation_contract=_preparation_contract(),
        previous_merge_receipt=previous_receipt,
        previous_source_graph=previous_source,
        prior_reviews=_prior_merge_reviews(
            engine,
            engine.steps(history.head_ref()),
            destination,
            source=metadata.head.target,
            target=history.head_ref().target,
        ),
    )
    return plan_merge(context)


def _answers(plan, overrides=None):
    choices = {
        conflict.conflict_id: {"kind": conflict.allowed_answer_kinds[0]}
        for conflict in plan.conflicts
    }
    choices.update(overrides or {})
    card = ReviewCard(
        situation_cold="The source changes protected beliefs.",
        why_human_now="Review the branch changes.",
        consequences="Approval applies these changes.",
        decision_needed="Approve or reject.",
    )
    return MergeAnswers(
        answers=choices, cards={group.subject_key: card for group in plan.review_groups}
    )


def _commit(history, branch, plan, answers):
    before = history.head_ref()
    prepared = prepare_merge_candidate(plan, answers, history)
    assert history.head_ref() == before
    committed = commit_branch_merge_with_history(
        history, build_merge_patch(plan, answers), expected_main_head=plan.destination_head
    )
    assert committed.projection.graph.model_dump(exclude={"validation_messages"}) == (
        prepared.projection.graph.model_dump(exclude={"validation_messages"})
    )
    receipt = branch.write_merge_receipt(branch_merge_receipt_from_committed_patch(committed.patch))
    assert branch.validated_merge_receipts()[-1] == receipt
    assert history.materialize(write_outputs=False).state == history.state()
    return committed


def _approve(history, proposal_id):
    proposal = history.state().proposals[proposal_id]
    assert not proposal_is_stale(history.state(), proposal)
    history.append(
        Patch(
            kind="approval",
            author="human",
            summary="Approve merge review",
            ops=proposal_approval_operations(history.state(), proposal),
        ),
        authorized_by=_HUMAN,
        discard_on_reject=True,
    )
    assert history.state().proposals[proposal_id].status == "approved"
    assert history.materialize(write_outputs=False).state == history.state()


def _blocker(identity="blk/observed", **overrides):
    return {
        "id": identity,
        "type": "blocker",
        "title": "Base title",
        "description": "Base description",
        **overrides,
    }


def _update(identity, **changes):
    return {"op": "update_nodes", "nodes": [{"id": identity, "changes": changes}]}


def _checker(history, plan):
    checker = MergeAnswersCheck(
        plan=plan, history=history, task_id=plan.merge_task_id, turn_id="one-turn"
    )
    identity = CommandTurnIdentity(
        episode_id=None,
        task_id=plan.merge_task_id,
        turn_id="one-turn",
        authority="validate_only",
    )
    return checker, identity


def _request(answers, index=0):
    return MergeAnswersCommandRequest(
        mailbox_id="a" * 32,
        request_id=f"{index:032x}",
        credential="b" * 64,
        verb="merge_answers",
        arguments=MergeAnswersArguments(action="check", answers=answers.model_dump_json()),
    )


def test_merge_carries_uncontested_changes_alongside_answered_conflict(manifest):
    history, branch = _fork_history(
        manifest,
        [
            _blocker(),
            {
                "id": "rq/review",
                "type": "research_question",
                "title": "Base belief",
                "question": "Which cause explains the observation?",
            },
        ],
    )
    _append(branch, _update("blk/observed", title="Branch title", description="Branch content"))
    _append(branch, {"op": "create_nodes", "nodes": [_blocker("blk/new-finding")]})
    _append(
        history,
        _update(
            "blk/observed", title="Destination title", resolution_condition="Destination resolution"
        ),
    )
    _append(branch, _update("rq/review", title="Branch belief"))
    plan = _plan(history, branch)
    assert len(plan.conflicts) == 1
    assert len(plan.review_groups) == 1
    selected = _answers(plan, {plan.conflicts[0].conflict_id: {"kind": "destination"}})
    _commit(history, branch, plan, selected)
    node = history.state().nodes["blk/observed"]
    assert (node.title, node.description, node.resolution_condition) == (
        "Destination title",
        "Branch content",
        "Destination resolution",
    )
    assert "blk/new-finding" in history.state().nodes
    assert history.state().nodes["rq/review"].title == "Base belief"
    assert len(history.state().proposals) == 1
    _approve(history, next(iter(history.state().proposals)))
    assert history.state().nodes["rq/review"].title == "Branch belief"


def test_wrongly_typed_answer_names_conflict_and_can_be_corrected_in_same_turn(manifest):
    history, branch = _fork_history(manifest, [_blocker()])
    _append(branch, _update("blk/observed", title="Branch title"))
    _append(history, _update("blk/observed", title="Destination title"))
    plan = _plan(history, branch)
    conflict_id = plan.conflicts[0].conflict_id
    checker, identity = _checker(history, plan)
    before = history.head_ref()
    bad = _answers(plan, {conflict_id: {"kind": "merged", "value": 42}})
    response = checker(_request(bad), identity)
    assert response.status == "invalid"
    assert response.result["outcome"] == "answers_rejected"
    assert [
        (item["code"], item["related_node_ids"]) for item in response.result["diagnostics"]
    ] == [("invalid-merge-answer", [conflict_id])]
    assert history.head_ref() == before
    assert checker.receipts == []
    corrected = _answers(plan, {conflict_id: {"kind": "merged", "value": "Resolved title"}})
    assert checker(_request(corrected, 1), identity).status == "ok"
    receipt = checker.require_receipt(corrected.model_dump_json().encode())
    assert receipt.turn_id == identity.turn_id
    _commit(history, branch, plan, corrected)
    assert history.state().nodes["blk/observed"].title == "Resolved title"


def test_decided_creation_builds_coherent_status_operations_outside_field_delta(manifest):
    history, branch = _fork_history(manifest, [_blocker()])
    _append(
        branch,
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "dec/design",
                    "type": "decision",
                    "title": "Design",
                    "question": "Which?",
                    "options": ["A", "B"],
                }
            ],
        },
    )
    _append(branch, _update("dec/design", status="decided", selected_option="B"))
    plan = _plan(history, branch)
    assert not plan.conflicts and not plan.review_groups
    _commit(history, branch, plan, _answers(plan))
    decision = history.state().nodes["dec/design"]
    assert (decision.status, decision.selected_option, decision.options) == (
        "decided",
        "B",
        ["A", "B"],
    )


def test_duplicate_advisory_is_not_a_rejection(manifest):
    history, branch = _fork_history(manifest, [_blocker("blk/observed-condition")])
    _append(branch, {"op": "create_nodes", "nodes": [_blocker("blk/observed-conditions")]})
    plan = _plan(history, branch)
    prepared = prepare_merge_candidate(plan, _answers(plan), history)
    assert any(item.code == "possible-duplicate" for item in prepared.patch.admission_messages)
    assert all(item.level == "flag" for item in prepared.patch.admission_messages)
    _commit(history, branch, plan, _answers(plan))
    assert "blk/observed-conditions" in history.state().nodes


def test_new_hypothesis_review_survives_filing_approval_and_replay(manifest):
    question = {
        "id": "rq/parent",
        "type": "research_question",
        "title": "Question",
        "question": "Why?",
    }
    history, branch = _fork_history(
        manifest,
        [
            question,
            {
                "id": "ev/result",
                "type": "evidence",
                "title": "Result",
                "observation": "The predicted result occurred",
                "origin": "analytic",
            },
        ],
    )
    _append(
        branch,
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "hyp/new",
                    "type": "hypothesis",
                    "title": "New claim",
                    "statement": "A claim",
                }
            ],
        },
    )
    _append(
        branch,
        {
            "op": "create_edges",
            "edges": [
                {
                    "id": "edge/new-hypothesis",
                    "source": "rq/parent",
                    "target": "hyp/new",
                    "relation": "has_hypothesis",
                }
            ],
        },
    )
    _append(
        branch,
        {
            "op": "create_edges",
            "edges": [
                {
                    "id": "edge/result",
                    "source": "ev/result",
                    "target": "hyp/new",
                    "relation": "supports",
                    "assessment": {"relevance": "direct", "weight": "moderate"},
                }
            ],
        },
    )
    branch.append(
        Patch(
            kind="work",
            author="agent",
            summary="Review the new branch claim",
            source_operation_id="fixture-branch",
            run_truth_scope=sorted(history.state().project_truth_scope),
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": "prop/source-status",
                            "title": "Activate claim",
                            "card": {
                                "situation_cold": "Evidence supports the claim",
                                "why_human_now": "Review",
                                "consequences": "The claim becomes active",
                                "decision_needed": "Approve",
                            },
                            "ops": [
                                {
                                    "op": "update_nodes",
                                    "intent": "status_change",
                                    "nodes": [
                                        {
                                            "id": "hyp/new",
                                            "changes": {"status": "active"},
                                            "cause": {
                                                "kind": "evidence_edge",
                                                "ref_id": "edge/result",
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        ),
        discard_on_reject=True,
    )
    _approve(branch, "prop/source-status")
    plan = _plan(history, branch)
    assert len(plan.review_groups) == 1
    _commit(history, branch, plan, _answers(plan))
    state = history.state()
    assert state.nodes["hyp/new"].status == "proposed"
    assert state.nodes["hyp/new"].standing == "asserted"
    assert "edge/new-hypothesis" in state.edges
    assert len(state.proposals) == 1
    _approve(history, next(iter(state.proposals)))
    assert history.state().nodes["hyp/new"].status == "active"
    assert history.state().nodes["hyp/new"].standing == "accepted"


def test_connected_node_removals_form_one_review_and_cannot_stale_each_other(manifest):
    history, branch = _fork_history(
        manifest,
        [
            {
                "id": "rq/parent",
                "type": "research_question",
                "title": "Question",
                "question": "Why?",
            },
            {"id": "hyp/child", "type": "hypothesis", "title": "Claim", "statement": "A claim"},
        ],
        [
            {
                "id": "edge/connection",
                "source": "rq/parent",
                "target": "hyp/child",
                "relation": "has_hypothesis",
            }
        ],
    )
    _append(branch, {"op": "remove_nodes", "node_ids": ["rq/parent"]})
    _append(branch, {"op": "remove_nodes", "node_ids": ["hyp/child"]})
    plan = _plan(history, branch)
    assert len(plan.review_groups) == 1
    _commit(history, branch, plan, _answers(plan))
    state = history.state()
    assert {"rq/parent", "hyp/child"} <= state.nodes.keys()
    assert "edge/connection" in state.edges
    assert len(state.proposals) == 1
    _approve(history, next(iter(state.proposals)))
    state = history.state()
    assert not ({"rq/parent", "hyp/child"} & state.nodes.keys())
    assert "edge/connection" not in state.edges


def test_check_timeout_is_unavailable_without_a_canonical_write(manifest, monkeypatch):
    history, branch = _fork_history(manifest, [_blocker()])
    _append(branch, _update("blk/observed", description="Branch content"))
    plan = _plan(history, branch)
    selected = _answers(plan)
    checker, identity = _checker(history, plan)
    before = history.head_ref()
    original_bytes = {path.name: path.read_bytes() for path in history.patches_dir.iterdir()}

    def timeout(*args, **kwargs):
        raise TimeoutError("Check transport did not return a result")

    with monkeypatch.context() as fault:
        fault.setattr(history, "validate_candidate", timeout)
        response = checker(_request(selected), identity)
        assert response.status == "unavailable"
        assert checker.refusal is None
        with pytest.raises(StateUnavailable):
            checker.require_receipt(selected.model_dump_json().encode())
    assert history.head_ref() == before
    assert {
        path.name: path.read_bytes() for path in history.patches_dir.iterdir()
    } == original_bytes
    assert not checker.receipts
    assert branch.reconcile_merge_receipt(plan.provenance.merge_id) is None
    assert not branch.merge_receipts()
    assert checker(_request(selected, 1), identity).status == "ok"
    checker.require_receipt(selected.model_dump_json().encode())
    _commit(history, branch, plan, selected)


@pytest.mark.parametrize("published", [False, True], ids=["before-append", "after-append"])
def test_uncertain_append_recovers_zero_or_one_commit_and_one_receipt(
    manifest, monkeypatch, published
):
    history, branch = _fork_history(manifest, [_blocker()])
    _append(branch, _update("blk/observed", description="Branch content"))
    plan = _plan(history, branch)
    selected = _answers(plan)
    prepare_merge_candidate(plan, selected, history)
    candidate = build_merge_patch(plan, selected)
    append = history.append

    def uncertain(candidate, **kwargs):
        if published:
            append(candidate, **kwargs)
        raise TimeoutError("Append transport lost its response")

    with monkeypatch.context() as fault:
        fault.setattr(history, "append", uncertain)
        with pytest.raises(TimeoutError):
            commit_branch_merge_with_history(
                history, candidate, expected_main_head=plan.destination_head
            )
    recovered = branch.reconcile_merge_receipt(plan.provenance.merge_id)
    assert (recovered is not None) is published
    commits = [patch for patch in history.load_patches() if patch.branch_merge is not None]
    assert len(commits) == int(published)
    assert len(branch.merge_receipts()) == int(published)
    if recovered is None:
        commit_branch_merge_with_history(
            history, candidate, expected_main_head=plan.destination_head
        )
        recovered = branch.reconcile_merge_receipt(plan.provenance.merge_id)
    assert recovered is not None
    with pytest.raises(BranchMergeAlreadyCommitted):
        commit_branch_merge_with_history(
            history, candidate, expected_main_head=plan.destination_head
        )
    assert branch.reconcile_merge_receipt(plan.provenance.merge_id) == recovered
    assert branch.validated_merge_receipts() == [recovered]
    assert len([patch for patch in history.load_patches() if patch.branch_merge is not None]) == 1
    assert history.state().nodes["blk/observed"].description == "Branch content"
    assert history.materialize(write_outputs=False).state == history.state()
