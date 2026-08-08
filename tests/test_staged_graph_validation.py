from __future__ import annotations

from rcp.core.authority import DECIDE_DECISION, QUEUE_DECISION, permits
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import Decision, GraphState, Patch
from rcp.core.validation import validate_patch


def _agent_patch(*operations: dict[str, object]) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Exercised staged graph validation.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=list(operations),
    )


def _validate(*operations: dict[str, object]):
    state = GraphState(project_truth_scope=["repo-a"])
    return validate_patch(state, _agent_patch(*operations), ["repo-a"])


def _research_question(node_id: str, *, title: str = "Staged question") -> dict[str, object]:
    return {
        "id": node_id,
        "type": "research_question",
        "title": title,
        "question": "Can later operations use graph objects created earlier in this patch?",
    }


def test_create_node_then_update_it_validates_in_written_order() -> None:
    report = _validate(
        {"op": "create_nodes", "nodes": [_research_question("rq/staged-question")]},
        {
            "op": "update_nodes",
            "nodes": [
                {
                    "id": "rq/staged-question",
                    "changes": {"motivation": "The prior operation established this node."},
                }
            ],
        },
    )

    assert not report.rejected


def test_ambiguity_operations_are_replay_only_and_preserve_written_order() -> None:
    operations = (
        {
            "op": "create_ambiguities",
            "ambiguities": [
                {
                    "id": "amb/staged-ambiguity",
                    "question": "Which interpretation should be retained?",
                    "why_it_matters": "The answer changes the next experiment.",
                }
            ],
        },
        {
            "op": "resolve_ambiguities",
            "resolutions": [{"id": "amb/staged-ambiguity", "status": "resolved"}],
        },
    )
    state = GraphState(project_truth_scope=["repo-a"])
    patch = _agent_patch(*operations)
    admission = validate_patch(state, patch, ["repo-a"])
    replay = validate_patch(state, patch, ["repo-a"], mode="replay")

    assert admission.rejected
    assert sum(message.code == "legacy-only-operation" for message in admission.messages) == 2
    assert not replay.rejected
    materialized = apply_valid_patch(state, patch)
    assert materialized.ambiguities["amb/staged-ambiguity"].status == "resolved"


def test_same_node_id_created_by_separate_operations_is_rejected() -> None:
    report = _validate(
        {"op": "create_nodes", "nodes": [_research_question("rq/repeated-id")]},
        {
            "op": "create_nodes",
            "nodes": [_research_question("rq/repeated-id", title="Repeated question")],
        },
    )

    assert report.rejected
    assert any(message.code == "duplicate-node-id" for message in report.messages)


def test_new_proposal_cannot_target_decision_created_earlier_in_the_same_patch() -> None:
    report = _validate(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "dec/staged-rule",
                    "type": "decision",
                    "title": "Staged evaluation rule",
                    "question": "Which evaluation rule should govern the experiment?",
                    "options": ["matched", "shifted"],
                },
                {
                    "id": "exp/staged-evaluation",
                    "type": "experiment",
                    "title": "Staged evaluation",
                    "objective": "Evaluate the intervention under the chosen rule.",
                },
            ],
        },
        {
            "op": "create_edges",
            "edges": [
                {
                    "source": "exp/staged-evaluation",
                    "target": "dec/staged-rule",
                    "relation": "governed_by",
                }
            ],
        },
        {
            "op": "create_proposals",
            "proposals": [
                {
                    "id": "prop/select-staged-rule",
                    "title": "Select the staged evaluation rule",
                    "card": {
                        "situation_cold": "The experiment needs one evaluation rule.",
                        "why_human_now": "Selecting the rule is a human decision.",
                        "consequences": "The experiment will use the matched rule.",
                        "decision_needed": "Approve or reject this selection.",
                    },
                    "ops": [
                        {
                            "op": "update_nodes",
                            "nodes": [
                                {
                                    "id": "dec/staged-rule",
                                    "changes": {
                                        "selected_option": "matched",
                                        "status": "decided",
                                    },
                                }
                            ],
                        }
                    ],
                    "related_node_ids": ["dec/staged-rule"],
                    "base_rev": 0,
                }
            ],
        },
    )

    assert report.rejected
    assert any("decide_decision" in message.message for message in report.messages)


def test_agent_created_decision_may_be_open_or_ready_but_not_revisit_or_decided() -> None:
    def decision(status: str) -> dict[str, object]:
        raw: dict[str, object] = {
            "id": f"dec/{status}",
            "type": "decision",
            "title": f"{status.title()} decision",
            "question": "Which option should be used?",
            "options": ["first", "second"],
            "status": status,
        }
        if status == "decided":
            raw["selected_option"] = "first"
        return raw

    for status in ("open", "ready"):
        assert not _validate({"op": "create_nodes", "nodes": [decision(status)]}).rejected
    for status in ("revisit", "decided"):
        report = _validate({"op": "create_nodes", "nodes": [decision(status)]})
        assert report.rejected


def test_ready_ballot_is_checked_after_written_order_staging() -> None:
    incomplete = _validate(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "dec/incomplete",
                    "type": "decision",
                    "title": "Incomplete ballot",
                    "question": "Which option?",
                    "options": ["only", "only"],
                    "status": "ready",
                }
            ],
        }
    )
    completed_later = _validate(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "dec/completed-later",
                    "type": "decision",
                    "title": "Completed ballot",
                    "question": "Which option?",
                    "options": ["first"],
                    "status": "ready",
                }
            ],
        },
        {
            "op": "update_nodes",
            "nodes": [
                {
                    "id": "dec/completed-later",
                    "changes": {"options": ["first", "second"]},
                }
            ],
        },
    )
    plain_open = _validate(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "dec/plain-open",
                    "type": "decision",
                    "title": "Plain open decision",
                    "question": "Which option?",
                    "options": [],
                }
            ],
        }
    )
    prior = Decision(
        id="dec/revisit-incomplete",
        type="decision",
        title="Incomplete revisit ballot",
        question="Which option?",
        options=["only"],
        selected_option="only",
        status="decided",
    )
    revisit_incomplete = validate_patch(
        GraphState(project_truth_scope=["repo-a"], nodes={prior.id: prior}),
        _agent_patch(
            {
                "op": "update_nodes",
                "nodes": [{"id": prior.id, "changes": {"status": "revisit"}}],
            }
        ),
        ["repo-a"],
    )

    assert incomplete.rejected
    assert any(message.code == "incomplete-decision-ballot" for message in incomplete.messages)
    assert revisit_incomplete.rejected
    assert any(
        message.code == "incomplete-decision-ballot" for message in revisit_incomplete.messages
    )
    assert not completed_later.rejected
    assert not plain_open.rejected


def test_agent_may_queue_a_prior_decision_but_never_decide_it() -> None:
    decision = Decision(
        id="dec/prior-choice",
        type="decision",
        title="Prior choice",
        question="Which option?",
        options=["first", "second"],
        selected_option="first",
        status="decided",
    )
    state = GraphState(project_truth_scope=["repo-a"], nodes={decision.id: decision})

    for status in ("open", "ready", "revisit"):
        report = validate_patch(
            state,
            _agent_patch(
                {
                    "op": "update_nodes",
                    "nodes": [{"id": decision.id, "changes": {"status": status}}],
                }
            ),
            ["repo-a"],
        )
        assert not report.rejected

    decide = validate_patch(
        state,
        _agent_patch(
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": decision.id,
                        "changes": {"selected_option": "second", "status": "decided"},
                    }
                ],
            }
        ),
        ["repo-a"],
    )
    assert decide.rejected
    assert any("only a human may" in message.message for message in decide.messages)


def test_revisit_requires_a_prior_decision() -> None:
    decision = Decision(
        id="dec/no-prior-choice",
        type="decision",
        title="No prior choice",
        question="Which option?",
        options=["first", "second"],
    )
    state = GraphState(project_truth_scope=["repo-a"], nodes={decision.id: decision})
    report = validate_patch(
        state,
        _agent_patch(
            {
                "op": "update_nodes",
                "nodes": [{"id": decision.id, "changes": {"status": "revisit"}}],
            }
        ),
        ["repo-a"],
    )

    assert report.rejected
    assert any("prior decision" in message.message for message in report.messages)


def test_permission_stub_splits_queue_from_decide() -> None:
    agent = _agent_patch()
    human = Patch(
        kind="approval",
        author="human",
        summary="Checked Decision permissions.",
        ops=[],
    )

    assert permits(agent, QUEUE_DECISION)
    assert not permits(agent, DECIDE_DECISION)
    assert permits(human, QUEUE_DECISION)
    assert permits(human, DECIDE_DECISION)


def test_ordinary_human_edit_may_queue_but_never_decide() -> None:
    decision = Decision(
        id="dec/human-queue",
        type="decision",
        title="Human queue",
        question="Which option?",
        options=["first", "second"],
        selected_option="first",
        status="decided",
    )
    state = GraphState(project_truth_scope=["repo-a"], nodes={decision.id: decision})

    for status in ("open", "ready", "revisit"):
        queue = Patch(
            kind="approval",
            author="human",
            summary="Queued the Decision.",
            ops=[
                {
                    "op": "update_nodes",
                    "nodes": [
                        {
                            "id": decision.id,
                            "base_updated_rev": decision.updated_rev,
                            "changes": {"status": status},
                        }
                    ],
                }
            ],
        )
        assert not validate_patch(state, queue, ["repo-a"]).rejected

    unnamed_choice = Patch(
        kind="approval",
        author="human",
        summary="Tried to decide through the ordinary editor.",
        ops=[
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": decision.id,
                        "base_updated_rev": decision.updated_rev,
                        "changes": {"selected_option": "second", "status": "decided"},
                    }
                ],
            }
        ],
    )
    report = validate_patch(state, unnamed_choice, ["repo-a"])
    assert report.rejected
    assert any(message.code == "unnamed-decision-action" for message in report.messages)


def test_direct_choice_is_legal_from_ready_and_revisit() -> None:
    for source_status in ("ready", "revisit"):
        decision = Decision(
            id=f"dec/{source_status}-choice",
            type="decision",
            title=f"{source_status.title()} choice",
            question="Which option?",
            options=["first", "second"],
            selected_option="first" if source_status == "revisit" else None,
            status=source_status,
        )
        state = GraphState(project_truth_scope=["repo-a"], nodes={decision.id: decision})
        choice = Patch(
            kind="approval",
            author="human",
            human_action="decision_choice",
            summary="Chose a Decision option.",
            ops=[
                {
                    "op": "update_nodes",
                    "nodes": [
                        {
                            "id": decision.id,
                            "base_updated_rev": decision.updated_rev,
                            "changes": {"selected_option": "second", "status": "decided"},
                        }
                    ],
                }
            ],
        )

        assert not validate_patch(state, choice, ["repo-a"]).rejected


def test_edge_can_reference_a_node_created_later_in_the_same_patch() -> None:
    patch = _agent_patch(
        {
            "op": "create_edges",
            "edges": [
                {
                    "source": "rq/forward-reference",
                    "target": "hyp/forward-reference",
                    "relation": "has_hypothesis",
                }
            ],
        },
        {
            "op": "create_nodes",
            "nodes": [
                _research_question("rq/forward-reference"),
                {
                    "id": "hyp/forward-reference",
                    "type": "hypothesis",
                    "title": "Forward reference",
                    "statement": "The validator recognizes a same-patch node reference.",
                },
            ],
        },
    )
    state = GraphState(project_truth_scope=["repo-a"])
    report = validate_patch(state, patch, ["repo-a"])

    assert not report.rejected
    materialized = apply_valid_patch(state, patch)
    assert (
        materialized.edges["rq/forward-reference::has_hypothesis::hyp/forward-reference"].layer
        == "epistemic"
    )


def test_forward_edge_layer_is_derived_after_later_endpoints_materialize() -> None:
    patch = _agent_patch(
        {
            "op": "create_edges",
            "edges": [
                {
                    "source": "rq/forward-blocked",
                    "target": "blk/forward-blocker",
                    "relation": "blocked_by",
                }
            ],
        },
        {
            "op": "create_nodes",
            "nodes": [
                _research_question("rq/forward-blocked"),
                {
                    "id": "blk/forward-blocker",
                    "type": "blocker",
                    "title": "Forward blocker",
                    "description": "The blocker is created after its edge.",
                },
            ],
        },
    )
    state = GraphState(project_truth_scope=["repo-a"])

    report = validate_patch(state, patch, ["repo-a"])

    assert not report.rejected
    materialized = apply_valid_patch(state, patch)
    assert materialized.edges["rq/forward-blocked::blocked_by::blk/forward-blocker"].layer == "seam"
