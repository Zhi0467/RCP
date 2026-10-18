from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import MergeAnswersCommandRequest
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.core.models import GraphBranchMetadata, Patch
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history import HistoryManager
from rcp.runs.branch_merge import (
    BranchMergeContext,
    BranchMergeEligibility,
    BranchMergeRunOutcome,
    BranchMergeStage,
    merge_planning_context,
    stream_branch_merge_run,
)
from rcp.runs.merge_contract import MergeAnswers
from rcp.runs.merge_patch import build_merge_patch, prepare_merge_candidate
from rcp.runs.merge_plan import plan_merge
from rcp.runs.patch_validator import MergeAnswersCheck
from rcp.service import RunRequest
from tests.helpers import unchecked_write_authority
from tests.test_branch_merge import _branch_source_ref, _context, _local_scope


def _append(history, *ops: dict, human: bool = False) -> None:
    if human:
        for operation in ops:
            if operation["op"] == "update_nodes":
                for node in operation["nodes"]:
                    node["base_updated_rev"] = history.state().nodes[node["id"]].updated_rev
    history.append(
        Patch(
            kind="approval" if human else "work",
            author="human" if human else "agent",
            summary="Record a merge fixture change.",
            run_truth_scope=[] if human else ["repo-a"],
            source_operation_id=None if human else "branch-work",
            ops=list(ops),
        )
    )


def _update(node_id: str, **changes) -> dict:
    return {"op": "update_nodes", "nodes": [{"id": node_id, "changes": changes}]}


def _new_blocker() -> dict:
    return {
        "op": "create_nodes",
        "nodes": [
            {
                "id": "blk/branch-only",
                "type": "blocker",
                "title": "Branch finding",
                "description": "A sourced branch finding.",
                "source_refs": [_branch_source_ref(repository="repo-a")],
            }
        ],
    }


@pytest.fixture
def merge_history(manifest):
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    _append(
        history,
        {
            "op": "set_ontology",
            "ontology": {
                "fields": [
                    {
                        "owner_type": "blocker",
                        "name": name,
                        "definition": f"The {name} label.",
                        "kind": "text",
                        "agent_writable": name != "human_note",
                    }
                    for name in ("branch_label", "main_note", "obsolete", "human_note")
                ]
            },
        },
        human=True,
    )
    _append(
        history,
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "blk/existing",
                    "type": "blocker",
                    "title": "Base title",
                    "description": "Base description.",
                    "extension_fields": {"branch_label": "base", "obsolete": "remove me"},
                },
                {
                    "id": "ev/base",
                    "type": "evidence",
                    "title": "Base evidence",
                    "observation": "An analytic observation.",
                    "origin": "analytic",
                },
                {
                    "id": "rq/merge",
                    "type": "research_question",
                    "title": "Base question",
                    "question": "What should the project test?",
                },
                {
                    "id": "dec/choice",
                    "type": "decision",
                    "title": "Design choice",
                    "question": "Which design should run?",
                    "options": ["Original design"],
                },
            ],
        },
        {
            "op": "create_edges",
            "edges": [
                {
                    "id": "edge/base",
                    "source": "ev/base",
                    "target": "blk/existing",
                    "relation": "addresses",
                }
            ],
        },
    )
    template = _context()
    base_head = history.head_ref()
    branch_id = template.metadata.branch_id
    branch = history.create_branch(
        GraphBranchMetadata(
            branch_id=branch_id,
            episode_id=branch_id,
            project_id=template.metadata.project_id,
            base_head=base_head,
            head=GraphHeadRef(
                target=GraphTargetRef(kind="branch", branch_id=branch_id),
                revision=base_head.revision,
                transition_id=base_head.transition_id,
            ),
            authorized_by=template.authorized_by,
        )
    )

    def load_context() -> BranchMergeContext:
        metadata = branch.branch_metadata()
        return BranchMergeContext.create(
            merge_task_id=template.merge_task_id,
            authorized_by=template.authorized_by,
            metadata=metadata,
            eligibility=BranchMergeEligibility(branch_head=metadata.head),
            base_graph=branch.base_state(),
            branch_graph=branch.state(),
            main_head=history.head_ref(),
            main_graph=history.state(),
            run_truth_scope=history.state().project_truth_scope,
        )

    return history, branch, load_context


class _NoProvider:
    def stream(self, *_args, **_kwargs):
        pytest.fail("An ordinary deterministic merge must not launch a provider")


async def _run(tmp_path: Path, history, load_context: Callable, launcher=None):
    root = tmp_path / "stage"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    stage = BranchMergeStage(local_stage=root, remote_stage=None, workspace=workspace)
    outcome = BranchMergeRunOutcome()
    plan = plan_merge(merge_planning_context(load_context(), history, created_at=datetime.now(UTC)))
    checks = MergeAnswersCheck(
        plan=plan, history=history, task_id=plan.merge_task_id, turn_id="initial"
    )

    async def start_check_turn(current_plan):
        checks.set_plan(current_plan, turn_id=current_plan.plan_id)
        if launcher is not None:
            launcher.checks = checks
        return "client merge-answers check answers.json", "client", None

    frames = [
        frame
        async for frame in stream_branch_merge_run(
            RunRequest(provider="codex", run_on="laptop"),
            launcher or _NoProvider(),
            load_context=load_context,
            main_history=history,
            stage=stage,
            write_scope=_local_scope(stage, load_context()),
            checks=checks,
            start_check_turn=start_check_turn,
            graph_read_scope=GraphReadScope(project_id="project", target=GraphTargetRef()),
            history_client="python3 validator.py",
            outcome=outcome,
        )
    ]
    return outcome, frames, workspace


@pytest.mark.asyncio
async def test_deterministic_merge_rebuilds_after_main_moves_without_provider(
    merge_history, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history, branch, load_context = merge_history
    _append(branch, _update("blk/existing", description="Branch description."))
    initial = load_context()
    append = history.append
    moved = False

    def advance_main_once(patch, **kwargs):
        nonlocal moved
        if patch.branch_merge is not None and not moved:
            moved = True
            _append(history, _update("blk/existing", title="Concurrent main title"))
        return append(patch, **kwargs)

    monkeypatch.setattr(history, "append", advance_main_once)

    outcome, _frames, _workspace = await _run(tmp_path, history, load_context)

    assert outcome.status == "committed", outcome.diagnostic
    assert outcome.rebase_rounds == 1
    assert outcome.native_session_id is None
    assert history.state().revision == initial.main_head.revision + 2
    assert history.state().nodes["blk/existing"].title == "Concurrent main title"
    assert history.state().nodes["blk/existing"].description == "Branch description."
    assert outcome.receipt is not None
    assert outcome.receipt.provenance.rebased_main_head.revision == initial.main_head.revision + 1


@pytest.mark.asyncio
async def test_provider_merge_without_session_cannot_restart_after_main_moves(
    merge_history, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history, branch, load_context = merge_history
    _append(branch, _update("blk/existing", description="Branch description."))
    _append(history, _update("blk/existing", description="Main description."))
    initial = load_context()

    class AnswersWithoutSession:
        checks: MergeAnswersCheck

        def __init__(self):
            self.sessions = []

        async def stream(self, _provider, _prompt, *, cwd, session_id, **_kwargs):
            self.sessions.append(session_id)
            answers = MergeAnswers(
                answers={
                    conflict.conflict_id: {"kind": "merged", "value": "Resolution."}
                    for conflict in self.checks.plan.conflicts
                },
                cards={},
            ).model_dump_json()
            (cwd / "answers.json").write_text(answers)
            result = self.checks(
                MergeAnswersCommandRequest(
                    mailbox_id="a" * 32,
                    request_id="b" * 32,
                    credential="c" * 64,
                    verb="merge_answers",
                    arguments={"action": "check", "answers": answers},
                ),
                CommandTurnIdentity(
                    episode_id=None,
                    task_id=self.checks.task_id,
                    turn_id=self.checks.turn_id,
                    authority="broker",
                ),
            )
            assert result.status == "ok"
            yield AgentEvent(event="provider_exit", text='{"return_code":0}')
            yield AgentEvent(event="done")

    launcher = AnswersWithoutSession()

    append = history.append
    merge_append_attempts = 0

    def advance_main_at_merge(patch, **kwargs):
        nonlocal merge_append_attempts
        if patch.branch_merge is not None:
            merge_append_attempts += 1
            _append(history, _update("blk/existing", description="Concurrent main description"))
        return append(patch, **kwargs)

    monkeypatch.setattr(history, "append", advance_main_at_merge)

    outcome, _frames, _workspace = await _run(tmp_path, history, load_context, launcher)

    assert outcome.status == "rejected", outcome.diagnostic
    assert outcome.native_session_id is None
    assert outcome.rebase_rounds == 1
    assert launcher.sessions == [None]
    assert merge_append_attempts == 1
    assert outcome.committed is None
    assert outcome.receipt is None
    assert history.state().revision == initial.main_head.revision + 1
    assert history.state().nodes["blk/existing"].description == "Concurrent main description"
    assert all(patch.branch_merge is None for patch in history.load_patches())


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_check", ["refused", "hidden_clash"])
async def test_merge_collects_restored_checked_answers_after_failed_alternative(
    merge_history, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_check
) -> None:
    from rcp.runs.merge_contract import (
        MergeDiagnostic,
        MergeHiddenClash,
        MergeRefused,
        NodeGroupPromotion,
    )

    history, branch, load_context = merge_history
    _append(branch, _update("blk/existing", description="Branch description."))
    _append(history, _update("blk/existing", description="Main description."))
    diagnostics = (
        MergeDiagnostic(
            code="invalid-alternative", message="Rejected", related_node_ids=("blk/existing",)
        ),
    )
    failure = (
        MergeRefused(diagnostics)
        if failed_check == "refused"
        else MergeHiddenClash(
            NodeGroupPromotion(node_ids=("blk/existing",), diagnostics=diagnostics)
        )
    )
    prepare = prepare_merge_candidate

    def prepare_alternative(plan, answers, candidate_history):
        if any(
            answer.kind == "merged" and answer.value == "Rejected alternative."
            for answer in answers.answers.values()
        ):
            raise failure
        return prepare(plan, answers, candidate_history)

    monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", prepare_alternative)

    class RestoreCheckedAnswers:
        checks: MergeAnswersCheck
        launches = 0

        async def stream(self, _provider, _prompt, *, cwd, **_kwargs):
            self.launches += 1
            assert self.launches == 1
            checked = None
            for index, value in enumerate(("Checked resolution.", "Rejected alternative.")):
                content = MergeAnswers(
                    answers={
                        conflict.conflict_id: {"kind": "merged", "value": value}
                        for conflict in self.checks.plan.conflicts
                    },
                    cards={},
                ).model_dump_json()
                (cwd / "answers.json").write_text(content)
                result = self.checks(
                    MergeAnswersCommandRequest(
                        mailbox_id="a" * 32,
                        request_id=f"{index:032x}",
                        credential="c" * 64,
                        verb="merge_answers",
                        arguments={"action": "check", "answers": content},
                    ),
                    CommandTurnIdentity(
                        episode_id=None,
                        task_id=self.checks.task_id,
                        turn_id=self.checks.turn_id,
                        authority="broker",
                    ),
                )
                assert result.status == ("ok" if index == 0 else "invalid")
                if index == 0:
                    checked = content
                else:
                    with pytest.raises(type(failure)):
                        self.checks.require_receipt(content.encode())
            assert checked is not None
            (cwd / "answers.json").write_text(checked)
            yield AgentEvent(event="provider_exit", text='{"return_code":0}')
            yield AgentEvent(event="done")

    launcher = RestoreCheckedAnswers()
    outcome, _frames, _workspace = await _run(tmp_path, history, load_context, launcher)

    assert outcome.status == "committed", outcome.diagnostic
    assert launcher.launches == 1
    assert history.state().nodes["blk/existing"].description == "Checked resolution."
    assert outcome.receipt is not None


def _forked(manifest, *main_ops: dict, human_ops: tuple[dict, ...] = ()):
    """One main history and a branch forked after the given main operations."""

    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    _append(history, *main_ops)
    for operation in human_ops:
        _append(history, operation, human=True)
    template = _context()
    base_head = history.head_ref()
    branch = history.create_branch(
        template.metadata.model_copy(
            update={
                "base_head": base_head,
                "head": base_head.model_copy(update={"target": template.metadata.head.target}),
            }
        )
    )

    def load_context() -> BranchMergeContext:
        metadata = branch.branch_metadata()
        return BranchMergeContext.create(
            merge_task_id=template.merge_task_id,
            authorized_by=template.authorized_by,
            metadata=metadata,
            eligibility=BranchMergeEligibility(branch_head=metadata.head),
            base_graph=branch.base_state(),
            branch_graph=branch.state(),
            main_head=history.head_ref(),
            main_graph=history.state(),
            run_truth_scope=["repo-a"],
        )

    return history, branch, load_context


def _replace_edge(edge, **changes) -> list[dict]:
    raw = edge.model_dump(mode="json", exclude={"created_rev", "layer"})
    raw.update(changes)
    return [
        {"op": "remove_edges", "edge_ids": [edge.id]},
        {"op": "create_edges", "edges": [raw]},
    ]


def test_legacy_merge_request_without_binding_retains_main_dispatch():
    from rcp.runs.branch_merge_request import BranchMergeRunRequest

    request = BranchMergeRunRequest.model_validate(
        {
            "episode_id": "legacy-owner",
            "provider": "codex",
            "model": "gpt-5",
            "reasoning": "medium",
            "run_on": "laptop",
            "run_truth_scope": ["repo-a"],
            "chat_scope": "project",
            "mode": "work",
            "trigger": "human",
            "patch_kind": "work",
        }
    )
    assert request.merge_binding is None
    assert "merge_binding" not in request.model_dump(mode="json")
    assert "merge_binding" not in json.loads(request.model_dump_json())
    assert request.episode_id == "legacy-owner"
    context = _context()
    old_context = context.model_dump(mode="json", exclude={"dispatcher_episode_id"})
    assert BranchMergeContext.model_validate(old_context).context_id == context.context_id


def _changes(context, *coordinates):
    """Explicit fixture coordinates, never a planner or a three-way comparison."""
    from tests.test_merge_patch import MISSING, change

    def read(graph, collection, entity_id, field_name):
        item = getattr(graph, collection)
        if entity_id is not None:
            item = item.get(entity_id, MISSING)
        if item is not MISSING and field_name is not None:
            item = getattr(item, field_name)
        return item.model_dump(mode="json") if hasattr(item, "model_dump") else item

    return tuple(
        change(
            *coordinate,
            *(
                read(graph, *coordinate)
                for graph in (context.base_graph, context.branch_graph, context.main_graph)
            ),
        )
        for coordinate in coordinates
    )


def _field_conflict(change):
    from rcp.runs.merge_contract import FieldConflict

    return FieldConflict(
        conflict_id=change.change_id,
        collection=change.collection,
        entity_id=change.entity_id,
        field_name=change.field_name,
        base=change.base,
        branch=change.branch,
        destination=change.destination,
        allowed_answer_kinds=("branch", "destination")
        if change.collection == "nodes" and change.field_name in {"status", "standing"}
        else ("branch", "destination", "merged"),
    )


def _review_group(state, changes, *operations):
    from rcp.core.models import CURRENT_PROPOSAL_DEPENDENCY_RULE, Proposal
    from rcp.core.validation.proposals import proposal_dependencies_and_effects
    from rcp.runs.merge_contract import ReviewGroup, merge_contract_digest

    proposal = Proposal.model_validate(
        {
            "id": "prop/fixture-review",
            "title": "Review merged changes",
            "card": {
                "situation_cold": "The source changed an existing belief.",
                "why_human_now": "This change requires review.",
                "consequences": "Approval carries the changed belief.",
                "decision_needed": "Approve or reject the changed belief.",
            },
            "dependency_rule": CURRENT_PROPOSAL_DEPENDENCY_RULE,
            "ops": list(operations),
        }
    )
    return ReviewGroup(
        subject_key="nodes/" + "/".join(sorted({c.entity_id for c in changes})),
        changes=changes,
        footprint=proposal_dependencies_and_effects(state, proposal),
        effect_digest=merge_contract_digest(proposal.model_dump(mode="json")["ops"]),
    )


def test_ordinary_builder_validates_without_writing(merge_history):
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = merge_history
    _append(
        branch,
        _new_blocker(),
        _update("blk/existing", description="Branch description."),
        {"op": "remove_edges", "edge_ids": ["edge/base"]},
    )
    _append(history, _update("blk/existing", title="Main title"))
    context = load_context()
    plan = plan_from_context(
        context,
        direct_changes=_changes(
            context,
            ("nodes", "blk/branch-only", None),
            ("nodes", "blk/existing", "description"),
            ("edges", "edge/base", None),
        ),
    )
    before = history.head_ref()
    source = branch.state().model_dump_json()

    prepared = prepare_merge_candidate(plan, answers(plan), history)

    merged = prepared.projection.graph
    assert merged.nodes["blk/existing"].title == "Main title"
    assert merged.nodes["blk/existing"].description == "Branch description."
    assert (
        merged.nodes["blk/branch-only"].source_refs
        == branch.state().nodes["blk/branch-only"].source_refs
    )
    assert "edge/base" not in merged.edges
    assert history.head_ref() == before
    assert branch.state().model_dump_json() == source
    assert (
        build_merge_patch(plan, answers(plan)).model_dump_json()
        == build_merge_patch(plan, answers(plan)).model_dump_json()
    )


def test_builder_combines_direct_changes_conflicts_and_protected_review(merge_history):
    from rcp.runs.merge_contract import MergedAnswer
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = merge_history
    _append(branch, _update("rq/merge", question="A revised question?"), human=True)
    _append(branch, _update("blk/existing", description="Branch description."), _new_blocker())
    _append(history, _update("blk/existing", description="Main description."))
    context = load_context()
    protected = _changes(context, ("nodes", "rq/merge", "question"))
    group = _review_group(
        context.main_graph,
        protected,
        {**_update("rq/merge", question="A revised question?"), "intent": "content_change"},
    )
    (clash,) = _changes(context, ("nodes", "blk/existing", "description"))
    conflict = _field_conflict(clash)
    plan = plan_from_context(
        context,
        direct_changes=_changes(context, ("nodes", "blk/branch-only", None)),
        conflicts=(conflict,),
        review_groups=(group,),
    )

    prepared = prepare_merge_candidate(
        plan,
        answers(
            plan, overrides={conflict.conflict_id: MergedAnswer(value="Resolved description.")}
        ),
        history,
    )

    merged = prepared.projection.graph
    assert merged.nodes["blk/existing"].description == "Resolved description."
    assert merged.nodes["blk/existing"].title == "Base title"
    assert "blk/branch-only" in merged.nodes
    assert merged.nodes["rq/merge"].question == "What should the project test?"
    assert len(merged.proposals) == 1
    proposal = next(iter(merged.proposals.values()))
    assert proposal.ops[0].nodes[0].changes == {"question": "A revised question?"}


def test_decision_options_and_selection_remain_one_update(merge_history):
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = merge_history
    context = load_context()
    branch.append(
        Patch(
            kind="work",
            author="agent",
            profile="orchestrator",
            summary="Choose the revised design.",
            run_truth_scope=["repo-a"],
            source_operation_id="branch-work",
            task_id="branch-work",
            episode_id=context.metadata.episode_id,
            authorized_by=context.authorized_by,
            agent_action="decision_choice",
            ops=[
                _update(
                    "dec/choice",
                    options=["Revised design"],
                    selected_option="Revised design",
                    status="decided",
                )
            ],
        )
    )
    context = load_context()
    plan = plan_from_context(
        context,
        direct_changes=_changes(
            context,
            *(("nodes", "dec/choice", field) for field in ("options", "selected_option", "status")),
        ),
    )

    candidate = build_merge_patch(plan, answers(plan))
    prepared = prepare_merge_candidate(plan, answers(plan), history)

    assert candidate.agent_action == "decision_choice"
    assert prepared.projection.graph.nodes["dec/choice"].selected_option == "Revised design"
    updates = [
        n
        for op in candidate.ops
        if op.op == "update_nodes"
        for n in op.nodes
        if n.id == "dec/choice"
    ]
    assert len(updates) == 1
    assert updates[0].changes["options"] == ["Revised design"]
    assert updates[0].changes["selected_option"] == "Revised design"


def test_project_configuration_change_is_a_hard_refusal(merge_history):
    from rcp.runs.merge_contract import MergeRefused
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = merge_history
    ontology = branch.state().ontology.model_dump(mode="json")
    ontology["fields"][0]["definition"] = "A changed project definition."
    _append(branch, {"op": "set_ontology", "ontology": ontology}, human=True)
    context = load_context()
    plan = plan_from_context(context, direct_changes=_changes(context, ("ontology", None, None)))
    before = history.head_ref()

    with pytest.raises(MergeRefused) as failure:
        prepare_merge_candidate(plan, answers(plan), history)

    assert {item.code for item in failure.value.diagnostics} == {"graph-action-refused"}
    assert history.head_ref() == before


@pytest.mark.parametrize("with_review", [False, True])
def test_invalid_human_only_field_never_falls_back_to_review(merge_history, with_review):
    from rcp.runs.merge_contract import MergeRefused
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = merge_history
    fields = {**branch.state().nodes["blk/existing"].extension_fields, "human_note": "Private"}
    _append(branch, _update("blk/existing", extension_fields=fields), human=True)
    if with_review:
        _append(branch, _update("rq/merge", question="Revised?"), human=True)
    context = load_context()
    groups = ()
    if with_review:
        groups = (
            _review_group(
                context.main_graph,
                _changes(context, ("nodes", "rq/merge", "question")),
                {**_update("rq/merge", question="Revised?"), "intent": "content_change"},
            ),
        )
    plan = plan_from_context(
        context,
        direct_changes=_changes(context, ("nodes", "blk/existing", "extension_fields")),
        review_groups=groups,
    )
    before = history.head_ref()

    with pytest.raises(MergeRefused) as failure:
        prepare_merge_candidate(plan, answers(plan), history)

    assert "extension-field-human-only" in {item.code for item in failure.value.diagnostics}
    assert history.head_ref() == before
    assert history.state().proposals == {}


def test_editing_a_node_main_already_accepted_resets_standing(manifest):
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = _forked(
        manifest,
        _new_blocker(),
        human_ops=({"op": "set_standing", "node_id": "blk/branch-only", "standing": "accepted"},),
    )
    _append(branch, _update("blk/branch-only", description="A revised branch finding."))
    context = load_context()
    plan = plan_from_context(
        context,
        direct_changes=_changes(
            context,
            ("nodes", "blk/branch-only", "description"),
            ("nodes", "blk/branch-only", "standing"),
        ),
    )

    prepared = prepare_merge_candidate(plan, answers(plan), history)

    assert (
        prepared.projection.graph.nodes["blk/branch-only"].description
        == "A revised branch finding."
    )
    assert prepared.projection.graph.nodes["blk/branch-only"].standing == "asserted"


def test_human_content_edit_preserves_agreed_standing(manifest):
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = _forked(
        manifest,
        _new_blocker(),
        human_ops=({"op": "set_standing", "node_id": "blk/branch-only", "standing": "accepted"},),
    )
    _append(branch, _update("blk/branch-only", description="A revised finding."), human=True)
    context = load_context()
    plan = plan_from_context(
        context, direct_changes=_changes(context, ("nodes", "blk/branch-only", "description"))
    )

    prepared = prepare_merge_candidate(plan, answers(plan), history)

    assert prepared.projection.graph.nodes["blk/branch-only"].description == "A revised finding."
    assert prepared.projection.graph.nodes["blk/branch-only"].standing == "accepted"


@pytest.mark.parametrize("pick", ["branch", "destination"])
def test_standing_choice_survives_implicit_content_effects(manifest, pick):
    from rcp.runs.merge_contract import SideAnswer
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = _forked(manifest, _new_blocker())
    _append(branch, _update("blk/branch-only", description="A revised finding."))
    _append(
        history,
        {"op": "set_standing", "node_id": "blk/branch-only", "standing": "accepted"},
        human=True,
    )
    context = load_context()
    (standing,) = _changes(context, ("nodes", "blk/branch-only", "standing"))
    conflict = _field_conflict(standing)
    plan = plan_from_context(
        context,
        direct_changes=_changes(context, ("nodes", "blk/branch-only", "description")),
        conflicts=(conflict,),
    )

    prepared = prepare_merge_candidate(
        plan, answers(plan, overrides={conflict.conflict_id: SideAnswer(kind=pick)}), history
    )

    assert prepared.projection.graph.nodes["blk/branch-only"].description == "A revised finding."
    assert prepared.projection.graph.nodes["blk/branch-only"].standing == (
        "accepted" if pick == "destination" else "asserted"
    )


@pytest.mark.parametrize("conflicting", [False, True])
def test_edge_replacement_preserves_destination_work(merge_history, conflicting):
    from rcp.runs.merge_contract import MergedAnswer
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = merge_history
    original = branch.state().edges["edge/base"]
    _append(branch, *_replace_edge(original, explanation="Branch detail."))
    _append(
        history,
        *_replace_edge(
            original,
            relation="informs",
            target="dec/choice",
            explanation="Main detail." if conflicting else original.explanation,
        ),
    )
    context = load_context()
    (detail,) = _changes(context, ("edges", original.id, "explanation"))
    conflict = _field_conflict(detail)
    plan = plan_from_context(
        context,
        direct_changes=() if conflicting else (detail,),
        conflicts=(conflict,) if conflicting else (),
    )
    response = answers(
        plan,
        overrides={conflict.conflict_id: MergedAnswer(value="Resolved detail.")}
        if conflicting
        else {},
    )
    before = history.head_ref()

    candidate = build_merge_patch(plan, response)
    prepared = prepare_merge_candidate(plan, response, history)

    assert [op.op for op in candidate.ops] == ["remove_edges", "create_edges"]
    edge = prepared.projection.graph.edges[original.id]
    assert (edge.relation, edge.target, edge.explanation) == (
        "informs",
        "dec/choice",
        "Resolved detail." if conflicting else "Branch detail.",
    )
    assert history.head_ref() == before

    from rcp.runs.branch_merge import commit_branch_merge_with_history

    commit_branch_merge_with_history(history, candidate, expected_main_head=before)
    assert history.state().edges[original.id] == edge
    assert history.materialize(write_outputs=False).state == history.state()
    assert branch.head_ref() == plan.source_head


@pytest.mark.parametrize("new_experiment", [False, True])
@pytest.mark.parametrize("refresh_guidance", [False, True])
def test_merge_recomputes_guidance_validity(
    manifest, new_experiment: bool, refresh_guidance: bool
) -> None:
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    experiment = {
        "id": "exp/guided",
        "type": "experiment",
        "title": "Guided experiment",
        "objective": "Follow the gate result.",
        "status": "implementing",
        "current_summary": "Waiting for the gate.",
        "next_action": "Resolve the gate.",
    }
    create_experiment = {"op": "create_nodes", "nodes": [experiment]}
    create_edge = {
        "op": "create_edges",
        "edges": [
            {
                "id": "edge/gate",
                "source": experiment["id"],
                "target": "blk/branch-only",
                "relation": "blocked_by",
            }
        ],
    }
    _append(history, _new_blocker())
    if not new_experiment:
        _append(history, create_experiment, create_edge)
    template = _context()
    base_head = history.head_ref()
    branch = history.create_branch(
        template.metadata.model_copy(
            update={
                "base_head": base_head,
                "head": base_head.model_copy(update={"target": template.metadata.head.target}),
            }
        )
    )
    if new_experiment:
        _append(branch, create_experiment, create_edge)
    guidance = _update(
        experiment["id"], current_summary="The gate is resolved.", next_action="Continue the work."
    )
    _append(branch, _update("blk/branch-only", status="resolved"), guidance)
    if refresh_guidance:
        _append(branch, guidance)
    source = branch.state()
    assert source.nodes[experiment["id"]].current_summary_stale is not refresh_guidance

    def load_context() -> BranchMergeContext:
        metadata = branch.branch_metadata()
        return BranchMergeContext.create(
            merge_task_id=template.merge_task_id,
            authorized_by=template.authorized_by,
            metadata=metadata,
            eligibility=BranchMergeEligibility(branch_head=metadata.head),
            base_graph=branch.base_state(),
            branch_graph=source,
            main_head=history.head_ref(),
            main_graph=history.state(),
            run_truth_scope=["repo-a"],
        )

    from tests.test_merge_patch import answers, plan_from_context

    context = load_context()
    coordinates = [("nodes", "blk/branch-only", "status")]
    if new_experiment:
        coordinates += [("nodes", experiment["id"], None), ("edges", "edge/gate", None)]
    else:
        coordinates += [
            ("nodes", experiment["id"], field) for field in ("current_summary", "next_action")
        ]
    plan = plan_from_context(context, direct_changes=_changes(context, *coordinates))
    prepared = prepare_merge_candidate(plan, answers(plan), history)
    merged = prepared.projection.graph.nodes[experiment["id"]]
    assert merged.status == "implementing"
    assert merged.current_summary == "The gate is resolved."
    assert merged.next_action == "Continue the work."
    assert merged.current_summary_stale is True
    assert merged.next_action_stale is True
    assert prepared.projection.graph.nodes["blk/branch-only"].status == "resolved"
    assert branch.state() == source
    assert history.head_ref() == base_head


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("human", [False, True])
def test_nested_merge_uses_exact_parent_and_destination_rules(merge_history, protected, human):
    import uuid

    from rcp.runs.branch_merge import (
        branch_merge_receipt_from_committed_patch,
        commit_branch_merge_with_history,
    )
    from tests.test_merge_patch import answers, plan_from_context

    history, destination, original_context = merge_history
    _append(destination, _update("blk/existing", description="Parent before fork."))
    parent_head = destination.head_ref()
    fork_id = str(uuid.uuid4())
    metadata = GraphBranchMetadata(
        branch_id=fork_id,
        episode_id=None if human else destination.branch_metadata().episode_id,
        project_id=destination.branch_metadata().project_id,
        kind="fork",
        base_head=destination.branch_metadata().base_head,
        parent_head=parent_head,
        created_by="orchestrator:root",
        head=parent_head.model_copy(
            update={"target": GraphTargetRef(kind="branch", branch_id=fork_id)}
        ),
        authorized_by=destination.branch_metadata().authorized_by,
    )
    fork = history.create_branch(metadata)
    changed_id = "rq/merge" if protected else "blk/existing"
    changes = {"question": "Fork question?"} if protected else {"description": "Fork finding."}
    _append(fork, _update(changed_id, **changes), human=protected)
    _append(destination, _update("blk/existing", title="Destination advanced."))
    context = BranchMergeContext.create(
        merge_task_id="nested-merge",
        authorized_by=original_context().authorized_by,
        metadata=fork.branch_metadata(),
        eligibility=BranchMergeEligibility(branch_head=fork.head_ref()),
        base_graph=history.history_engine.state_at(parent_head),
        branch_graph=fork.state(),
        main_head=destination.head_ref(),
        main_graph=destination.state(),
        run_truth_scope=["repo-a"],
    )
    (field_name,) = changes
    planned = _changes(context, ("nodes", changed_id, field_name))
    groups = (
        (
            _review_group(
                context.main_graph,
                planned,
                {**_update(changed_id, **changes), "intent": "content_change"},
            ),
        )
        if protected
        else ()
    )
    plan = plan_from_context(
        context, direct_changes=() if protected else planned, review_groups=groups
    )
    prepared = prepare_merge_candidate(plan, answers(plan), destination)
    committed = commit_branch_merge_with_history(
        destination,
        build_merge_patch(plan, answers(plan)),
        expected_main_head=plan.destination_head,
    )
    assert committed.projection.graph == prepared.projection.graph
    assert committed.projection.head.target == destination.graph_target
    assert (
        branch_merge_receipt_from_committed_patch(committed.patch).result_main_head.target
        == destination.graph_target
    )
    assert committed.patch.branch_merge is not None
    assert (committed.patch.branch_merge.episode_id is None) is human
    assert committed.patch.branch_merge.branch_base_head == parent_head
    assert destination.state().nodes["blk/existing"].title == "Destination advanced."
    if protected:
        assert destination.state().nodes[changed_id].question == "What should the project test?"
        assert len(destination.state().proposals) == 1
    else:
        assert destination.state().nodes[changed_id].description == "Fork finding."
    assert history.state().nodes["blk/existing"].description == "Base description."


def test_extension_fields_conflict_uses_the_whole_answer(merge_history):
    from rcp.runs.merge_contract import MergedAnswer
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = merge_history
    _append(branch, _update("blk/existing", extension_fields={"branch_label": "branch"}))
    _append(
        history,
        _update(
            "blk/existing",
            extension_fields={
                "branch_label": "base",
                "obsolete": "remove me",
                "main_note": "Preserve main",
            },
        ),
    )
    context = load_context()
    (change,) = _changes(context, ("nodes", "blk/existing", "extension_fields"))
    conflict = _field_conflict(change)
    plan = plan_from_context(context, conflicts=(conflict,))
    merged_fields = {"branch_label": "branch", "main_note": "Preserve main"}

    prepared = prepare_merge_candidate(
        plan,
        answers(plan, overrides={conflict.conflict_id: MergedAnswer(value=merged_fields)}),
        history,
    )

    assert prepared.projection.graph.nodes["blk/existing"].extension_fields == merged_fields


def test_direct_prefix_decision_and_review_are_classified_without_duplication(merge_history):
    from tests.test_merge_patch import answers, plan_from_context

    history, branch, load_context = merge_history
    _append(branch, _new_blocker())
    context = load_context()
    branch.append(
        Patch(
            kind="work",
            author="agent",
            profile="orchestrator",
            summary="Choose the branch design.",
            run_truth_scope=["repo-a"],
            source_operation_id="branch-work",
            task_id="branch-work",
            episode_id=context.metadata.episode_id,
            authorized_by=context.authorized_by,
            agent_action="decision_choice",
            ops=[_update("dec/choice", status="decided", selected_option="Original design")],
        )
    )
    _append(branch, _update("rq/merge", question="A revised question?"), human=True)
    context = load_context()
    review_changes = _changes(context, ("nodes", "rq/merge", "question"))
    group = _review_group(
        context.main_graph,
        review_changes,
        {**_update("rq/merge", question="A revised question?"), "intent": "content_change"},
    )
    plan = plan_from_context(
        context,
        direct_changes=_changes(
            context,
            ("nodes", "blk/branch-only", None),
            ("nodes", "dec/choice", "status"),
            ("nodes", "dec/choice", "selected_option"),
        ),
        review_groups=(group,),
    )
    candidate = build_merge_patch(plan, answers(plan))
    prepared = prepare_merge_candidate(plan, answers(plan), history)
    creates = [node.id for op in candidate.ops if op.op == "create_nodes" for node in op.nodes]
    assert creates == ["blk/branch-only"]
    decision_updates = [
        node
        for op in candidate.ops
        if op.op == "update_nodes"
        for node in op.nodes
        if node.id == "dec/choice"
    ]
    assert len(decision_updates) == 1
    pending = list(prepared.projection.graph.proposals.values())
    assert len(pending) == 1
    assert [(op.op, [node.id for node in op.nodes]) for op in pending[0].ops] == [
        ("update_nodes", ["rq/merge"])
    ]
    assert prepared.projection.graph.nodes["dec/choice"].status == "decided"
    assert (
        prepared.projection.graph.nodes["blk/branch-only"].description
        == "A sourced branch finding."
    )


def test_empty_answers_prepare_and_commit_preserve_postfork_destination_standing(merge_history):
    from rcp.runs.branch_merge import commit_branch_merge_with_history
    from rcp.runs.merge_contract import MergeAnswers
    from tests.test_merge_patch import plan_from_context

    history, branch, load_context = merge_history
    _append(branch, _update("blk/existing", description="Source content."))
    _append(
        history,
        {"op": "set_standing", "node_id": "blk/existing", "standing": "accepted"},
        human=True,
    )
    context = load_context()
    plan = plan_from_context(
        context, direct_changes=_changes(context, ("nodes", "blk/existing", "description"))
    )
    selected = MergeAnswers(answers={}, cards={})
    before = history.head_ref()
    prepared = prepare_merge_candidate(plan, selected, history)
    assert history.head_ref() == before
    committed = commit_branch_merge_with_history(
        history, build_merge_patch(plan, selected), expected_main_head=before
    )
    assert prepared.projection.graph == committed.projection.graph
    assert history.state().nodes["blk/existing"].description == "Source content."
    assert history.state().nodes["blk/existing"].standing == "accepted"
    assert not history.state().proposals
    assert history.materialize(write_outputs=False).state == history.state()
