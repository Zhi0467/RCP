"""Detached orchestrator and ordinary worker passes keep their original authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from rcp.agents import AgentEvent, AgentLauncher
from rcp.background import BackgroundAgentTasks, TaskAwaitingRemoteResult
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.tasks import auto_research_stream as owner

from .test_auto_research_stream import (
    _dispatcher,
    _events,
    _execution,
    _service,
    _setup_branch_auto_research,
    _WorkerLauncher,
)
from .test_work_agent_io import _recorded_failed_correction, _recorded_pass

_ANSWER = "The accepted operation finished on its original branch."
_PATCH = json.dumps(
    {
        "summary": "Recorded the completed remote observation.",
        "repositories_read": ["repo-a"],
        "ops": [
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "ev/remote",
                        "type": "evidence",
                        "title": "Remote result",
                        "observation": "The accepted pass finished after the connection closed.",
                        "origin": "internal_run",
                    }
                ],
            }
        ],
    }
)


async def _waiting_turn(manifest, tmp_path, monkeypatch, role):
    main = _service(manifest, tmp_path)
    service, store, _episode, root, worker = _setup_branch_auto_research(
        main, tmp_path / "store", root_status="running"
    )
    task = root if role == "orchestrator" else worker
    request = AutoResearchRunRequest.model_validate(task.request)
    execution = _execution(store, task)
    launcher = _WorkerLauncher()

    async def disconnected(_launcher, _request, _prompt, **kwargs):
        # Use local storage for the exact same launch snapshot. The provider has
        # accepted the prompt, so its pending outcome must not become a failed turn.
        canonical = (
            owner._canonical_orchestrator_turn(execution, request)
            if role == "orchestrator"
            else owner._canonical_worker_turn(execution, request)
        )
        assert execution.stage_root is not None
        stage = owner._WorkerStage(Path(execution.stage_root), None, kwargs["workspace"], "", None)
        owner._record_auto_research_finalization_context(
            execution, canonical, stage, kwargs["write_scope"], None
        )
        execution.bind_write_scope(kwargs["write_scope"], resumes_native_session=False)
        kwargs["outcome"].remote_result_pending = True
        yield owner._sse(
            AgentEvent(event="remote_result_pending", text="Link lost after acceptance.")
        )

    monkeypatch.setattr(owner, "_stream_agent_events", disconnected)
    stream = (
        owner.stream_auto_research_orchestrator_run
        if role == "orchestrator"
        else owner.stream_auto_research_worker_run
    )

    def unexpected_stream(*_args):
        raise AssertionError("The owner source must be consumed without starting another turn.")

    background = BackgroundAgentTasks(store, unexpected_stream)
    with pytest.raises(TaskAwaitingRemoteResult):
        await background._consume(
            task.project_id,
            task.kind,
            request,
            execution,
            source=stream(
                service,
                cast(AgentLauncher, launcher),
                request,
                tmp_path / "data",
                execution,
                command_dispatcher=_dispatcher(store),
            ),
        )
    return service, launcher, request, execution


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["orchestrator", "worker"])
async def test_recorded_auto_research_pass_settles_original_operation(
    manifest, tmp_path, monkeypatch, role
):
    service, launcher, request, execution = await _waiting_turn(
        manifest, tmp_path, monkeypatch, role
    )
    revision = service.history.state().revision
    operation_id = execution.operation_id
    recorded = _recorded_pass(_PATCH, _ANSWER)
    for _attempt in range(2):
        events = await _events(
            owner.finalize_recorded_auto_research_result(
                service,
                cast(AgentLauncher, launcher),
                request,
                tmp_path / "unused",
                execution,
                recorded,
            )
        )
        assert [event.text for event in events if event.event == "answer"] == [_ANSWER]
        assert events[-1].event == "done"
        assert not [event for event in events if event.event == "error"]
    assert service.history.state().revision == revision + 1
    patch = service.history.load_patches()[-1]
    assert patch.source_operation_id == operation_id
    task = execution.store.agent_task(operation_id)
    assert task is not None
    assert service.history.graph_target == task.graph_target
    assert launcher.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["orchestrator", "worker"])
async def test_recorded_auto_research_correction_keeps_primary_answer(
    manifest, tmp_path, monkeypatch, role
):
    service, launcher, request, execution = await _waiting_turn(
        manifest, tmp_path, monkeypatch, role
    )
    execution.store.record_agent_task_contract(
        execution.operation_id,
        owner._AUTO_RESEARCH_PRIMARY_ANSWER_ROLE,
        _ANSWER,
        hashlib.sha256(_ANSWER.encode()).hexdigest(),
    )
    owner.checkpoint_required_session(execution, "recorded-thread")
    events = await _events(
        owner.finalize_recorded_auto_research_result(
            service,
            cast(AgentLauncher, launcher),
            request,
            tmp_path / "unused",
            execution,
            _recorded_pass("invalid patch", "Correction prose is not the primary answer."),
        )
    )
    assert [event.text for event in events if event.event == "answer"] == [_ANSWER]
    assert any('"status":"rejected"' in (event.text or "") for event in events)
    assert events[-1].event == "done"
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_recorded_auto_research_correction_refuses_another_session(
    manifest, tmp_path, monkeypatch
):
    service, launcher, request, execution = await _waiting_turn(
        manifest, tmp_path, monkeypatch, "orchestrator"
    )
    execution.store.record_agent_task_contract(
        execution.operation_id,
        owner._AUTO_RESEARCH_PRIMARY_ANSWER_ROLE,
        _ANSWER,
        hashlib.sha256(_ANSWER.encode()).hexdigest(),
    )
    owner.checkpoint_required_session(execution, "required-session")
    events = await _events(
        owner.finalize_recorded_auto_research_result(
            service,
            cast(AgentLauncher, launcher),
            request,
            tmp_path / "unused",
            execution,
            _recorded_pass(_PATCH, _ANSWER),
        )
    )
    assert [event.event for event in events] == ["error"]
    assert execution.stage_root is not None
    assert not (Path(execution.stage_root) / "patch.json").exists()
    assert launcher.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["orchestrator", "worker"])
async def test_auto_research_correction_disconnect_stays_pending(
    manifest, tmp_path, monkeypatch, role
):
    main = _service(manifest, tmp_path)
    service, store, _episode, root, worker = _setup_branch_auto_research(
        main, tmp_path / "store", root_status="running"
    )
    task = root if role == "orchestrator" else worker
    execution = _execution(store, task)
    request = AutoResearchRunRequest.model_validate(task.request)
    calls = 0

    async def disconnected_correction(_launcher, _request, _prompt, **kwargs):
        nonlocal calls
        calls += 1
        outcome = kwargs["outcome"]
        if calls == 1:
            (kwargs["workspace"] / "patch.json").write_text("invalid patch", encoding="utf-8")
            outcome.session_id = "same-session"
            outcome.completed = True
            outcome.answers = [_ANSWER]
        else:
            assert kwargs["required_session_id"] == "same-session"
            outcome.remote_result_pending = True
            yield owner._sse(AgentEvent(event="remote_result_pending"))

    monkeypatch.setattr(owner, "_stream_agent_events", disconnected_correction)
    stream = (
        owner.stream_auto_research_orchestrator_run
        if role == "orchestrator"
        else owner.stream_auto_research_worker_run
    )
    events = await _events(
        stream(
            service,
            cast(AgentLauncher, _WorkerLauncher()),
            request,
            tmp_path / "data",
            execution,
            command_dispatcher=_dispatcher(store),
        )
    )
    assert calls == 2
    assert [event.event for event in events] == ["answer", "remote_result_pending"]


@pytest.mark.asyncio
async def test_detached_auto_research_mailbox_restores_same_credential(
    manifest, tmp_path, monkeypatch
):
    import asyncio

    from rcp.agents.graph_read_scope import GraphReadScope
    from rcp.runs.tasks import work_turn_runtime
    from rcp.transport import RemoteRunStage

    from .test_auto_research_stream import _LocalBackedRemoteStage

    service, _launcher, request, execution = await _waiting_turn(
        manifest, tmp_path, monkeypatch, "orchestrator"
    )
    _LocalBackedRemoteStage.base = tmp_path / "remote"
    remote = _LocalBackedRemoteStage("test-host").open("retained")
    execution.checkpoint_stage(remote.host, str(remote.root))
    scope = GraphReadScope.resolve("project", service.history)
    staged = owner.stage_command_mailbox(
        local_stage=None,
        remote_stage=cast(RemoteRunStage, remote),
        episode_id=request.episode_id,
        task_id=execution.operation_id,
        turn_id=f"{execution.operation_id}:orchestrator",
    )
    outcome = owner._ProviderOutcome(remote_result_pending=True)

    async def serve_until_suspended(*_args, suspend, **_kwargs):
        await asyncio.to_thread(suspend.wait)

    async with owner._worker_mailbox_lifecycle(
        staged,
        execution=execution,
        outcome=outcome,
        graph_read_scope=scope,
        start=serve_until_suspended,
    ):
        pass
    lifecycle = execution.command_mailbox
    assert isinstance(lifecycle, owner.WorkValidatorMailboxLifecycle)
    assert lifecycle.detached
    lifecycle.suspend(timeout=2)
    monkeypatch.setattr(work_turn_runtime, "RemoteRunStage", _LocalBackedRemoteStage)
    monkeypatch.setattr(owner, "_serve_auto_research_commands", serve_until_suspended)
    restored = owner.resume_auto_research_command_mailbox(
        lambda: service, execution, command_dispatcher=_dispatcher(execution.store)
    )
    assert restored is not None
    try:
        assert restored.staged.credential.identity == staged.credential.identity
        assert restored.staged.credential.mailbox_id == staged.credential.mailbox_id
        assert restored.staged.credential.token == staged.credential.token
        assert restored.detached
    finally:
        restored.suspend(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["orchestrator", "worker"])
async def test_failed_recorded_auto_research_correction_preserves_primary_answer(
    manifest, tmp_path, monkeypatch, role
):
    service, launcher, request, execution = await _waiting_turn(
        manifest, tmp_path, monkeypatch, role
    )
    execution.store.record_agent_task_contract(
        execution.operation_id,
        owner._AUTO_RESEARCH_PRIMARY_ANSWER_ROLE,
        _ANSWER,
        hashlib.sha256(_ANSWER.encode()).hexdigest(),
    )
    owner.checkpoint_required_session(execution, "recorded-thread")
    events = await _events(
        owner.finalize_recorded_auto_research_result(
            service,
            cast(AgentLauncher, launcher),
            request,
            tmp_path / "unused",
            execution,
            _recorded_failed_correction("The correction failed after the primary answer."),
        )
    )
    assert events[0].event == "answer" and events[0].text == _ANSWER
    assert any(event.event == "error" for event in events)
    assert not any(event.event == "done" for event in events)
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_recorded_auto_research_refuses_changed_write_scope(manifest, tmp_path, monkeypatch):
    service, launcher, request, execution = await _waiting_turn(
        manifest, tmp_path, monkeypatch, "worker"
    )
    execution.write_scope_fingerprint = "0" * 64
    with pytest.raises(ValueError, match="write scope changed"):
        await _events(
            owner.finalize_recorded_auto_research_result(
                service,
                cast(AgentLauncher, launcher),
                request,
                tmp_path / "unused",
                execution,
                _recorded_pass(_PATCH, _ANSWER),
            )
        )
    assert launcher.calls == 0
