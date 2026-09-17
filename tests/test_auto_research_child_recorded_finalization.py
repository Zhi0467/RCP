"""A child Work turn that outlives its connection settles on its own task.

A lost link is not a lost child. The result lands on the operation that opened
the pass, under the authority the parent episode already gave it, and no second
child is admitted to go and fetch it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rcp.runs.tasks.auto_research_child_work as child_module
import rcp.runs.tasks.work as work_module

from .helpers import agent_patch_json, seed_patch
from .test_api import ScriptedLauncher
from .test_work_agent_io import _decided_output, _one_result_app

_ANSWER = "The child check is complete."
_ROLE = child_module.AUTO_RESEARCH_CHILD_FINALIZATION_CONTEXT_ROLE


async def _retained_child_turn(tmp_path: Path):
    """One staged child turn whose launch snapshot is already written down."""

    service, request, execution = _one_result_app(tmp_path)
    resolved = work_module._resolve_work_execution(service, request, execution)
    turn, staged = await work_module._stage_work_turn(
        service, resolved, tmp_path / "data", execution
    )
    # Keep filesystem I/O local while retaining exactly what a supervised remote
    # launch retains, under this owner's own role.
    work_module._record_work_finalization_context(turn, staged, role=_ROLE)
    execution.bind_write_scope(turn.write_scope, resumes_native_session=False)
    await work_module._prepare_work_prompt_context(turn, staged)
    await turn.validator_lifecycle.close()
    return service, request, execution


def _recorded_child_pass():
    from .test_work_agent_io import _recorded_pass

    return _recorded_pass(agent_patch_json(seed_patch()), _ANSWER)


@pytest.mark.asyncio
async def test_a_recorded_child_pass_settles_on_its_own_task(tmp_path) -> None:
    service, request, execution = await _retained_child_turn(tmp_path)
    launcher = ScriptedLauncher([{}], message="must not launch")

    frames = [
        frame
        async for frame in child_module.finalize_recorded_auto_research_child_work_result(
            service, launcher, request, tmp_path / "not-used", execution, _recorded_child_pass()
        )
    ]

    decided = _decided_output(frames)
    assert '"status":"applied"' in str(decided[0]["text"])
    assert decided[-1] == {"event": "done"}
    assert service.history.state().revision == 2
    # The task's own durable result is read off this frame, so a recovered turn
    # that never emits one completes with no answer of its own.
    events = [json.loads(frame.removeprefix("data: ")) for frame in frames]
    assert [item["text"] for item in events if item["event"] == "answer"] == [_ANSWER]
    # No provider is asked for anything: the turn already happened.
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_recorded_child_finalization_can_resume_without_applying_twice(tmp_path) -> None:
    """A crash after the child's own write must not repeat that write."""

    service, request, execution = await _retained_child_turn(tmp_path)
    launcher = ScriptedLauncher([{}], message="must not launch")
    recorded = _recorded_child_pass()

    for _attempt in range(2):
        frames = [
            frame
            async for frame in child_module.finalize_recorded_auto_research_child_work_result(
                service, launcher, request, tmp_path / "not-used", execution, recorded
            )
        ]
        assert _decided_output(frames)[-1] == {"event": "done"}

    transcript = [
        json.loads(line)
        for line in service.chat_path(
            request.chat_id, chat_scope=request.chat_scope, node_id=request.node_id
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert service.history.state().revision == 2
    assert [(item["operationId"], item["role"]) for item in transcript] == [
        (execution.operation_id, "user"),
        (execution.operation_id, "assistant"),
    ]
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_a_child_task_never_routes_to_ordinary_work(tmp_path) -> None:
    """Two owners stage alike and settle differently, so the role decides."""

    from rcp.runs.remote_finalization import recorded_finalizer

    _service, _request, execution = await _retained_child_turn(tmp_path)
    store = execution.store

    assert (
        recorded_finalizer(store, execution.operation_id)
        is child_module.finalize_recorded_auto_research_child_work_result
    )
    assert (
        recorded_finalizer(store, execution.operation_id)
        is not work_module.finalize_recorded_work_result
    )
    # The kind alone says project_chat, which ordinary Work, Discuss, the
    # Experiment loop and this owner all share.
    assert store.agent_task(execution.operation_id).kind == "project_chat"


@pytest.mark.asyncio
async def test_ordinary_work_cannot_load_a_child_launch_snapshot(tmp_path) -> None:
    service, request, execution = await _retained_child_turn(tmp_path)

    with pytest.raises(ValueError, match="no retained finalization context"):
        work_module._load_work_finalization_context(service, request, execution)
