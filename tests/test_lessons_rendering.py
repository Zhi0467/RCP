from __future__ import annotations

import uuid

import pytest

from rcp.runs.tasks.discuss import stream_discuss_run
from rcp.runs.tasks.work import stream_work_run
from rcp.service import RunRequest

from .helpers import create_named_app
from .test_api import ScriptedLauncher, _chat_task_execution


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["discuss", "work"])
@pytest.mark.parametrize("unusable", [False, True])
async def test_launch_admits_shared_memory_and_points_to_live_index(
    manifest, tmp_path, mode, unusable
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    request = RunRequest(
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Inspect context.",
        run_truth_scope=["repo-a"],
        mode=mode,
    )
    execution = _chat_task_execution(app, request, "memory-launch")
    if unusable:
        from pathlib import Path

        from rcp.agents.memory_files import prepare_project_memory

        shared, _ = prepare_project_memory(execution.store, app.state.default_project_id)
        index = Path(shared) / "MEMORY.md"
        index.unlink()
        index.mkdir()
    launcher = ScriptedLauncher([{}], message="Context read.")
    stream = stream_discuss_run if mode == "discuss" else stream_work_run
    frames = [
        frame
        async for frame in stream(
            app.state.service, launcher, request, tmp_path / "data", execution=execution
        )
    ]
    assert not any('"event":"error"' in frame for frame in frames), frames
    assert launcher.calls == 1
    shared = tmp_path / "data" / "memory" / app.state.default_project_id / "shared"
    assert shared.is_dir()
    assert str(shared / "MEMORY.md") in launcher.prompts[0]
    if unusable:
        assert "unusable" in launcher.prompts[0] and "repair" in launcher.prompts[0]
    assert not (launcher.workspaces[0] / "lessons.md").exists()
    if mode == "work":
        assert str(shared) in launcher.launch_kwargs[0]["write_scope"].writable_roots
    else:
        assert shared in launcher.launch_kwargs[0]["write_dirs"]


@pytest.mark.asyncio
async def test_discuss_memory_failure_is_reported_to_chat(manifest, tmp_path, monkeypatch):
    from rcp.runs.tasks import discuss
    from rcp.transport.state import StateUnavailable

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    request = RunRequest(
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Inspect context.",
        run_truth_scope=["repo-a"],
        mode="discuss",
    )
    execution = _chat_task_execution(app, request, "memory-failure")
    launcher = ScriptedLauncher([{}], message="Must not launch.")

    def fail(*args):
        raise StateUnavailable("memory base unavailable")

    # Fail the write-root preparation, after prompt preparation has succeeded.
    monkeypatch.setattr(discuss, "prepare_project_memory", fail)
    frames = [
        frame
        async for frame in discuss.stream_discuss_run(
            app.state.service, launcher, request, tmp_path / "data", execution=execution
        )
    ]
    assert any(
        '"event":"error"' in frame and "memory base unavailable" in frame for frame in frames
    )
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_discuss_excluded_task_has_no_memory_write_root(manifest, tmp_path, monkeypatch):
    from rcp.runs.tasks import discuss

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    request = RunRequest(
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Inspect context.",
        run_truth_scope=["repo-a"],
        mode="discuss",
    )
    execution = _chat_task_execution(app, request, "excluded-memory")
    with execution.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='artifact_edit' WHERE operation_id=?",
            (execution.operation_id,),
        )
    launcher = ScriptedLauncher([{}], message="Context read.")

    def refuse(*args):
        raise AssertionError("Excluded task must not prepare memory")

    # Isolate memory admission from the ordinary chat-only stage binding.
    monkeypatch.setattr(discuss, "_prepare_local_chat_workspace", lambda stage, **_: stage)
    monkeypatch.setattr(discuss, "prepare_project_memory", refuse)
    frames = [
        frame
        async for frame in discuss.stream_discuss_run(
            app.state.service, launcher, request, tmp_path / "data", execution=execution
        )
    ]
    assert not any('"event":"error"' in frame for frame in frames), frames
    assert launcher.calls == 1
    assert launcher.launch_kwargs[0]["write_dirs"] == []
