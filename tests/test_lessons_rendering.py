from __future__ import annotations

import uuid
from datetime import datetime, timedelta

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
    lessons = [
        store.add_lesson(
            project,
            f"{index}:" + "界" * 590,
            user_id=owner.user_id,
            display_name=owner.display_name,
        )
        for index in range(80)
    ]
    now = datetime.fromisoformat(store.now())
    with store.connection() as connection:
        for index, lesson in enumerate(lessons):
            connection.execute(
                "UPDATE operational_lessons SET updated_at=? WHERE lesson_id=?",
                ((now + timedelta(seconds=index)).isoformat(), lesson["lesson_id"]),
            )
            if index % 10:
                connection.execute(
                    "UPDATE operational_lessons SET author_kind='agent', operation_id=?, "
                    "user_id=NULL, display_name=NULL, human_owned=0 WHERE lesson_id=?",
                    (execution.operation_id, lesson["lesson_id"]),
                )
    lessons = store.list_lessons(project)
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
    groups = [
        [lesson for lesson in included if lesson["human_owned"] is human_owned]
        for human_owned in (True, False)
    ]
    for group in groups:
        assert len(group) > 1
        newest_first = sorted(group, key=lambda lesson: lesson["updated_at"], reverse=True)
        positions = [content.index(lesson["lesson_id"]) for lesson in newest_first]
        assert positions == sorted(positions)
    assert max(content.index(lesson["lesson_id"]) for lesson in groups[0]) < min(
        content.index(lesson["lesson_id"]) for lesson in groups[1]
    )
    for lesson in included:
        assert lesson["text"] in content
