"""An Experiment-loop turn that outlives its connection still arms its episode.

The loop is the owner with the most to lose from a dropped link: its result is a
joint Patch/watch admission that binds the episode to the session a later wake
resumes. A pass that finished on the host must reach that handoff on the task
that opened it, once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rcp.runs.tasks.experiment_loop as loop_module
from rcp.storage import AppStore

from .helpers import append_fixture_patch, seed_patch
from .helpers import create_named_app as create_app
from .test_experiment_loop_agent_io import (
    _execution,
    _experiment_patch,
    _loop_request,
)
from .test_work_agent_io import _recorded_pass

_ANSWER = "Inspected the bounded work."
_EPISODE_ID = "00000000-0000-4000-8000-0000000000a1"
_ROLE = loop_module.EXPERIMENT_LOOP_FINALIZATION_CONTEXT_ROLE


def _watch_handoff(watcher_cwd: Path) -> str:
    return json.dumps(
        {
            "external": [
                {
                    "check_command": "false",
                    "log_path": str(watcher_cwd / "detached.log"),
                    "cwd": str(watcher_cwd),
                }
            ],
            "graph": [],
        }
    )


async def _retained_loop_turn(
    manifest,
    tmp_path: Path,
    *,
    operation_id: str = "loop-recorded",
    episode_context: str | None = None,
):
    """One staged loop turn whose launch snapshot is already written down.

    Keeps filesystem I/O local while retaining exactly what a supervised remote
    launch retains. The provider is never run: this turn is the one the host
    finished after the link dropped.
    """

    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    request = _loop_request(
        _EPISODE_ID,
        "chat-loop-recovery",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    execution = _execution(store, project_id, operation_id, request)
    turn, staged = await loop_module._stage_work_turn(
        service,
        loop_module._resolve_work_execution(service, request, execution),
        data_dir,
        execution,
    )
    execution.bind_write_scope(turn.write_scope, resumes_native_session=False)
    prompt_context = await loop_module._prepare_work_prompt_context(turn, staged)
    loop_module._record_work_finalization_context(turn, staged, role=_ROLE)
    if episode_context is None:
        loop_module._record_experiment_loop_episode_context(turn, prompt_context)
    else:
        execution.store.record_agent_task_contract(
            execution.operation_id,
            loop_module.EXPERIMENT_LOOP_EPISODE_CONTEXT_ROLE,
            episode_context,
            "0" * 64,
        )
    await turn.validator_lifecycle.close()
    return service, request, execution, turn.workspace


async def _finalize(service, request, execution, recorded) -> list[dict]:
    from .test_api import ScriptedLauncher

    launcher = ScriptedLauncher([{}], message="must not launch")
    frames = [
        frame
        async for frame in loop_module.finalize_recorded_experiment_loop_result(
            service, launcher, request, Path("/not-used"), execution, recorded
        )
    ]
    assert launcher.calls == 0
    return [json.loads(frame.removeprefix("data: ")) for frame in frames]


@pytest.mark.asyncio
async def test_a_recorded_loop_pass_arms_the_episode_of_its_own_task(
    manifest, tmp_path: Path
) -> None:
    service, request, execution, workspace = await _retained_loop_turn(manifest, tmp_path)
    (workspace / "watch.json").write_text(_watch_handoff(tmp_path), encoding="utf-8")

    events = await _finalize(service, request, execution, _recorded_pass("", _ANSWER))

    assert [item["event"] for item in events if item["event"] == "error"] == []
    assert next(item["text"] for item in events if item["event"] == "answer") == _ANSWER
    episode = execution.store.experiment_episode(_EPISODE_ID)
    assert episode is not None
    # The binding lands on the operation that opened the pass, at its own
    # invocation, so the next wake resumes this session and not another.
    assert episode.last_turn_operation_id == execution.operation_id
    assert episode.last_turn_invocation == 1
    assert episode.native_session_id == "recorded-thread"
    assert episode.stage_root == execution.stage_root
    assert len(episode.last_watcher_ids) == 1


@pytest.mark.asyncio
async def test_recorded_loop_finalization_can_resume_without_arming_twice(
    manifest, tmp_path: Path
) -> None:
    """A crash after the episode handoff must not arm a second watcher."""

    service, request, execution, workspace = await _retained_loop_turn(manifest, tmp_path)
    (workspace / "watch.json").write_text(_watch_handoff(tmp_path), encoding="utf-8")
    recorded = _recorded_pass("", _ANSWER)

    first = await _finalize(service, request, execution, recorded)
    second = await _finalize(service, request, execution, recorded)

    assert [item["event"] for item in first if item["event"] == "error"] == []
    assert [item["event"] for item in second if item["event"] == "error"] == []
    episode = execution.store.experiment_episode(_EPISODE_ID)
    assert episode is not None
    assert episode.last_turn_invocation == 1
    assert len(episode.last_watcher_ids) == 1
    armed = [
        watcher
        for watcher in execution.store.watchers(
            execution.store.agent_task(execution.operation_id).project_id
        )
        if watcher.origin_operation_id == execution.operation_id
    ]
    assert len(armed) == 1
    # Operational history is a product of this system, not a log. One
    # invocation's handoff is reported once however often recovery replays it.
    # A first invocation is its own root, so count each operation once.
    owners = {execution.operation_id, loop_module.root_experiment_loop_operation_id(execution)}
    categories = [
        receipt.category
        for owner in owners
        for receipt in execution.store.agent_task_receipts(owner)
    ]
    assert categories.count("experiment_loop_handoff_prepared") == 1
    assert categories.count("watchers_armed") == 1


@pytest.mark.asyncio
async def test_a_loop_task_routes_to_the_owner_that_retained_its_context(
    manifest, tmp_path: Path
) -> None:
    """Four owners stage alike and settle differently, so the role decides."""

    import rcp.runs.tasks.work as work_module
    from rcp.runs.remote_finalization import recorded_finalizer

    _service, _request, execution, _workspace = await _retained_loop_turn(manifest, tmp_path)

    assert (
        recorded_finalizer(execution.store, execution.operation_id)
        is loop_module.finalize_recorded_experiment_loop_result
    )
    assert (
        recorded_finalizer(execution.store, execution.operation_id)
        is not work_module.finalize_recorded_work_result
    )


@pytest.mark.asyncio
async def test_a_loop_turn_without_its_episode_context_refuses_to_finalize(
    manifest, tmp_path: Path
) -> None:
    """The episode this settles is the one its launch read, or none at all."""

    service, request, execution, workspace = await _retained_loop_turn(
        manifest, tmp_path, episode_context="{}"
    )
    (workspace / "watch.json").write_text(_watch_handoff(tmp_path), encoding="utf-8")

    with pytest.raises(ValueError, match="retained Experiment-loop episode context is invalid"):
        await _finalize(service, request, execution, _recorded_pass("", _ANSWER))


@pytest.mark.asyncio
async def test_a_moved_loop_stage_refuses_to_finalize(manifest, tmp_path: Path) -> None:
    """Recovery attaches the stage the launch named, or none at all."""

    service, request, execution, _workspace = await _retained_loop_turn(manifest, tmp_path)
    execution.checkpoint_stage("", str(tmp_path / "somewhere-else"))

    with pytest.raises(ValueError, match="belongs to another stage"):
        await _finalize(service, request, execution, _recorded_pass("", _ANSWER))


@pytest.mark.asyncio
async def test_a_loop_launch_retains_its_context_under_its_own_role(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    """A supervised launch writes the snapshot the loop's own owner reads.

    Retaining it under Work's role would let ordinary Work settle a loop turn,
    which would answer the chat and never arm the episode.
    """

    import rcp.runs.tasks.work as work_module

    from .test_experiment_loop_agent_io import _LoopLauncher

    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    request = _loop_request(
        _EPISODE_ID,
        "chat-loop-launch",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    execution = _execution(store, project_id, "loop-supervised", request)

    stage = loop_module._stage_work_turn

    async def remote_stage_work_turn(*args, **kwargs):
        # The stage stays local so the turn can run here; only the host name
        # that decides whether this turn can outlive its connection is set.
        turn, staged = await stage(*args, **kwargs)
        turn.execution_host = "recorded-host"
        return turn, staged

    monkeypatch.setattr(loop_module, "_stage_work_turn", remote_stage_work_turn)
    async for _frame in loop_module.stream_experiment_loop_task(
        service,
        _LoopLauncher("provider-session-loop-launch", tmp_path, write_handoff=True),
        request,
        data_dir,
        execution=execution,
    ):
        pass

    assert store.agent_task_contract(execution.operation_id, _ROLE) is not None
    assert (
        store.agent_task_contract(
            execution.operation_id, work_module.WORK_FINALIZATION_CONTEXT_ROLE
        )
        is None
    )
    assert (
        store.agent_task_contract(
            execution.operation_id, loop_module.EXPERIMENT_LOOP_EPISODE_CONTEXT_ROLE
        )
        is not None
    )


@pytest.mark.asyncio
async def test_a_local_loop_launch_retains_nothing_to_recover(manifest, tmp_path: Path) -> None:
    """A local provider dies with RCP, so there is no finished pass to fetch."""

    from .test_experiment_loop_agent_io import _LoopLauncher

    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    request = _loop_request(
        _EPISODE_ID,
        "chat-loop-local",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    execution = _execution(store, project_id, "loop-local", request)

    async for _frame in loop_module.stream_experiment_loop_task(
        service,
        _LoopLauncher("provider-session-loop-local", tmp_path, write_handoff=True),
        request,
        data_dir,
        execution=execution,
    ):
        pass

    assert store.agent_task_contract(execution.operation_id, _ROLE) is None
    assert (
        store.agent_task_contract(
            execution.operation_id, loop_module.EXPERIMENT_LOOP_EPISODE_CONTEXT_ROLE
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_recorded_loop_pass_cannot_correct_its_own_deliverable(
    manifest, tmp_path: Path
) -> None:
    """The provider that could answer a correction stopped when its link did."""

    service, request, execution, workspace = await _retained_loop_turn(manifest, tmp_path)
    # An observer the loop cannot validate is exactly what a live turn would
    # hand back for one correction round.
    (workspace / "watch.json").write_text(
        json.dumps({"external": [{"check_command": ""}], "graph": []}), encoding="utf-8"
    )

    events = await _finalize(service, request, execution, _recorded_pass("", _ANSWER))

    errors = [item["text"] for item in events if item["event"] == "error"]
    assert errors and "watcher handoff failed" in errors[-1]
    rejected = [
        receipt
        for receipt in execution.store.agent_task_receipts(execution.operation_id)
        if receipt.category == "watcher_handoff_rejected"
    ]
    assert len(rejected) == 1
    assert execution.store.experiment_episode(_EPISODE_ID).last_turn_operation_id is None


@pytest.mark.asyncio
async def test_a_stage_that_changed_after_the_host_finished_admits_the_recorded_patch(
    manifest, tmp_path: Path
) -> None:
    """Half this turn's admission is its Patch, and the stage is mutable.

    Between the supervisor finishing and RCP reconnecting, nothing stops the
    stage file from being rewritten or swept. What the host proved is what may
    be admitted.
    """

    from .helpers import agent_patch_json

    service, request, execution, workspace = await _retained_loop_turn(manifest, tmp_path)
    (workspace / "watch.json").write_text(_watch_handoff(tmp_path), encoding="utf-8")
    recorded_patch = agent_patch_json(_experiment_patch(invocation_ceiling=4))
    recorded = _recorded_pass(recorded_patch, _ANSWER)
    # Something rewrote the stage after the host was done with it.
    (workspace / "patch.json").write_text('{"ops": []}', encoding="utf-8")

    events = await _finalize(service, request, execution, recorded)

    assert [item["event"] for item in events if item["event"] == "error"] == []
    assert (workspace / "patch.json").read_text(encoding="utf-8") == recorded_patch
    retained = execution.store.agent_task_patch_output(execution.operation_id)
    assert retained is not None
    assert '{"ops": []}' not in retained


@pytest.mark.asyncio
async def test_a_resettled_watcher_handoff_keeps_its_own_limit(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    """Two deliverables, two policies, and the Patch's is the looser one.

    A loop Patch correction can rewrite watch.json, which resettles the watcher
    handoff. That resettlement is still watcher work and keeps the watcher
    limit; handing it the Patch limit would buy a second full provider call the
    watcher policy does not allow.
    """

    from rcp.limits import (
        EXPERIMENT_LOOP_WATCH_CORRECTION_MAX_ROUNDS,
        PATCH_CORRECTION_MAX_ROUNDS,
    )

    from .test_experiment_loop_agent_io import _LoopLauncher

    assert EXPERIMENT_LOOP_WATCH_CORRECTION_MAX_ROUNDS != PATCH_CORRECTION_MAX_ROUNDS
    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    request = _loop_request(
        _EPISODE_ID,
        "chat-loop-resettle",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    execution = _execution(store, project_id, "loop-resettle", request)

    offered: list[int] = []
    settle = loop_module._settle_watch_deliverable

    def record_the_limit(*args, **kwargs):
        offered.append(kwargs["maximum_corrections"])
        return settle(*args, **kwargs)

    # Make the handoff look rewritten after it first settled, which is the only
    # way the post-Patch resettlement runs at all.
    read = loop_module._read_watch_request
    reads: list[int] = []

    def changed_on_the_second_look(*args, **kwargs):
        reads.append(1)
        text = read(*args, **kwargs)
        if len(reads) > 1 and text is not None:
            return text.replace("detached.log", "detached-two.log")
        return text

    monkeypatch.setattr(loop_module, "_settle_watch_deliverable", record_the_limit)
    monkeypatch.setattr(loop_module, "_read_watch_request", changed_on_the_second_look)

    async for _frame in loop_module.stream_experiment_loop_task(
        service,
        _LoopLauncher("provider-session-loop-resettle", tmp_path, write_handoff=True),
        request,
        data_dir,
        execution=execution,
    ):
        pass

    # The initial settlement and the resettlement both count as watcher work.
    assert len(offered) >= 2
    assert set(offered) == {EXPERIMENT_LOOP_WATCH_CORRECTION_MAX_ROUNDS}


@pytest.mark.asyncio
async def test_a_supervised_loop_correction_is_itself_supervised(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    """A correction of a recoverable pass has to be recoverable too.

    Otherwise a link lost during the correction leaves no journal for
    reconciliation, and the operational invocation this turn already spent gets
    reattempted from the beginning.
    """

    from .test_experiment_loop_agent_io import _LoopLauncher

    data_dir = tmp_path / "data"
    app = create_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    store: AppStore = app.state.background_tasks.store
    request = _loop_request(
        _EPISODE_ID,
        "chat-loop-supervised-correction",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    execution = _execution(store, project_id, "loop-supervised-correction", request)

    stage = loop_module._stage_work_turn

    async def remote_stage_work_turn(*args, **kwargs):
        turn, staged = await stage(*args, **kwargs)
        turn.execution_host = "recorded-host"
        return turn, staged

    corrections: list[dict] = []
    stream = loop_module._stream_turn_agent_events

    def record_the_launch(turn, launcher, prompt, **kwargs):
        corrections.append(kwargs)
        return stream(turn, launcher, prompt, **kwargs)

    monkeypatch.setattr(loop_module, "_stage_work_turn", remote_stage_work_turn)
    monkeypatch.setattr(loop_module, "_stream_turn_agent_events", record_the_launch)

    launcher = _LoopLauncher("provider-session-supervised", tmp_path, write_handoff=False)

    async def invalid_then_nothing(_provider, prompt, **kwargs):
        # An observer the loop cannot validate is what sends a live turn back
        # for exactly one correction round.
        Path(kwargs["cwd"]).joinpath("watch.json").write_text(
            json.dumps({"external": [{"check_command": ""}], "graph": []}), encoding="utf-8"
        )
        async for event in _LoopLauncher.stream(launcher, _provider, prompt, **kwargs):
            yield event

    launcher.stream = invalid_then_nothing  # type: ignore[assignment]

    async for _frame in loop_module.stream_experiment_loop_task(
        service, launcher, request, data_dir, execution=execution
    ):
        pass

    assert corrections, "the invalid handoff should have asked for a correction"
    assert all(item["supervise_remote"] is True for item in corrections)
