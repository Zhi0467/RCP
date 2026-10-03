from __future__ import annotations

import uuid

import pytest

from rcp.limits import LESSONS_RENDER_MAX_BYTES
from rcp.runs.tasks.discuss import stream_discuss_run
from rcp.runs.tasks.work import stream_work_run
from rcp.service import RunRequest

from .helpers import create_named_app
from .test_api import ScriptedLauncher, _chat_task_execution


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["discuss", "work"])
async def test_launch_stages_bounded_lessons_in_priority_order(manifest, tmp_path, mode):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project = app.state.default_project_id
    owner = store.local_owner
    request = RunRequest(
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Inspect context.",
        run_truth_scope=["repo-a"],
        mode=mode,
    )
    execution = _chat_task_execution(app, request, "lessons-launch")
    for index in range(80):
        store.add_lesson(
            project,
            f"{index}:" + "界" * 590,
            user_id=owner.user_id,
            display_name=owner.display_name,
        )
    lessons = store.list_lessons(project)
    with store.connection() as connection:
        connection.execute(
            "UPDATE operational_lessons SET author_kind='agent', operation_id=?, "
            "user_id=NULL, display_name=NULL, human_owned=0 WHERE lesson_id=?",
            (execution.operation_id, lessons[0]["lesson_id"]),
        )
    launcher = ScriptedLauncher([{}], message="Context read.")
    stream = stream_discuss_run if mode == "discuss" else stream_work_run
    frames = [
        frame
        async for frame in stream(
            app.state.service, launcher, request, tmp_path / "data", execution=execution
        )
    ]
    assert not any('"event":"error"' in frame for frame in frames)
    assert launcher.calls == 1
    path = launcher.workspaces[0] / "lessons.md"
    content = path.read_text()
    assert str(path) in launcher.prompts[0]
    assert path.stat().st_size <= LESSONS_RENDER_MAX_BYTES
    included = [lesson for lesson in lessons if lesson["lesson_id"] in content]
    assert 0 < len(included) < len(lessons)
    assert str(len(lessons) - len(included)) in content.splitlines()[-1]
    assert lessons[0]["lesson_id"] not in content
    for lesson in included:
        assert lesson["text"] in content
