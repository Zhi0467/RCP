from __future__ import annotations

from uuid import uuid4

import pytest

from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    WriteAuthority,
    capture_change_authority,
    classify_graph_changes,
)
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import AuthorizedHuman, GraphState, Patch
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation import ValidationContext
from tests.helpers import seed_patch, unchecked_write_authority

HYPOTHESIS = "hyp/replanning-restores-plasticity"


def _classify(state, *operations, authority=None, **fields):
    if "branch_merge" in fields:
        fields.update(
            authorized_by=AuthorizedHuman(
                space_id=str(uuid4()), user_id=str(uuid4()), display_name="Member"
            ),
            source_operation_id="merge-task",
            task_id="merge-task",
        )
    patch = Patch(
        kind="work",
        author="agent",
        summary="Classify candidate changes",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=list(operations),
        **fields,
    )
    return classify_graph_changes(
        state,
        patch,
        authority=authority or unchecked_write_authority(),
        project_id="project",
        apply_target=GraphTargetRef(),
        context=ValidationContext(project_truth_scope=["repo-a"]),
    )[0]


def _state():
    return apply_valid_patch(GraphState(project_truth_scope=["repo-a"]), seed_patch())


def _update(**changes):
    return {"op": "update_nodes", "nodes": [{"id": HYPOTHESIS, "changes": changes}]}


def test_protected_content_is_reviewable_only_after_the_proposed_effect_passes_core():
    state = _state()
    result = _classify(state, _update(title="Updated protected claim"))
    assert result.disposition == "reviewable"
    assert [operation.intent for operation in result.review_operations] == ["content_change"]
    assert state.nodes[HYPOTHESIS].title != "Updated protected claim"
    invalid = _classify(state, _update(standing="accepted"))
    assert invalid.disposition == "refused"
    assert "immutable-node-field" in {message.code for message in invalid.diagnostics}


def test_atomic_creation_keeps_same_patch_authority_and_advisory_flags_pass():
    state = GraphState(project_truth_scope=["repo-a"])
    result = _classify(state, *seed_patch().model_dump(mode="json")["ops"])
    assert result.disposition == "admitted"
    # The incomplete card emits advisory flags, never a rejection.
    result = _classify(
        _state(),
        {
            "op": "create_proposals",
            "proposals": [
                {
                    "id": "proposal/review",
                    "title": "Review",
                    "card": {},
                    "ops": [{**_update(title="Changed"), "intent": "content_change"}],
                }
            ],
        },
    )
    assert result.disposition == "admitted"
    assert any(message.level == "flag" for message in result.diagnostics)


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        (
            {
                "op": "update_nodes",
                "nodes": [{"id": "hyp/missing", "changes": {"title": "Missing"}}],
            },
            "unknown-node",
        ),
        (
            {
                "op": "supersede_nodes",
                "nodes": [{"id": HYPOTHESIS, "superseded_by": "hyp/missing"}],
            },
            "invalid-agent-proposal-shape",
        ),
        ({"op": "set_project_truth_scope", "truth_scope": ["repo-a"]}, "graph-action-refused"),
        ({"op": "remove_nodes", "node_ids": []}, "invalid-remove-nodes-operation"),
    ],
)
def test_unrepresentable_or_invalid_changes_are_hard_refusals(operation, code):
    result = _classify(_state(), operation)
    assert result.disposition == "refused"
    assert code in {message.code for message in result.diagnostics}
    assert not result.review_operations


@pytest.mark.parametrize("failure", ["membership", "task"])
def test_lost_live_authority_never_becomes_a_review(failure):
    authorizer = AuthorizedHuman(space_id=str(uuid4()), user_id=str(uuid4()), display_name="Member")
    task = AgentTaskAuthority(
        operation_id="task",
        project_id="project",
        apply_target=GraphTargetRef(),
        authorized_by=authorizer,
        dispatch_authority=AgentDispatchAuthority(
            profile="ordinary",
            task_contract="work_auto",
            scope=AgentDispatchScope(
                chat_scope="project", chat_id="chat", patch_kind="work", run_truth_scope=["repo-a"]
            ),
        ),
    )

    def resolve(project_id, operation_id):
        if failure == "task":
            raise KeyError(operation_id)
        return task

    result = _classify(
        _state(),
        _update(title="Protected edit"),
        source_operation_id="task",
        authority=WriteAuthority(
            agent_authority_resolver=resolve, project_membership_check=lambda project, member: False
        ),
    )
    assert result.disposition == "refused"
    assert result.diagnostics[0].code == (
        "agent_authorizer_departed" if failure == "membership" else "write-authority-refused"
    )
    assert not result.review_operations


def _merge_provenance():
    branch_id = str(uuid4())
    return {
        "merge_id": "0" * 64,
        "branch_id": branch_id,
        "episode_id": None,
        "branch_base_head": {"target": {"kind": "main"}, "revision": 0},
        "branch_head": {"target": {"kind": "branch", "branch_id": branch_id}, "revision": 1},
        "rebased_main_head": {"target": {"kind": "main"}, "revision": 0},
        "merge_task_id": "merge-task",
    }


@pytest.mark.parametrize("review_standing", [False, True])
def test_created_hypothesis_can_be_reviewed_after_its_asserted_creation(review_standing):
    state = GraphState(project_truth_scope=["repo-a"])
    patch = seed_patch().model_copy(update={"kind": "work", "profile": "orchestrator"})
    document = patch.model_dump(mode="json")
    document["branch_merge"] = _merge_provenance()
    document["authorized_by"] = {
        "space_id": str(uuid4()),
        "user_id": str(uuid4()),
        "display_name": "Member",
    }
    document["source_operation_id"] = document["task_id"] = "merge-task"
    document["ops"].extend(
        [
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": HYPOTHESIS,
                        "changes": {"status": "supported"},
                        "cause": {"kind": "human_edit"},
                    }
                ],
            },
            {"op": "set_standing", "node_id": HYPOTHESIS, "standing": "accepted"},
        ]
    )
    if not review_standing:
        document["ops"].pop()
    patch = Patch.model_validate(document)
    result = classify_graph_changes(
        state,
        patch,
        authority=unchecked_write_authority(),
        project_id="project",
        apply_target=GraphTargetRef(),
        context=ValidationContext(project_truth_scope=["repo-a"]),
        operation_groups=((0, 1), (2, 3) if review_standing else (2,)),
    )
    assert [item.disposition for item in result] == ["admitted", "reviewable"]
    assert [operation.intent for operation in result[1].review_operations] == (
        ["status_change", "standing_change"] if review_standing else ["status_change"]
    )


def test_accepted_ordinary_removal_uses_gate_two_review_form():
    from rcp.core.models import Blocker

    state = _state()
    state.nodes["blocker/review"] = Blocker(
        id="blocker/review",
        type="blocker",
        title="Review",
        description="A blocker",
        standing="accepted",
    )
    result = _classify(
        state,
        {"op": "remove_nodes", "node_ids": ["blocker/review"]},
        profile="orchestrator",
        branch_merge=_merge_provenance(),
    )
    assert result.disposition == "reviewable"
    assert result.review_operations[0].intent == "removal"
    assert {message.code for message in result.diagnostics} == {"accepted-node-removal"}


def test_captured_authority_is_pure_but_cannot_widen_or_replace_live_authority():
    authorizer = AuthorizedHuman(space_id=str(uuid4()), user_id=str(uuid4()), display_name="Member")
    task = AgentTaskAuthority(
        operation_id="task",
        project_id="project",
        apply_target=GraphTargetRef(),
        authorized_by=authorizer,
        dispatch_authority=AgentDispatchAuthority(
            profile="ordinary",
            task_contract="work_auto",
            scope=AgentDispatchScope(
                chat_scope="project", chat_id="chat", patch_kind="work", run_truth_scope=["repo-a"]
            ),
        ),
    )
    member = True
    live = WriteAuthority(
        agent_authority_resolver=lambda project, operation: task,
        project_membership_check=lambda project, user: member,
    )
    state = _state()
    capture = capture_change_authority(
        state,
        Patch(
            kind="work",
            author="agent",
            summary="Plan changes",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            source_operation_id="task",
            ops=[],
        ),
        authority=live,
        project_id="project",
        apply_target=GraphTargetRef(),
    )
    patch = Patch.model_validate(
        {**capture.patch_template.model_dump(), "ops": [_update(title="Reviewed")]}
    )
    member = False

    def classify(candidate, authority):
        return classify_graph_changes(
            state,
            candidate,
            authority=authority,
            project_id="project",
            apply_target=GraphTargetRef(),
            context=ValidationContext(project_truth_scope=["repo-a"]),
        )[0]

    assert classify(patch, capture).disposition == "reviewable"
    assert classify(patch, live).diagnostics[0].code == "agent_authorizer_departed"
    for change in (
        {"profile": "orchestrator"},
        {"run_truth_scope": ["repo-b"]},
        {"task_id": "another-task"},
    ):
        result = classify(patch.model_copy(update=change), capture)
        assert result.disposition == "refused"
        assert result.diagnostics[0].code == "write-authority-refused"
    with pytest.raises(ValueError):
        capture_change_authority(
            state,
            capture.patch_template,
            authority=live,
            project_id="project",
            apply_target=GraphTargetRef(),
        )
