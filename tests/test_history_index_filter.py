from __future__ import annotations

import pytest

from rcp.core.models import Ambiguity, GatedCard, GlossaryTerm, GraphState, Proposal
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history import HistoryManager, pages
from rcp.history.delta import semantic_delta
from rcp.history.queries import parse_head
from rcp.history.state_delta import capture_state_delta
from rcp.history.store import IndexedStep
from tests.helpers import seed_patch, unchecked_write_authority
from tests.test_branch_history import _branch_patch


@pytest.mark.parametrize(
    ("entity_id", "revision"),
    [
        ("ev/target", 6),
        ("rq/learning-after-shift::has_hypothesis::hyp/replanning-restores-plasticity", 1),
    ],
)
def test_queries_skip_unrelated_indexed_deltas(manifest, monkeypatch, entity_id, revision):
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    history.append(seed_patch())
    for index in range(4):
        history.append(_branch_patch(f"ev/unrelated-{index}"))
    history.append(_branch_patch("ev/target"))
    engine = history.history_engine
    first = engine.resolve(parse_head("main@0"), default_target=GraphTargetRef())
    current = engine.resolve(parse_head("main@head"), default_target=GraphTargetRef())
    changes, supports = pages.entity_changes, pages._supports
    inspected = []

    def observe_changes(delta, ids=frozenset()):
        inspected.append(delta)
        return changes(delta, ids)

    def observe_supports(delta, changed):
        inspected.append(delta)
        return supports(delta, changed)

    monkeypatch.setattr(pages, "entity_changes", observe_changes)
    log = engine.log(entity_id, current)
    assert [entry.step.head for entry in log.entries] == [f"main@{revision}"]
    assert len(inspected) == 1
    inspected.clear()
    monkeypatch.setattr(pages, "entity_changes", changes)
    monkeypatch.setattr(pages, "_supports", observe_supports)
    diff = engine.diff(first, current, ids=frozenset({entity_id}))
    assert [change.id for change in diff.changes] == [entity_id]
    assert [step.head for step in diff.steps] == [f"main@{revision}"]
    assert len(inspected) == 1


@pytest.mark.parametrize(
    ("field", "value", "entity_id", "kind"),
    [
        (
            "proposals",
            {"p/one": Proposal(id="p/one", title="Proposal", card=GatedCard(), ops=[])},
            "p/one",
            "proposal",
        ),
        (
            "ambiguities",
            {"a/one": Ambiguity(id="a/one", question="Which?", why_it_matters="Scope")},
            "a/one",
            "ambiguity",
        ),
        (
            "glossary",
            {"term": GlossaryTerm(term="term", plain_definition="Definition")},
            "term",
            "glossary",
        ),
        ("project_truth_scope", ["repository"], "project_truth_scope", "global"),
    ],
)
def test_sidecar_ids_remain_queryable_without_node_or_edge_index(field, value, entity_id, kind):
    before = GraphState()
    after = before.model_copy(update={field: value, "revision": 1})
    first, last = GraphHeadRef(revision=0), GraphHeadRef(revision=1)
    step = IndexedStep(
        before_head=first,
        after_head=last,
        patch=seed_patch(),
        delta=semantic_delta(before, after),
        changed_node_ids=frozenset(),
        changed_edge_ids=frozenset(),
        accepted=True,
        state_delta=capture_state_delta(before, after),
    )
    log = pages.compute_log(entity_id, last, [step], "sidecar").page()
    diff = pages.compute_diff(
        first, last, before, after, [], [step], frozenset({entity_id}), "sidecar"
    ).page()
    assert [(entry.change.kind, entry.change.id) for entry in log.entries] == [(kind, entity_id)]
    assert [(change.kind, change.id) for change in diff.changes] == [(kind, entity_id)]
    assert [item.head for item in diff.steps] == ["main@1"]
