"""Manual contract fixtures and builder/preparation behavior, independent of planning."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cache
from types import SimpleNamespace

import pytest

from rcp.agents import agent_output_schema
from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    WriteAuthority,
    capture_change_authority,
)
from rcp.core.models import AuthorizedHuman, Blocker, BranchMergeProvenance, GraphState, Patch
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef, TransitionTrace
from rcp.core.transitions import (
    TRANSITION_RULESET_TAG,
    GraphTransitionManager,
    transition_trigger_manifest,
)
from rcp.core.validation import ValidationReport
from rcp.runs.merge_contract import (
    FieldConflict,
    MergeAnswers,
    MergeAnswersRejected,
    MergeChange,
    MergeHiddenClash,
    MergePlan,
    MergePreparationContract,
    MergeRefused,
    MissingValue,
    NodeGroupConflict,
    PresentValue,
    ReviewCard,
    merge_contract_digest,
)

MISSING = object()
FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)
BRANCH_ID = "12b3d806-f989-4db0-8a04-413b3abde287"


def value(raw):
    if raw is MISSING:
        return MissingValue()
    if isinstance(raw, (MissingValue, PresentValue)):
        return raw
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    return PresentValue(value=raw)


def change(collection, entity_id, field_name, base, branch, destination):
    return MergeChange(
        change_id=f"{collection}/{entity_id}/{field_name}",
        collection=collection,
        entity_id=entity_id,
        field_name=field_name,
        base=value(base),
        branch=value(branch),
        destination=value(destination),
    )


@cache
def preparation_contract():
    return MergePreparationContract(
        ruleset_tag=TRANSITION_RULESET_TAG,
        trigger_manifest=transition_trigger_manifest().model_dump(mode="json"),
        transition_trace_schema=TransitionTrace.model_json_schema(),
        orchestrator_patch_schema=agent_output_schema(profile="orchestrator"),
    )


def plan_from_context(
    context,
    *,
    direct_changes=(),
    conflicts=(),
    review_groups=(),
    source_human_facts=(),
    prior_reviews=(),
    authority=None,
    template_episode_id=None,
    previous_merge_receipt=None,
    previous_source_graph=None,
):
    """Copy snapshots into a hand-authored plan and capture real dispatch admission."""
    metadata = context.metadata
    provenance = BranchMergeProvenance(
        merge_id=merge_contract_digest(
            {"branch": metadata.branch_id, "head": metadata.head.model_dump(mode="json")}
        ),
        branch_id=metadata.branch_id,
        episode_id=metadata.episode_id,
        branch_base_head=metadata.effective_parent_head,
        branch_head=metadata.head,
        rebased_main_head=context.main_head,
        merge_task_id=context.merge_task_id,
    )
    scope = tuple(sorted(context.run_truth_scope))
    if authority is None:
        task = AgentTaskAuthority(
            operation_id=context.merge_task_id,
            project_id=metadata.project_id,
            apply_target=context.main_head.target,
            authorized_by=context.authorized_by,
            dispatch_authority=AgentDispatchAuthority(
                profile="orchestrator",
                task_contract="orchestrate",
                scope=AgentDispatchScope(run_truth_scope=list(scope), patch_kind="work"),
            ),
            merge_source=metadata.head.target,
            merge_dispatcher="member",
        )
        authority = WriteAuthority(
            agent_authority_resolver=lambda project, operation: task,
            project_membership_check=lambda project, user: True,
        )
    template = Patch(
        kind="work",
        author="agent",
        summary="Merge source branch changes.",
        source_operation_id=context.merge_task_id,
        authorized_by=context.authorized_by,
        profile="orchestrator",
        task_id=context.merge_task_id,
        episode_id=template_episode_id,
        branch_merge=provenance,
        run_truth_scope=list(scope),
        created_at=FIXED_TIME,
        ops=[],
    )
    capture = capture_change_authority(
        context.main_graph,
        template,
        authority=authority,
        project_id=metadata.project_id,
        apply_target=context.main_head.target,
    )
    return MergePlan(
        context_id=merge_contract_digest({"task": context.merge_task_id}),
        captured_authority=capture,
        merge_task_id=context.merge_task_id,
        authorized_by=context.authorized_by,
        provenance=provenance,
        base_head=metadata.effective_parent_head,
        source_head=metadata.head,
        destination_head=context.main_head,
        base_graph=context.base_graph.model_copy(deep=True),
        source_graph=context.branch_graph.model_copy(deep=True),
        destination_graph=context.main_graph.model_copy(deep=True),
        run_truth_scope=scope,
        source_human_facts=source_human_facts,
        prior_reviews=prior_reviews,
        previous_merge_receipt=previous_merge_receipt,
        previous_source_graph=previous_source_graph,
        preparation_contract=preparation_contract(),
        direct_changes=direct_changes,
        conflicts=conflicts,
        review_groups=review_groups,
    )


def make_plan(*, base=None, source=None, destination=None, **kwargs):
    """Small valid snapshots; no old branch merge implementation or planner needed."""
    base = base or GraphState(
        revision=1,
        project_truth_scope=["repo-a"],
        nodes={"blk/one": Blocker(id="blk/one", type="blocker", title="Base", description="Base")},
    )
    source = source or base.model_copy(deep=True, update={"revision": 2})
    destination = destination or base.model_copy(deep=True)
    base_head = GraphHeadRef(revision=base.revision, transition_id="a" * 64)
    source_head = GraphHeadRef(
        target=GraphTargetRef(kind="branch", branch_id=BRANCH_ID),
        revision=source.revision,
        transition_id="b" * 64,
    )
    destination_head = GraphHeadRef(revision=destination.revision, transition_id="a" * 64)
    context = SimpleNamespace(
        metadata=SimpleNamespace(
            branch_id=BRANCH_ID,
            episode_id=None,
            project_id="project",
            effective_parent_head=base_head,
            head=source_head,
        ),
        merge_task_id="merge-task",
        authorized_by=AuthorizedHuman(
            space_id="42ff4a99-7e19-4444-aaad-1b841f30f969",
            user_id="7fc084b7-7a43-4d7f-b002-156b78ad722e",
            display_name="Member",
        ),
        main_head=destination_head,
        base_graph=base,
        branch_graph=source,
        main_graph=destination,
        run_truth_scope=destination.project_truth_scope,
    )
    return plan_from_context(context, **kwargs)


def answers(plan, overrides=None, cards=None):
    choices = {
        conflict.conflict_id: {"kind": conflict.allowed_answer_kinds[0]}
        for conflict in plan.conflicts
    }
    choices.update(overrides or {})
    prose = ReviewCard(
        situation_cold="The branch changes a reviewed belief.",
        why_human_now="The change needs human review.",
        consequences="Approval carries the proposed change.",
        decision_needed="Approve or reject the proposed change.",
    )
    supplied_cards = {group.subject_key: prose for group in plan.review_groups}
    supplied_cards.update(cards or {})
    return MergeAnswers(answers=choices, cards=supplied_cards)


def field_conflict(field_name="title", *, base="Base", branch="Branch", destination="Destination"):
    return FieldConflict(
        conflict_id=f"nodes/blk/one/{field_name}",
        collection="nodes",
        entity_id="blk/one",
        field_name=field_name,
        base=value(base),
        branch=value(branch),
        destination=value(destination),
        allowed_answer_kinds=("branch", "destination")
        if field_name in {"status", "standing"}
        else ("branch", "destination", "merged"),
    )


def title_plan(**kwargs):
    initial = make_plan()
    source = initial.source_graph.model_copy(deep=True)
    destination = initial.destination_graph.model_copy(deep=True)
    source.nodes["blk/one"].title = "Branch"
    destination.nodes["blk/one"].title = "Destination"
    return make_plan(
        source=source, destination=destination, conflicts=(field_conflict(),), **kwargs
    )


def test_builder_is_deterministic_and_leaves_immutable_plan_snapshots_untouched():
    from rcp.runs.merge_patch import build_merge_patch

    plan = title_plan()
    before = plan.model_dump_json()
    selected = answers(
        plan, {plan.conflicts[0].conflict_id: {"kind": "merged", "value": "Combined"}}
    )
    first = build_merge_patch(plan, selected)
    second = build_merge_patch(plan, selected)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.created_at == FIXED_TIME
    assert plan.model_dump_json() == before
    assert first.ops[0].nodes[0].changes["title"] == "Combined"


@pytest.mark.parametrize("side, expected", [("branch", "Branch"), ("destination", "Destination")])
def test_field_answers_choose_exact_snapshot_value(side, expected):
    from rcp.core.materialize import apply_valid_patch
    from rcp.runs.merge_patch import build_merge_patch

    plan = title_plan()
    patch = build_merge_patch(plan, answers(plan, {plan.conflicts[0].conflict_id: {"kind": side}}))
    assert apply_valid_patch(plan.destination_graph, patch).nodes["blk/one"].title == expected


@pytest.mark.parametrize("bad", [17, False, [], None])
def test_field_type_rejection_names_the_conflict_without_coercion(bad):
    from rcp.runs.merge_patch import build_merge_patch

    plan = title_plan()
    conflict_id = plan.conflicts[0].conflict_id
    with pytest.raises(MergeAnswersRejected) as rejected:
        build_merge_patch(plan, answers(plan, {conflict_id: {"kind": "merged", "value": bad}}))
    assert [(item.code, item.related_node_ids) for item in rejected.value.diagnostics] == [
        ("invalid-merge-answer", (conflict_id,))
    ]


def test_judgment_conflict_disallows_merged_answer():
    from rcp.runs.merge_patch import build_merge_patch

    conflict = field_conflict(
        "standing", base="proposed", branch="accepted", destination="rejected"
    )
    plan = make_plan(conflicts=(conflict,))
    with pytest.raises(MergeAnswersRejected) as rejected:
        build_merge_patch(
            plan, answers(plan, {conflict.conflict_id: {"kind": "merged", "value": "accepted"}})
        )
    assert [(item.code, item.related_node_ids) for item in rejected.value.diagnostics] == [
        ("invalid-merge-answer", (conflict.conflict_id,))
    ]


@pytest.mark.parametrize("mutation", ["missing", "unknown", "extra-card"])
def test_answers_bind_exact_conflict_and_review_subject_keys(mutation):
    from rcp.runs.merge_patch import build_merge_patch

    plan = title_plan()
    payload = answers(plan).model_dump(mode="json")
    if mutation == "missing":
        payload["answers"].clear()
    elif mutation == "unknown":
        payload["answers"]["unknown-conflict"] = {"kind": "branch"}
    else:
        payload["cards"]["unknown-subject"] = {
            "situation_cold": "Situation",
            "why_human_now": "Review",
            "consequences": "Effect",
            "decision_needed": "Choice",
        }
    with pytest.raises(MergeAnswersRejected) as rejected:
        build_merge_patch(plan, MergeAnswers.model_validate(payload))
    assert {item.code for item in rejected.value.diagnostics} == {"invalid-merge-answer"}
    if mutation == "extra-card":
        assert rejected.value.diagnostics[0].related_node_ids == ("unknown-subject",)


def test_node_group_merged_answer_applies_nested_fields_and_rejects_unknown_fields():
    from rcp.core.materialize import apply_valid_patch
    from rcp.runs.merge_patch import build_merge_patch

    initial = title_plan()
    field = initial.conflicts[0]
    group = NodeGroupConflict(
        conflict_id="group/blk/one",
        node_ids=("blk/one",),
        fields=(field,),
        base={"blk/one": value(initial.base_graph.nodes["blk/one"])},
        branch={"blk/one": value(initial.source_graph.nodes["blk/one"])},
        destination={"blk/one": value(initial.destination_graph.nodes["blk/one"])},
        diagnostic_codes=("coupled-fields",),
    )
    plan = make_plan(
        source=initial.source_graph, destination=initial.destination_graph, conflicts=(group,)
    )
    merged = {"blk/one": {"title": {"kind": "merged", "value": "Combined"}}}
    patch = build_merge_patch(
        plan, answers(plan, {group.conflict_id: {"kind": "merged", "value": merged}})
    )
    assert apply_valid_patch(plan.destination_graph, patch).nodes["blk/one"].title == "Combined"
    merged["blk/one"]["description"] = {"kind": "branch"}
    with pytest.raises(MergeAnswersRejected):
        build_merge_patch(
            plan, answers(plan, {group.conflict_id: {"kind": "merged", "value": merged}})
        )


class DiagnosticHistory:
    """A dry-preparation boundary that returns controlled structured diagnostics."""

    def __init__(self, plan, report):
        self.plan = plan
        self.report = report
        self.calls = []

    def head_ref(self):
        return self.plan.destination_head

    def validate_candidate(self, patch, **_kwargs):
        from rcp.core.materialize import prepare_patch_bookkeeping

        self.calls.append(patch)
        patch = patch.model_copy(update={"revision": self.plan.destination_head.revision + 1})
        if self.report.rejected:
            return patch, self.report, self.plan.destination_graph
        prepared = GraphTransitionManager().prepare_validated(
            self.plan.destination_graph,
            [prepare_patch_bookkeeping(self.plan.destination_graph, patch)],
            pre_head=self.plan.destination_head,
        )
        return prepared.patch, self.report, self.plan.destination_graph


def test_flag_only_preparation_passes_without_publishing():
    from rcp.runs.merge_patch import prepare_merge_candidate

    plan = title_plan()
    report = ValidationReport()
    report.flag("advisory", "Context only", related_node_ids=["blk/one"])
    history = DiagnosticHistory(plan, report)
    prepared = prepare_merge_candidate(plan, answers(plan), history)
    assert len(history.calls) == 1
    assert prepared.projection.graph.nodes["blk/one"].title == "Branch"
    assert prepared.patch.transition is not None
    assert [
        (item.level, item.code, item.related_node_ids) for item in prepared.patch.admission_messages
    ] == [("flag", "advisory", ["blk/one"])]
    assert plan.destination_graph.nodes["blk/one"].title == "Destination"


@pytest.mark.parametrize("open_conflict", [False, True, "promoted"])
def test_preparation_keeps_structured_rejections_and_promotes_each_group_once(open_conflict):
    from rcp.runs.merge_patch import prepare_merge_candidate

    initial = title_plan()
    if open_conflict == "promoted":
        conflict = NodeGroupConflict(
            conflict_id="group/blk/one",
            node_ids=("blk/one",),
            fields=(initial.conflicts[0],),
            base={"blk/one": value(initial.base_graph.nodes["blk/one"])},
            branch={"blk/one": value(initial.source_graph.nodes["blk/one"])},
            destination={"blk/one": value(initial.destination_graph.nodes["blk/one"])},
            diagnostic_codes=("incoherent-decision-transition",),
        )
        plan = make_plan(
            source=initial.source_graph,
            destination=initial.destination_graph,
            conflicts=(conflict,),
        )
    elif open_conflict:
        plan = initial
    else:
        plan = make_plan(
            direct_changes=(change("nodes", "blk/one", "title", "Base", "Branch", "Base"),)
        )
    report = ValidationReport()
    report.flag("advisory", "Do not promote advisories", related_node_ids=["blk/other"])
    report.reject("incoherent-decision-transition", "Fields conflict", related_node_ids=["blk/one"])
    expected = MergeAnswersRejected if open_conflict else MergeHiddenClash
    with pytest.raises(expected) as rejected:
        prepare_merge_candidate(plan, answers(plan), DiagnosticHistory(plan, report))
    diagnostics = (
        rejected.value.diagnostics if open_conflict else rejected.value.promotion.diagnostics
    )
    assert [(item.code, item.related_node_ids) for item in diagnostics] == [
        ("incoherent-decision-transition", ("blk/one",))
    ]
    if not open_conflict:
        assert rejected.value.promotion.node_ids == ("blk/one",)


def test_preparation_rejection_without_nodes_is_a_hard_refusal():
    from rcp.runs.merge_patch import prepare_merge_candidate

    plan = title_plan()
    report = ValidationReport()
    report.reject("missing-dependency", "Dependency unavailable")
    with pytest.raises(MergeRefused) as refused:
        prepare_merge_candidate(plan, answers(plan), DiagnosticHistory(plan, report))
    assert refused.value.diagnostics[0].code == "missing-dependency"


def test_preparation_rechecks_plan_identity_and_current_preparation_contract():
    from rcp.runs.merge_patch import prepare_merge_candidate

    plan = title_plan()
    bad_identity = plan.model_copy(update={"plan_id": "f" * 64})
    with pytest.raises(MergeRefused):
        prepare_merge_candidate(
            bad_identity, answers(plan), DiagnosticHistory(plan, ValidationReport())
        )
    payload = plan.model_dump(mode="python", exclude={"plan_id"})
    payload["preparation_contract"]["ruleset_tag"] = "obsolete"
    stale = MergePlan.model_validate(payload)
    with pytest.raises(MergeRefused):
        prepare_merge_candidate(stale, answers(stale), DiagnosticHistory(stale, ValidationReport()))


@pytest.mark.parametrize("answer_kind, survives", [("keep-removal", False), ("keep-node", True)])
def test_presence_conflict_preserves_selected_removal_or_destination_node(answer_kind, survives):
    from rcp.core.materialize import apply_valid_patch
    from rcp.runs.merge_contract import PresenceConflict
    from rcp.runs.merge_patch import build_merge_patch

    initial = make_plan()
    source = initial.source_graph.model_copy(deep=True)
    source.nodes.pop("blk/one")
    destination = initial.destination_graph.model_copy(deep=True)
    destination.nodes["blk/one"].description = "Destination work"
    conflict = PresenceConflict(
        conflict_id="presence/blk/one",
        collection="nodes",
        entity_id="blk/one",
        base=value(initial.base_graph.nodes["blk/one"]),
        branch=value(MISSING),
        destination=value(destination.nodes["blk/one"]),
        removed_on="branch",
    )
    plan = make_plan(source=source, destination=destination, conflicts=(conflict,))
    patch = build_merge_patch(plan, answers(plan, {conflict.conflict_id: {"kind": answer_kind}}))
    graph = apply_valid_patch(destination, patch)
    assert ("blk/one" in graph.nodes) is survives
    if survives:
        assert graph.nodes["blk/one"].description == "Destination work"


@pytest.mark.parametrize("answer_kind, survives", [("keep-removal", False), ("keep-node", True)])
def test_incident_edge_conflict_keeps_or_removes_node_and_competing_edge_together(
    answer_kind, survives
):
    from rcp.core.materialize import apply_valid_patch
    from rcp.core.models import Edge, Evidence
    from rcp.runs.merge_contract import IncidentEdgeConflict
    from rcp.runs.merge_patch import build_merge_patch

    initial = make_plan()
    base = initial.base_graph.model_copy(deep=True)
    base.nodes["ev/one"] = Evidence(
        id="ev/one",
        type="evidence",
        title="Observation",
        observation="An analytic observation",
        origin="analytic",
    )
    source = base.model_copy(deep=True, update={"revision": 2})
    source.nodes.pop("blk/one")
    destination = base.model_copy(deep=True)
    edge = Edge(id="edge/one", source="ev/one", target="blk/one", relation="addresses")
    destination.edges[edge.id] = edge
    conflict = IncidentEdgeConflict(
        conflict_id="incident/blk/one",
        node_id="blk/one",
        base=value(base.nodes["blk/one"]),
        branch=value(MISSING),
        destination=value(destination.nodes["blk/one"]),
        removed_on="branch",
        incident_edges=(change("edges", edge.id, None, MISSING, MISSING, edge),),
    )
    plan = make_plan(base=base, source=source, destination=destination, conflicts=(conflict,))
    patch = build_merge_patch(plan, answers(plan, {conflict.conflict_id: {"kind": answer_kind}}))
    graph = apply_valid_patch(destination, patch)
    assert ("blk/one" in graph.nodes) is survives
    assert (edge.id in graph.edges) is survives


def test_preparation_rejects_changed_destination_head_before_validation(monkeypatch):
    from rcp.history import RevisionConflict
    from rcp.runs.merge_patch import prepare_merge_candidate

    plan = title_plan()
    history = DiagnosticHistory(plan, ValidationReport())
    moved = plan.destination_head.model_copy(update={"transition_id": "c" * 64})
    monkeypatch.setattr(history, "head_ref", lambda: moved)
    with pytest.raises(RevisionConflict):
        prepare_merge_candidate(plan, answers(plan), history)
    assert history.calls == []


def test_merged_nested_field_uses_the_models_nested_type():
    from rcp.runs.merge_patch import build_merge_patch

    conflict = field_conflict("source_refs", base=[], branch=[], destination=[])
    plan = make_plan(conflicts=(conflict,))
    with pytest.raises(MergeAnswersRejected) as rejected:
        build_merge_patch(
            plan,
            answers(
                plan, {conflict.conflict_id: {"kind": "merged", "value": [{"repository": 42}]}}
            ),
        )
    assert [(item.code, item.related_node_ids) for item in rejected.value.diagnostics] == [
        ("invalid-merge-answer", (conflict.conflict_id,))
    ]


@pytest.mark.parametrize("operation_kind", ["supersede_nodes", "merge_nodes"])
@pytest.mark.parametrize("protected", [False, True])
def test_retirement_changes_reconstruct_semantic_operation(operation_kind, protected):
    from rcp.core.materialize import apply_valid_patch
    from rcp.core.models import CURRENT_PROPOSAL_DEPENDENCY_RULE, Proposal, ResearchQuestion
    from rcp.core.validation.proposals import proposal_dependencies_and_effects
    from rcp.runs.merge_contract import ReviewGroup
    from rcp.runs.merge_patch import build_merge_patch

    base = make_plan().base_graph.model_copy(deep=True)
    if protected:
        base.nodes = {
            identity: ResearchQuestion(
                id=identity, type="research_question", title=identity, question="What changes?"
            )
            for identity in ("rq/old", "rq/new")
        }
        predecessor, successor = "rq/old", "rq/new"
    else:
        predecessor, successor = "blk/one", "blk/two"
        base.nodes[successor] = base.nodes[predecessor].model_copy(update={"id": successor})
    if operation_kind == "supersede_nodes":
        operation = {
            "op": operation_kind,
            "nodes": [
                {"id": predecessor, "superseded_by": successor, "explanation": "Replacement"}
            ],
        }
        relation, intent = "supersedes", "supersede"
    else:
        operation = {
            "op": operation_kind,
            "merges": [
                {"duplicate": predecessor, "canonical": successor, "explanation": "Duplicate"}
            ],
        }
        relation, intent = "duplicate_of", "merge"
    source = apply_valid_patch(
        base, Patch(kind="approval", author="human", summary="Retire", revision=2, ops=[operation])
    )
    edge_id = f"{predecessor}::{relation}::{successor}"
    changes = (
        change("nodes", predecessor, "status", "open", "superseded", "open"),
        change("edges", edge_id, None, MISSING, source.edges[edge_id], MISSING),
    )
    groups = ()
    if protected:
        proposed = Proposal(
            id="proposal/fixture",
            title="Review retirement",
            card={
                "situation_cold": "Retire a belief",
                "why_human_now": "Review",
                "consequences": "Replacement",
                "decision_needed": "Approve",
            },
            dependency_rule=CURRENT_PROPOSAL_DEPENDENCY_RULE,
            ops=[{**operation, "intent": intent}],
        )
        groups = (
            ReviewGroup(
                subject_key=f"nodes/{predecessor}",
                changes=changes,
                footprint=proposal_dependencies_and_effects(base, proposed),
                effect_digest=merge_contract_digest(proposed.model_dump(mode="json")["ops"]),
            ),
        )
    plan = make_plan(
        base=base,
        source=source,
        destination=base,
        direct_changes=() if protected else changes,
        review_groups=groups,
    )
    patch = build_merge_patch(plan, answers(plan))
    if protected:
        proposal = next(op for op in patch.ops if op.op == "create_proposals").proposals[0]
        assert [op.op for op in proposal.ops] == [operation_kind]
        assert proposal.ops[0].intent == intent
        assert apply_valid_patch(base, patch).nodes[predecessor].status == "open"
    else:
        assert [op.op for op in patch.ops] == [operation_kind]
        actual = apply_valid_patch(base, patch)
        assert actual.nodes[predecessor].status == "superseded"
        assert actual.edges[edge_id].explanation == source.edges[edge_id].explanation


def test_retirement_without_successor_is_still_a_semantic_supersede():
    from rcp.runs.merge_patch import build_merge_patch

    initial = make_plan()
    source = initial.source_graph.model_copy(deep=True)
    source.nodes["blk/one"].status = "superseded"
    plan = make_plan(
        source=source,
        direct_changes=(change("nodes", "blk/one", "status", "open", "superseded", "open"),),
    )
    patch = build_merge_patch(plan, answers(plan))
    assert [op.op for op in patch.ops] == ["supersede_nodes"]
    assert patch.ops[0].nodes[0].superseded_by is None


def test_new_retired_node_is_created_before_its_semantic_retirement():
    from rcp.core.materialize import apply_valid_patch
    from rcp.runs.merge_patch import build_merge_patch

    base = make_plan().base_graph
    source = base.model_copy(deep=True, update={"revision": 2})
    retired = Blocker(
        id="blk/retired", type="blocker", title="Retired", description="Done", status="superseded"
    )
    source.nodes[retired.id] = retired
    plan = make_plan(
        base=base,
        source=source,
        direct_changes=(change("nodes", retired.id, None, MISSING, retired, MISSING),),
    )
    patch = build_merge_patch(plan, answers(plan))
    assert [operation.op for operation in patch.ops] == ["create_nodes", "supersede_nodes"]
    assert patch.ops[0].nodes[0].status == "open"
    assert apply_valid_patch(base, patch).nodes[retired.id].status == "superseded"


@pytest.mark.parametrize("refusal_kind", ["membership", "task"])
def test_preparation_keeps_live_authority_refusal_typed(refusal_kind):
    from rcp.core.authority import AgentAuthorizerDeparted, AgentTaskAuthorityRefused

    refusal = AgentAuthorizerDeparted if refusal_kind == "membership" else AgentTaskAuthorityRefused
    from rcp.runs.merge_patch import prepare_merge_candidate

    plan = title_plan()

    class DepartedHistory(DiagnosticHistory):
        def validate_candidate(self, patch):
            raise refusal("Live task authority was revoked")

    with pytest.raises(MergeRefused) as rejected:
        prepare_merge_candidate(plan, answers(plan), DepartedHistory(plan, ValidationReport()))
    assert rejected.value.diagnostics[0].code == refusal.code


@pytest.mark.parametrize("open_conflict", [False, "field", "group"])
def test_real_cross_side_decision_clash_returns_typed_promotion_or_answer_error(open_conflict):
    from rcp.core.models import Decision
    from rcp.runs.merge_patch import build_merge_patch, prepare_merge_candidate

    decision = Decision(
        id="dec/choice", type="decision", title="Design", question="Which?", options=["A", "B"]
    )
    base = GraphState(revision=1, project_truth_scope=["repo-a"], nodes={decision.id: decision})
    source = base.model_copy(deep=True, update={"revision": 2})
    source.nodes[decision.id] = decision.model_copy(update={"options": ["A"]})
    destination = base.model_copy(deep=True, update={"revision": 2})
    destination.nodes[decision.id] = decision.model_copy(
        update={"status": "decided", "selected_option": "B"}
    )
    changed = change("nodes", decision.id, "options", ["A", "B"], ["A"], ["A", "B"])
    field = FieldConflict(
        conflict_id="decision-options",
        collection="nodes",
        entity_id=decision.id,
        field_name="options",
        base=changed.base,
        branch=changed.branch,
        destination=changed.destination,
        allowed_answer_kinds=("branch", "destination", "merged"),
    )
    conflicts = ()
    if open_conflict == "field":
        conflicts = (field,)
    elif open_conflict == "group":
        conflicts = (
            NodeGroupConflict(
                conflict_id="decision-group",
                node_ids=(decision.id,),
                fields=(
                    field,
                    FieldConflict(
                        conflict_id="decision-status",
                        collection="nodes",
                        entity_id=decision.id,
                        field_name="status",
                        base=value("open"),
                        branch=value("open"),
                        destination=value("decided"),
                        allowed_answer_kinds=("branch", "destination"),
                    ),
                    FieldConflict(
                        conflict_id="decision-selected",
                        collection="nodes",
                        entity_id=decision.id,
                        field_name="selected_option",
                        base=value(None),
                        branch=value(None),
                        destination=value("B"),
                        allowed_answer_kinds=("branch", "destination", "merged"),
                    ),
                ),
                base={decision.id: value(decision)},
                branch={decision.id: value(source.nodes[decision.id])},
                destination={decision.id: value(destination.nodes[decision.id])},
                diagnostic_codes=("incoherent-decision-transition",),
            ),
        )
    plan = make_plan(
        base=base,
        source=source,
        destination=destination,
        direct_changes=() if conflicts else (changed,),
        conflicts=conflicts,
    )
    selected = answers(plan)
    if open_conflict == "group":
        selected = answers(
            plan,
            {
                "decision-group": {
                    "kind": "merged",
                    "value": {
                        decision.id: {
                            "options": {"kind": "branch"},
                            "status": {"kind": "destination"},
                            "selected_option": {"kind": "destination"},
                        }
                    },
                }
            },
        )
    expected = MergeAnswersRejected if open_conflict else MergeHiddenClash
    for action in (
        lambda: build_merge_patch(plan, selected),
        lambda: prepare_merge_candidate(
            plan, selected, DiagnosticHistory(plan, ValidationReport())
        ),
    ):
        with pytest.raises(expected) as rejected:
            action()
        diagnostics = (
            rejected.value.diagnostics if open_conflict else rejected.value.promotion.diagnostics
        )
        assert {(item.code, item.related_node_ids) for item in diagnostics} == {
            ("incoherent-decision-transition", (decision.id,))
        }
        if not open_conflict:
            assert rejected.value.promotion.node_ids == (decision.id,)


@pytest.mark.parametrize(
    "code", ["invalid-proposal-ops", "missing-dependency", "write-authority-refused"]
)
@pytest.mark.parametrize("open_conflict", [False, True])
def test_late_hard_rejection_naming_a_node_never_becomes_a_conflict(code, open_conflict):
    from rcp.runs.merge_patch import prepare_merge_candidate

    plan = (
        title_plan()
        if open_conflict
        else make_plan(
            direct_changes=(change("nodes", "blk/one", "title", "Base", "Branch", "Base"),)
        )
    )
    report = ValidationReport()
    report.flag("incoherent-decision-transition", "Advisory only", related_node_ids=["blk/other"])
    report.reject(code, "A hard rejection names an affected node", related_node_ids=["blk/one"])
    with pytest.raises(MergeRefused) as refused:
        prepare_merge_candidate(plan, answers(plan), DiagnosticHistory(plan, report))
    assert [(item.code, item.related_node_ids) for item in refused.value.diagnostics] == [
        (code, ("blk/one",))
    ]
