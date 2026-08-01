from __future__ import annotations

from copy import deepcopy

from rcp.control import derive_experiment_control_state, governing_decision_bundle
from rcp.core.models import (
    Blocker,
    Decision,
    Edge,
    Experiment,
    ExperimentAttempt,
    ExperimentDecisionPin,
    GatedCard,
    GraphState,
    Hypothesis,
    Patch,
    Proposal,
)
from rcp.core.validation import validate_patch

EXPERIMENT_ID = "exp/control-loop"
DECISION_ID = "dec/resource-shape"
PIN = ExperimentDecisionPin(
    decision_id=DECISION_ID,
    decision_revision=3,
    selected_option="4xA100",
)


def _state(*, attempts: list[ExperimentAttempt] | None = None, ceiling: int = 5) -> GraphState:
    experiment = Experiment(
        id=EXPERIMENT_ID,
        type="experiment",
        title="Control loop",
        objective="Test the loop.",
        attempt_ceiling=ceiling,
        attempts=attempts or [],
    )
    decision = Decision(
        id=DECISION_ID,
        type="decision",
        title="Resource shape",
        question="Which resource shape?",
        options=["4xA100", "8xA100"],
        selected_option="4xA100",
        status="decided",
        updated_rev=3,
    )
    blocker = Blocker(
        id="blk/capacity",
        type="blocker",
        title="Capacity",
        description="Wait for capacity.",
        status="resolved",
    )
    hypothesis = Hypothesis(
        id="hyp/target",
        type="hypothesis",
        title="Target",
        statement="The intervention helps.",
    )
    return GraphState(
        revision=3,
        project_truth_scope=["repo"],
        nodes={node.id: node for node in (experiment, decision, blocker, hypothesis)},
        edges={
            "governed": Edge(
                id="governed",
                source=EXPERIMENT_ID,
                target=DECISION_ID,
                relation="governed_by",
                layer="action",
            ),
            "blocked": Edge(
                id="blocked",
                source=EXPERIMENT_ID,
                target=blocker.id,
                relation="blocked_by",
                layer="action",
            ),
            "tests": Edge(
                id="tests",
                source=EXPERIMENT_ID,
                target=hypothesis.id,
                relation="tests",
                layer="seam",
            ),
        },
    )


def _attempt(
    *,
    attempt_id: str = "attempt-1",
    status: str = "running",
    selected_option: str = "4xA100",
    attempt_kind: str = "external_run",
) -> ExperimentAttempt:
    return ExperimentAttempt.model_validate(
        {
            "id": attempt_id,
            "sequence": 1,
            "purpose": "Run the configured experiment.",
            "attempt_kind": attempt_kind,
            "decision_bundle": [
                {
                    "decision_id": DECISION_ID,
                    "decision_revision": 3,
                    "selected_option": selected_option,
                }
            ],
            "status": status,
            "job_refs": ["4471"],
        }
    )


def _patch(ops: list[dict], *, stamp: bool = True) -> Patch:
    return Patch(
        revision=4,
        kind="experiment_loop",
        author="agent",
        summary="Reflect the bounded experiment loop.",
        ops=ops,
        run_truth_scope=["repo"],
        repositories_read=[],
        experiment_control_node_id=EXPERIMENT_ID if stamp else None,
        experiment_decision_bundle=[PIN] if stamp else [],
    )


def _codes(report) -> set[str]:
    return {message.code for message in report.messages if message.level == "reject"}


def test_readiness_is_derived_from_decisions_proposals_blockers_and_ceiling() -> None:
    state = _state()
    ready = derive_experiment_control_state(state, EXPERIMENT_ID)
    assert ready.ready
    assert ready.reasons == []
    assert ready.governing_decisions == [PIN]

    decision = state.nodes[DECISION_ID]
    state.nodes[DECISION_ID] = decision.model_copy(
        update={"status": "open", "selected_option": None}
    )
    state.proposals["proposal/resource"] = Proposal(
        id="proposal/resource",
        title="Change resources",
        card=GatedCard(),
        ops=[
            {
                "op": "update_nodes",
                "nodes": [{"id": DECISION_ID, "changes": {"selected_option": "8xA100"}}],
            }
        ],
        related_node_ids=[DECISION_ID],
        base_rev=3,
    )
    blocker = state.nodes["blk/capacity"]
    state.nodes[blocker.id] = blocker.model_copy(update={"status": "open"})
    experiment = state.nodes[EXPERIMENT_ID]
    state.nodes[EXPERIMENT_ID] = experiment.model_copy(
        update={"attempt_ceiling": 1, "attempts": [_attempt(status="completed")]}
    )

    gated = derive_experiment_control_state(state, EXPERIMENT_ID)
    assert not gated.ready
    assert gated.reasons == [
        f"Decision {DECISION_ID} is not decided with a selected option.",
        f"Decision {DECISION_ID} has a pending proposal.",
        "Blocker blk/capacity is open.",
        "Attempt ceiling reached: 1 of 1 attempts used.",
    ]


def _belief_patch_ops(
    *,
    relation: str = "weakens",
    changes: dict[str, object] | None = None,
    cause: dict[str, object] | None = None,
    target: str = "hyp/target",
) -> list[dict[str, object]]:
    return [
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "ev/result",
                    "type": "evidence",
                    "title": "Result",
                    "observation": "Val perplexity rose by 1.5.",
                    "origin": "internal_run",
                }
            ],
        },
        {
            "op": "create_edges",
            "edges": [
                {"source": EXPERIMENT_ID, "target": "ev/result", "relation": "produces"},
                {"source": "ev/result", "target": "hyp/target", "relation": relation},
            ],
        },
        {
            "op": "create_proposals",
            "proposals": [
                {
                    "id": "proposal/belief",
                    "title": "Weaken the target hypothesis",
                    "card": {"decision_needed": "Accept this belief change?"},
                    "related_node_ids": [target],
                    "base_rev": 3,
                    "ops": [
                        {
                            "op": "update_nodes",
                            "nodes": [
                                {
                                    "id": target,
                                    "changes": changes or {"status": "weakened"},
                                    "cause": (
                                        cause
                                        if cause is not None
                                        else {
                                            "kind": "proposal_resolution",
                                            "ref_id": "proposal/belief",
                                        }
                                    ),
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    ]


def test_a_loop_proposes_the_belief_change_its_own_evidence_implies() -> None:
    state = _state()
    assert not validate_patch(state, _patch(_belief_patch_ops()), ["repo"]).rejected

    # The status move is the only thing the human is being asked to accept.
    wider = _belief_patch_ops(changes={"status": "rejected", "scope": "Narrower."})
    assert "experiment-loop-belief-proposal-operations" in _codes(
        validate_patch(state, _patch(wider), ["repo"])
    )

    # The recorded cause is the human's approval, so the belief transition never
    # claims the loop moved it.
    borrowed = _belief_patch_ops(
        cause={"kind": "evidence_edge", "ref_id": "ev/result::weakens::hyp/target"}
    )
    assert "experiment-loop-belief-cause" in _codes(
        validate_patch(state, _patch(borrowed), ["repo"])
    )

    # And the same patch must actually point evidence at that hypothesis.
    ungrounded = deepcopy(_belief_patch_ops())
    ungrounded[1]["edges"] = [
        {"source": EXPERIMENT_ID, "target": "ev/result", "relation": "produces"}
    ]
    assert "experiment-loop-belief-grounding" in _codes(
        validate_patch(state, _patch(ungrounded), ["repo"])
    )

    # A hypothesis this experiment does not test is neither a belief target nor a
    # governing decision, so it falls back to the decision-scope refusal.
    foreign = _belief_patch_ops(target="hyp/unrelated")
    assert "experiment-loop-proposal-scope" in _codes(
        validate_patch(state, _patch(foreign), ["repo"])
    )


def test_a_proposal_that_only_references_a_decision_does_not_gate_the_run() -> None:
    state = _state()
    state.proposals["proposal/elsewhere"] = Proposal(
        id="proposal/elsewhere",
        title="Split a hypothesis",
        card=GatedCard(),
        ops=[
            {
                "op": "update_nodes",
                "nodes": [{"id": "hyp/target", "changes": {"scope": "Narrower."}}],
            }
        ],
        # A seed may name a decision it merely read while raising this.
        related_node_ids=[DECISION_ID, "hyp/target"],
        base_rev=3,
    )
    assert derive_experiment_control_state(state, EXPERIMENT_ID).ready


def test_decision_drift_reports_a_moved_or_contested_pin_without_gating() -> None:
    state = _state(attempts=[_attempt(status="completed")])
    assert derive_experiment_control_state(state, EXPERIMENT_ID).decision_drift == []

    decision = state.nodes[DECISION_ID]
    state.nodes[DECISION_ID] = decision.model_copy(
        update={"selected_option": "8xA100", "updated_rev": 7}
    )
    moved = derive_experiment_control_state(state, EXPERIMENT_ID)
    assert moved.ready
    assert [item.decision_id for item in moved.decision_drift] == [DECISION_ID]
    assert moved.decision_drift[0].pinned_option == "4xA100"
    assert moved.decision_drift[0].current_option == "8xA100"
    assert not moved.decision_drift[0].proposed


def test_active_loop_marker_uses_control_work_or_nonterminal_attempts() -> None:
    state = _state()
    operation_active = derive_experiment_control_state(
        state, EXPERIMENT_ID, active_control_node_ids=[EXPERIMENT_ID]
    )
    assert operation_active.active
    assert operation_active.reasons == ["An experiment loop is already active."]

    state = _state(attempts=[_attempt()])
    attempt_active = derive_experiment_control_state(state, EXPERIMENT_ID)
    assert attempt_active.active
    assert not attempt_active.ready


def test_run_pins_the_governing_decision_bundle_in_stable_order() -> None:
    state = _state()
    second = Decision(
        id="dec/analysis",
        type="decision",
        title="Analysis",
        question="Which analysis?",
        selected_option="paired",
        status="decided",
        updated_rev=2,
    )
    state.nodes[second.id] = second
    state.edges["second"] = Edge(
        id="second",
        source=EXPERIMENT_ID,
        target=second.id,
        relation="governed_by",
        layer="action",
    )
    assert [item.decision_id for item in governing_decision_bundle(state, EXPERIMENT_ID)] == [
        "dec/analysis",
        DECISION_ID,
    ]


def test_loop_patch_can_append_and_close_its_own_attempt() -> None:
    state = _state()
    append = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": EXPERIMENT_ID,
                        "changes": {"status": "running", "attempts": [_attempt()]},
                    }
                ],
            }
        ]
    )
    assert not validate_patch(state, append, ["repo"]).rejected

    state = _state(attempts=[_attempt()])
    closed = _attempt(status="completed").model_copy(update={"outcome": "Finished."})
    close = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [{"id": EXPERIMENT_ID, "changes": {"attempts": [closed]}}],
            }
        ]
    )
    assert not validate_patch(state, close, ["repo"]).rejected

    # Lowering the human-owned ceiling stops new attempts; it must not prevent
    # the watcher turn from closing work that was already launched.
    first = _attempt(status="completed")
    second = _attempt(attempt_id="attempt-2").model_copy(update={"sequence": 2})
    state = _state(attempts=[first, second], ceiling=1)
    closed_second = second.model_copy(update={"status": "completed", "outcome": "Finished."})
    close_after_lowering = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": EXPERIMENT_ID,
                        "changes": {"attempts": [first, closed_second]},
                    }
                ],
            }
        ]
    )
    assert not validate_patch(state, close_after_lowering, ["repo"]).rejected


def test_loop_patch_cannot_append_more_than_one_attempt() -> None:
    state = _state()
    first = _attempt(status="completed")
    second = _attempt(attempt_id="attempt-2").model_copy(update={"sequence": 2})
    patch = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": EXPERIMENT_ID,
                        "changes": {"attempts": [first, second]},
                    }
                ],
            }
        ]
    )

    assert "experiment-loop-multiple-attempts" in _codes(validate_patch(state, patch, ["repo"]))


def test_loop_patch_cannot_edit_the_pinned_bundle_or_other_graph_authority() -> None:
    state = _state(attempts=[_attempt()])
    rewritten = _attempt(status="completed", selected_option="8xA100")
    operations = [
        {
            "op": "update_nodes",
            "nodes": [
                {"id": EXPERIMENT_ID, "changes": {"attempts": [rewritten]}},
                {"id": DECISION_ID, "changes": {"status": "open"}},
            ],
        },
        {"op": "set_standing", "node_id": EXPERIMENT_ID, "standing": "accepted"},
    ]
    codes = _codes(validate_patch(state, _patch(operations), ["repo"]))
    assert "experiment-loop-attempt-mutation" in codes
    assert "experiment-loop-foreign-update" in codes
    assert "experiment-loop-operation" in codes


def test_loop_patch_cannot_mutate_completion_criteria_or_exceed_ceiling() -> None:
    state = _state(attempts=[_attempt(status="completed")], ceiling=1)
    second = _attempt(attempt_id="attempt-2", status="running").model_copy(update={"sequence": 2})
    patch = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": EXPERIMENT_ID,
                        "changes": {
                            "completion_criteria": ["Never agent editable."],
                            "attempts": [
                                *_state(attempts=[_attempt(status="completed")])
                                .nodes[EXPERIMENT_ID]
                                .attempts,
                                second,
                            ],
                        },
                    }
                ],
            }
        ]
    )
    codes = _codes(validate_patch(state, patch, ["repo"]))
    assert "experiment-loop-experiment-field" in codes
    assert "experiment-loop-attempt-ceiling" in codes


def test_loop_patch_may_create_evidence_blockers_and_epistemic_edges_only() -> None:
    state = _state()
    evidence = {
        "id": "ev/result",
        "type": "evidence",
        "title": "Result",
        "observation": "The run completed.",
        "origin": "internal_run",
    }
    valid = _patch(
        [
            {"op": "create_nodes", "nodes": [evidence]},
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": evidence["id"],
                        "target": "hyp/target",
                        "relation": "supports",
                    }
                ],
            },
        ]
    )
    assert not validate_patch(state, valid, ["repo"]).rejected

    invalid = deepcopy(valid.ops)
    invalid[0]["nodes"] = [
        {
            "id": "hyp/new",
            "type": "hypothesis",
            "title": "New",
            "statement": "New hypothesis.",
        }
    ]
    invalid[1]["edges"] = [
        {
            "source": EXPERIMENT_ID,
            "target": "hyp/target",
            "relation": "tests",
        }
    ]
    codes = _codes(validate_patch(state, _patch(invalid), ["repo"]))
    assert "experiment-loop-created-node" in codes
    assert "experiment-loop-edge-layer" in codes


def test_loop_attaches_its_own_evidence_and_blockers_to_its_experiment() -> None:
    state = _state()
    evidence = {
        "id": "ev/result",
        "type": "evidence",
        "title": "Result",
        "observation": "The run completed.",
        "origin": "internal_run",
    }
    blocker = {
        "id": "blk/exhausted",
        "type": "blocker",
        "title": "Exhausted",
        "description": "The attempt ceiling was reached.",
    }
    attached = _patch(
        [
            {"op": "create_nodes", "nodes": [evidence, blocker]},
            {
                "op": "create_edges",
                "edges": [
                    {"source": EXPERIMENT_ID, "target": "ev/result", "relation": "produces"},
                    {"source": EXPERIMENT_ID, "target": "blk/exhausted", "relation": "blocked_by"},
                ],
            },
        ]
    )
    assert not validate_patch(state, attached, ["repo"]).rejected

    # Attaching a node the patch did not create would let the loop claim
    # someone else's evidence or block itself on an unrelated blocker.
    foreign = deepcopy(attached.ops)
    foreign[1]["edges"] = [
        {"source": EXPERIMENT_ID, "target": "blk/capacity", "relation": "blocked_by"}
    ]
    assert "experiment-loop-self-attachment" in _codes(
        validate_patch(state, _patch(foreign), ["repo"])
    )

    # And it may only attach to its own experiment.
    foreign_source = deepcopy(attached.ops)
    foreign_source[1]["edges"] = [
        {"source": "exp/other", "target": "ev/result", "relation": "produces"}
    ]
    assert "experiment-loop-self-attachment" in _codes(
        validate_patch(state, _patch(foreign_source), ["repo"])
    )


def test_proposal_only_iteration_is_typed_and_scoped_to_a_governing_decision() -> None:
    state = _state()
    proposal = {
        "id": "prop/change-resources",
        "title": "Change resources",
        "card": {
            "situation_cold": "The current resource shape is insufficient.",
            "why_human_now": "The governing decision must change.",
            "consequences": "The next run uses another resource shape.",
            "decision_needed": "Choose whether to revisit the resource decision.",
        },
        "ops": [
            {
                "op": "update_nodes",
                "nodes": [{"id": DECISION_ID, "changes": {"status": "revisit"}}],
            }
        ],
        "related_node_ids": [DECISION_ID],
        "base_rev": 3,
    }
    proposal_attempt = _attempt(attempt_kind="proposal_only", status="completed").model_copy(
        update={"job_refs": []}
    )
    valid = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [{"id": EXPERIMENT_ID, "changes": {"attempts": [proposal_attempt]}}],
            },
            {"op": "create_proposals", "proposals": [proposal]},
        ]
    )
    assert not validate_patch(state, valid, ["repo"]).rejected

    running_state = _state(attempts=[_attempt()])
    closed_attempt = _attempt(status="completed").model_copy(update={"outcome": "Needs change."})
    close_and_propose = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [{"id": EXPERIMENT_ID, "changes": {"attempts": [closed_attempt]}}],
            },
            {"op": "create_proposals", "proposals": [proposal]},
        ]
    )
    assert not validate_patch(running_state, close_and_propose, ["repo"]).rejected

    wrong_scope = deepcopy(proposal)
    wrong_scope["related_node_ids"] = ["hyp/target"]
    codes = _codes(
        validate_patch(
            state,
            _patch([{"op": "create_proposals", "proposals": [wrong_scope]}]),
            ["repo"],
        )
    )
    assert "experiment-loop-proposal-scope" in codes

    # A target that is neither a pinned decision nor a hypothesis this experiment
    # tests falls through both proposal shapes.
    hidden_foreign_update = deepcopy(proposal)
    hidden_foreign_update["ops"] = [
        {
            "op": "update_nodes",
            "nodes": [{"id": "blk/capacity", "changes": {"status": "resolved"}}],
        }
    ]
    hidden_foreign_update["related_node_ids"] = [DECISION_ID]
    codes = _codes(
        validate_patch(
            state,
            _patch(
                [
                    {
                        "op": "update_nodes",
                        "nodes": [
                            {
                                "id": EXPERIMENT_ID,
                                "changes": {"attempts": [proposal_attempt]},
                            }
                        ],
                    },
                    {"op": "create_proposals", "proposals": [hidden_foreign_update]},
                ]
            ),
            ["repo"],
        )
    )
    assert "experiment-loop-proposal-operations" in codes


def test_proposal_only_attempt_requires_a_proposal_in_the_same_patch() -> None:
    attempt = _attempt(attempt_kind="proposal_only", status="completed").model_copy(
        update={"job_refs": []}
    )
    patch = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [{"id": EXPERIMENT_ID, "changes": {"attempts": [attempt]}}],
            }
        ]
    )
    assert "experiment-loop-proposal-attempt" in _codes(validate_patch(_state(), patch, ["repo"]))

    launched_proposal_attempt = _attempt(attempt_kind="proposal_only", status="completed")
    launched = _patch(
        [
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": EXPERIMENT_ID,
                        "changes": {"attempts": [launched_proposal_attempt]},
                    }
                ],
            },
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": "prop/with-job",
                        "title": "Change resources",
                        "card": {},
                        "ops": [
                            {
                                "op": "update_nodes",
                                "nodes": [{"id": DECISION_ID, "changes": {"status": "revisit"}}],
                            }
                        ],
                        "related_node_ids": [DECISION_ID],
                        "base_rev": 3,
                    }
                ],
            },
        ]
    )
    assert "experiment-loop-proposal-job" in _codes(validate_patch(_state(), launched, ["repo"]))

    nonterminal_attempt = attempt.model_copy(update={"status": "running"})
    nonterminal = launched.model_copy(
        update={
            "ops": [
                {
                    "op": "update_nodes",
                    "nodes": [
                        {
                            "id": EXPERIMENT_ID,
                            "changes": {"attempts": [nonterminal_attempt]},
                        }
                    ],
                },
                launched.ops[1],
            ]
        }
    )
    assert "experiment-loop-proposal-status" in _codes(
        validate_patch(_state(), nonterminal, ["repo"])
    )


def test_loop_patch_requires_persisted_rcp_control_binding() -> None:
    report = validate_patch(_state(), _patch([], stamp=False), ["repo"])
    assert "experiment-loop-control-node" in _codes(report)


def test_old_attempts_load_with_backward_compatible_control_defaults() -> None:
    attempt = ExperimentAttempt(id="old", sequence=1, purpose="Legacy attempt")
    assert attempt.attempt_kind == "external_run"
    assert attempt.decision_bundle == []
    assert attempt.debug is None
