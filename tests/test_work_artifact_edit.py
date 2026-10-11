from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.runs.tasks.work import stream_work_run

from .helpers import append_fixture_patch, seed_patch
from .test_api import ScriptedLauncher, _chat_task_execution
from .test_artifact_edit_runtime import _setup


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_work_edit_stages_editable_copy_and_publishes_once(
    manifest, tmp_path, monkeypatch, late_failure
):
    app, request, previous, source, _ = _setup(manifest, tmp_path)
    append_fixture_patch(app.state.service, seed_patch())
    previous.store.fail_agent_task(previous.operation_id, "Replaced with composer Work")
    request = request.model_copy(
        update={
            "mode": "work",
            "session_id": None,
            "run_truth_scope": ["repo-a"],
            "artifact_edit": request.artifact_edit.model_copy(
                update={"operation_id": "work-edit", "launch_kind": "discuss", "stage_root": None}
            ),
        }
    )
    execution = _chat_task_execution(app, request, "work-edit")
    pointers = []
    from rcp.runs.tasks import work

    render = work.PromptFactory.work_turn_prompt

    def capture(**kwargs):
        pointers.extend(kwargs["attachments"])
        return render(**kwargs)

    monkeypatch.setattr(work.PromptFactory, "work_turn_prompt", capture)
    launcher = ScriptedLauncher(
        [
            {
                "turns/edit/artifacts/chart.html": "<p>updated</p>",
                "turns/edit/artifacts/other.txt": "new",
            }
        ],
        message="Updated the chart.",
    )
    stream = launcher.stream

    async def finish(*args, **kwargs):
        async for event in stream(*args, **kwargs):
            if late_failure and event.event == "done":
                yield AgentEvent(event="error", text="Provider failed after answering")
            yield event

    monkeypatch.setattr(launcher, "stream", finish)
    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_work_run(
            app.state.service, launcher, request, tmp_path / "data", execution
        )
    ]
    if late_failure:
        assert any(event.event == "error" for event in events)
        assert not any(event.event == "done" for event in events)
        assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>base</p>"
        return
    assert not [event.text for event in events if event.event == "error"]
    assert events[-1].event == "done"
    pointer = next(
        item for item in pointers if item.get("source_artifact_id") == source.artifact_id
    )
    assert pointer["editable"] is True
    assert Path(pointer["path"]) == launcher.workspaces[0] / "turns/edit/artifacts/chart.html"
    assert launcher.launch_kwargs[0]["capability"] == "work_auto"
    artifacts = [event.artifact for event in events if event.event == "artifact"]
    assert sorted(item.name for item in artifacts) == ["chart.html", "other.txt"]
    edited = next(item for item in artifacts if item.name == "chart.html")
    assert edited.artifact_id == source.artifact_id
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>updated</p>"
    assert execution.store.agent_task_has_receipt("work-edit", "artifact_edit_published")
