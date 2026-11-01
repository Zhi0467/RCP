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
async def test_launch_admits_shared_memory_and_points_to_live_index(manifest, tmp_path, mode):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    request = RunRequest(
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Inspect context.",
        run_truth_scope=["repo-a"],
        mode=mode,
    )
    execution = _chat_task_execution(app, request, "memory-launch")
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
    assert not (launcher.workspaces[0] / "lessons.md").exists()
    if mode == "work":
        assert str(shared) in launcher.launch_kwargs[0]["write_scope"].writable_roots
    else:
        assert shared in launcher.launch_kwargs[0]["write_dirs"]
