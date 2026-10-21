"""Recorded graph and report turns settle without launching another provider."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import rcp.runs.tasks.episode_report as report_run
import rcp.runs.tasks.graph as graph_run
from rcp.agents import AgentEvent, AgentLauncher, AgentProcessControl
from rcp.background import (
    AgentTaskExecution,
    BackgroundAgentTasks,
    TaskAwaitingRemoteResult,
    TaskFailed,
)
from rcp.runs.remote_finalization import recorded_finalizer
from rcp.runs.shared import _sse
from rcp.service import ProjectService, RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord

from .helpers import agent_patch_json, seed_patch
from .test_api import ScriptedLauncher
from .test_episode_report import _setup_report
from .test_work_agent_io import _one_result_app, _recorded_pass


def _retain(execution, role, content):
    execution.store.record_agent_task_contract(
        execution.operation_id, role, content, hashlib.sha256(content.encode()).hexdigest()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", [0, 1])
async def test_recorded_graph_round_applies_on_original_operation(
    tmp_path, round_number, monkeypatch
):
    service, _request, original = _one_result_app(tmp_path)
    store = original.store
    request = RunRequest(run_truth_scope=["repo-a"])
    now = store.now()
    task = store.agent_task(original.operation_id)
    assert task is not None
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="graph-recorded",
            project_id=task.project_id,
            kind="seed",
            status="running",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Awaiting remote result",
            authorized_by=task.authorized_by,
            dispatch_authority=resolve_dispatch_authority("seed", request),
        )
    )
    execution = AgentTaskExecution(
        operation_id="graph-recorded", store=store, control=AgentProcessControl()
    )
    if round_number == 0:

        async def disconnected(*args, **kwargs):
            kwargs["outcome"].remote_result_pending = True
            yield _sse(AgentEvent(event="remote_result_pending", text="Connection lost."))

        monkeypatch.setattr(graph_run, "_stream_graph_agent_events", disconnected)
        source = graph_run.stream_graph_run(
            service,
            cast(AgentLauncher, ScriptedLauncher([{}])),
            "seed",
            request,
            tmp_path / "data",
            execution,
        )
        background = BackgroundAgentTasks(store, lambda *_args: source)
        with pytest.raises(TaskAwaitingRemoteResult):
            await background._consume(task.project_id, "seed", request, execution)
        assert service.history.state().revision == 1
        assert execution.stage_root is not None
        stage = Path(execution.stage_root)
    else:
        stage = tmp_path / "retained-graph"
        stage.mkdir()
        execution.checkpoint_stage("", str(stage))
    _retain(execution, graph_run.GRAPH_FINALIZATION_ROLE, request.model_dump_json())
    snapshot = graph_run._GraphFinalizationSnapshot(
        request=request,
        kind="seed",
        stage_host="",
        stage_root=str(stage),
        workspace=str(stage),
        run_truth_scope=["repo-a"],
        pre_launch_patch_digest="inherited",
        requires_new_patch=bool(round_number),
        last_problem=None,
        session_id="recorded-thread",
        round=round_number,
    )
    _retain(
        execution,
        f"{graph_run.GRAPH_FINALIZATION_ROLE}_round_{round_number}",
        snapshot.model_dump_json(),
    )
    launcher = ScriptedLauncher([{}], message="must not launch")
    frames = [
        frame
        async for frame in graph_run.finalize_recorded_graph_result(
            cast(ProjectService, service),
            cast(AgentLauncher, launcher),
            request,
            tmp_path,
            execution,
            _recorded_pass(agent_patch_json(seed_patch()), "Graph update complete."),
        )
    ]
    assert service.history.state().revision == 2
    assert json.loads(frames[-1].removeprefix("data: "))["event"] == "done"
    assert launcher.calls == 0
    completed_task = store.agent_task(execution.operation_id)
    assert completed_task is not None and completed_task.stage_root is None
    assert (
        recorded_finalizer(store, execution.operation_id)
        is graph_run.finalize_recorded_graph_result
    )


@pytest.mark.asyncio
async def test_recorded_report_finishes_original_attempt_without_relaunch(
    manifest, tmp_path, monkeypatch
):
    service, store, request, execution, stage = _setup_report(manifest, tmp_path)
    _retain(execution, report_run.EPISODE_REPORT_FINALIZATION_ROLE, request.model_dump_json())
    html = "<html><body><figure>Recorded evidence</figure></body></html>"
    calls = []

    async def disconnected(*args, **kwargs):
        calls.append(kwargs)
        yield _sse(AgentEvent(event="runtime", text="codex.exec-json.v1"))
        yield _sse(AgentEvent(event="runtime_fallback", text=json.dumps({"reason": "test"})))
        kwargs["outcome"].remote_result_pending = True
        yield _sse(AgentEvent(event="remote_result_pending", text="Connection lost."))

    monkeypatch.setattr(report_run, "_stream_agent_events", disconnected)
    source = report_run.stream_episode_report_run(
        cast(ProjectService, service),
        cast(AgentLauncher, ScriptedLauncher([{}])),
        request,
        execution,
    )
    background = BackgroundAgentTasks(store, lambda *_args: source)
    task = store.agent_task(execution.operation_id)
    assert task is not None
    with pytest.raises(TaskAwaitingRemoteResult):
        await background._consume(task.project_id, "episode_report", request, execution)
    assert execution.runtime_id == "codex.exec-json.v1"
    assert any(
        receipt.category == "provider_runtime_fallback"
        for receipt in store.agent_task_receipts(execution.operation_id)
    )
    assert calls[0]["required_session_id"] == request.session_id
    assert len(calls) == 1
    attempt = store.current_episode_report_attempt(request.episode_id)
    assert attempt is not None and attempt.status == "running"
    # The stage changed after the completed host snapshot; recovery must restore the snapshot.
    (stage / "episode-report.html").write_text("changed after completion", encoding="utf-8")
    recorded = _recorded_pass("", "Report written.")
    recorded = replace(
        recorded,
        events=recorded.events.replace("recorded-thread", "native-session"),
        outcome={**recorded.outcome, "root_thread_id": "native-session"},
        patch=None,
        owner_deliverables={"episode-report.html": html},
    )
    launcher = ScriptedLauncher([{}], message="must not launch")
    frames = [
        frame
        async for frame in report_run.finalize_recorded_episode_report_result(
            cast(ProjectService, service),
            cast(AgentLauncher, launcher),
            request,
            tmp_path,
            execution,
            recorded,
        )
    ]
    assert json.loads(frames[-1].removeprefix("data: "))["event"] == "done"
    episode = store.episode(request.episode_id)
    assert episode is not None and episode.wrapup_state == "ready"
    assert len(store.episode_report_attempts(request.episode_id)) == 1
    assert launcher.calls == 0
    assert (
        recorded_finalizer(store, execution.operation_id)
        is report_run.finalize_recorded_episode_report_result
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failed", [False, True])
async def test_incomplete_recorded_report_records_error_without_relaunch(
    manifest, tmp_path, provider_failed
):
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    _retain(execution, report_run.EPISODE_REPORT_FINALIZATION_ROLE, request.model_dump_json())
    attempt = store.allocate_episode_report_attempt(request.episode_id)
    store.mark_episode_report_attempt_running(attempt.attempt_id)
    recorded = _recorded_pass("", "")
    events = (
        (json.dumps({"type": "turn.failed", "error": {"message": "report provider failed"}}) + "\n")
        if provider_failed
        else ""
    )
    recorded = replace(recorded, events=events, patch=None)
    launcher = ScriptedLauncher([{}], message="must not launch")
    source = report_run.finalize_recorded_episode_report_result(
        cast(ProjectService, service),
        cast(AgentLauncher, launcher),
        request,
        tmp_path,
        execution,
        recorded,
    )
    background = BackgroundAgentTasks(store, lambda *_args: source)
    task = store.agent_task(execution.operation_id)
    assert task is not None
    with pytest.raises(TaskFailed):
        await background._consume(
            task.project_id, "episode_report", request, execution, recorded=True
        )
    assert len(store.episode_report_attempts(request.episode_id)) == 1
    assert store.episode_report_attempts(request.episode_id)[0].status == "failed"
    assert launcher.calls == 0
