from __future__ import annotations

import pytest

from rcp.core.materialize import apply_valid_patch
from rcp.core.models import Edge, GraphState, Patch
from rcp.core.validation import validate_patch
from rcp.history import HistoryManager


def _patch(*ops: dict) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Check nonblocking graph quality advice.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=list(ops),
    )


def _experiment(node_id: str = "exp/source", title: str = "Source experiment") -> dict:
    return {"id": node_id, "type": "experiment", "title": title, "objective": "Test a claim."}


def _evidence(origin: str = "internal_run") -> dict:
    return {
        "id": "ev/result",
        "type": "evidence",
        "title": "Observed result",
        "observation": "The result was measured.",
        "origin": origin,
    }


def _state(*nodes: dict, edges: list[dict] | None = None) -> GraphState:
    return GraphState(
        project_truth_scope=["repo-a"],
        nodes={node["id"]: node for node in nodes},
        edges={edge["id"]: edge for edge in edges or []},
    )


def _flags(state: GraphState, patch: Patch):
    report = validate_patch(state, patch, ["repo-a"])
    assert not report.rejected, report.messages
    return report.flags


def test_internal_evidence_without_experiment_is_advisory_and_applies() -> None:
    state = _state()
    patch = _patch({"op": "create_nodes", "nodes": [_evidence()]})
    assert {flag.code for flag in _flags(state, patch)} == {
        "internal-evidence-without-experiment",
        "isolated-operational-node",
    }
    assert "ev/result" in apply_valid_patch(state, patch).nodes
    assert state.nodes == {}


@pytest.mark.parametrize(
    "origin", ["external_publication", "external_instance", "analytic", "unknown"]
)
def test_other_evidence_origins_do_not_need_a_producing_experiment(origin: str) -> None:
    flags = _flags(_state(), _patch({"op": "create_nodes", "nodes": [_evidence(origin)]}))
    assert not any(flag.code == "internal-evidence-without-experiment" for flag in flags)


def test_later_edge_and_title_edit_use_the_complete_candidate() -> None:
    patch = _patch(
        {"op": "create_nodes", "nodes": [_experiment(), _evidence()]},
        {
            "op": "create_edges",
            "edges": [{"source": "exp/source", "target": "ev/result", "relation": "produces"}],
        },
        {"op": "create_nodes", "nodes": [_experiment("exp/unrelated", "Source experiment")]},
        {
            "op": "update_nodes",
            "nodes": [{"id": "exp/unrelated", "changes": {"title": "A different test"}}],
        },
    )
    flags = _flags(_state(), patch)
    assert [(flag.code, flag.related_node_ids) for flag in flags] == [
        ("isolated-operational-node", ["exp/unrelated"])
    ]


@pytest.mark.parametrize(
    "node",
    [
        _experiment(),
        _evidence("analytic"),
        {"id": "dec/choice", "type": "decision", "title": "Choice", "question": "Which path?"},
        {
            "id": "blk/problem",
            "type": "blocker",
            "title": "Problem",
            "description": "Missing input.",
        },
    ],
)
def test_new_operational_nodes_receive_isolation_advice(node: dict) -> None:
    flags = _flags(_state(), _patch({"op": "create_nodes", "nodes": [node]}))
    assert any(flag.code == "isolated-operational-node" for flag in flags)


@pytest.mark.parametrize(
    "node",
    [
        {"id": "rq/new", "type": "research_question", "title": "Question", "question": "Why?"},
        {"id": "hyp/new", "type": "hypothesis", "title": "Claim", "statement": "A causes B."},
    ],
)
def test_new_beliefs_are_not_flagged_as_isolated(node: dict) -> None:
    assert not _flags(_state(), _patch({"op": "create_nodes", "nodes": [node]}))


def test_old_issues_do_not_repeat_even_when_unrelated_fields_change() -> None:
    state = _state(_experiment(), _experiment("exp/other"), _evidence())
    patch = _patch(
        {
            "op": "update_nodes",
            "nodes": [{"id": "ev/result", "changes": {"interpretation": "More context."}}],
        }
    )
    assert not _flags(state, patch)


def test_origin_update_can_introduce_missing_provenance() -> None:
    patch = _patch(
        {
            "op": "update_nodes",
            "nodes": [{"id": "ev/result", "changes": {"origin": "internal_run"}}],
        }
    )
    assert [flag.code for flag in _flags(_state(_evidence("analytic")), patch)] == [
        "internal-evidence-without-experiment"
    ]


def test_removing_last_edge_introduces_provenance_and_isolation_issues() -> None:
    state = _state(
        _experiment(),
        _evidence(),
        edges=[
            {
                "id": "edge/produces",
                "source": "exp/source",
                "target": "ev/result",
                "relation": "produces",
            }
        ],
    )
    flags = _flags(state, _patch({"op": "remove_edges", "edge_ids": ["edge/produces"]}))
    assert {(flag.code, tuple(flag.related_node_ids)) for flag in flags} == {
        ("internal-evidence-without-experiment", ("ev/result",)),
        ("isolated-operational-node", ("ev/result",)),
        ("isolated-operational-node", ("exp/source",)),
    }


def test_only_valid_produces_endpoint_types_count_as_evidence_provenance() -> None:
    # Historical/custom graph data must not make any produces-shaped edge proof.
    state = _state(_evidence("analytic"))
    state.edges["legacy"] = Edge(
        id="legacy", source="ev/result", target="ev/result", relation="produces"
    )
    patch = _patch(
        {
            "op": "update_nodes",
            "nodes": [{"id": "ev/result", "changes": {"origin": "internal_run"}}],
        }
    )
    assert [flag.code for flag in _flags(state, patch)] == ["internal-evidence-without-experiment"]


def test_identical_titles_normalize_whitespace_and_case_and_report_one_new_group() -> None:
    state = _state(
        _experiment("exp/first", "Shared title"),
        _experiment("exp/second", "SHARED TITLE"),
        _experiment("exp/third", "Other title"),
    )
    patch = _patch(
        {
            "op": "update_nodes",
            "nodes": [{"id": "exp/third", "changes": {"title": " shared\n TITLE "}}],
        }
    )
    flags = _flags(state, patch)
    assert [(flag.code, flag.related_node_ids) for flag in flags] == [
        ("identical-node-title", ["exp/first", "exp/second", "exp/third"]),
    ]


def test_titles_of_different_node_types_do_not_warn() -> None:
    experiment = _experiment(title="Observed result")
    flags = _flags(_state(), _patch({"op": "create_nodes", "nodes": [experiment, _evidence()]}))
    assert not any(flag.code == "identical-node-title" for flag in flags)


def test_old_title_group_has_no_pairwise_output_and_new_member_gets_one_warning() -> None:
    nodes = [_experiment(f"exp/test-{index}", "Shared title") for index in range(100)]
    state = _state(*nodes)
    assert not _flags(
        state,
        _patch(
            {
                "op": "update_nodes",
                "nodes": [{"id": nodes[0]["id"], "changes": {"design": "More detail."}}],
            }
        ),
    )
    flags = _flags(
        state,
        _patch({"op": "create_nodes", "nodes": [_experiment("exp/addition", "Shared title")]}),
    )
    titles = [flag for flag in flags if flag.code == "identical-node-title"]
    assert len(titles) == 1
    assert set(titles[0].related_node_ids) == {*state.nodes, "exp/addition"}


@pytest.mark.parametrize("connect", [True, False])
def test_human_batch_advice_describes_final_atomic_graph(manifest, connect: bool) -> None:
    def approval(operation: dict) -> Patch:
        return Patch(
            kind="approval", author="human", summary="Edit the research graph.", ops=[operation]
        )

    history = HistoryManager(manifest)
    patches = [
        approval({"op": "create_nodes", "nodes": [_experiment()]}),
        approval({"op": "create_nodes", "nodes": [_evidence()]}),
    ]
    if connect:
        patches.append(
            approval(
                {
                    "op": "create_edges",
                    "edges": [
                        {"source": "exp/source", "target": "ev/result", "relation": "produces"}
                    ],
                }
            )
        )
    committed, result = history.append_batch(patches)
    assert len(committed) == 1
    messages = committed[0].admission_messages
    if connect:
        assert not messages
    else:
        assert {message.code for message in messages} == {
            "isolated-operational-node",
            "internal-evidence-without-experiment",
        }
        assert len(messages) == 3
    assert result.state.validation_messages == messages
    assert history.state().validation_messages == messages


def test_rejected_partial_graph_and_historical_replay_get_no_new_advice() -> None:
    patch = _patch(
        {"op": "create_nodes", "nodes": [_evidence()]},
        {
            "op": "create_edges",
            "edges": [{"source": "exp/missing", "target": "ev/result", "relation": "produces"}],
        },
    )
    report = validate_patch(_state(), patch, ["repo-a"])
    assert report.rejected
    assert not report.flags

    replay = validate_patch(
        _state(), _patch({"op": "create_nodes", "nodes": [_evidence()]}), ["repo-a"], mode="replay"
    )
    assert not replay.rejected
    assert not replay.flags
