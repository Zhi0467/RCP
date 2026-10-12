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
from rcp.core.models import RELATION_SPEC, GraphState, Patch, ProjectNode
from rcp.core.operations import graph_operations_from_proposal, operation_dict
from rcp.core.validation import NODE_PREFIXES, ValidationContext, validate_patch
from rcp.core.validation.proposals import proposal_is_stale
from tests.helpers import seed_patch

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
    "evidence": {"observation": "The run reported a value."},
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

    def new_node(self) -> dict[str, Any]:
        node_type = self.rng.choice(sorted(REQUIRED))
        node_id = f"{NODE_PREFIXES[node_type]}/n{self.rng.randrange(8)}"
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


@pytest.mark.parametrize("seed", range(4))
def test_random_accepted_patches_keep_the_graph_well_formed(seed: int) -> None:
    rng = random.Random(seed)
    generator = _Generator(rng)
    state = apply_valid_patch(GraphState(project_truth_scope=SCOPE), seed_patch())
    accepted: Counter[str] = Counter()
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
        for name, ids in _violations(state).items():
            if name not in first_break and ids - before.get(name, set()):
                ops = [operation_dict(op) for op in patch.ops]
                first_break[name] = (ids - before.get(name, set()), ops, patch.author)
    assert first_break == {}
    assert {kind: accepted[kind] for kind in OP_KINDS} == {
        kind: max(accepted[kind], MIN_ACCEPTED) for kind in OP_KINDS
    }
