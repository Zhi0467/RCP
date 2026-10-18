"""Random patch sequences through the real gate keep the graph well-formed.

A development test, not a runtime check: it proves the per-operation checks
are complete. A failure names an operation whose checks miss a case.
"""

from __future__ import annotations

import random
import typing
from collections import Counter
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from rcp.core.authority import WriteAuthority
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import RELATION_SPEC, GraphState, Patch, ProjectNode, graph_fields
from rcp.core.operations import graph_operations_from_proposal, operation_dict
from rcp.core.validation import NODE_PREFIXES, ValidationContext, validate_patch
from rcp.core.validation.proposals import proposal_approval_operations, proposal_is_stale
from rcp.runs.merge_contract import (
    MergeAnswers,
    MergeAnswersRejected,
    MergeHiddenClash,
    MergeRefused,
)
from rcp.runs.merge_patch import prepare_merge_candidate
from tests.helpers import seed_patch
from tests.test_merge_regressions import _answers, _append, _commit, _fork_history, _plan

SCOPE = ["repo-a"]
STEPS = 1500
MIN_ACCEPTED = 5
OP_KINDS = (
    "create_nodes",
    "update_nodes",
    "create_edges",
    "remove_edges",
    "remove_nodes",
    "supersede_nodes",
    "merge_nodes",
    "create_proposals",
    "resolve_proposals",
    "withdraw_proposals",
    "set_standing",
)
REQUIRED = {
    "research_question": {"question": "Does the effect hold?"},
    "hypothesis": {"statement": "The effect holds."},
    "decision": {"question": "Which setup?", "options": ["Setup one", "Setup two"]},
    "experiment": {"objective": "Measure the effect."},
    "evidence": {"observation": "The run reported a value.", "origin": "analytic"},
    "blocker": {"description": "The cluster is down."},
}
ASSESSED = {"supports", "weakens", "refutes", "inconclusive", "contradicts"}
# Node fields whose name says they hold an id. Each is either a reference to a
# live node, history kept as written, or not a node id at all.
LIVE_NODE_REFS: frozenset[str] = frozenset()
HISTORY_NODE_REFS = {
    # The attempt ledger records the Decision revision it ran under; later graph
    # edits must not rewrite what an attempt was launched with.
    "attempts.decision_bundle.decision_id": "attempt pins are a ledger snapshot",
}
NOT_NODE_REFS = {
    "source_refs.session_id": "a provider session id",
    "attempts.source_refs.session_id": "a provider session id",
}


def _id_fields(model: type[BaseModel], prefix: str = "") -> set[str]:
    found: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        if name.endswith(("_id", "_ids")):
            found.add(path)
        for arg in (field.annotation, *typing.get_args(field.annotation)):
            for inner in (arg, *typing.get_args(arg)):
                if isinstance(inner, type) and issubclass(inner, BaseModel):
                    found |= _id_fields(inner, f"{path}.")
    return found


def _node_id_values(value: Any, path: tuple[str, ...]) -> list[str]:
    if not path:
        return value if isinstance(value, list) else [] if value is None else [value]
    if isinstance(value, list):
        return [found for item in value for found in _node_id_values(item, path)]
    return _node_id_values(getattr(value, path[0], None), path[1:])


def _violations(state: GraphState) -> dict[str, set[str]]:
    """Each broken invariant, keyed by name, with the offending object ids."""

    found: dict[str, set[str]] = {}
    for edge in state.edges.values():
        if edge.source not in state.nodes or edge.target not in state.nodes:
            found.setdefault("edge-endpoint-exists", set()).add(edge.id)
            continue
        spec = RELATION_SPEC.get(edge.relation)  # type: ignore[call-overload]
        source, target = state.nodes[edge.source].type, state.nodes[edge.target].type
        if (
            spec is None
            or source not in spec.source_types
            or target not in spec.target_types
            or (spec.same_type and source != target)
        ):
            found.setdefault("edge-relation-fits-types", set()).add(edge.id)
    for proposal in state.proposals.values():
        if proposal.status != "pending" or proposal_is_stale(state, proposal):
            continue
        if not set(proposal.related_node_ids) <= set(state.nodes):
            found.setdefault("open-proposal-targets-exist", set()).add(proposal.id)
    for node in state.nodes.values():
        for path in LIVE_NODE_REFS:
            for ref in _node_id_values(node, tuple(path.split("."))):
                if ref not in state.nodes:
                    found.setdefault("node-ref-field-is-live", set()).add(f"{node.id}:{path}")
    return found


class _Generator:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.ever: list[str] = []
        self.proposal_count = 0

    def node_id(self, state: GraphState, node_type: str | None = None) -> str:
        pool = [n for n, node in state.nodes.items() if node_type in (None, node.type)]
        removed = [n for n in self.ever if n not in state.nodes]
        roll = self.rng.random()
        if pool and roll < 0.7:
            return self.rng.choice(pool)
        if removed and roll < 0.9:
            return self.rng.choice(removed)
        node_type = node_type or self.rng.choice(sorted(REQUIRED))
        return f"{NODE_PREFIXES[node_type]}/n{self.rng.randrange(8)}"

    def new_node(self, node_type: str | None = None, suffix: str | None = None) -> dict[str, Any]:
        node_type = node_type or self.rng.choice(sorted(REQUIRED))
        node_id = f"{NODE_PREFIXES[node_type]}/{suffix or f'n{self.rng.randrange(8)}'}"
        self.ever.append(node_id)
        return {"id": node_id, "type": node_type, "title": "Node", **REQUIRED[node_type]}

    def edge(self, state: GraphState) -> dict[str, Any]:
        relation = self.rng.choice(sorted(RELATION_SPEC))
        spec = RELATION_SPEC[relation]
        fit = self.rng.random() < 0.7
        source = self.node_id(state, self.rng.choice(sorted(spec.source_types)) if fit else None)
        source_type = state.nodes[source].type if source in state.nodes else None
        target_type = source_type if spec.same_type and fit else None
        if fit and target_type is None:
            target_type = self.rng.choice(sorted(spec.target_types))
        edge: dict[str, Any] = {
            "source": source,
            "target": self.node_id(state, target_type),
            "relation": relation,
        }
        if relation in ASSESSED and source.startswith("ev/"):
            edge["assessment"] = {"relevance": "direct", "weight": "moderate"}
        return edge

    def merge_base(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Stratify real operations so rare endpoint/relation types cannot disappear."""
        nodes = [self.new_node(kind, f"base{i}") for kind in sorted(REQUIRED) for i in range(2)]
        edges = []
        for relation, spec in sorted(RELATION_SPEC.items()):
            source_type = self.rng.choice(sorted(spec.source_types))
            target_type = (
                source_type if spec.same_type else self.rng.choice(sorted(spec.target_types))
            )
            edge = {
                "id": f"edge/{relation}",
                "source": f"{NODE_PREFIXES[source_type]}/base0",
                "target": f"{NODE_PREFIXES[target_type]}/base1",
                "relation": relation,
                "explanation": f"Base {relation}",
            }
            if relation in ASSESSED and source_type == "evidence":
                edge["assessment"] = {"relevance": "direct", "weight": "moderate"}
            edges.append(edge)
        return nodes, edges

    def operation(self, state: GraphState, kind: str) -> dict[str, Any]:
        rng = self.rng
        if kind == "create_nodes":
            return {"op": kind, "nodes": [self.new_node()]}
        if kind == "update_nodes":
            node_id = self.node_id(state)
            return {"op": kind, "nodes": [{"id": node_id, "changes": {"title": "Edited"}}]}
        if kind == "create_edges":
            return {"op": kind, "edges": [self.edge(state)]}
        if kind == "remove_edges":
            edges = sorted(state.edges) or ["rq/n0::has_hypothesis::hyp/n0"]
            return {"op": kind, "edge_ids": [rng.choice(edges)]}
        if kind == "remove_nodes":
            return {"op": kind, "node_ids": [self.node_id(state)]}
        if kind == "supersede_nodes":
            node_id = self.node_id(state)
            node_type = state.nodes[node_id].type if node_id in state.nodes else None
            item: dict[str, Any] = {"id": node_id}
            if rng.random() < 0.8:
                item["superseded_by"] = self.node_id(state, node_type)
            return {"op": kind, "nodes": [item]}
        if kind == "merge_nodes":
            duplicate = self.node_id(state)
            node_type = state.nodes[duplicate].type if duplicate in state.nodes else None
            canonical = self.node_id(state, node_type)
            return {"op": kind, "merges": [{"duplicate": duplicate, "canonical": canonical}]}
        if kind == "set_standing":
            standing = rng.choice(["asserted", "accepted", "contested"])
            return {"op": kind, "node_id": self.node_id(state), "standing": standing}
        if kind == "create_proposals":
            inner_kind = rng.choice(
                ["remove_nodes", "supersede_nodes", "merge_nodes", "update_nodes", "set_standing"]
            )
            inner = self.operation(state, inner_kind)
            inner["intent"] = {
                "remove_nodes": "removal",
                "supersede_nodes": "supersede",
                "merge_nodes": "merge",
                "update_nodes": "content_change",
                "set_standing": "standing_change",
            }[inner_kind]
            self.proposal_count += 1
            proposal = {
                "id": f"prop/p{self.proposal_count}",
                "title": "Proposed change",
                "card": {"decision_needed": "Approve?"},
                "ops": [inner],
                "base_rev": state.revision,
            }
            return {"op": kind, "proposals": [proposal]}
        pending = [p.id for p in state.proposals.values() if p.status == "pending"]
        pool = pending if pending and rng.random() < 0.8 else sorted(state.proposals)
        proposal_id = rng.choice(pool or ["prop/p0"])
        if kind == "withdraw_proposals":
            return {"op": kind, "proposals": [{"id": proposal_id}]}
        status = rng.choice(["approved", "approved", "rejected"])
        return {"op": kind, "resolutions": [{"id": proposal_id, "status": status}]}

    def patch(self, state: GraphState) -> Patch:
        kind = self.rng.choice(OP_KINDS)
        ops = [self.operation(state, kind)]
        if kind == "resolve_proposals" and ops[0]["resolutions"][0]["status"] == "approved":
            proposal = state.proposals.get(ops[0]["resolutions"][0]["id"])
            if proposal is not None:
                ops = [operation_dict(op) for op in graph_operations_from_proposal(proposal.ops)]
                ops.append(self.operation(state, kind))
                ops[-1]["resolutions"] = [{"id": proposal.id, "status": "approved"}]
        human = kind in {"resolve_proposals", "set_standing"} or (
            kind not in {"create_proposals", "withdraw_proposals"} and self.rng.random() < 0.4
        )
        return Patch(
            revision=state.revision + 1,
            kind="approval" if human else "work",
            author="human" if human else "agent",
            summary="Generated change.",
            run_truth_scope=[] if human else SCOPE,
            repositories_read=[] if human else SCOPE,
            ops=ops,
        )


def test_node_id_fields_are_classified() -> None:
    node_models = typing.get_args(typing.get_args(ProjectNode)[0])
    found = set().union(*(_id_fields(model) for model in node_models))
    assert found == LIVE_NODE_REFS | set(HISTORY_NODE_REFS) | set(NOT_NODE_REFS)


def _random_accepted_patches(seed: int) -> tuple[Counter[str], Counter[str]]:
    rng = random.Random(seed)
    generator = _Generator(rng)
    state = apply_valid_patch(GraphState(project_truth_scope=SCOPE), seed_patch())
    accepted: Counter[str] = Counter()
    node_types: Counter[str] = Counter()
    relation_types: Counter[str] = Counter()
    # The first patch that broke each invariant, as a minimal reproducer.
    first_break: dict[str, tuple[set[str], list[dict[str, Any]], str | None]] = {}
    for _ in range(STEPS):
        try:
            patch = generator.patch(state)
        except ValidationError:
            continue
        # Both gates, without the live membership and task lookups.
        if WriteAuthority().validate_permissions(state, patch).rejected:
            continue
        if validate_patch(state, patch, ValidationContext(SCOPE)).rejected:
            continue
        before = _violations(state)
        state = apply_valid_patch(state, patch)
        accepted.update({op.op for op in patch.ops})
        for operation in patch.ops:
            if operation.op == "create_nodes":
                node_types.update(node.type for node in operation.nodes)
            elif operation.op == "create_edges":
                relation_types.update(edge.relation for edge in operation.edges)
        for name, ids in _violations(state).items():
            if name not in first_break and ids - before.get(name, set()):
                ops = [operation_dict(op) for op in patch.ops]
                first_break[name] = (ids - before.get(name, set()), ops, patch.author)
    assert first_break == {}, seed
    assert {kind: accepted[kind] for kind in OP_KINDS} == {
        kind: max(accepted[kind], MIN_ACCEPTED) for kind in OP_KINDS
    }
    return node_types, relation_types


def test_random_accepted_patches_keep_the_graph_well_formed() -> None:
    node_types: Counter[str] = Counter()
    relation_types: Counter[str] = Counter()
    for seed in range(4):
        nodes, relations = _random_accepted_patches(seed)
        node_types.update(nodes)
        relation_types.update(relations)
    assert set(node_types) == set(REQUIRED), node_types
    assert set(relation_types) == set(RELATION_SPEC), relation_types


def _side_values(mode: int, label: str) -> tuple[str | None, str | None]:
    """The four three-way cases, independent of planner classification."""
    return (
        (f"Branch {label}", None),
        (None, f"Destination {label}"),
        (f"Agreed {label}", f"Agreed {label}"),
        (f"Branch {label}", f"Destination {label}"),
    )[mode]


def _merge_oracle(base, source, destination, plan, answers):
    """Whole content-field oracle from input graphs, never from the built operations."""
    expected = {}
    conflicts = {
        (conflict.collection, conflict.entity_id, conflict.field_name): conflict
        for conflict in plan.conflicts
        if conflict.kind == "field"
    }
    for collection in ("nodes", "edges"):
        before, left, right = (getattr(state, collection) for state in (base, source, destination))
        for identity in left.keys() | right.keys():
            node = left.get(identity) or right[identity]
            for field in graph_fields(type(node), role="content"):
                key = (collection, identity, field)
                if identity not in before:
                    expected[key] = getattr(node, field)
                    continue
                b, s, d = (getattr(items[identity], field) for items in (before, left, right))
                if s == b:
                    expected[key] = d
                elif d == b or s == d:
                    expected[key] = s
                else:
                    conflict = conflicts[key]
                    answer = answers.answers[conflict.conflict_id]
                    expected[key] = (
                        answer.value
                        if answer.kind == "merged"
                        else (s if answer.kind == "branch" else d)
                    )
    return expected


def _assert_content(state, expected):
    assert set(state.nodes) == {
        identity for collection, identity, _ in expected if collection == "nodes"
    }
    assert set(state.edges) == {
        identity for collection, identity, _ in expected if collection == "edges"
    }
    for (collection, identity, field), value in expected.items():
        assert getattr(getattr(state, collection)[identity], field) == value, (
            collection,
            identity,
            field,
        )


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="merge_plan._encode_candidates omits construct_effects' required flags argument",
)
def test_generated_three_way_merge_preserves_content_through_approval(manifest, seed):
    rng = random.Random(seed)
    generator = _Generator(rng)
    nodes, edges = generator.merge_base()
    history, branch = _fork_history(manifest, nodes, edges=edges)
    base = branch.base_state()
    node_coverage: Counter[str] = Counter()
    relation_coverage: Counter[str] = Counter()
    for index, node in enumerate(nodes):
        node_coverage[node["type"]] += 1
        branch_changes, destination_changes = {}, {}
        for offset, field in enumerate(("title", next(iter(REQUIRED[node["type"]])))):
            values = _side_values((index + seed + offset) % 4, f"{node['id']} {field} {seed}")
            for changes, value in zip((branch_changes, destination_changes), values, strict=True):
                if value is not None:
                    changes[field] = value
        for owner, changes in ((branch, branch_changes), (history, destination_changes)):
            if changes:
                _append(
                    owner, {"op": "update_nodes", "nodes": [{"id": node["id"], "changes": changes}]}
                )
    branch_edges, destination_edges = [], []
    for index, edge in enumerate(edges):
        relation_coverage[edge["relation"]] += 1
        values = _side_values((index + seed) % 4, f"{edge['id']} {seed}")
        for replacements, value in zip((branch_edges, destination_edges), values, strict=True):
            if value is not None:
                replacements.append({**edge, "explanation": value})
    for owner, replacements in ((branch, branch_edges), (history, destination_edges)):
        _append(
            owner,
            {"op": "remove_edges", "edge_ids": [edge["id"] for edge in replacements]},
            {"op": "create_edges", "edges": replacements},
        )
    for owner, side in ((branch, "branch"), (history, "destination")):
        for kind in sorted(REQUIRED):
            _append(
                owner, {"op": "create_nodes", "nodes": [generator.new_node(kind, f"{side}{seed}")]}
            )
    for owner, standing in ((branch, "accepted"), (history, "contested")):
        for node in nodes:
            _append(owner, {"op": "set_standing", "node_id": node["id"], "standing": standing})
    source, destination = branch.state(), history.state()
    assert set(node_coverage) == set(REQUIRED)
    assert set(relation_coverage) == set(RELATION_SPEC)
    plan = _plan(history, branch)
    selected = _answers(plan)
    choices = {}
    for conflict in plan.conflicts:
        assert conflict.kind == "field"
        if conflict.collection == "nodes" and conflict.field_name in {"status", "standing"}:
            assert conflict.allowed_answer_kinds == ("branch", "destination")
        kind = rng.choice(conflict.allowed_answer_kinds)
        choices[conflict.conflict_id] = {"kind": kind}
        if kind == "merged":
            choices[conflict.conflict_id]["value"] = f"Combined {conflict.conflict_id} {seed}"
    selected = MergeAnswers(answers=choices, cards=selected.cards)
    expected = _merge_oracle(base, source, destination, plan, selected)
    _commit(history, branch, plan, selected)
    merged = history.state()
    pending = list(merged.proposals.values())
    assert pending
    # Protected content and relation changes are still the destination's values.
    for proposal in pending:
        assert proposal.status == "pending"
        assert not proposal_is_stale(merged, proposal)
        for operation in proposal.ops:
            if operation.op == "update_nodes":
                for update in operation.nodes:
                    for field in update.changes:
                        assert getattr(merged.nodes[update.id], field) == getattr(
                            destination.nodes[update.id], field
                        )
            if operation.op == "remove_edges":
                for identity in operation.edge_ids or []:
                    assert merged.edges[identity] == destination.edges[identity]
            if operation.op == "set_standing":
                assert (
                    merged.nodes[operation.node_id].standing
                    == destination.nodes[operation.node_id].standing
                )
    # Every approval order sampled by these seeds must preserve all remaining cards.
    rng.shuffle(pending)
    for proposal in pending:
        state = history.state()
        assert not proposal_is_stale(state, state.proposals[proposal.id])
        approval = proposal_approval_operations(state, state.proposals[proposal.id])
        for operation in approval:
            if operation.op == "set_standing":
                expected[("nodes", operation.node_id, "standing")] = operation.standing
        _append(history, *(operation_dict(operation) for operation in approval))
    _assert_content(history.state(), expected)
    _assert_content(history.materialize(write_outputs=False).state, expected)
    assert _violations(history.state()) == {}


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="merge_plan._encode_candidates omits construct_effects' required flags argument",
)
def test_generated_incompatible_decision_choices_refuse_without_partial_write(manifest, seed):
    rng = random.Random(seed)
    options = [f"Option {value}" for value in rng.sample(range(100), 3)]
    generator = _Generator(rng)
    decision = {**generator.new_node("decision", "choice"), "options": options}
    history, branch = _fork_history(manifest, [decision])
    chosen = rng.choice(options)
    _append(
        branch,
        {
            "op": "update_nodes",
            "nodes": [
                {
                    "id": decision["id"],
                    "changes": {"options": [option for option in options if option != chosen]},
                }
            ],
        },
    )
    _append(
        history,
        {
            "op": "update_nodes",
            "nodes": [
                {"id": decision["id"], "changes": {"selected_option": chosen, "status": "decided"}}
            ],
        },
    )
    before = history.state().model_dump_json(), history.head_ref(), history.slice(1)
    source_before = branch.state().model_dump_json(), branch.head_ref()
    with pytest.raises((MergeRefused, MergeAnswersRejected, MergeHiddenClash)) as rejected:
        plan = _plan(history, branch)
        prepare_merge_candidate(plan, _answers(plan), history)
    diagnostics = (
        rejected.value.promotion.diagnostics
        if isinstance(rejected.value, MergeHiddenClash)
        else rejected.value.diagnostics
    )
    assert diagnostics
    assert (history.state().model_dump_json(), history.head_ref(), history.slice(1)) == before
    assert (branch.state().model_dump_json(), branch.head_ref()) == source_before
    assert branch.validated_merge_receipts() == []
