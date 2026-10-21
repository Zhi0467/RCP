"""Recorded merges reuse the accepted operation and its frozen plan and answers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime
from unittest.mock import Mock

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.write_scope import resolve_project_write_scope
from rcp.background import AgentTaskExecution, TaskAwaitingRemoteResult
from rcp.core.models import Patch
from rcp.runs import branch_merge as merge_runtime
from rcp.runs import merge_plan
from rcp.runs.branch_merge import (
    MERGE_FINALIZATION_PASS_PREFIX,
    BranchMergeRunOutcome,
    BranchMergeStage,
    MergeFinalizationPass,
    merge_planning_context,
)
from rcp.runs.branch_merge_context import load_branch_merge_context
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.runs.patch_validator import MergeAnswersCheck
from rcp.runs.remote_finalization import recorded_finalizer
from rcp.runs.shared import _ProviderOutcome, _sse
from rcp.runs.tasks import branch_merge as owner

from .helpers import append_fixture_patch
from .test_branch_merge_api import (
    _admit_held_merge_task,
    _answers_for_plan,
    _branch_patch,
    _create_branch_harness,
)
from .test_work_agent_io import _recorded_pass


def _retained_turn(manifest, tmp_path, monkeypatch, *, code_turn=False):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    if not code_turn:
        # A real competing edit requires the graph provider to answer a question.
        for branch, title in ((True, "Branch title"), (False, "Destination title")):
            patch = Patch.model_validate(
                {
                    "kind": "work",
                    "author": "agent",
                    "producer": "agent",
                    "summary": "Change the merge gate title.",
                    "run_truth_scope": ["repo-a"],
                    "source_operation_id": harness.root.operation_id,
                    "ops": [
                        {
                            "op": "update_nodes",
                            "nodes": [{"id": "blk/merge-ready", "changes": {"title": title}}],
                        }
                    ],
                }
            )
            if branch:
                harness.branch.append(patch, expected_revision=harness.branch.state().revision)
            else:
                append_fixture_patch(harness.service, patch)
    task = _admit_held_merge_task(harness, monkeypatch)
    request = BranchMergeRunRequest.model_validate(task.request)
    assert request.run_on is not None and task.authorized_by is not None
    stage = tmp_path / "retained-stage"
    workspace = stage / "workspace"
    workspace.mkdir(parents=True)
    execution = AgentTaskExecution(task.operation_id, harness.store, AgentProcessControl())
    execution.checkpoint_stage("", str(stage))
    scope = resolve_project_write_scope(
        manifest=harness.service.manifest,
        project_id=task.project_id,
        execution_machine=request.run_on,
        capability="orchestrate",
        stage_root=str(stage),
        workspace_root=str(workspace),
        admitted_aliases=[],
        repository_pointers=[],
        remote_stage=None,
        app_data_dir=tmp_path / "data",
        repository_inventory=harness.service.repository_ownership_inventory(
            project_id=task.project_id
        ),
    )
    execution.bind_write_scope(scope, resumes_native_session=False)
    context = load_branch_merge_context(
        harness.service,
        harness.store,
        harness.episode,
        operation_id=task.operation_id,
        authorized_by=task.authorized_by,
        merge_binding=request.merge_binding,
    )
    plan = merge_plan.plan_merge(
        merge_planning_context(
            context, harness.service.history, created_at=datetime.fromisoformat(harness.store.now())
        )
    )
    retained = MergeFinalizationPass(
        stage_host="",
        stage_root=str(stage),
        workspace=str(workspace),
        write_scope=scope,
        context=context,
        plan=plan,
        session_id=None,
        turn_id=None if code_turn else "recorded-merge",
    )
    for role, value in (
        (owner.BRANCH_MERGE_FINALIZATION_ROLE, request),
        (f"{MERGE_FINALIZATION_PASS_PREFIX}0", retained),
    ):
        content = value.model_dump_json()
        harness.store.record_agent_task_contract(
            task.operation_id, role, content, hashlib.sha256(content.encode()).hexdigest()
        )
    answers = json.dumps(_answers_for_plan(plan.model_dump(mode="json")))
    recorded = dataclasses.replace(
        _recorded_pass("", "Merged the branch."),
        patch=None,
        owner_deliverables={"answers.json": None if code_turn else answers},
    )
    return harness, task, request, execution, workspace, recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("code_turn", [False, True])
async def test_recorded_branch_merge_checks_snapshot_and_finishes_original_operation(
    manifest, tmp_path, monkeypatch, code_turn
):
    harness, task, request, execution, workspace, recorded = _retained_turn(
        manifest, tmp_path, monkeypatch, code_turn=code_turn
    )
    background = harness.app.state.background_tasks
    retained = owner._retained_merge_pass(execution)
    assert retained.context is not None and retained.plan is not None
    context = retained.context
    stage = BranchMergeStage(local_stage=workspace.parent, remote_stage=None, workspace=workspace)
    launcher = Mock()
    verify = Mock()
    launches = []
    provider = _ProviderOutcome()
    merge_outcome = BranchMergeRunOutcome()

    async def detached(*args, **kwargs):
        launches.append(kwargs)
        kwargs["outcome"].remote_result_pending = True
        yield _sse(
            AgentEvent(event="remote_result_pending", text="The accepted turn lost its link.")
        )

    async def start_check_turn(plan):
        return "unused-check-command", "unused-history-command", None

    monkeypatch.setattr(merge_runtime, "_stream_agent_events", detached)
    if code_turn:
        source = merge_runtime.stream_code_merge_run(
            request,
            launcher,
            stage=stage,
            write_scope=retained.write_scope,
            code_block="Merge the retained worktree.",
            verify=verify,
            outcome=provider,
        )
    else:
        source = merge_runtime.stream_branch_merge_run(
            request,
            launcher,
            load_context=lambda: context,
            main_history=harness.service.history,
            stage=stage,
            write_scope=retained.write_scope,
            checks=MergeAnswersCheck(
                plan=retained.plan,
                history=harness.service.history,
                task_id=task.operation_id,
                turn_id="recorded-merge",
            ),
            outcome=merge_outcome,
            graph_read_scope=GraphReadScope.resolve(task.project_id, harness.service.history),
            history_client="unused-history-command",
            start_check_turn=start_check_turn,
        )
    before = harness.service.history.state().revision
    task_ids = {item.operation_id for item in harness.store.agent_tasks(task.project_id)}
    harness.store.mark_agent_task_running(task.operation_id)
    with pytest.raises(TaskAwaitingRemoteResult) as waiting:
        await background._consume(task.project_id, task.kind, request, execution, source=source)
    # The background worker persists this phase after the consumer's explicit signal.
    harness.store.update_agent_task_message(
        task.operation_id, str(waiting.value), phase="awaiting_remote_result"
    )
    awaiting = harness.store.agent_task(task.operation_id)
    assert awaiting is not None and awaiting.phase == "awaiting_remote_result"
    assert awaiting.status == "running"
    assert provider.remote_result_pending if code_turn else merge_outcome.remote_result_pending
    assert harness.service.history.state().revision == before
    assert len(launches) == 1
    verify.assert_not_called()

    # Recovery must read the supervisor snapshot, not whatever is now in scratch.
    (workspace / "answers.json").write_text("invalid later output")
    assert recorded_finalizer(harness.store, task.operation_id) is (
        owner.finalize_recorded_branch_merge_result
    )
    outcome = await background._consume(
        task.project_id,
        task.kind,
        request,
        execution,
        source=owner.finalize_recorded_branch_merge_result(
            harness.service, launcher, request, tmp_path, execution, recorded
        ),
        recorded=True,
    )
    background._complete_task_outcome(task, request, execution, outcome)
    settled = harness.store.agent_task(task.operation_id)
    assert settled is not None and settled.status == "succeeded"
    assert {item.operation_id for item in harness.store.agent_tasks(task.project_id)} == task_ids
    assert len(launches) == 1
    assert harness.service.history.state().revision == before + 1
    assert harness.service.history.load_patches()[-1].task_id == task.operation_id
    checks = harness.store.agent_task_receipts_by_category(task.operation_id, "merge_answers_check")
    assert bool(checks) is not code_turn
    assert harness.store.agent_task_receipts_by_category(task.operation_id, "branch_merge_outcome")
    assert execution.applied_revision == before + 1
    assert launcher.mock_calls == []


@pytest.mark.asyncio
async def test_recorded_merge_destination_movement_requires_retry_without_provider(
    manifest, tmp_path, monkeypatch
):
    harness, task, request, execution, _workspace, recorded = _retained_turn(
        manifest, tmp_path, monkeypatch
    )
    append_fixture_patch(harness.service, _branch_patch(harness.root.operation_id, "evidence"))
    before = harness.service.history.state().revision
    launcher = Mock()
    events = [
        json.loads(frame.removeprefix("data: "))
        async for frame in owner.finalize_recorded_branch_merge_result(
            harness.service, launcher, request, tmp_path, execution, recorded
        )
    ]
    assert events[-1]["event"] == "error"
    assert harness.store.agent_task_receipts_by_category(
        task.operation_id, "branch_merge_retryable"
    )
    assert harness.service.history.state().revision == before
    assert launcher.mock_calls == []
