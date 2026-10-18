from __future__ import annotations

from uuid import uuid4

import pytest

from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    AuthorityRefusal,
    WriteAuthority,
    Writer,
    capture_change_authority,
    classify_graph_changes,
)
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import AuthorizedHuman, GraphState, Patch, ValidationMessage
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation import ValidationContext, ValidationReport, validate_patch
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

    authority = WriteAuthority(
        agent_authority_resolver=resolve, project_membership_check=lambda project, member: False
    )
    result = _classify(
        _state(),
        _update(title="Protected edit"),
        source_operation_id="task",
        authority=authority,
    )
    assert result.disposition == "refused"
    assert result.diagnostics[0].code == (
        "agent_authorizer_departed" if failure == "membership" else "agent-task-authority-refused"
    )
    assert not result.review_operations
    template = seed_patch().model_copy(
        update={"kind": "work", "ops": [], "source_operation_id": "task"}
    )
    with pytest.raises(AuthorityRefusal) as caught:
        capture_change_authority(
            _state(),
            template,
            authority=authority,
            project_id="project",
            apply_target=GraphTargetRef(),
        )
    assert caught.value.diagnostics[0].code == result.diagnostics[0].code


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
        # The Decision action is derived from the ops; these ops record no outcome.
        {"agent_action": "decision_choice"},
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


def test_capture_and_classification_retain_original_authority_diagnostics(monkeypatch):
    diagnostics = (
        ValidationMessage(
            level="reject",
            code="human-authorizer-not-member",
            message="Membership changed",
            related_node_ids=[HYPOTHESIS],
            operation_index=2,
            rule_id="membership",
        ),
        ValidationMessage(
            level="reject",
            code="agent-task-authority-refused",
            message="Task binding changed",
            related_edge_ids=["edge/cause"],
            cause_chain=[{"task": "old-task"}],
        ),
    )
    report = ValidationReport()
    report.messages.extend(diagnostics)
    authority = WriteAuthority()
    template = seed_patch().model_copy(update={"ops": []})
    monkeypatch.setattr(
        authority, "admit", lambda state, patch, **kwargs: (patch, Writer.from_patch(patch), report)
    )
    with pytest.raises(AuthorityRefusal) as caught:
        capture_change_authority(
            _state(),
            template,
            authority=authority,
            project_id="project",
            apply_target=GraphTargetRef(),
        )
    assert caught.value.diagnostics == diagnostics
    assert all(
        actual is expected
        for actual, expected in zip(caught.value.diagnostics, diagnostics, strict=True)
    )

    def refuse(*args, **kwargs):
        raise caught.value

    monkeypatch.setattr(authority, "admit", refuse)
    result = _classify(_state(), _update(title="Protected edit"), authority=authority)
    assert result.disposition == "refused"
    assert result.diagnostics == diagnostics
    assert all(
        actual is expected for actual, expected in zip(result.diagnostics, diagnostics, strict=True)
    )


def test_gated_transition_names_the_node_for_hidden_clash_promotion():
    patch = seed_patch().model_copy(update={"ops": []})
    patch = Patch.model_validate({**patch.model_dump(), "ops": [_update(status="supported")]})
    report = validate_patch(_state(), patch, ValidationContext(project_truth_scope=["repo-a"]))
    refusal = next(item for item in report.messages if item.code == "gated-transition")
    assert refusal.related_node_ids == [HYPOTHESIS]


@pytest.mark.parametrize("code", ["accepted-node-removal", "incoherent-decision-transition"])
def test_nested_proposal_refusal_stays_hard_for_review_and_coherence_codes(monkeypatch, code):
    from rcp.core.models import Blocker
    from rcp.core.validation.proposals import PROPOSAL_REPLAY_RULE_ID

    state = _state()
    state.nodes["blocker/review"] = Blocker(
        id="blocker/review",
        type="blocker",
        title="Review",
        description="Blocked",
        standing="accepted",
    )
    diagnostic = ValidationMessage(
        level="reject",
        code=code,
        message="Nested effect refused",
        related_node_ids=["blocker/review"],
        rule_id=PROPOSAL_REPLAY_RULE_ID,
        operation_index=0,
    )
    report = ValidationReport()
    report.messages.append(diagnostic)
    calls = []

    def validate(*args, **kwargs):
        calls.append(args[1])
        return report

    monkeypatch.setattr("rcp.core.validation.validate_patch", validate)
    result = _classify(
        state,
        {"op": "remove_nodes", "node_ids": ["blocker/review"]},
        profile="orchestrator",
        branch_merge=_merge_provenance(),
    )
    assert result.disposition == "refused"
    assert not result.review_operations
    assert result.diagnostics[0] == diagnostic
    assert len(calls) == 1


@pytest.mark.parametrize(
    "order", [("ordinary", "review", "choice"), ("choice", "ordinary", "review")]
)
def test_explicit_groups_derive_their_own_action_and_review_probe_action(order):
    from rcp.core.models import Blocker, Decision

    state = _state()
    state.nodes["blk/one"] = Blocker(
        id="blk/one", type="blocker", title="Base", description="A blocker"
    )
    state.nodes["dec/one"] = Decision(
        id="dec/one", type="decision", title="Choose", question="Which?", options=["A", "B"]
    )
    operations = {
        "ordinary": {
            "op": "update_nodes",
            "nodes": [{"id": "blk/one", "changes": {"title": "New"}}],
        },
        "review": _update(title="Reconsider this claim"),
        "choice": {
            "op": "update_nodes",
            "nodes": [{"id": "dec/one", "changes": {"status": "decided", "selected_option": "A"}}],
        },
    }
    template = Patch(
        kind="work",
        author="agent",
        profile="orchestrator",
        authorized_by=AuthorizedHuman(
            space_id=str(uuid4()), user_id=str(uuid4()), display_name="Member"
        ),
        task_id="task",
        source_operation_id="task",
        summary="Classify groups",
        run_truth_scope=["repo-a"],
        ops=[],
    )
    captured = capture_change_authority(
        state,
        template,
        authority=unchecked_write_authority(),
        project_id="project",
        apply_target=GraphTargetRef(),
    )
    patch = Patch.model_validate(
        {
            **template.model_dump(),
            "agent_action": "decision_choice",
            "ops": [operations[name] for name in order],
        }
    )
    results = classify_graph_changes(
        state,
        patch,
        authority=captured,
        project_id="project",
        apply_target=GraphTargetRef(),
        context=ValidationContext(project_truth_scope=["repo-a"]),
        operation_groups=((0,), (1,), (2,)),
    )
    assert [result.disposition for result in results] == [
        "reviewable" if name == "review" else "admitted" for name in order
    ]
    assert state.nodes["dec/one"].status == "open"
    assert state.nodes["blk/one"].title == "Base"
    review = results[order.index("review")]
    assert review.review_operations[0].nodes[0].changes == {"title": "Reconsider this claim"}


def test_grouped_creation_preserves_same_patch_relation_authority_before_a_decision():
    from rcp.core.models import Decision

    state = GraphState(project_truth_scope=["repo-a"])
    state.nodes["dec/one"] = Decision(
        id="dec/one", type="decision", title="Choose", question="Which?", options=["A", "B"]
    )
    operations = seed_patch().model_dump(mode="json")["ops"]
    operations.append(
        {
            "op": "update_nodes",
            "nodes": [{"id": "dec/one", "changes": {"status": "decided", "selected_option": "B"}}],
        }
    )
    patch = Patch(
        kind="work",
        author="agent",
        profile="orchestrator",
        authorized_by=AuthorizedHuman(
            space_id=str(uuid4()), user_id=str(uuid4()), display_name="Member"
        ),
        task_id="task",
        source_operation_id="task",
        summary="Create and choose",
        run_truth_scope=["repo-a"],
        agent_action="decision_choice",
        ops=operations,
    )
    results = classify_graph_changes(
        state,
        patch,
        authority=unchecked_write_authority(),
        project_id="project",
        apply_target=GraphTargetRef(),
        context=ValidationContext(project_truth_scope=["repo-a"]),
        operation_groups=tuple((index,) for index in range(len(patch.ops))),
    )
    assert all(result.disposition == "admitted" for result in results)


@pytest.mark.parametrize(
    ("status", "selected", "options", "expected_code"),
    [
        ("decided", "A", ["B", "C"], "incoherent-decision-transition"),
        ("ready", None, ["A"], "incomplete-decision-ballot"),
    ],
)
def test_field_coherence_refusals_are_distinct_node_named_clashes(
    status, selected, options, expected_code
):
    from rcp.core.models import Decision

    state = _state()
    state.nodes["dec/one"] = Decision(
        id="dec/one",
        type="decision",
        title="Choose",
        question="Which?",
        options=["A", "B", "C"],
        status=status,
        selected_option=selected,
    )
    result = _classify(
        state,
        {"op": "update_nodes", "nodes": [{"id": "dec/one", "changes": {"options": options}}]},
        profile="orchestrator",
        branch_merge=_merge_provenance(),
    )
    assert result.disposition == "clash"
    assert [
        (item.code, item.related_node_ids) for item in result.diagnostics if item.level == "reject"
    ] == [(expected_code, ["dec/one"])]
    assert not result.review_operations


def test_a_hard_refusal_keeps_a_mixed_coherence_failure_hard():
    from rcp.core.models import Decision

    state = _state()
    state.nodes["dec/one"] = Decision(
        id="dec/one",
        type="decision",
        title="Choose",
        question="Which?",
        options=["A", "B"],
        status="decided",
        selected_option="A",
    )
    result = _classify(
        state,
        {
            "op": "update_nodes",
            "nodes": [
                {"id": "dec/one", "changes": {"options": ["B"]}},
                {"id": "blk/missing", "changes": {"title": "Missing"}},
            ],
        },
        profile="orchestrator",
        branch_merge=_merge_provenance(),
    )
    assert result.disposition == "refused"
    assert {item.code for item in result.diagnostics if item.level == "reject"} == {
        "incoherent-decision-transition",
        "unknown-node",
    }


@pytest.mark.parametrize("inside_proposal", [False, True])
def test_coherence_classification_excludes_proposed_effect_replay(inside_proposal):
    from rcp.core.validation import is_coherence_clash
    from rcp.core.validation.proposals import PROPOSAL_REPLAY_RULE_ID

    message = ValidationMessage(
        level="reject",
        code="incoherent-decision-transition",
        message="Field combination failed",
        related_node_ids=["dec/one"],
        rule_id=PROPOSAL_REPLAY_RULE_ID if inside_proposal else None,
    )
    advisory = ValidationMessage(level="flag", code="unrelated-advisory", message="Advisory")
    assert is_coherence_clash([advisory, message]) is not inside_proposal
    assert not is_coherence_clash([advisory])
    assert not is_coherence_clash([message.model_copy(update={"related_node_ids": []})])
