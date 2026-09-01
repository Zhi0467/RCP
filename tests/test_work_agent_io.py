from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path

import pytest

import rcp.runs.tasks.work as work_module
from rcp.agents.command_mailbox import StagedCommandMailbox
from rcp.runs.patch_validator import stage_patch_validation_mailbox
from rcp.runs.tasks.work import _WorkValidatorMailboxLifecycle, stream_work_run
from rcp.service import RunRequest

from .helpers import (
    agent_patch_json,
    append_fixture_patch,
    create_named_app,
    seed_patch,
    shape_invalid_patch,
)
from .test_api import ScriptedLauncher, _chat_task_execution

_COMMAND_STATE_PREFIXES = ("rcp-command-", ".rcp-command-", ".rcp-mailbox-")


def _request() -> RunRequest:
    return RunRequest(
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Run the check and reflect any graph change.",
        run_truth_scope=["repo-a"],
        mode="work",
    )


def _assert_command_state_removed(staged: StagedCommandMailbox) -> None:
    assert staged.credential.expired
    assert not Path(staged.credential_path).exists()
    assert not any(
        name.startswith(_COMMAND_STATE_PREFIXES) for name in staged.mailbox.entry_names()
    )


@pytest.mark.asyncio
async def test_initial_validator_preserves_setup_failure_over_serve_and_cleanup_failures(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    request = _request()
    execution = _chat_task_execution(
        app.state.background_tasks.store,
        operation_id="work-mailbox-initial-failure",
        project_id=app.state.default_project_id,
        request=request,
    )
    staged_mailboxes: list[StagedCommandMailbox] = []
    started: list[str] = []
    finished: list[str] = []
    original_stage = work_module._stage_chat_patch_inputs
    original_cleanup = StagedCommandMailbox.cleanup

    def capture_stage(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        staged_mailboxes.append(staged.validator_staged)
        return staged

    async def fail_serve(*, staged, stop, **_kwargs):
        turn_id = staged.credential.identity.turn_id
        started.append(turn_id)
        try:
            await stop.wait()
            raise RuntimeError("secondary validator serve failure")
        finally:
            finished.append(turn_id)

    def fail_cleanup(staged):
        original_cleanup(staged)
        raise RuntimeError("secondary validator cleanup failure")

    def fail_launch_receipt(*_args, **_kwargs):
        raise ValueError("primary Work launch receipt failure")

    monkeypatch.setattr(work_module, "_stage_chat_patch_inputs", capture_stage)
    monkeypatch.setattr(work_module, "serve_patch_validation_mailbox", fail_serve)
    monkeypatch.setattr(work_module, "_record_agent_launch_receipt", fail_launch_receipt)
    monkeypatch.setattr(StagedCommandMailbox, "cleanup", fail_cleanup)

    with pytest.raises(ValueError, match="primary Work launch receipt failure"):
        async for _frame in stream_work_run(
            service,
            ScriptedLauncher([{}], message="not reached"),
            request,
            tmp_path / "data",
            execution=execution,
        ):
            pass

    assert len(staged_mailboxes) == 1
    _assert_command_state_removed(staged_mailboxes[0])
    assert started == finished == [f"{execution.operation_id}:work"]
    warnings = [
        event.message for event in execution.store.agent_task_events(execution.operation_id)
    ]
    assert any("secondary validator serve failure" in message for message in warnings)
    assert any("secondary validator cleanup failure" in message for message in warnings)


@pytest.mark.asyncio
async def test_correction_validator_closes_when_post_stage_receipt_fails(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    request = _request()
    execution = _chat_task_execution(
        app.state.background_tasks.store,
        operation_id="work-mailbox-correction-failure",
        project_id=app.state.default_project_id,
        request=request,
    )
    invalid = shape_invalid_patch().model_copy(update={"kind": "work"})
    staged_mailboxes: list[StagedCommandMailbox] = []
    started: list[str] = []
    finished: list[str] = []
    original_chat_stage = work_module._stage_chat_patch_inputs
    original_correction_stage = work_module.stage_patch_validation_mailbox
    original_serve = work_module.serve_patch_validation_mailbox
    original_receipt = work_module._record_agent_launch_receipt
    original_cleanup = StagedCommandMailbox.cleanup

    def capture_chat_stage(*args, **kwargs):
        staged = original_chat_stage(*args, **kwargs)
        staged_mailboxes.append(staged.validator_staged)
        return staged

    def capture_correction_stage(**kwargs):
        staged = original_correction_stage(**kwargs)
        staged_mailboxes.append(staged)
        return staged

    async def tracked_serve(**kwargs):
        turn_id = kwargs["staged"].credential.identity.turn_id
        started.append(turn_id)
        try:
            await original_serve(**kwargs)
        finally:
            finished.append(turn_id)

    def fail_correction_receipt(*args, **kwargs):
        if kwargs.get("continuation") == "graph_correction":
            raise RuntimeError("primary correction receipt failure")
        return original_receipt(*args, **kwargs)

    def fail_correction_cleanup(staged):
        original_cleanup(staged)
        if "work-patch-correction" in staged.credential.identity.turn_id:
            raise RuntimeError("secondary correction cleanup failure")

    monkeypatch.setattr(work_module, "_stage_chat_patch_inputs", capture_chat_stage)
    monkeypatch.setattr(work_module, "stage_patch_validation_mailbox", capture_correction_stage)
    monkeypatch.setattr(work_module, "serve_patch_validation_mailbox", tracked_serve)
    monkeypatch.setattr(work_module, "_record_agent_launch_receipt", fail_correction_receipt)
    monkeypatch.setattr(StagedCommandMailbox, "cleanup", fail_correction_cleanup)

    with pytest.raises(RuntimeError, match="primary correction receipt failure"):
        async for _frame in stream_work_run(
            service,
            ScriptedLauncher(
                [{"patch.json": agent_patch_json(invalid)}],
                message="The operational work completed.",
            ),
            request,
            tmp_path / "data",
            execution=execution,
        ):
            pass

    assert len(staged_mailboxes) == 2
    for staged in staged_mailboxes:
        _assert_command_state_removed(staged)
    assert started == finished
    assert len(started) == 2
    warnings = [
        event.message for event in execution.store.agent_task_events(execution.operation_id)
    ]
    assert any("secondary correction cleanup failure" in message for message in warnings)


@pytest.mark.asyncio
async def test_manual_graph_repair_preserves_post_stage_failure_over_mailbox_failures(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    request = _request().model_copy(
        update={"message": None, "session_id": "manual-repair-native-session"}
    )
    execution = _chat_task_execution(
        app.state.background_tasks.store,
        operation_id="work-mailbox-manual-repair-failure",
        project_id=app.state.default_project_id,
        request=request,
    )
    stage = tmp_path / "data" / "run-stage" / "chat-manual-repair-failure"
    stage.mkdir(parents=True)
    (stage / "workspace").mkdir()
    execution.store.record_chat_stage_layout(
        execution.operation_id,
        stage_root=str(stage),
        workspace_root=str(stage / "workspace"),
    )
    execution.checkpoint_stage("", str(stage))
    execution.continuation = "graph_repair"
    staged_mailboxes: list[StagedCommandMailbox] = []
    started: list[str] = []
    finished: list[str] = []
    original_stage = work_module._stage_chat_patch_inputs
    original_cleanup = StagedCommandMailbox.cleanup

    def capture_stage(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        staged_mailboxes.append(staged.validator_staged)
        return staged

    async def fail_serve(*, staged, stop, **_kwargs):
        turn_id = staged.credential.identity.turn_id
        started.append(turn_id)
        try:
            await stop.wait()
            raise RuntimeError("secondary manual repair serve failure")
        finally:
            finished.append(turn_id)

    def fail_cleanup(staged):
        original_cleanup(staged)
        raise RuntimeError("secondary manual repair cleanup failure")

    def fail_launch_receipt(*_args, **kwargs):
        assert kwargs["continuation"] == "graph_repair"
        raise RuntimeError("primary manual repair launch receipt failure")

    launcher = ScriptedLauncher([{}], message="not reached")
    monkeypatch.setattr(work_module, "_stage_chat_patch_inputs", capture_stage)
    monkeypatch.setattr(work_module, "serve_patch_validation_mailbox", fail_serve)
    monkeypatch.setattr(
        work_module,
        "_rejected_graph_update_for_repair",
        lambda _execution: work_module.GraphUpdateResult(
            status="rejected",
            validation_messages=["Repair the rejected Patch."],
            repairable=True,
        ),
    )
    monkeypatch.setattr(
        work_module,
        "_parent_task_contract_path",
        lambda *_args: str(stage / "original-task-contract.md"),
    )
    monkeypatch.setattr(work_module, "_record_agent_launch_receipt", fail_launch_receipt)
    monkeypatch.setattr(StagedCommandMailbox, "cleanup", fail_cleanup)

    with pytest.raises(RuntimeError, match="primary manual repair launch receipt failure"):
        async for _frame in stream_work_run(
            service,
            launcher,
            request,
            tmp_path / "data",
            execution=execution,
        ):
            pass

    assert launcher.calls == 0
    assert len(staged_mailboxes) == 1
    _assert_command_state_removed(staged_mailboxes[0])
    assert started == finished == [f"{execution.operation_id}:work-graph-repair"]
    warnings = [
        event.message for event in execution.store.agent_task_events(execution.operation_id)
    ]
    assert any("secondary manual repair serve failure" in message for message in warnings)
    assert any("secondary manual repair cleanup failure" in message for message in warnings)


@pytest.mark.parametrize(
    "continuation",
    [
        "fresh",
        "retry",
        "handoff",
        "watcher_wake",
        "message_wake",
        "graph_condition_wake",
        "lifecycle_wake",
    ],
)
def test_new_logical_work_continuations_clear_stale_handoffs(continuation: str) -> None:
    assert work_module._clears_stale_turn_handoffs(continuation) is True  # type: ignore[arg-type]


@pytest.mark.parametrize("continuation", ["resume", "graph_repair"])
def test_same_logical_work_continuations_preserve_handoffs(continuation: str) -> None:
    assert work_module._clears_stale_turn_handoffs(continuation) is False  # type: ignore[arg-type]


def test_non_work_checkpoint_continuation_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported Work continuation"):
        work_module._clears_stale_turn_handoffs("auto_research_continuation")


@pytest.mark.asyncio
async def test_local_work_keeps_rcp_inputs_outside_provider_workspace(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    request = _request()
    execution = _chat_task_execution(
        app.state.background_tasks.store,
        operation_id="work-contained-inputs",
        project_id=app.state.default_project_id,
        request=request,
    )
    launcher = ScriptedLauncher([{}], message="The local Work turn completed.")

    frames = [
        frame
        async for frame in stream_work_run(
            service,
            launcher,
            request,
            tmp_path / "data",
            execution=execution,
        )
    ]

    assert not any('"event":"error"' in frame for frame in frames)
    workspace = launcher.workspaces[0]
    stage = workspace.parent
    assert workspace.name == "workspace"
    assert execution.stage_root == str(stage)
    assert not (workspace / "inputs").exists()
    input_names = {item.name for item in (stage / "inputs").iterdir()}
    assert any(name.startswith("chat-master-v") for name in input_names)
    assert any(name.startswith("chat-patch-schema-") for name in input_names)
    assert any(name.startswith("rcp-agent-client-") for name in input_names)
    assert stage / "inputs" in launcher.launch_kwargs[0]["read_dirs"]
    launch = next(
        receipt
        for receipt in execution.store.agent_task_receipts(execution.operation_id)
        if receipt.category == "agent_launch"
    )
    assert launch.payload["canonical_write_roots"][0] == str(workspace)


@pytest.mark.asyncio
async def test_work_watcher_binding_keeps_originating_episode_lineage(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    request = _request()
    execution = _chat_task_execution(
        app.state.background_tasks.store,
        operation_id="work-episode-watcher-binding",
        project_id=app.state.default_project_id,
        request=request,
    )
    episode_id = "auto-research-episode"
    original_agent_task = execution.store.agent_task

    def episode_bound_task(operation_id: str):
        task = original_agent_task(operation_id)
        return (
            task.model_copy(update={"episode_id": episode_id})
            if task is not None and operation_id == execution.operation_id
            else task
        )

    monkeypatch.setattr(execution.store, "agent_task", episode_bound_task)
    bindings = []

    def capture_binding(_store, _specs, binding, **_kwargs):
        bindings.append(binding)
        return []

    monkeypatch.setattr(work_module, "arm_watchers", capture_binding)
    launcher = ScriptedLauncher(
        [
            {
                "watch.json": json.dumps(
                    {
                        "external": [
                            {
                                "check_command": "false",
                                "log_path": str(tmp_path / "work.log"),
                                "cwd": str(tmp_path),
                            }
                        ],
                        "graph": [],
                    }
                )
            }
        ],
        message="Detached work is still running.",
    )

    frames = [
        frame
        async for frame in stream_work_run(
            service,
            launcher,
            request,
            tmp_path / "data",
            execution=execution,
        )
    ]

    assert not any('"event":"error"' in frame for frame in frames)
    assert len(bindings) == 1
    assert bindings[0].episode_id == episode_id


@pytest.mark.asyncio
async def test_validator_cleanup_finishes_under_caller_cancellation_without_removing_handoffs(
    tmp_path, monkeypatch
) -> None:
    stage = tmp_path / "work-mailbox-cancel"
    stage.mkdir()
    staged = stage_patch_validation_mailbox(
        local_stage=stage,
        remote_stage=None,
        task_id="work-mailbox-cancel",
        turn_id="work-mailbox-cancel:work",
        timeout_seconds=30,
    )
    workspace = Path(staged.workspace)
    handoffs = {
        "patch.json": "patch survives",
        "watch.json": "watch survives",
        "messages.json": "messages survive",
    }
    for name, content in handoffs.items():
        (workspace / name).write_text(content, encoding="utf-8")

    stop = asyncio.Event()
    serve_finished = asyncio.Event()
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    cleanup_finished = threading.Event()
    original_cleanup = StagedCommandMailbox.cleanup

    async def serve_until_stopped() -> None:
        try:
            await stop.wait()
        finally:
            serve_finished.set()

    def blocking_cleanup(current):
        cleanup_started.set()
        if not cleanup_release.wait(timeout=5):
            raise AssertionError("test did not release validator cleanup")
        original_cleanup(current)
        cleanup_finished.set()

    monkeypatch.setattr(StagedCommandMailbox, "cleanup", blocking_cleanup)
    serve_task = asyncio.create_task(serve_until_stopped())
    lifecycle = _WorkValidatorMailboxLifecycle(
        staged=staged,
        execution=None,
        stop=stop,
        task=serve_task,
    )
    close_task = asyncio.create_task(lifecycle.close())
    assert await asyncio.to_thread(cleanup_started.wait, 2)
    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert serve_finished.is_set()
    assert serve_task.done()
    assert cleanup_finished.is_set()
    _assert_command_state_removed(staged)
    for name, content in handoffs.items():
        assert (workspace / name).read_text(encoding="utf-8") == content
