from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import rcp.runs.tasks.auto_research_child_work as child_module
import rcp.runs.tasks.experiment_loop as loop_module
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
    if staged.credential_path is not None:
        assert not Path(staged.credential_path).exists()
    else:
        assert staged.invocation_gate is not None
        assert not Path(staged.invocation_gate.socket_path).exists()
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
        "handoff",
        "watcher_wake",
        "message_wake",
        "graph_condition_wake",
        "lifecycle_wake",
    ],
)
def test_new_logical_work_continuations_clear_stale_handoffs(continuation: str) -> None:
    assert work_module._clears_stale_turn_handoffs(continuation) is True  # type: ignore[arg-type]


@pytest.mark.parametrize("continuation", ["resume", "retry", "graph_repair"])
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "continuation"),
    [
        (work_module, "resume"),
        (work_module, "retry"),
        (work_module, "message_wake"),
        (loop_module, "resume"),
        (loop_module, "retry"),
        (loop_module, "watcher_wake"),
        (child_module, "resume"),
        (child_module, "retry"),
        (child_module, "message_wake"),
        (child_module, "watcher_wake"),
    ],
)
async def test_operational_continuation_renders_current_launch_client(
    manifest, tmp_path, monkeypatch, owner, continuation
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    request = _request()
    execution = _chat_task_execution(
        app.state.background_tasks.store,
        operation_id="current-launch-turn",
        project_id=app.state.default_project_id,
        request=request,
    )
    previous_stage = tmp_path / "previous-stage"
    previous_stage.mkdir()
    previous = stage_patch_validation_mailbox(
        local_stage=previous_stage,
        remote_stage=None,
        task_id="previous-turn",
        turn_id="previous-turn:work",
        timeout_seconds=30,
        authority="broker",
    )
    launch_args = (
        "launch",
        "--key",
        "<idempotency-key>",
        "--cwd",
        "<working-directory>",
        "--",
        "<argv...>",
    )
    previous_command = previous.client_command(*launch_args)
    original = previous_stage / "original.md"
    original.write_text(previous_command)
    previous.cleanup()
    monkeypatch.setattr(owner, "_parent_task_contract_path", lambda *_args: str(original))
    turn, staged = await work_module._stage_work_turn(
        service,
        work_module._resolve_work_execution(service, request, execution),
        tmp_path / "data",
        execution,
    )
    try:
        execution.continuation = continuation
        execution.retry_feedback = ("The previous invocation was interrupted.",)
        turn.request = request.model_copy(update={"watcher_ids": ["observer-1"]})
        if owner is child_module:
            composed = child_module._compose_child_prompt(
                turn,
                staged,
                SimpleNamespace(
                    worker_id="child-1", instruction="Complete the bounded child check."
                ),
                mail_path="/inputs/mail.json",
            )
        elif owner is loop_module:
            turn.request = turn.request.model_copy(
                update={
                    "patch_kind": "experiment_loop",
                    "control_node_id": "exp/one",
                    "control_invocation": 1,
                    "control_invocation_ceiling": 3,
                }
            )
            prepared = SimpleNamespace(
                episode_context_baseline={},
                loop_control_path="/inputs/loop-control.json",
                watcher_state_path="/inputs/watcher-state.json",
                context_replacement=None,
                wake_episode=SimpleNamespace(last_graph_result="applied", last_watcher_ids=[]),
            )
            monkeypatch.setattr(
                owner, "_experiment_session_contract_path", lambda _turn: str(original)
            )
            compose = getattr(
                owner,
                "_compose_wake_prompt"
                if continuation == "watcher_wake"
                else f"_compose_{continuation}_prompt",
            )
            composed = compose(turn, staged, prepared)
        else:
            compose = getattr(
                owner,
                "_compose_fresh_prompt"
                if continuation == "message_wake"
                else f"_compose_{continuation}_prompt",
            )
            composed = compose(turn, staged)
        contract = Path(composed.contract_path).read_text()
        scope_context = composed.prompt + "\n" + contract
        for path in (*turn.write_scope.writable_roots, *turn.write_scope.protected_write_paths):
            assert path in scope_context
        if owner is work_module and continuation == "message_wake":
            assert turn.patch_inputs.validator_staged.client_command(*launch_args) not in contract
            tooling = list((turn.local_stage / "inputs").glob("task-*-execution.md"))
            assert len(tooling) == 1
            assert tooling[0].name in composed.prompt
            contract = tooling[0].read_text()
        assert turn.patch_inputs.validator_staged.client_command(*launch_args) in contract
        assert previous_command not in contract
    finally:
        await turn.validator_lifecycle.close()


#: Written while a turn is being launched and streamed, and by nothing else.
_LAUNCH_ONLY_RECEIPTS = frozenset(
    {
        "agent_launch",
        "agent_prompt",
        "chat_context",
        "chat_context_assembled",
        "chat_master_context",
        "chat_stage_layout",
        "operation_admitted",
        "operation_created",
        "provider_readiness",
        "retry_deliverable_baseline",
    }
)


def _durable_state(service, store, root: Path) -> dict[str, object]:
    """What the turn left behind, in the terms someone reading it later sees.

    Frames are what a watching client saw, and comparing only those is how a
    recorded path that skipped session binding or result-view settlement stayed
    green. This is the graph, the transcript, the session, the verdict, and the
    receipt categories -- with the ids and paths two independent runs cannot
    share left out.
    """

    task = store.agent_task("work-one-result")
    graph = json.loads((Path(service.history.workspace.root) / "graph.json").read_text("utf-8"))
    nodes = graph.get("nodes")
    node_ids = (
        sorted(nodes) if isinstance(nodes, dict) else sorted(item["id"] for item in (nodes or []))
    )
    return {
        "graph_revision": graph.get("revision"),
        "status": task.status,
        "phase": task.phase,
        "native_session_id_bound": task.native_session_id is not None,
        "graph_nodes": node_ids,
        # A recorded pass launches nothing, so it writes none of the receipts a
        # launch writes. Everything after the provider stopped must match.
        "settlement_receipts": sorted(
            {
                receipt.category
                for receipt in store.agent_task_receipts("work-one-result")
                if receipt.category not in _LAUNCH_ONLY_RECEIPTS
            }
        ),
        "chat_exchanges": len(
            [
                item
                for item in sorted((root / "data").rglob("*.json"))
                if item.name.startswith("chat-")
            ]
        ),
    }


def _decided_output(frames: list[str]) -> list[dict[str, object]]:
    """What a finished Work turn decided, in the terms a reader of it sees.

    Artifact and answer frames carry a stage path and a session id that two
    independent runs cannot share; the frames that say what the turn concluded
    carry neither, and those are the comparison.
    """

    decided = []
    for frame in frames:
        event = json.loads(frame.removeprefix("data: ").strip())
        if event["event"] in {"message", "error", "done", "paused"}:
            decided.append({key: value for key, value in event.items() if value not in (None, "")})
    return decided


def _isolated_manifest(root: Path) -> Path:
    """The fixture manifest under a root of its own.

    Two apps built from one manifest fight over the project identity it names,
    so a test that delivers the same result twice gives each delivery its own.
    """

    repo_a = root / "repo-a"
    repo_b = root / "repo-b"
    research = repo_a / ".research"
    research.mkdir(parents=True)
    repo_b.mkdir()
    path = research / "manifest.toml"
    path.write_text(
        f'''name = "test-paper"

[[machines]]
alias = "laptop"
host = ""

[[repositories]]
alias = "repo-a"
machine = "laptop"
path = "{repo_a}"

[[repositories]]
alias = "repo-b"
machine = "laptop"
path = "{repo_b}"

[project]
truth_scope = ["repo-a", "repo-b"]

[state]
repository = "repo-a"

[agent]
default_run_truth_scope = ["repo-a"]

[execution]
run_on = "laptop"
''',
        encoding="utf-8",
    )
    return path


def _one_result_app(root: Path):
    """An app of its own, so two deliveries of one result never share a graph."""

    root.mkdir(parents=True, exist_ok=True)
    app = create_named_app(str(_isolated_manifest(root)), data_dir=root / "data")
    request = RunRequest(
        chat_scope="project",
        chat_id="chat-one-result",
        message="Run the check and reflect any graph change.",
        run_truth_scope=["repo-a"],
        mode="work",
    )
    store = app.state.background_tasks.store
    execution = _chat_task_execution(
        store,
        operation_id="work-one-result",
        project_id=app.state.default_project_id,
        request=request,
    )
    # A real dispatch leaves this behind, and the stage a second staging attaches
    # to is validated against it. The fixture builds its execution directly, so
    # it has to leave the same trace a launch would.
    store.record_agent_task_receipt(
        "work-one-result",
        "operation_created",
        {
            "kind": "project_chat",
            "attempt": 1,
            "has_parent": False,
            "continuation_cause": "fresh",
            "resumed": False,
        },
    )
    return app.state.service, request, execution


async def _delivered_live(
    root: Path, turns: list[dict[str, str]], answer: str
) -> tuple[list[dict[str, object]], ScriptedLauncher]:
    service, request, execution = _one_result_app(root)
    launcher = ScriptedLauncher(turns, message=answer)
    frames = [
        frame
        async for frame in stream_work_run(
            service, launcher, request, root / "data", execution=execution
        )
    ]
    return _decided_output(frames), launcher, _durable_state(service, execution.store, root)


def _recorded_pass(
    patch_text: str,
    answer: str,
    watch_text: str | None = None,
    experiment_watch: dict[str, str] | None = None,
):
    """One completed Codex pass, as a host would have recorded it."""

    from rcp.runs.recorded_turn import recorded_provider_turn

    events = "".join(
        json.dumps(item) + "\n"
        for item in (
            {"type": "thread.started", "thread_id": "recorded-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}},
        )
    )
    outcome = {
        "version": 1,
        "pid_file": "/stage/one.pid",
        "provider": "codex",
        "runtime_id": "codex.exec-json.v1",
        "provider_version": "0.153.4",
        "accepted": True,
        "terminal_event": True,
        "journal_complete": True,
        "error": None,
        "stopped": False,
        "events_sha256": hashlib.sha256(events.encode("utf-8")).hexdigest(),
        "patch_present": True,
        "patch_sha256": hashlib.sha256(patch_text.encode("utf-8")).hexdigest(),
        "watch_present": watch_text is not None,
        "watch_sha256": (
            hashlib.sha256(watch_text.encode("utf-8")).hexdigest()
            if watch_text is not None
            else None
        ),
        "experiment_watch_snapshotted": True,
        "experiment_watch_sha256": {
            name: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for name, text in (experiment_watch or {}).items()
        },
        "root_thread_id": "recorded-thread",
        "input_message_ids": [],
        "steer_requests": {},
    }
    return recorded_provider_turn(
        "/stage/one.pid",
        {
            "accepted": {"version": 1, "pid_file": "/stage/one.pid", "at": 1.0},
            "outcome": outcome,
            "events": events,
            "stderr": "",
            "patch": patch_text,
            "watch": watch_text,
            "experiment_watch": dict(experiment_watch or {}),
        },
    )


def _recorded_failed_correction(message: str):
    """One correction pass the host recorded as failed, carrying no answer."""

    from rcp.runs.recorded_turn import recorded_provider_turn

    events = "".join(
        json.dumps(item) + "\n"
        for item in (
            {"type": "thread.started", "thread_id": "recorded-thread"},
            {"type": "turn.failed", "error": {"message": message}},
        )
    )
    return recorded_provider_turn(
        "/stage/one.pid",
        {
            "accepted": {"version": 1, "pid_file": "/stage/one.pid", "at": 1.0},
            "outcome": {
                "version": 1,
                "pid_file": "/stage/one.pid",
                "provider": "codex",
                "runtime_id": "codex.exec-json.v1",
                "provider_version": "0.153.4",
                "accepted": True,
                "terminal_event": True,
                "journal_complete": True,
                "error": None,
                "stopped": False,
                "events_sha256": hashlib.sha256(events.encode("utf-8")).hexdigest(),
                "patch_present": False,
                "patch_sha256": None,
                "root_thread_id": "recorded-thread",
                "input_message_ids": [],
                "steer_requests": {},
            },
            "events": events,
            "stderr": "",
            "patch": None,
        },
    )


async def _delivered_from_record(
    root: Path, patch_text: str, answer: str
) -> tuple[list[dict[str, object]], ScriptedLauncher, object]:
    service, request, execution = _one_result_app(root)
    launcher = ScriptedLauncher([{}], message="")
    # The launch happened; only the link did not survive it.
    resolved = work_module._resolve_work_execution(service, request, execution)
    primed, _staged = await work_module._stage_work_turn(
        service, resolved, root / "data", execution
    )
    work_module._record_work_finalization_context(primed, _staged)
    execution.bind_write_scope(primed.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(primed, _staged)
    await primed.validator_lifecycle.close()

    frames = [
        frame
        async for frame in work_module.finalize_recorded_work_result(
            service,
            launcher,
            request,
            root / "data",
            execution,
            _recorded_pass(patch_text, answer),
        )
    ]
    return _decided_output(frames), launcher, _durable_state(service, execution.store, root)


@pytest.mark.asyncio
async def test_a_recorded_result_finalizes_the_way_its_live_delivery_would(tmp_path) -> None:
    """One provider result, two links: the durable output cannot tell them apart."""

    patch_text = agent_patch_json(seed_patch())
    answer = "The Work turn reflected one graph change."

    live, _, live_state = await _delivered_live(
        tmp_path / "live", [{"patch.json": patch_text}], answer
    )
    recorded, _, recorded_state = await _delivered_from_record(
        tmp_path / "recorded", patch_text, answer
    )

    assert recorded_state == live_state
    # The comparison is only worth something if the turn actually did something.
    assert live_state["graph_revision"] == 2
    assert live_state["graph_nodes"]
    assert "patch_applied" in live_state["settlement_receipts"]
    assert recorded == live
    assert [item["event"] for item in live] == ["message", "done"]
    assert '"status":"applied"' in str(live[0]["text"])


@pytest.mark.asyncio
async def test_a_recorded_result_is_rejected_where_a_live_one_would_be_corrected(tmp_path) -> None:
    """Correcting a bad deliverable needs a provider still listening; a record has none."""

    answer = "The Work turn reflected one graph change."
    live, live_launcher, _live_state = await _delivered_live(
        tmp_path / "live",
        [
            {"patch.json": agent_patch_json(shape_invalid_patch())},
            {"patch.json": agent_patch_json(seed_patch())},
        ],
        answer,
    )
    recorded, recorded_launcher, _ = await _delivered_from_record(
        tmp_path / "recorded", agent_patch_json(shape_invalid_patch()), answer
    )

    assert '"status":"applied"' in str(live[0]["text"])
    assert live_launcher.calls > 1
    assert '"status":"rejected"' in str(recorded[0]["text"])
    assert recorded_launcher.calls == 0


@pytest.mark.asyncio
async def test_recorded_finalization_never_reenters_launch_preparation(
    tmp_path, monkeypatch
) -> None:
    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    primed, _staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    work_module._record_work_finalization_context(primed, _staged)
    execution.bind_write_scope(primed.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(primed, _staged)
    await primed.validator_lifecycle.close()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recorded finalization reentered provider launch preparation")

    monkeypatch.setattr(work_module, "_resolve_work_execution", forbidden)
    monkeypatch.setattr(work_module, "_stage_work_turn", forbidden)
    launcher = ScriptedLauncher([{}], message="")

    frames = [
        frame
        async for frame in work_module.finalize_recorded_work_result(
            service,
            launcher,
            request,
            tmp_path / "not-used",
            execution,
            _recorded_pass(
                agent_patch_json(seed_patch()),
                "The recorded provider finished the Work turn.",
            ),
        )
    ]

    decided = _decided_output(frames)
    assert '"status":"applied"' in str(decided[0]["text"])
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_recorded_finalization_can_resume_without_applying_the_turn_twice(tmp_path) -> None:
    """A crash after an owner write must not duplicate that write on restart."""

    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    primed, _staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    work_module._record_work_finalization_context(primed, _staged)
    execution.bind_write_scope(primed.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(primed, _staged)
    await primed.validator_lifecycle.close()
    launcher = ScriptedLauncher([{}], message="")
    recorded = _recorded_pass(
        agent_patch_json(seed_patch()),
        "The recorded provider finished the Work turn.",
    )

    for _attempt in range(2):
        frames = [
            frame
            async for frame in work_module.finalize_recorded_work_result(
                service,
                launcher,
                request,
                tmp_path / "not-used",
                execution,
                recorded,
            )
        ]
        assert [item["event"] for item in _decided_output(frames)] == ["message", "done"]

    graph = json.loads(
        (Path(service.history.workspace.root) / "graph.json").read_text(encoding="utf-8")
    )
    transcript = [
        json.loads(line)
        for line in service.chat_path(
            request.chat_id,
            chat_scope=request.chat_scope,
            node_id=request.node_id,
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert graph["revision"] == 2
    assert [(item["operationId"], item["role"]) for item in transcript] == [
        (execution.operation_id, "user"),
        (execution.operation_id, "assistant"),
    ]
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_a_recorded_retry_keeps_the_baseline_it_launched_with(tmp_path) -> None:
    """A retry's own output must not become its own predecessor.

    Recapturing the baseline at finalization reads the stage the provider has
    already written to, so the digests match and settlement quietly drops the
    graph update the retry existed to produce.
    """

    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    primed, _staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    execution.bind_write_scope(primed.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(primed, _staged)
    await primed.validator_lifecycle.close()
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "retry_deliverable_baseline",
        {"patch_sha256": "the-previous-attempt", "watch_sha256": None},
        tier="diagnostic",
    )
    (primed.workspace / "patch.json").write_text(agent_patch_json(seed_patch()), encoding="utf-8")

    baseline = work_module._recorded_retry_deliverable_baseline(execution)

    assert baseline.patch_digest == "the-previous-attempt"
    assert (
        baseline.patch_digest
        != hashlib.sha256(
            (primed.workspace / "patch.json").read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("deliverable", ["patch.json", "watch.json", "experiment-watch"])
async def test_work_correction_disconnect_waits_and_recovers_original_reply(
    tmp_path, monkeypatch, deliverable
) -> None:
    """Every automatic correction keeps the task and its completed human reply."""

    import rcp.runs.tasks.experiment_watcher_maintenance as maintenance_module
    import rcp.runs.tasks.work_turn_runtime as runtime_module
    from rcp.agents import AgentEvent
    from rcp.runs.experiment_loop import experiment_watcher_output_name
    from rcp.runs.shared import _sse

    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    primed, staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    work_module._record_work_finalization_context(primed, staged)
    execution.bind_write_scope(primed.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(primed, staged)
    composed = work_module._compose_fresh_prompt(primed, staged, retry_diagnostics_path=None)
    await primed.validator_lifecycle.close()
    # Exercise the owner's remote opt-in while keeping filesystem I/O local.
    # The actual SSH supervisor is covered by the remote provider journey.
    primed.supervise_remote = True
    primed.outcome.completed = True
    primed.outcome.session_id = "recorded-thread"
    original_answer = "The research work is complete."
    primed.outcome.answers = [original_answer]
    finalization = work_module._work_finalization_context(primed, staged)
    assert work_module._settle_work_outcome(finalization) == []
    assert (
        execution.store.agent_task_contract(
            execution.operation_id, work_module._WORK_PRIMARY_ANSWER_ROLE
        )
        == original_answer
    )

    correction_calls = []

    async def disconnected_correction(*_args, **kwargs):
        correction_calls.append(kwargs)
        assert kwargs["supervise_remote"] is True
        kwargs["outcome"].remote_result_pending = True
        yield _sse(AgentEvent(event="remote_result_pending", text="Connection lost."))

    monkeypatch.setattr(runtime_module, "_stream_agent_events", disconnected_correction)
    monkeypatch.setattr(maintenance_module, "_stream_agent_events", disconnected_correction)
    if deliverable == "experiment-watch":
        name = experiment_watcher_output_name("exp/test")
        resource = SimpleNamespace(control_node_id="exp/test", graph_target=None)
        finalization.experiment_resources = [
            SimpleNamespace(resource=resource, watch_path=str(primed.workspace / name))
        ]
        monkeypatch.setattr(
            maintenance_module,
            "_experiment_maintenance_binding",
            lambda *_args: SimpleNamespace(origin_task_kind="project_chat"),
        )
        monkeypatch.setattr(
            execution.store, "admit_experiment_watcher_maintenance", lambda *_args: None
        )
    else:
        name = deliverable
    (primed.workspace / name).write_text("not valid JSON", encoding="utf-8")
    if deliverable != "patch.json":
        # Later corrections can disconnect after the graph is already applied.
        (primed.workspace / "patch.json").write_text(
            agent_patch_json(seed_patch()), encoding="utf-8"
        )
    launcher = ScriptedLauncher([{}], message="must not launch")
    frames = [
        frame
        async for frame in work_module.finalize_work_result(
            finalization,
            launcher,
            work_module._RetryDeliverableBaseline(None, None, {}),
            original_answer,
            launch_turn=primed,
            staged=staged,
            composed=composed,
        )
    ]
    assert len(correction_calls) == 1
    assert [json.loads(frame.removeprefix("data: "))["event"] for frame in frames] == [
        "remote_result_pending"
    ]
    if deliverable != "patch.json":
        assert service.history.state().revision == 2
    assert not any(
        receipt.category in {"graph_patch_rejected", "experiment_watcher_maintenance_rejected"}
        for receipt in execution.store.agent_task_receipts(execution.operation_id)
    )

    # The host finishes its correction while RCP is disconnected. Its reply is
    # maintenance prose; only its deliverables may replace the primary output.
    (primed.workspace / name).unlink()
    recorded = _recorded_pass(agent_patch_json(seed_patch()), "I repaired the handoff file.")
    for _attempt in range(2):
        recovered = [
            frame
            async for frame in work_module.finalize_recorded_work_result(
                service, launcher, request, tmp_path / "unused", execution, recorded
            )
        ]
        assert _decided_output(recovered)[-1] == {"event": "done"}
    transcript = [
        json.loads(line)
        for line in service.chat_path(
            request.chat_id, chat_scope=request.chat_scope, node_id=request.node_id
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    replies = [item for item in transcript if item["role"] == "assistant"]
    assert len(replies) == 1
    assert replies[0]["text"] == original_answer
    assert service.history.state().revision == 2
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_finalization_cannot_enable_corrections_without_launch_context(tmp_path) -> None:
    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    turn, staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    await turn.validator_lifecycle.close()
    assert (
        execution.store.agent_task_contract(
            execution.operation_id, work_module.WORK_FINALIZATION_CONTEXT_ROLE
        )
        is None
    )
    (turn.workspace / "patch.json").write_text(agent_patch_json(seed_patch()), encoding="utf-8")
    launcher = ScriptedLauncher([{}], message="must not launch")
    with pytest.raises(ValueError, match="complete live launch context"):
        async for _frame in work_module.finalize_work_result(
            work_module._work_finalization_context(turn, staged),
            launcher,
            work_module._RetryDeliverableBaseline(None, None, {}),
            "A recorded answer",
            maximum_corrections=1,
        ):
            pass
    assert service.history.state().revision == 1
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_a_recorded_correction_that_failed_keeps_the_reply_it_already_gave(
    tmp_path,
) -> None:
    """A correction's failure is not the loss of the answer the turn produced.

    The supervision that lets a turn be recovered also covers its corrections,
    so a link lost during one hands recovery the correction's journal. The live
    path had already delivered the reply before that correction started, and a
    reader stops at the first error, so the reply has to arrive ahead of it.
    """

    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    primed, staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    work_module._record_work_finalization_context(primed, staged)
    execution.bind_write_scope(primed.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(primed, staged)
    await primed.validator_lifecycle.close()
    original_answer = "The research work is complete."
    primed.outcome.completed = True
    primed.outcome.session_id = "recorded-thread"
    primed.outcome.answers = [original_answer]
    assert (
        work_module._settle_work_outcome(work_module._work_finalization_context(primed, staged))
        == []
    )
    assert (
        execution.store.agent_task_contract(
            execution.operation_id, work_module._WORK_PRIMARY_ANSWER_ROLE
        )
        == original_answer
    )

    failed_correction = _recorded_failed_correction("the correction died")

    frames = [
        frame
        async for frame in work_module.finalize_recorded_work_result(
            service,
            ScriptedLauncher([{}], message="must not launch"),
            request,
            tmp_path / "not-used",
            execution,
            failed_correction,
        )
    ]
    events_out = [json.loads(frame.removeprefix("data: ")) for frame in frames]
    kinds = [item["event"] for item in events_out]

    assert "answer" in kinds and "error" in kinds
    assert kinds.index("answer") < kinds.index("error")
    assert next(item["text"] for item in events_out if item["event"] == "answer") == original_answer


def test_recovery_makes_the_stage_hold_exactly_the_watcher_outputs_the_pass_wrote(
    tmp_path,
) -> None:
    """Watcher maintenance is read by discovery, so the whole set is evidence.

    Settling reads whichever `experiment-watch-*.json` files the stage holds at
    the time. A file replaced after the host finished would arm different
    observers under this turn's authority, and one added afterwards would be
    admitted as this turn's, so restoring the record has to remove it.
    """

    from rcp.runs.recorded_settlement import write_recorded_patch

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    kept = '{"observers": [{"check_command": "true"}], "stops": []}'
    # The stage as RCP finds it: one output rewritten, one invented, and the
    # third -- which the pass did write -- deleted outright.
    workspace.joinpath("experiment-watch-aaaa.json").write_text("{}", encoding="utf-8")
    workspace.joinpath("experiment-watch-cccc.json").write_text("{}", encoding="utf-8")

    recorded = _recorded_pass(
        "",
        "done",
        experiment_watch={
            "experiment-watch-aaaa.json": kept,
            "experiment-watch-bbbb.json": kept,
        },
    )
    write_recorded_patch(workspace, None, recorded)

    assert sorted(item.name for item in workspace.glob("experiment-watch-*.json")) == [
        "experiment-watch-aaaa.json",
        "experiment-watch-bbbb.json",
    ]
    assert workspace.joinpath("experiment-watch-aaaa.json").read_text(encoding="utf-8") == kept
    assert workspace.joinpath("experiment-watch-bbbb.json").read_text(encoding="utf-8") == kept


def test_a_journal_predating_watcher_snapshots_leaves_the_stage_alone(tmp_path) -> None:
    """Silence about the set is not a claim that the pass wrote none."""

    from rcp.runs.recorded_settlement import write_recorded_patch

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    existing = '{"observers": [], "stops": []}'
    workspace.joinpath("experiment-watch-aaaa.json").write_text(existing, encoding="utf-8")

    recorded = _recorded_pass("", "done")
    older = dataclasses.replace(recorded, experiment_watch={}, experiment_watch_snapshotted=False)
    write_recorded_patch(workspace, None, older)

    assert workspace.joinpath("experiment-watch-aaaa.json").read_text(encoding="utf-8") == existing


@pytest.mark.asyncio
async def test_a_host_lost_during_watcher_maintenance_leaves_the_task_waiting(
    tmp_path, monkeypatch
) -> None:
    """An unreachable host is not a turn that asked for no maintenance.

    Swallowing the outage completes the task as a success with its requested
    maintenance silently undone, and nothing retries it: Background only sees a
    finalizer that ran to the end. Letting it out is what leaves the task
    waiting for its stage to come back.
    """

    import rcp.runs.tasks.experiment_watcher_maintenance as maintenance_module
    from rcp.transport import StateUnavailable

    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    primed, staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    work_module._record_work_finalization_context(primed, staged)
    execution.bind_write_scope(primed.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(primed, staged)
    await primed.validator_lifecycle.close()

    def host_went_away(*_args, **_kwargs):
        raise StateUnavailable("could not inspect chat watcher outputs: ssh exited 255")

    monkeypatch.setattr(maintenance_module, "read_experiment_watcher_outputs", host_went_away)

    with pytest.raises(StateUnavailable):
        async for _frame in work_module.finalize_recorded_work_result(
            service,
            ScriptedLauncher([{}], message="must not launch"),
            request,
            tmp_path / "not-used",
            execution,
            _recorded_pass(agent_patch_json(seed_patch()), "The work is done."),
        ):
            pass


@pytest.mark.asyncio
async def test_a_live_supervised_turn_whose_host_vanishes_waits_for_its_journal(
    tmp_path, monkeypatch
) -> None:
    """A journalled pass is not lost when the link dies while it settles.

    The provider already finished and the host already recorded it. Failing the
    task would bury that result, and classifying it as a lost link could run the
    operational turn again. Leaving it pending hands the same pass to
    reconciliation, which is where an unreachable host is already waited on.
    """

    import rcp.runs.tasks.experiment_watcher_maintenance as maintenance_module
    from rcp.transport import StateUnavailable

    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    primed, staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    work_module._record_work_finalization_context(primed, staged)
    execution.bind_write_scope(primed.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(primed, staged)
    composed = work_module._compose_fresh_prompt(primed, staged, retry_diagnostics_path=None)
    await primed.validator_lifecycle.close()
    primed.supervise_remote = True
    primed.outcome.completed = True
    primed.outcome.session_id = "recorded-thread"
    primed.outcome.answers = ["The research work is complete."]
    finalization = work_module._work_finalization_context(primed, staged)
    assert work_module._settle_work_outcome(finalization) == []
    (primed.workspace / "patch.json").write_text(agent_patch_json(seed_patch()), encoding="utf-8")

    def host_went_away(*_args, **_kwargs):
        raise StateUnavailable("could not inspect chat watcher outputs: ssh exited 255")

    monkeypatch.setattr(maintenance_module, "read_experiment_watcher_outputs", host_went_away)

    frames = [
        frame
        async for frame in work_module.finalize_work_result(
            finalization,
            ScriptedLauncher([{}], message="must not launch"),
            work_module._RetryDeliverableBaseline(None, None, {}),
            "The research work is complete.",
            launch_turn=primed,
            staged=staged,
            composed=composed,
        )
    ]

    events = [json.loads(frame.removeprefix("data: ")) for frame in frames]
    # The frame Background reads to leave this task awaiting a remote result.
    assert "remote_result_pending" in [item["event"] for item in events]
    assert [item for item in events if item["event"] == "error"] == []
