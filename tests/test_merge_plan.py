from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rcp.core.authority import CapturedChangeAuthority
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import (
    AuthorizedHuman,
    BranchMergeProvenance,
    BranchMergeReceipt,
    Decision,
    Edge,
    GatedCard,
    GraphState,
    Patch,
    Proposal,
    ResearchQuestion,
    Standing,
)
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.runs import merge_plan
from rcp.runs.merge_contract import (
    IncidentEdgeConflict,
    MergeDiagnostic,
    MergePlan,
    MergePlanningContext,
    MergePreparationContract,
    MergeRefused,
    MissingValue,
    NodeGroupConflict,
    NodeGroupPromotion,
    PresenceConflict,
    PresentValue,
    PriorReview,
    merge_contract_digest,
)
from rcp.runs.merge_plan import (
    ReviewCandidate,
    compare_graphs,
    group_reviews,
    plan_merge,
    promote_node_group,
    retain_pending_effects,
)

BRANCH_ID = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
MEMBER = AuthorizedHuman(
    space_id="bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb",
    user_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    display_name="Member",
)
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _state():
    node = ResearchQuestion(
        id="rq/question",
        type="research_question",
        title="Original",
        question="What changes?",
        motivation="Original motivation",
    )
    return GraphState(revision=1, project_truth_scope=["repo-a"], nodes={node.id: node})


def _edit(state, **fields):
    node_id = next(iter(state.nodes))
    return state.model_copy(
        update={"nodes": {**state.nodes, node_id: state.nodes[node_id].model_copy(update=fields)}}
    )


def _context(base, source, destination, *, previous=None, reviews=()):
    base_head = GraphHeadRef(revision=base.revision)
    source_head = GraphHeadRef(
        target=GraphTargetRef(kind="branch", branch_id=BRANCH_ID), revision=source.revision
    )
    destination_head = GraphHeadRef(revision=destination.revision)
    provenance = BranchMergeProvenance(
        merge_id="0" * 64,
        branch_id=BRANCH_ID,
        episode_id=None,
        branch_base_head=base_head,
        branch_head=source_head,
        rebased_main_head=destination_head,
        merge_task_id="merge-task",
    )
    template = Patch(
        kind="work",
        author="agent",
        producer="agent",
        profile="orchestrator",
        task_id="merge-task",
        source_operation_id="merge-task",
        summary="Merge",
        authorized_by=MEMBER,
        branch_merge=provenance,
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        created_at=NOW,
        ops=[],
    )
    receipt = None
    if previous:
        previous_provenance = provenance.model_copy(
            update={"branch_head": source_head.model_copy(update={"revision": previous.revision})}
        )
        receipt = BranchMergeReceipt(
            outcome="no_change",
            provenance=previous_provenance,
            result_main_head=destination_head,
            authorized_by=MEMBER,
            created_at=NOW,
        )
    return MergePlanningContext(
        context_id="1" * 64,
        captured_authority=CapturedChangeAuthority(
            project_id="project", apply_target=destination_head.target, patch_template=template
        ),
        merge_task_id="merge-task",
        authorized_by=MEMBER,
        provenance=provenance,
        base_head=base_head,
        source_head=source_head,
        destination_head=destination_head,
        base_graph=base,
        source_graph=source,
        destination_graph=destination,
        run_truth_scope=("repo-a",),
        prior_reviews=reviews,
        previous_source_graph=previous,
        previous_merge_receipt=receipt,
        preparation_contract=MergePreparationContract(
            ruleset_tag="test",
            trigger_manifest={},
            transition_trace_schema={},
            orchestrator_patch_schema={},
        ),
    )


@pytest.mark.parametrize(
    ("source", "destination", "expected", "conflicted"),
    [
        ("Branch", "Original", "Branch", False),
        ("Original", "Destination", None, False),
        ("Same", "Same", None, False),
        ("Branch", "Destination", "Branch", True),
    ],
)
def test_whole_field_three_way(source, destination, expected, conflicted):
    base = _state()
    changes, conflicts = compare_graphs(
        base, _edit(base, title=source), _edit(base, title=destination)
    )
    assert [c.branch.value for c in changes] == ([] if expected is None else [expected])
    assert bool(conflicts) == conflicted


def test_nested_values_are_whole_fields_and_engine_fields_do_not_conflict():
    base = _edit(_state(), extension_fields={"left": "old", "right": "old"})
    source = _edit(base, extension_fields={"left": "new", "right": "old"}, updated_rev=2)
    target = _edit(base, extension_fields={"left": "old", "right": "new"}, updated_rev=3)
    changes, conflicts = compare_graphs(base, source, target)
    assert [c.field_name for c in changes] == ["extension_fields"]
    assert conflicts[0].branch.value == {"left": "new", "right": "old"}
    assert compare_graphs(base, _edit(base, updated_rev=5), target) == ((), ())


def test_missing_is_distinct_from_null_on_concurrent_creation():
    node = Decision(id="dec/one", type="decision", title="One", question="Choose?")
    base = GraphState()
    source = GraphState(nodes={node.id: node})
    target = GraphState(nodes={node.id: node.model_copy(update={"rationale": "Try"})})
    _, conflicts = compare_graphs(base, source, target)
    field = next(c for c in conflicts if c.field_name == "rationale")
    assert field.base == MissingValue()
    assert field.branch == PresentValue(value=None)
    assert MissingValue() != PresentValue(value=None)


@pytest.mark.parametrize("removed_on", ["branch", "destination"])
def test_remove_versus_edit_is_presence_conflict(removed_on):
    base = _state()
    removed = base.model_copy(update={"nodes": {}})
    edited = _edit(base, title="Edited")
    source, target = (removed, edited) if removed_on == "branch" else (edited, removed)
    changes, conflicts = compare_graphs(base, source, target)
    assert len(changes) == len(conflicts) == 1
    assert isinstance(conflicts[0], PresenceConflict)
    assert conflicts[0].removed_on == removed_on


@pytest.mark.parametrize("removed_on", ["branch", "destination"])
def test_removal_conflicts_with_new_or_edited_incident_edges(removed_on):
    base = _state()
    other = ResearchQuestion(
        id="rq/other", type="research_question", title="Other", question="Other?"
    )
    base.nodes[other.id] = other
    removed = base.model_copy(update={"nodes": {other.id: other}})
    edge = Edge(
        id="rq/question::has_subquestion::rq/other",
        source="rq/question",
        target=other.id,
        relation="has_subquestion",
    )
    edited = base.model_copy(update={"edges": {edge.id: edge}})
    source, target = (removed, edited) if removed_on == "branch" else (edited, removed)
    changes, conflicts = compare_graphs(base, source, target)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert isinstance(conflict, IncidentEdgeConflict)
    assert conflict.removed_on == removed_on
    assert conflict.incident_edges[0].entity_id == edge.id
    assert all(c.collection == "nodes" for c in changes)
    base.edges[edge.id] = edge.model_copy(update={"explanation": "Before"})
    assert isinstance(compare_graphs(base, source, target)[1][0], IncidentEdgeConflict)


def test_status_and_standing_conflicts_only_allow_side_answers():
    base = _state()
    _, conflicts = compare_graphs(
        base,
        _edit(base, status="answered", standing=Standing.ACCEPTED),
        _edit(base, status="abandoned", standing=Standing.CONTESTED),
    )
    assert {c.field_name for c in conflicts} == {"status", "standing"}
    assert all(c.allowed_answer_kinds == ("branch", "destination") for c in conflicts)


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "supersede_nodes", "nodes": [{"id": "rq/question", "superseded_by": "rq/other"}]},
        {"op": "merge_nodes", "merges": [{"duplicate": "rq/question", "canonical": "rq/other"}]},
    ],
)
def test_retirement_descriptors_preserve_values_for_builder_semantic_encoding(operation):
    base = _state()
    base.nodes["rq/other"] = base.nodes["rq/question"].model_copy(update={"id": "rq/other"})
    source = apply_valid_patch(
        base, Patch(kind="approval", author="human", summary="Retire", ops=[operation])
    )
    changes, conflicts = compare_graphs(base, source, base)
    assert not conflicts
    assert any(
        c.collection == "nodes" and c.field_name == "status" and c.branch.value == "superseded"
        for c in changes
    )
    assert any(c.collection == "edges" and c.field_name is None for c in changes)


def _review_candidate(state, operation):
    proposal = Proposal.model_validate(
        {"id": "prop/probe", "title": "Review", "card": {}, "ops": [operation]}
    )
    patch_ops = [{k: v for k, v in operation.items() if k != "intent"}]
    after = apply_valid_patch(
        state, Patch(kind="approval", author="human", summary="Inspect", ops=patch_ops)
    )
    changes, _ = compare_graphs(state, after, state)
    return ReviewCandidate(changes, tuple(proposal.ops))


def test_connected_removals_join_but_shared_read_only_successor_does_not():
    base = _state()
    for name in ("other", "successor"):
        base.nodes[f"rq/{name}"] = base.nodes["rq/question"].model_copy(update={"id": f"rq/{name}"})
    edge = Edge(
        id="rq/question::has_subquestion::rq/other",
        source="rq/question",
        target="rq/other",
        relation="has_subquestion",
    )
    base.edges[edge.id] = edge
    removals = [
        _review_candidate(base, {"op": "remove_nodes", "intent": "removal", "node_ids": [n]})
        for n in ("rq/question", "rq/other")
    ]
    assert len(group_reviews(base, removals)) == 1
    successors = [
        _review_candidate(
            base,
            {
                "op": "supersede_nodes",
                "intent": "supersede",
                "nodes": [{"id": n, "superseded_by": "rq/successor"}],
            },
        )
        for n in ("rq/question", "rq/other")
    ]
    assert len(group_reviews(base, successors)) == 2


def test_promote_union_of_both_sides_and_refuse_second_promotion():
    base = _state()
    context = _context(base, _edit(base, title="Branch"), _edit(base, motivation="Destination"))
    changes, _ = compare_graphs(base, context.source_graph, context.destination_graph)
    plan = MergePlan(**context.model_dump(), direct_changes=changes)
    promotion = NodeGroupPromotion(
        node_ids=("rq/question",),
        diagnostics=(
            MergeDiagnostic(
                code="coupled-fields",
                message="Invalid combination",
                related_node_ids=("rq/question",),
            ),
        ),
    )
    promoted = promote_node_group(plan, promotion)
    assert not promoted.direct_changes
    group = promoted.conflicts[0]
    assert isinstance(group, NodeGroupConflict)
    assert {f.field_name for f in group.fields} == {"title", "motivation"}
    assert group.diagnostic_codes == ("coupled-fields",)
    assert promoted.plan_id != plan.plan_id
    assert promote_node_group(plan, promotion).plan_id == promoted.plan_id
    with pytest.raises(MergeRefused) as failure:
        promote_node_group(promoted, promotion)
    assert failure.value.diagnostics == promotion.diagnostics


def test_promotion_keeps_review_slots_and_binds_outstanding_effects():
    base = _state()
    base.nodes["rq/other"] = base.nodes["rq/question"].model_copy(update={"id": "rq/other"})
    candidates = [
        _review_candidate(
            base,
            {
                "op": "update_nodes",
                "intent": "content_change",
                "nodes": [{"id": node_id, "changes": {"title": "Branch"}}],
            },
        )
        for node_id in base.nodes
    ]
    groups = group_reviews(base, candidates)
    selected = next(g for g in groups if g.changes[0].entity_id == "rq/question")
    untouched = next(g for g in groups if g != selected)
    source = base.model_copy(
        update={
            "nodes": {
                n: node.model_copy(update={"title": "Branch"}) for n, node in base.nodes.items()
            }
        }
    )
    context = _context(base, source, base)
    prior = PriorReview(
        subject_key=selected.subject_key,
        effect_digest="1" * 64,
        provenance=context.provenance,
        proposal=Proposal.model_validate(
            {
                "id": "prop/earlier",
                "title": "Earlier",
                "card": {},
                "ops": [
                    {
                        "op": "update_nodes",
                        "intent": "content_change",
                        "nodes": [{"id": "rq/question", "changes": {"motivation": "Pending"}}],
                    }
                ],
            }
        ),
    )
    context = context.model_copy(update={"prior_reviews": (prior,)})
    selected = selected.model_copy(update={"replaces_proposal_ids": (prior.proposal.id,)})
    plan = MergePlan(**context.model_dump(), review_groups=(selected, untouched))
    before = plan.model_dump_json()
    promotion = NodeGroupPromotion(
        node_ids=("rq/question",),
        diagnostics=(MergeDiagnostic(code="coupled-fields", message="Promote reviewed fields"),),
    )
    promoted = promote_node_group(plan, promotion)
    changed, unchanged = promoted.review_groups
    assert unchanged == untouched
    assert changed.subject_key == selected.subject_key
    assert changed.changes == selected.changes
    assert changed.footprint == selected.footprint
    assert changed.replaces_proposal_ids == selected.replaces_proposal_ids
    assert changed.effect_digest == merge_contract_digest(
        [c.model_dump(mode="json") for c in selected.changes]
        + [op.model_dump(mode="json") for op in prior.proposal.ops]
    )
    assert changed.effect_digest != selected.effect_digest
    assert plan.model_dump_json() == before
    assert promote_node_group(plan, promotion).plan_id == promoted.plan_id


@pytest.mark.parametrize("status", ["pending", "rejected", "approved"])
def test_remerge_keeps_pending_effects_and_never_resurrects_settled_work(status):
    base = _state()
    previous = _edit(base, title="Earlier source").model_copy(update={"revision": 2})
    source = _edit(previous, motivation="New source").model_copy(update={"revision": 3})
    destination = _edit(base, title="Later destination").model_copy(update={"revision": 4})
    context = _context(base, source, destination, previous=previous)
    proposal = Proposal.model_validate(
        {
            "id": "prop/earlier",
            "title": "Earlier",
            "card": {},
            "status": status,
            "ops": [
                {
                    "op": "update_nodes",
                    "intent": "content_change",
                    "nodes": [
                        {"id": "rq/question", "changes": {"title": "Earlier resolved value"}}
                    ],
                }
            ],
        }
    )
    review = PriorReview(
        subject_key="subject",
        proposal=proposal,
        provenance=context.provenance,
        effect_digest="2" * 64,
    )
    context = context.model_copy(update={"prior_reviews": (review,)})
    changes, _ = compare_graphs(previous, source, destination)
    retained = retain_pending_effects(context, changes)
    assert {c.field_name for c in retained} == (
        {"title", "motivation"} if status == "pending" else {"motivation"}
    )
    if status == "pending":
        assert (
            next(c for c in retained if c.field_name == "title").branch.value
            == "Earlier resolved value"
        )
    unchanged = _context(base, previous, destination, previous=previous, reviews=(review,))
    assert retain_pending_effects(unchanged, ()) == ()


def test_identical_inputs_bind_stable_plan_and_do_not_mutate_snapshots(monkeypatch):
    base = _state()
    context = _context(base, _edit(base, title="Branch"), base)
    # Candidate encoding is slice 3's contract seam; this test isolates planning.
    monkeypatch.setattr(merge_plan, "_route_changes", lambda ctx, changes, conflicts: (changes, ()))
    before = context.model_dump_json()
    first, second = plan_merge(context), plan_merge(context)
    assert first.plan_id == second.plan_id
    assert context.model_dump_json() == before
    assert MergePlan.model_validate_json(first.model_dump_json()).plan_id == first.plan_id


def test_planner_routes_through_core_and_binds_review_effects(monkeypatch):
    from rcp.core.operations import NodeUpdate, UpdateNodesOperation
    from rcp.runs.merge_plan import CandidateGroup

    base = _state()
    context = _context(base, _edit(base, title="Branch"), base)

    def encode(ctx, changes, conflicts):
        return (
            CandidateGroup(
                changes,
                (
                    UpdateNodesOperation(
                        op="update_nodes",
                        nodes=[NodeUpdate(id="rq/question", changes={"title": "Branch"})],
                    ),
                ),
            ),
        )

    import sys
    from types import SimpleNamespace

    def construct(plan, selected):
        candidates = encode(plan, tuple(plan.direct_changes), plan.conflicts)
        return [
            SimpleNamespace(operations=c.operations, coordinates=set(selected)) for c in candidates
        ]

    monkeypatch.setitem(
        sys.modules, "rcp.runs.merge_patch", SimpleNamespace(_construct_effects=construct)
    )
    before = context.model_dump_json()
    plan = plan_merge(context)
    assert not plan.direct_changes
    assert len(plan.review_groups) == 1
    assert plan.review_groups[0].changes[0].field_name == "title"
    assert plan_merge(context).plan_id == plan.plan_id
    assert context.model_dump_json() == before
    promotion = NodeGroupPromotion(
        node_ids=("rq/question",),
        diagnostics=(MergeDiagnostic(code="coupled-fields", message="Promote reviewed fields"),),
    )
    promoted = promote_node_group(plan, promotion)
    assert len(promoted.review_groups) == 1
    assert promoted.review_groups[0].subject_key == plan.review_groups[0].subject_key
    assert promoted.review_groups[0].changes == plan.review_groups[0].changes
    assert promoted.review_groups[0].effect_digest != plan.review_groups[0].effect_digest
    assert promoted.conflicts[0].fields[0].field_name == "title"
    assert MergePlan.model_validate_json(promoted.model_dump_json()).plan_id == promoted.plan_id


def test_core_hard_refusal_propagates_without_creating_review():
    from rcp.core.operations import SetProjectTruthScopeOperation
    from rcp.runs.merge_plan import CandidateGroup, classify_candidates

    base = _state()
    source = base.model_copy(update={"project_truth_scope": ["repo-b"]})
    changes, _ = compare_graphs(base, source, base)
    with pytest.raises(MergeRefused) as failure:
        classify_candidates(
            _context(base, source, base),
            (
                CandidateGroup(
                    changes,
                    (
                        SetProjectTruthScopeOperation(
                            op="set_project_truth_scope", truth_scope=["repo-b"]
                        ),
                    ),
                ),
            ),
        )
    assert failure.value.diagnostics[0].code == "graph-action-refused"


def test_decision_action_is_derived_for_each_coherent_candidate():
    from rcp.core.models import Blocker
    from rcp.core.operations import CreateNodesOperation, NodeUpdate, UpdateNodesOperation
    from rcp.runs.merge_plan import CandidateGroup, classify_candidates

    decision = Decision(
        id="dec/choice", type="decision", title="Choice", question="Which?", options=["A", "B"]
    )
    base = GraphState(revision=1, project_truth_scope=["repo-a"], nodes={decision.id: decision})
    blocker = Blocker(
        id="blk/one", type="blocker", title="Blocker", description="Something is blocked"
    )
    source = base.model_copy(
        update={
            "nodes": {
                blocker.id: blocker,
                decision.id: decision.model_copy(
                    update={"status": "decided", "selected_option": "A", "rationale": "Choose A"}
                ),
            }
        }
    )
    changes, _ = compare_graphs(base, source, base)
    creation = CandidateGroup(
        tuple(c for c in changes if c.entity_id == blocker.id),
        (CreateNodesOperation(op="create_nodes", nodes=[blocker]),),
    )
    choice = CandidateGroup(
        tuple(c for c in changes if c.entity_id == decision.id),
        (
            UpdateNodesOperation(
                op="update_nodes",
                nodes=[
                    NodeUpdate(
                        id=decision.id,
                        changes={
                            "status": "decided",
                            "selected_option": "A",
                            "rationale": "Choose A",
                        },
                    )
                ],
            ),
        ),
    )
    direct, reviews = classify_candidates(_context(base, source, base), (creation, choice))
    assert not reviews
    assert {c.entity_id for c in direct} == {blocker.id, decision.id}


def test_pending_effect_cancelled_by_new_source_agreement_is_explicit(monkeypatch):
    import sys
    from types import SimpleNamespace

    base = _state()
    previous = _edit(base, title="Pending").model_copy(update={"revision": 2})
    source = base.model_copy(update={"revision": 3})
    destination = base.model_copy(update={"revision": 4})
    context = _context(base, source, destination, previous=previous)
    proposal = Proposal.model_validate(
        {
            "id": "prop/earlier",
            "title": "Earlier",
            "card": {},
            "ops": [
                {
                    "op": "update_nodes",
                    "intent": "content_change",
                    "nodes": [{"id": "rq/question", "changes": {"title": "Pending"}}],
                }
            ],
        }
    )
    review = PriorReview(
        subject_key="subject",
        proposal=proposal,
        provenance=context.provenance,
        effect_digest="2" * 64,
    )
    context = context.model_copy(update={"prior_reviews": (review,)})
    monkeypatch.setitem(
        sys.modules,
        "rcp.runs.merge_patch",
        SimpleNamespace(_construct_effects=lambda plan, selected: []),
    )
    plan = plan_merge(context)
    assert not plan.direct_changes
    assert len(plan.review_groups) == 1
    group = plan.review_groups[0]
    assert group.replaces_proposal_ids == (proposal.id,)
    assert group.changes[0].branch == group.changes[0].destination
    assert not group.footprint.effects


def test_connected_pending_predecessor_retains_its_other_subject(monkeypatch):
    import sys
    from types import SimpleNamespace

    from rcp.core.operations import RemoveNodesOperation

    base = _state()
    other = base.nodes["rq/question"].model_copy(update={"id": "rq/other"})
    base.nodes[other.id] = other
    edge = Edge(
        id="rq/question::has_subquestion::rq/other",
        source="rq/question",
        target=other.id,
        relation="has_subquestion",
    )
    base.edges[edge.id] = edge
    previous = base.model_copy(update={"revision": 2, "nodes": {other.id: other}, "edges": {}})
    source = previous.model_copy(update={"revision": 3, "nodes": {}})
    destination = base.model_copy(update={"revision": 4})
    context = _context(base, source, destination, previous=previous)
    proposal = Proposal.model_validate(
        {
            "id": "prop/earlier",
            "title": "Removal",
            "card": {},
            "ops": [{"op": "remove_nodes", "intent": "removal", "node_ids": ["rq/question"]}],
        }
    )
    review = PriorReview(
        subject_key="earlier-subject",
        proposal=proposal,
        provenance=context.provenance,
        effect_digest="2" * 64,
    )
    context = context.model_copy(update={"prior_reviews": (review,)})

    def encode(plan, selected):
        return [
            SimpleNamespace(
                operations=[RemoveNodesOperation(op="remove_nodes", node_ids=[other.id])],
                coordinates=set(selected),
            )
        ]

    monkeypatch.setitem(
        sys.modules, "rcp.runs.merge_patch", SimpleNamespace(_construct_effects=encode)
    )
    plan = plan_merge(context)
    assert len(plan.review_groups) == 1
    group = plan.review_groups[0]
    assert {c.entity_id for c in group.changes} == {"rq/question", "rq/other"}
    assert group.replaces_proposal_ids == (proposal.id,)


def test_context_rejects_prior_review_from_another_source():
    base = _state()
    context = _context(base, _edit(base, title="Changed"), base)
    wrong_branch = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    provenance = context.provenance.model_copy(
        update={
            "branch_id": wrong_branch,
            "branch_head": context.source_head.model_copy(
                update={"target": GraphTargetRef(kind="branch", branch_id=wrong_branch)}
            ),
        }
    )
    review = PriorReview(
        subject_key="subject",
        proposal=Proposal(id="prop/foreign", title="Foreign", card=GatedCard(), ops=[]),
        provenance=provenance,
        effect_digest="2" * 64,
    )
    with pytest.raises(ValueError):
        plan_merge(context.model_copy(update={"prior_reviews": (review,)}))
