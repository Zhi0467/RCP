from __future__ import annotations

import pytest
from pydantic import ValidationError

from rcp.core.authority import (
    CREATE_EDGE,
    DECIDE_DECISION,
    MERGE_NODE,
    QUEUE_DECISION,
    RESTRUCTURE_PROTECTED_EPISTEMIC,
    SUPERSEDE_NODE,
    UPDATE_NODE,
    WriteAuthority,
    operation_actions,
    permits,
)
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import Decision, Experiment, GraphState, Patch
from rcp.core.validation import ValidationContext, validate_patch
from tests.helpers import seed_patch


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
    return validate_patch(state, _agent_patch(*operations), ValidationContext(["repo-a"]))


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


def test_current_node_update_rejects_nested_scalar_coercion_but_legacy_replay_preserves_it() -> (
    None
):
    state = GraphState(
        project_truth_scope=["repo-a"],
        nodes={
            "exp/strict-update": Experiment(
                id="exp/strict-update",
                type="experiment",
                title="Strict update",
                objective="Reject coercible update payloads at admission.",
                invocation_ceiling=3,
            )
        },
    )
    patch = _agent_patch(
        {
            "op": "update_nodes",
            "nodes": [
                {
                    "id": "exp/strict-update",
                    "changes": {"invocation_ceiling": "6"},
                }
            ],
        }
    )

    admission = validate_patch(state, patch, ValidationContext(["repo-a"]))

    assert admission.rejected
    assert any(message.code == "invalid-node-update" for message in admission.messages)
    with pytest.raises(ValidationError):
        apply_valid_patch(state, patch)

    legacy = patch.model_copy(update={"schema_generation": 1})
    replay = validate_patch(state, legacy, ValidationContext(["repo-a"], mode="replay"))
    updated = apply_valid_patch(state, legacy)
    assert not replay.rejected
    assert updated.nodes["exp/strict-update"].invocation_ceiling == 6


def test_same_patch_belief_creation_edit_and_connection_stay_direct() -> None:
    patch = _agent_patch(
        {
            "op": "create_nodes",
            "nodes": [
                _research_question("rq/staged-question"),
                {
                    "id": "hyp/staged-hypothesis",
                    "type": "hypothesis",
                    "title": "Staged hypothesis",
                    "statement": "The staged mechanism explains the result.",
                },
            ],
        },
        {
            "op": "update_nodes",
            "nodes": [
                {
                    "id": "rq/staged-question",
                    "changes": {"motivation": "The same Patch supplied the missing motivation."},
                }
            ],
        },
        {
            "op": "create_edges",
            "edges": [
                {
                    "source": "rq/staged-question",
                    "target": "hyp/staged-hypothesis",
                    "relation": "has_hypothesis",
                }
            ],
        },
    )
    state = GraphState(project_truth_scope=["repo-a"])

    report = validate_patch(state, patch, ValidationContext(["repo-a"]))

    assert not report.rejected
    assert operation_actions(state, patch, patch.ops[1]) == frozenset({UPDATE_NODE})
    assert operation_actions(state, patch, patch.ops[2]) == frozenset({CREATE_EDGE})


def test_existing_protected_relation_derives_the_protected_action() -> None:
    state = apply_valid_patch(
        GraphState(project_truth_scope=["repo-a"]),
        seed_patch().model_copy(update={"revision": 1}),
    )
    operation = {
        "op": "remove_edges",
        "edge_ids": ["rq/learning-after-shift::has_hypothesis::hyp/replanning-restores-plasticity"],
    }
    patch = _agent_patch(operation)

    assert operation_actions(state, patch, patch.ops[0]) == frozenset(
        {RESTRUCTURE_PROTECTED_EPISTEMIC}
    )


@pytest.mark.parametrize(
    ("operation", "ordinary_action"),
    [
        (
            {
                "op": "supersede_nodes",
                "nodes": [
                    {
                        "id": "dec/ordinary",
                        "superseded_by": "hyp/replanning-restores-plasticity",
                    }
                ],
            },
            SUPERSEDE_NODE,
        ),
        (
            {
                "op": "merge_nodes",
                "merges": [
                    {
                        "duplicate": "dec/ordinary",
                        "canonical": "hyp/replanning-restores-plasticity",
                    }
                ],
            },
            MERGE_NODE,
        ),
    ],
)
def test_generated_meta_relation_cannot_bypass_protected_endpoint_authority(
    operation,
    ordinary_action,
) -> None:
    state = apply_valid_patch(
        GraphState(project_truth_scope=["repo-a"]),
        seed_patch().model_copy(update={"revision": 1}),
    )
    addition = _agent_patch(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "dec/ordinary",
                    "type": "decision",
                    "title": "Ordinary decision",
                    "question": "Which ordinary option?",
                    "options": ["first", "second"],
                }
            ],
        }
    ).model_copy(update={"revision": 2})
    state = apply_valid_patch(state, addition)
    patch = _agent_patch(operation)

    assert operation_actions(state, patch, patch.ops[0]) == frozenset(
        {ordinary_action, RESTRUCTURE_PROTECTED_EPISTEMIC}
    )
    report = validate_patch(state, patch, ValidationContext(["repo-a"]))
    report.messages.extend(WriteAuthority().validate_permissions(state, patch).messages)
    assert report.rejected
    assert any(message.code == "graph-action-refused" for message in report.messages)
    assert any(message.code == "generated-relation-type-mismatch" for message in report.messages)


def test_create_edge_cannot_overwrite_an_existing_protected_relation_id() -> None:
    state = apply_valid_patch(
        GraphState(project_truth_scope=["repo-a"]),
        seed_patch().model_copy(update={"revision": 1}),
    )
    protected_edge_id = (
        "rq/learning-after-shift::has_hypothesis::hyp/replanning-restores-plasticity"
    )
    patch = _agent_patch(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "ev/collision",
                    "type": "evidence",
                    "title": "Collision evidence",
                    "observation": "An explicit edge id must not replace graph structure.",
                    "origin": "internal_run",
                }
            ],
        },
        {
            "op": "create_edges",
            "edges": [
                {
                    "id": protected_edge_id,
                    "source": "ev/collision",
                    "target": "hyp/replanning-restores-plasticity",
                    "relation": "supports",
                }
            ],
        },
    ).model_copy(update={"revision": 2})

    admission = validate_patch(state, patch, ValidationContext(["repo-a"]))
    replay = validate_patch(state, patch, ValidationContext(["repo-a"], mode="replay"))

    assert operation_actions(state, patch, patch.ops[1]) == frozenset({CREATE_EDGE})
    assert any(message.code == "duplicate-edge-id" for message in admission.messages)
    assert not replay.rejected


def test_existing_protected_node_cannot_be_removed_then_recreated_with_same_id() -> None:
    state = apply_valid_patch(
        GraphState(project_truth_scope=["repo-a"]),
        seed_patch().model_copy(update={"revision": 1}),
    )
    node_id = "rq/learning-after-shift"
    patch = _agent_patch(
        {"op": "remove_nodes", "node_ids": [node_id]},
        {"op": "create_nodes", "nodes": [_research_question(node_id)]},
    ).model_copy(update={"revision": 2})

    admission = validate_patch(state, patch, ValidationContext(["repo-a"]))
    admission.messages.extend(WriteAuthority().validate_permissions(state, patch).messages)
    replay = validate_patch(state, patch, ValidationContext(["repo-a"], mode="replay"))

    assert any(message.code == "graph-action-refused" for message in admission.messages)
    assert any(message.code == "initial-node-id-replacement" for message in admission.messages)
    assert not replay.rejected, [message.message for message in replay.messages]


@pytest.mark.parametrize(
    ("operation", "expected_action"),
    [
        (
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "rq/learning-after-shift",
                        "changes": {"question": "Can an ID replacement exempt this edit?"},
                    }
                ],
            },
            "update_protected_epistemic",
        ),
        (
            {
                "op": "create_edges",
                "edges": [
                    {
                        "id": "edge/replacement-exemption",
                        "source": "rq/learning-after-shift",
                        "target": "hyp/replanning-restores-plasticity",
                        "relation": "has_hypothesis",
                    }
                ],
            },
            "restructure_protected_epistemic",
        ),
    ],
)
def test_later_same_id_creation_cannot_exempt_an_earlier_protected_action(
    operation: dict[str, object], expected_action: str
) -> None:
    state = apply_valid_patch(
        GraphState(project_truth_scope=["repo-a"]),
        seed_patch().model_copy(update={"revision": 1}),
    )
    node_id = "rq/learning-after-shift"
    patch = _agent_patch(
        operation,
        {"op": "create_nodes", "nodes": [_research_question(node_id)]},
    ).model_copy(update={"revision": 2})

    report = validate_patch(state, patch, ValidationContext(["repo-a"]))
    report.messages.extend(WriteAuthority().validate_permissions(state, patch).messages)

    assert expected_action in operation_actions(state, patch, patch.ops[0])
    assert any(message.code == "graph-action-refused" for message in report.messages)
    assert any(message.code == "initial-node-id-replacement" for message in report.messages)


def test_decision_action_is_declared_by_human_action_not_guessed_from_update_shape() -> None:
    state = GraphState(project_truth_scope=["repo-a"])
    create = _agent_patch(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "dec/staged-rule",
                    "type": "decision",
                    "title": "Staged rule",
                    "question": "Which rule?",
                    "options": ["a", "b"],
                }
            ],
        }
    ).model_copy(update={"revision": 1})
    state = apply_valid_patch(state, create)
    operation = {
        "op": "update_nodes",
        "nodes": [
            {
                "id": "dec/staged-rule",
                "changes": {"selected_option": "a", "status": "decided"},
            }
        ],
    }
    ordinary_edit = _agent_patch(operation)
    declared_choice = Patch(
        kind="approval",
        author="human",
        human_action="decision_choice",
        summary="Declared a direct Decision choice.",
        ops=[operation],
    )

    assert operation_actions(state, ordinary_edit, ordinary_edit.ops[0]) == frozenset({UPDATE_NODE})
    assert operation_actions(state, declared_choice, declared_choice.ops[0]) == frozenset(
        {DECIDE_DECISION}
    )


def test_proposal_intent_cannot_leak_onto_an_ordinary_operation() -> None:
    with pytest.raises(ValidationError, match="intent"):
        _agent_patch(
            {"op": "create_nodes", "nodes": [_research_question("rq/staged-question")]},
            {
                "op": "update_nodes",
                "intent": "content_change",
                "nodes": [
                    {
                        "id": "rq/staged-question",
                        "changes": {"question": "A leaked intent cannot authorize this edit."},
                    }
                ],
            },
        )


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "update_nodes", "nodes": None},
        {"op": "create_edges", "edges": [{"source": [], "target": {}, "relation": []}]},
        {"op": "remove_nodes", "node_ids": None},
        {"op": "supersede_nodes", "nodes": None},
    ],
)
def test_malformed_operations_are_reported_instead_of_escaping_authority_derivation(
    operation,
) -> None:
    with pytest.raises(ValidationError):
        _agent_patch(operation)


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
    admission = validate_patch(state, patch, ValidationContext(["repo-a"]))
    replay = validate_patch(state, patch, ValidationContext(["repo-a"], mode="replay"))

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
                            "intent": "status_change",
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


def test_later_edit_stales_status_proposal_for_hypothesis_created_in_same_patch() -> None:
    report = _validate(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "hyp/staged-status-target",
                    "type": "hypothesis",
                    "title": "Staged status target",
                    "statement": "The same outer Patch created this belief.",
                },
                {
                    "id": "ev/staged-status-cause",
                    "type": "evidence",
                    "title": "Staged status cause",
                    "observation": "The evidence was staged in the same outer Patch.",
                    "origin": "analytic",
                },
            ],
        },
        {
            "op": "create_edges",
            "edges": [
                {
                    "id": "edge/staged-status-cause",
                    "source": "ev/staged-status-cause",
                    "target": "hyp/staged-status-target",
                    "relation": "supports",
                }
            ],
        },
        {
            "op": "create_proposals",
            "proposals": [
                {
                    "id": "prop/staged-status-target",
                    "title": "Change the staged belief status",
                    "card": {"decision_needed": "Approve this status change."},
                    "ops": [
                        {
                            "op": "update_nodes",
                            "intent": "status_change",
                            "nodes": [
                                {
                                    "id": "hyp/staged-status-target",
                                    "changes": {"status": "active"},
                                    "cause": {
                                        "kind": "evidence_edge",
                                        "ref_id": "edge/staged-status-cause",
                                    },
                                }
                            ],
                        }
                    ],
                    "base_rev": 0,
                }
            ],
        },
        {
            "op": "update_nodes",
            "nodes": [
                {
                    "id": "hyp/staged-status-target",
                    "changes": {
                        "statement": "A later same-Patch edit replaced the judged statement."
                    },
                }
            ],
        },
    )

    assert report.rejected
    assert any(message.code == "stale-created-proposal" for message in report.messages)


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
        ValidationContext(["repo-a"]),
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
            ValidationContext(["repo-a"]),
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
        ValidationContext(["repo-a"]),
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
        ValidationContext(["repo-a"]),
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
        assert not validate_patch(state, queue, ValidationContext(["repo-a"])).rejected

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
    report = validate_patch(state, unnamed_choice, ValidationContext(["repo-a"]))
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

        assert not validate_patch(state, choice, ValidationContext(["repo-a"])).rejected


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
    report = validate_patch(state, patch, ValidationContext(["repo-a"]))

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
                    "source": "hyp/forward-choice",
                    "target": "dec/forward-choice",
                    "relation": "has_decision",
                }
            ],
        },
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "hyp/forward-choice",
                    "type": "hypothesis",
                    "title": "Claim",
                    "statement": "Claim.",
                },
                {
                    "id": "dec/forward-choice",
                    "type": "decision",
                    "title": "Forward choice",
                    "question": "Which input?",
                },
            ],
        },
    )
    state = GraphState(project_truth_scope=["repo-a"])

    report = validate_patch(state, patch, ValidationContext(["repo-a"]))

    assert not report.rejected
    materialized = apply_valid_patch(state, patch)
    assert (
        materialized.edges["hyp/forward-choice::has_decision::dec/forward-choice"].layer == "seam"
    )


@pytest.fixture
def merge_check(monkeypatch):
    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.agents.command_protocol import MergeAnswersArguments, MergeAnswersCommandRequest
    from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
    from rcp.runs.merge_contract import MergePlan, MergePreparationContract
    from rcp.runs.patch_validator import MergeAnswersCheck

    # Planner and preparation are implemented in parallel slices. This tests only
    # the command boundary against their frozen contract.
    plan = MergePlan.model_construct(
        merge_task_id="merge-task",
        plan_id="a" * 64,
        source_head=GraphHeadRef(
            target=GraphTargetRef(kind="branch", branch_id="source"), revision=2
        ),
        destination_head=GraphHeadRef(revision=3),
        preparation_contract=MergePreparationContract(
            ruleset_tag="test",
            trigger_manifest={},
            transition_trace_schema={},
            orchestrator_patch_schema={},
        ),
    )
    history = object()
    checked = []

    def prepare(plan, answers, history):
        checked.append((plan, answers, history))
        return object()

    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", prepare)
    checker = MergeAnswersCheck(
        plan=plan, history=history, task_id="merge-task", turn_id="merge-turn"
    )
    identity = CommandTurnIdentity(
        episode_id=None, task_id="merge-task", turn_id="merge-turn", authority="validate_only"
    )

    def request(content=b'{"answers": {}, "cards": {}}\r\n', *, index=0):
        return MergeAnswersCommandRequest(
            mailbox_id="a" * 32,
            request_id=f"{index:032x}",
            credential="b" * 64,
            verb="merge_answers",
            arguments=MergeAnswersArguments(action="check", answers=content.decode("utf-8")),
        )

    return checker, identity, request, checked


def test_merge_answers_check_has_no_patch_budget_and_binds_exact_receipt(merge_check):
    import hashlib

    from rcp.limits import PATCH_SELF_CHECK_MAX_COUNT

    checker, identity, request, checked = merge_check
    content = b'{"answers": {}, "cards": {}}\r\n'
    for index in range(PATCH_SELF_CHECK_MAX_COUNT + 1):
        response = checker(request(content, index=index), identity)
        assert response.status == "ok"
    assert len(checked) == PATCH_SELF_CHECK_MAX_COUNT + 1
    assert all(plan is checker.plan and history is checker.history for plan, _, history in checked)
    receipt = checker.require_receipt(content)
    assert receipt.answers_digest == hashlib.sha256(content).hexdigest()
    assert receipt.task_id == identity.task_id
    assert receipt.turn_id == identity.turn_id
    assert receipt.plan_id == checker.plan.plan_id
    assert receipt.source_head == checker.plan.source_head
    assert receipt.destination_head == checker.plan.destination_head
    assert receipt.preparation_contract == checker.plan.preparation_contract
    with pytest.raises(ValueError):
        checker.require_receipt(content.replace(b"\r\n", b"\n"))
    checker.set_plan(checker.plan, turn_id="next-turn")
    with pytest.raises(ValueError):
        checker.require_receipt(content)


@pytest.mark.parametrize(
    "outcome", ["invalid", "hidden_clash", "refused", "timeout", "destination_moved"]
)
def test_merge_answers_check_preserves_typed_failures_without_receipt(
    merge_check, monkeypatch, outcome
):
    from rcp.history import RevisionConflict
    from rcp.runs.merge_contract import (
        MergeAnswersRejected,
        MergeDiagnostic,
        MergeHiddenClash,
        MergeRefused,
        NodeGroupPromotion,
    )
    from rcp.transport import StateUnavailable

    checker, identity, request, _ = merge_check
    diagnostics = (
        MergeDiagnostic(code="test-rejection", message="Rejected", related_node_ids=("node",)),
    )
    failures = {
        "invalid": MergeAnswersRejected(diagnostics),
        "hidden_clash": MergeHiddenClash(
            NodeGroupPromotion(node_ids=("node",), diagnostics=diagnostics)
        ),
        "refused": MergeRefused(diagnostics),
        "timeout": TimeoutError("Preparation timed out"),
        "destination_moved": RevisionConflict("Destination moved"),
    }

    def prepare(*_args):
        raise failures[outcome]

    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", prepare)
    response = checker(request(), identity)
    assert response.status == (
        "unavailable" if outcome in {"timeout", "destination_moved"} else "invalid"
    )
    assert not checker.receipts
    assert (checker.hidden_clash is not None) == (outcome == "hidden_clash")
    assert (checker.refusal is not None) == (outcome == "refused")
    assert (checker.unavailable is not None) == (outcome in {"timeout", "destination_moved"})
    assert checker.destination_moved == (outcome == "destination_moved")
    if outcome in {"timeout", "destination_moved"}:
        with pytest.raises(StateUnavailable):
            checker.require_receipt(b'{"answers": {}, "cards": {}}\r\n')
    if outcome in {"invalid", "hidden_clash", "refused"}:
        assert (
            response.result["outcome"]
            == {
                "invalid": "answers_rejected",
                "hidden_clash": "promotion",
                "refused": "refused",
            }[outcome]
        )
    if outcome in {"invalid", "refused"}:
        assert response.result["diagnostics"] == [diagnostics[0].model_dump(mode="json")]
    if outcome == "hidden_clash":
        assert response.result["promotion"]["node_ids"] == ["node"]


def test_merge_answers_rejects_patch_shape_and_foreign_turn_before_preparation(merge_check):
    from dataclasses import replace

    checker, identity, request, checked = merge_check
    response = checker(request(b'{"ops": []}'), identity)
    assert response.status == "invalid"
    assert checker(request(), replace(identity, turn_id="other-turn")).status == "invalid"
    assert not checked
    assert not checker.receipts


@pytest.mark.parametrize("turn_id", ["merge-turn", "rebased-turn"])
def test_merge_check_discards_inflight_receipt_when_plan_changes(merge_check, monkeypatch, turn_id):

    checker, identity, request, _ = merge_check

    def prepare(*_args):
        checker.set_plan(checker.plan, turn_id=turn_id)
        return object()

    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", prepare)
    assert checker(request(), identity).status == "unavailable"
    assert not checker.receipts


@pytest.mark.asyncio
async def test_repeated_native_session_events_do_not_repeat_checkpoint_receipts():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from rcp.agents import AgentEvent, AgentProcessControl
    from rcp.background import AgentTaskExecution, BackgroundAgentTasks

    store = Mock()
    execution = AgentTaskExecution(
        operation_id="merge-task", store=store, control=AgentProcessControl()
    )

    async def frames():
        for session_id in ("first", "first", "first", "second", "second"):
            event = AgentEvent(event="session", session_id=session_id)
            yield f"data: {event.model_dump_json()}\n\n"

    await BackgroundAgentTasks._consume_frames(
        SimpleNamespace(store=store),
        frames(),
        SimpleNamespace(provider="claude", run_on="local"),
        execution,
        Mock(),
        recorded=False,
    )
    checkpoints = [
        call.args[2]["native_session_id"]
        for call in store.record_agent_task_receipt.call_args_list
        if call.args[1] == "native_agent_checkpoint"
    ]
    assert checkpoints == ["first", "second"]


def test_merge_check_still_running_at_turn_end_is_unavailable(merge_check, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rcp.transport import StateUnavailable

    checker, identity, request, _ = merge_check
    entered, release = Event(), Event()

    def prepare(*args):
        entered.set()
        assert release.wait(5)
        return object()

    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", prepare)
    with ThreadPoolExecutor() as pool:
        result = pool.submit(checker, request(), identity)
        try:
            assert entered.wait(5)
            with pytest.raises(StateUnavailable):
                checker.require_receipt(b'{"answers": {}, "cards": {}}\r\n')
        finally:
            release.set()
        assert result.result().status == "ok"
    assert checker.require_receipt(b'{"answers": {}, "cards": {}}\r\n')


@pytest.mark.parametrize("failure_site", ["preparation", "receipt_store"])
def test_merge_check_internal_error_rejects_previous_receipt_and_clears_pending(
    merge_check, monkeypatch, failure_site
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    checker, identity, request, _ = merge_check
    assert checker(request(), identity).status == "ok"
    snapshot = checker.latest_answers
    receipts = list(checker.receipts)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Unexpected failure")

    if failure_site == "preparation":
        monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", fail)
    else:
        store = Mock()
        store.record_agent_task_receipt.side_effect = fail
        checker.execution = SimpleNamespace(store=store, operation_id="merge-task")
    response = checker(request(index=1), identity)
    assert response.status == "invalid"
    assert response.result["outcome"] == "internal_error"
    assert checker.internal_error
    assert checker._pending_checks == 0
    assert checker.receipts == receipts
    assert checker.latest_answers is snapshot
    with pytest.raises(ValueError, match="Unexpected failure"):
        checker.require_receipt(request().arguments.answers.encode())
    checker.set_plan(checker.plan, turn_id="new-turn")
    assert checker.internal_error is None
    assert checker.latest_answers is None


def test_merge_check_success_clears_refusal_and_retains_only_checked_answers(
    merge_check, monkeypatch
):
    from rcp.history import RevisionConflict
    from rcp.runs.merge_contract import MergeDiagnostic, MergeRefused

    checker, identity, request, _ = merge_check

    def refuse(*_args):
        raise MergeRefused((MergeDiagnostic(code="refused", message="Refused"),))

    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", refuse)
    assert checker(request(), identity).status == "invalid"
    assert checker.refusal is not None
    assert checker._pending_checks == 0
    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", lambda *_args: object())
    assert checker(request(index=1), identity).status == "ok"
    assert checker.refusal is None
    snapshot = checker.latest_answers
    assert snapshot is not None
    assert snapshot.answers == {}

    def moved(*_args):
        raise RevisionConflict("Destination moved")

    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", moved)
    changed = b'{"answers": {"new": {"kind": "branch"}}, "cards": {}}'
    assert checker(request(changed, index=2), identity).status == "unavailable"
    assert checker.destination_moved
    assert checker.latest_answers is snapshot
    assert checker._pending_checks == 0
    assert checker.require_receipt(request().arguments.answers.encode())


def test_merge_check_old_generation_completion_cannot_decrement_current_pending(
    merge_check, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rcp.transport import StateUnavailable

    checker, identity, request, _ = merge_check
    old_entered, old_release = Event(), Event()
    current_entered, current_release = Event(), Event()

    def old_prepare(*_args):
        old_entered.set()
        assert old_release.wait(5)
        return object()

    def current_prepare(*_args):
        current_entered.set()
        assert current_release.wait(5)
        return object()

    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", old_prepare)
    with ThreadPoolExecutor() as pool:
        old = pool.submit(checker, request(), identity)
        try:
            assert old_entered.wait(5)
            checker.set_plan(checker.plan, turn_id=identity.turn_id)
            monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", current_prepare)
            current = pool.submit(checker, request(index=1), identity)
            assert current_entered.wait(5)
            old_release.set()
            assert old.result(timeout=5).status == "unavailable"
            assert checker._pending_checks == 1
            with pytest.raises(StateUnavailable):
                checker.require_receipt(request().arguments.answers.encode())
        finally:
            old_release.set()
            current_release.set()
        assert current.result(timeout=5).status == "ok"
    assert checker._pending_checks == 0
