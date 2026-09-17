"""A Discuss turn that outlives its connection still answers its own chat.

Discuss holds no graph authority, which is why it is easy to assume there is
nothing to recover. There is: the reply, the session binding that lets the next
message continue the same provider conversation, and the turn's artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rcp.runs.tasks.discuss as discuss_module
from rcp.agents import AgentEvent
from rcp.runs.shared import _sse
from rcp.service import RunRequest

from .helpers import create_named_app
from .test_api import ScriptedLauncher, _chat_task_execution
from .test_work_agent_io import _isolated_manifest, _recorded_pass

_ANSWER = "Replanning restores plasticity in the recorded runs."


def _discuss_app(root: Path):
    """One Discuss chat with a project of its own."""

    root.mkdir(parents=True, exist_ok=True)
    app = create_named_app(str(_isolated_manifest(root)), data_dir=root / "data")
    request = RunRequest(
        chat_scope="project",
        chat_id="chat-discuss-recovery",
        message="What does the graph say about replanning?",
        run_truth_scope=["repo-a"],
        mode="discuss",
    )
    store = app.state.background_tasks.store
    execution = _chat_task_execution(
        store,
        operation_id="discuss-one-result",
        project_id=app.state.default_project_id,
        request=request,
    )
    return app.state.service, request, execution


def _retained_context(service, request, execution) -> discuss_module.DiscussFinalizationContext:
    """The snapshot a remote launch would have written, over a local stage.

    Keeps filesystem I/O local while retaining exactly what a supervised remote
    launch retains. Nothing here is guessed: `attach_retained_stage` rejects a
    workspace this stage does not imply, and `retained_artifact_directory`
    rejects a boundary this workspace does not imply, so a wrong value fails the
    load rather than passing silently.
    """

    assert execution.stage_root is not None
    workspace = Path(execution.stage_root) / "workspace"
    scope_id = execution.operation_id
    return discuss_module.DiscussFinalizationContext(
        service=service,
        request=request,
        execution=execution,
        workspace=workspace,
        remote_stage=None,
        artifact_scope_id=scope_id,
        artifact_directory=workspace / "turns" / scope_id / "artifacts",
        outcome=discuss_module._ProviderOutcome(session_id=None),
    )


def _transcript(service, request) -> list[dict]:
    path = service.chat_path(
        request.chat_id,
        chat_scope=request.chat_scope,
        node_id=request.node_id,
    )
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def _run_discuss(service, request, execution, launcher, data_dir: Path) -> list[str]:
    return [
        frame
        async for frame in discuss_module.stream_discuss_run(
            service, launcher, request, data_dir, execution
        )
    ]


@pytest.mark.asyncio
async def test_a_lost_discuss_link_leaves_the_turn_for_its_own_task(tmp_path, monkeypatch) -> None:
    """A pending remote result is not an unanswered turn, and settles nothing."""

    service, request, execution = _discuss_app(tmp_path)

    async def pending(*_args, **kwargs):
        kwargs["outcome"].remote_result_pending = True
        yield _sse(AgentEvent(event="remote_result_pending", text="Connection lost."))

    monkeypatch.setattr(discuss_module, "_stream_agent_events", pending)
    frames = await _run_discuss(
        service, request, execution, ScriptedLauncher([{}], message=""), tmp_path / "data"
    )

    events = [json.loads(frame.removeprefix("data: "))["event"] for frame in frames]
    assert events == ["remote_result_pending"]
    # No error, no answer, and no reply written: the task is owed a result, not
    # a verdict about one.
    assert [item for item in _transcript(service, request) if item["role"] == "assistant"] == []
    # This turn ran locally, so it retained nothing to recover from. A local
    # provider dies with the process that launched it.
    assert (
        execution.store.agent_task_contract(
            execution.operation_id, discuss_module.DISCUSS_FINALIZATION_CONTEXT_ROLE
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_recorded_discuss_pass_answers_its_original_chat(tmp_path) -> None:
    service, request, execution = _discuss_app(tmp_path)
    execution.checkpoint_stage("", str(tmp_path / "stage"))
    (Path(execution.stage_root) / "workspace").mkdir(parents=True)
    discuss_module._record_discuss_finalization_context(
        _retained_context(service, request, execution)
    )
    launcher = ScriptedLauncher([{}], message="must not launch")

    frames = [
        frame
        async for frame in discuss_module.finalize_recorded_discuss_result(
            service,
            launcher,
            request,
            tmp_path / "not-used",
            execution,
            _recorded_pass("", _ANSWER),
        )
    ]

    events = [json.loads(frame.removeprefix("data: ")) for frame in frames]
    assert [item["event"] for item in events if item["event"] in {"answer", "done"}] == [
        "answer",
        "done",
    ]
    assert next(item["text"] for item in events if item["event"] == "answer") == _ANSWER
    replies = [item for item in _transcript(service, request) if item["role"] == "assistant"]
    assert [item["text"] for item in replies] == [_ANSWER]
    # The session the host recorded is republished, which is how the next
    # message in this chat continues that provider conversation rather than
    # starting a fresh one.
    assert [item["session_id"] for item in events if item["event"] == "session"] == [
        "recorded-thread"
    ]
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_recorded_discuss_finalization_can_resume_without_answering_twice(tmp_path) -> None:
    """A crash after the reply is written must not write a second one."""

    service, request, execution = _discuss_app(tmp_path)
    execution.checkpoint_stage("", str(tmp_path / "stage"))
    (Path(execution.stage_root) / "workspace").mkdir(parents=True)
    discuss_module._record_discuss_finalization_context(
        _retained_context(service, request, execution)
    )
    launcher = ScriptedLauncher([{}], message="must not launch")
    recorded = _recorded_pass("", _ANSWER)
    # A Discuss agent that wrote a patch anyway gets one discard receipt, not
    # one per recovery attempt.
    (Path(execution.stage_root) / "workspace" / "patch.json").write_text(
        '{"operations": []}', encoding="utf-8"
    )

    for _attempt in range(2):
        frames = [
            frame
            async for frame in discuss_module.finalize_recorded_discuss_result(
                service, launcher, request, tmp_path / "not-used", execution, recorded
            )
        ]
        assert json.loads(frames[-1].removeprefix("data: "))["event"] == "done"

    transcript = _transcript(service, request)
    assert [(item["operationId"], item["role"]) for item in transcript] == [
        (execution.operation_id, "user"),
        (execution.operation_id, "assistant"),
    ]
    discarded = [
        receipt
        for receipt in execution.store.agent_task_receipts(execution.operation_id)
        if receipt.category == "discuss_patch_discarded"
    ]
    assert len(discarded) == 1
    assert launcher.calls == 0


def test_a_discuss_task_routes_to_the_owner_that_retained_its_context(tmp_path) -> None:
    from rcp.runs.remote_finalization import recorded_finalizer

    service, request, execution = _discuss_app(tmp_path)
    store = execution.store
    assert recorded_finalizer(store, execution.operation_id) is None

    execution.checkpoint_stage("", str(tmp_path / "stage"))
    (Path(execution.stage_root) / "workspace").mkdir(parents=True)
    discuss_module._record_discuss_finalization_context(
        _retained_context(service, request, execution)
    )

    assert (
        recorded_finalizer(store, execution.operation_id)
        is discuss_module.finalize_recorded_discuss_result
    )


def test_a_moved_discuss_stage_refuses_to_finalize(tmp_path) -> None:
    """Recovery attaches the stage the launch named, or none at all."""

    service, request, execution = _discuss_app(tmp_path)
    execution.checkpoint_stage("", str(tmp_path / "stage"))
    (Path(execution.stage_root) / "workspace").mkdir(parents=True)
    discuss_module._record_discuss_finalization_context(
        _retained_context(service, request, execution)
    )
    execution.checkpoint_stage("", str(tmp_path / "somewhere-else"))

    with pytest.raises(ValueError, match="belongs to another stage"):
        discuss_module._load_discuss_finalization_context(service, request, execution)


@pytest.mark.asyncio
async def test_a_discard_interrupted_before_its_receipt_still_warns_once(tmp_path) -> None:
    """The crash window between the warning and its guard is the real one.

    A completed discard is guarded by its receipt. A discard that died after
    warning has no receipt, so the next recovery runs the whole thing again --
    and must not tell the human twice, nor stay silent because an earlier
    attempt already spoke.
    """

    service, request, execution = _discuss_app(tmp_path)
    execution.checkpoint_stage("", str(tmp_path / "stage"))
    (Path(execution.stage_root) / "workspace").mkdir(parents=True)
    context = _retained_context(service, request, execution)
    discuss_module._record_discuss_finalization_context(context)
    (Path(execution.stage_root) / "workspace" / "patch.json").write_text(
        '{"operations": []}', encoding="utf-8"
    )

    class _DiedBeforeItsReceipt(RuntimeError):
        pass

    original = execution.store.record_agent_task_receipt

    def die_before_the_guard(operation_id, category, *args, **kwargs):
        if category == "discuss_patch_discarded":
            raise _DiedBeforeItsReceipt
        return original(operation_id, category, *args, **kwargs)

    execution.store.record_agent_task_receipt = die_before_the_guard  # type: ignore[method-assign]
    with pytest.raises(_DiedBeforeItsReceipt):
        discuss_module._discard_discuss_patch(context)
    execution.store.record_agent_task_receipt = original  # type: ignore[method-assign]

    discuss_module._discard_discuss_patch(context)

    warnings = [
        event
        for event in execution.store.agent_task_events(execution.operation_id)
        if event.level == "warning" and "no graph authority" in event.message
    ]
    assert len(warnings) == 1
    discarded = [
        receipt
        for receipt in execution.store.agent_task_receipts(execution.operation_id)
        if receipt.category == "discuss_patch_discarded"
    ]
    assert len(discarded) == 1


@pytest.mark.asyncio
async def test_a_discarded_patch_is_the_one_the_host_proved(tmp_path) -> None:
    """Discuss keeps a stray patch as evidence, so the evidence must be exact.

    A stage rewritten between host completion and reconnect would otherwise
    attribute unrelated text to this turn, or lose the evidence entirely.
    """

    service, request, execution = _discuss_app(tmp_path)
    execution.checkpoint_stage("", str(tmp_path / "stage"))
    workspace = Path(execution.stage_root) / "workspace"
    workspace.mkdir(parents=True)
    discuss_module._record_discuss_finalization_context(
        _retained_context(service, request, execution)
    )
    recorded_patch = '{"operations": [], "note": "what the host recorded"}'
    recorded = _recorded_pass(recorded_patch, _ANSWER)
    workspace.joinpath("patch.json").write_text("something else entirely", encoding="utf-8")
    launcher = ScriptedLauncher([{}], message="must not launch")

    async for _frame in discuss_module.finalize_recorded_discuss_result(
        service, launcher, request, tmp_path / "not-used", execution, recorded
    ):
        pass

    retained = execution.store.agent_task_patch_output(execution.operation_id)
    assert retained == recorded_patch
    discarded = [
        receipt
        for receipt in execution.store.agent_task_receipts(execution.operation_id)
        if receipt.category == "discuss_patch_discarded"
    ]
    assert len(discarded) == 1
    assert discarded[0].payload["byte_length"] == len(recorded_patch.encode("utf-8"))
