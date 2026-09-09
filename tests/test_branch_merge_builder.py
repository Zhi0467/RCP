from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from rcp.core.models import GraphBranchMetadata, Patch
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history import HistoryManager
from rcp.runs.branch_merge import (
    BranchMergeContext,
    BranchMergeEligibility,
    BranchMergeRunOutcome,
    BranchMergeStage,
    build_deterministic_merge_ops,
    parse_branch_merge_candidate,
    prepare_branch_merge_with_history,
    stream_branch_merge_run,
)
from rcp.service import RunRequest

from .test_branch_merge import _branch_source_ref, _context, _local_scope, _SequenceLauncher


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
    history = HistoryManager(manifest)
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
    branch = history.create_auto_research_branch(
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
            eligibility=BranchMergeEligibility(
                branch_head=metadata.head, episode_ending="completed"
            ),
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


@pytest.mark.asyncio
@pytest.mark.parametrize("new_experiment", [False, True])
@pytest.mark.parametrize("refresh_guidance", [False, True])
async def test_merge_recomputes_guidance_validity_without_provider(
    manifest, tmp_path: Path, new_experiment: bool, refresh_guidance: bool
) -> None:
    history = HistoryManager(manifest)
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
    branch = history.create_auto_research_branch(
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
            eligibility=BranchMergeEligibility(
                branch_head=metadata.head, episode_ending="completed"
            ),
            base_graph=branch.base_state(),
            branch_graph=source,
            main_head=history.head_ref(),
            main_graph=history.state(),
            run_truth_scope=["repo-a"],
        )

    outcome, _frames, _workspace = await _run(tmp_path, history, load_context)

    assert outcome.status == "committed", outcome.diagnostic
    assert outcome.correction_rounds == 0
    assert history.state().revision == base_head.revision + 1
    merged = history.state().nodes[experiment["id"]]
    assert merged.status == "implementing"
    assert merged.current_summary == "The gate is resolved."
    assert merged.next_action == "Continue the work."
    assert merged.current_summary_stale is True
    assert merged.next_action_stale is True
    assert history.state().nodes["blk/branch-only"].status == "resolved"
    assert branch.state() == source


async def _run(tmp_path: Path, history, load_context: Callable, launcher=None):
    root = tmp_path / "stage"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    stage = BranchMergeStage(local_stage=root, remote_stage=None, workspace=workspace)
    outcome = BranchMergeRunOutcome()
    frames = [
        frame
        async for frame in stream_branch_merge_run(
            RunRequest(provider="codex", run_on="laptop"),
            launcher or _NoProvider(),
            load_context=load_context,
            main_history=history,
            stage=stage,
            write_scope=_local_scope(stage, load_context()),
            validator_command="python3 validator.py validate patch.json",
            outcome=outcome,
        )
    ]
    return outcome, frames, workspace


@pytest.mark.asyncio
async def test_ordinary_builder_validates_and_commits_without_provider(
    merge_history, tmp_path: Path
) -> None:
    history, branch, load_context = merge_history
    _append(
        branch,
        _new_blocker(),
        _update(
            "blk/existing",
            description="Branch description.",
            extension_fields={"branch_label": "branch"},
        ),
        {"op": "remove_edges", "edge_ids": ["edge/base"]},
        {
            "op": "create_edges",
            "edges": [
                {
                    "id": "edge/branch",
                    "source": "ev/base",
                    "target": "blk/branch-only",
                    "relation": "addresses",
                }
            ],
        },
    )
    _append(
        history,
        _update(
            "blk/existing",
            title="Main title",
            extension_fields={
                "branch_label": "base",
                "obsolete": "remove me",
                "main_note": "Preserve main",
            },
        ),
    )
    context = load_context()
    ops, residue = build_deterministic_merge_ops(context)
    assert not residue
    candidate = parse_branch_merge_candidate(
        json.dumps({"summary": "Carry ordinary changes.", "ops": []}),
        context,
        deterministic_ops=ops,
    )
    prepared = prepare_branch_merge_with_history(
        history, candidate, expected_main_head=context.main_head, context=context
    )
    merged = prepared.projection.graph
    assert merged.nodes["blk/existing"].title == "Main title"
    assert merged.nodes["blk/existing"].description == "Branch description."
    assert merged.nodes["blk/existing"].extension_fields == {
        "branch_label": "branch",
        "main_note": "Preserve main",
    }
    assert (
        merged.nodes["blk/branch-only"].source_refs
        == branch.state().nodes["blk/branch-only"].source_refs
    )
    assert "edge/base" not in merged.edges
    assert merged.edges["edge/branch"].target == "blk/branch-only"
    assert history.head_ref() == context.main_head

    outcome, frames, _workspace = await _run(tmp_path, history, load_context)

    assert outcome.status == "committed", outcome.diagnostic
    assert outcome.correction_rounds == outcome.rebase_rounds == 0
    assert outcome.native_session_id is None
    assert outcome.receipt is not None
    assert outcome.receipt.provenance.branch_head == context.metadata.head
    assert history.state().revision == context.main_head.revision + 1
    assert history.state().nodes == merged.nodes
    assert history.state().edges == merged.edges
    assert history.materialize(write_outputs=False).state == history.state()
    assert branch.head_ref() == context.metadata.head
    assert any('"event":"done"' in frame for frame in frames)


def test_builder_leaves_protected_changes_and_conflicts_for_judgment(merge_history) -> None:
    history, branch, load_context = merge_history
    _append(branch, _update("rq/merge", question="A revised question?"), human=True)
    _append(branch, _update("blk/existing", description="Branch description."), _new_blocker())
    _append(history, _update("blk/existing", description="Main description."))

    ops, residue = build_deterministic_merge_ops(load_context())

    assert ops == [_new_blocker()]
    assert ("nodes", "rq/merge", "question") in residue
    assert ("nodes", "blk/existing", "description") in residue


@pytest.mark.asyncio
async def test_decision_options_and_selection_remain_one_agent_update(
    merge_history, tmp_path: Path
) -> None:
    history, branch, load_context = merge_history
    choice = _update(
        "dec/choice",
        options=["Revised design"],
        selected_option="Revised design",
        status="decided",
    )
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
            ops=[choice, _new_blocker()],
        )
    )
    ops, residue = build_deterministic_merge_ops(load_context())
    assert ops == [_new_blocker()]
    assert residue == {
        ("nodes", "dec/choice", "options"),
        ("nodes", "dec/choice", "selected_option"),
        ("nodes", "dec/choice", "status"),
    }
    launcher = _SequenceLauncher(
        [
            json.dumps(
                {
                    "summary": "Carry the design choice.",
                    "agent_action": "decision_choice",
                    "ops": [choice],
                }
            )
        ]
    )

    outcome, _frames, _workspace = await _run(tmp_path, history, load_context, launcher)

    assert outcome.status == "committed", outcome.diagnostic
    assert outcome.correction_rounds == 0
    assert launcher.sessions == [None]
    assert history.state().nodes["dec/choice"].selected_option == "Revised design"
    assert history.state().nodes["blk/branch-only"].source_refs


@pytest.mark.asyncio
async def test_mixed_merge_keeps_built_ops_through_residue_correction(
    merge_history, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history, branch, load_context = merge_history
    _append(branch, _new_blocker(), _update("blk/existing", description="Branch description."))
    _append(history, _update("blk/existing", description="Main description."))
    context = load_context()
    ops, residue = build_deterministic_merge_ops(context)
    assert residue == {("nodes", "blk/existing", "description")}
    candidates: list[Patch] = []
    validate = history.validate_candidate

    def record_candidate(patch, **kwargs):
        candidates.append(patch.model_copy(deep=True))
        return validate(patch, **kwargs)

    monkeypatch.setattr(history, "validate_candidate", record_candidate)
    launcher = _SequenceLauncher(
        [
            json.dumps(
                {
                    "summary": "Resolve the conflicting description.",
                    "ops": [_update("blk/existing", description="Resolution.", **extra)],
                }
            )
            for extra in ({"title": "An unrelated rewrite"}, {})
        ]
    )

    outcome, _frames, _workspace = await _run(tmp_path, history, load_context, launcher)

    assert outcome.status == "committed", outcome.diagnostic
    assert outcome.correction_rounds == 1
    assert launcher.sessions == [None, "native-session"]
    assert len(candidates) == 2
    expected = parse_branch_merge_candidate(
        json.dumps({"summary": "Built changes.", "ops": []}),
        context,
        deterministic_ops=ops,
    ).ops
    for candidate in candidates:
        assert candidate.ops[: len(expected)] == expected
    assert history.state().nodes["blk/existing"].description == "Resolution."
    assert history.state().nodes["blk/existing"].title == "Base title"
    assert history.state().nodes["blk/branch-only"].source_refs


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
    assert outcome.correction_rounds == 0
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
    launcher = _SequenceLauncher(
        [
            json.dumps(
                {
                    "summary": "Resolve the conflicting description.",
                    "ops": [_update("blk/existing", description="Resolution.")],
                }
            )
        ]
    )
    stream = launcher.stream

    async def omit_session_event(*args, **kwargs):
        async for event in stream(*args, **kwargs):
            if event.event != "session":
                yield event

    append = history.append
    merge_append_attempts = 0

    def advance_main_at_merge(patch, **kwargs):
        nonlocal merge_append_attempts
        if patch.branch_merge is not None:
            merge_append_attempts += 1
            _append(history, _update("blk/existing", title="Concurrent main title"))
        return append(patch, **kwargs)

    monkeypatch.setattr(launcher, "stream", omit_session_event)
    monkeypatch.setattr(history, "append", advance_main_at_merge)

    outcome, _frames, _workspace = await _run(tmp_path, history, load_context, launcher)

    assert outcome.status == "retryable", outcome.diagnostic
    assert outcome.native_session_id is None
    assert outcome.rebase_rounds == outcome.correction_rounds == 0
    assert launcher.sessions == [None]
    assert merge_append_attempts == 1
    assert outcome.committed is None
    assert outcome.receipt is None
    assert history.state().revision == initial.main_head.revision + 1
    assert history.state().nodes["blk/existing"].description == "Main description."
    assert history.state().nodes["blk/existing"].title == "Concurrent main title"
    assert all(patch.branch_merge is None for patch in history.load_patches())


@pytest.mark.asyncio
async def test_project_configuration_residue_fails_without_provider(
    merge_history, tmp_path: Path
) -> None:
    history, branch, load_context = merge_history
    ontology = branch.state().ontology.model_dump(mode="json")
    ontology["fields"][0]["definition"] = "A changed project definition."
    _append(branch, {"op": "set_ontology", "ontology": ontology}, human=True)
    before = history.head_ref()

    outcome, frames, _workspace = await _run(tmp_path, history, load_context)

    assert outcome.status == "rejected"
    assert "project configuration" in outcome.diagnostic
    assert "ontology" in outcome.diagnostic
    assert outcome.correction_rounds == 0
    assert history.head_ref() == before
    assert any('"event":"error"' in frame for frame in frames)


@pytest.mark.asyncio
async def test_invalid_built_candidate_retains_patch_and_fails_without_provider(
    merge_history, tmp_path: Path
) -> None:
    history, branch, load_context = merge_history
    fields = {**branch.state().nodes["blk/existing"].extension_fields, "human_note": "Private"}
    _append(branch, _update("blk/existing", extension_fields=fields), human=True)
    context = load_context()
    ops, residue = build_deterministic_merge_ops(context)
    assert ops and not residue

    outcome, _frames, workspace = await _run(tmp_path, history, load_context)

    assert outcome.status == "rejected"
    assert "human_note" in outcome.diagnostic
    assert outcome.correction_rounds == 0
    assert outcome.native_session_id is None
    assert outcome.receipt is None
    assert history.head_ref() == context.main_head
    assert json.loads((workspace / "patch.json").read_text(encoding="utf-8"))["ops"] == ops
    assert branch.head_ref() == context.metadata.head


@pytest.mark.asyncio
async def test_invalid_fixed_ops_reject_before_launching_for_protected_residue(
    merge_history, tmp_path: Path
) -> None:
    history, branch, load_context = merge_history
    fields = {**branch.state().nodes["blk/existing"].extension_fields, "human_note": "Private"}
    _append(branch, _update("blk/existing", extension_fields=fields), human=True)
    _append(branch, _update("rq/merge", question="A revised question?"), human=True)
    context = load_context()
    ops, residue = build_deterministic_merge_ops(context)
    assert ops
    assert residue == {("nodes", "rq/merge", "question")}

    outcome, frames, _workspace = await _run(tmp_path, history, load_context)

    assert outcome.status == "rejected"
    assert "human_note" in outcome.diagnostic
    assert outcome.native_session_id is None
    assert outcome.correction_rounds == 0
    assert outcome.receipt is None
    assert history.head_ref() == context.main_head
    assert branch.head_ref() == context.metadata.head
    assert any('"event":"error"' in frame for frame in frames)
