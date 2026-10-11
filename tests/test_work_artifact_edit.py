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
async def test_work_comment_on_another_stage_edits_a_copy_and_publishes_once(
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
    # This turn runs on its chat's own stage, not the origin's, so the commented
    # artifact is copied to its turn-relative path here.
    launcher = ScriptedLauncher(
        [
            {
                "turns/origin/artifacts/chart.html": "<p>updated</p>",
                "turns/work-edit/artifacts/other.txt": "new",
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
    assert Path(pointer["path"]) == launcher.workspaces[0] / "turns/origin/artifacts/chart.html"
    assert launcher.launch_kwargs[0]["capability"] == "work_auto"
    artifacts = [event.artifact for event in events if event.event == "artifact"]
    assert sorted(item.name for item in artifacts) == ["chart.html", "other.txt"]
    edited = next(item for item in artifacts if item.name == "chart.html")
    assert edited.artifact_id == source.artifact_id
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>updated</p>"
    assert execution.store.agent_task_has_receipt("work-edit", "artifact_edit_published")


async def _turn(app, launcher, request, operation_id, tmp_path):
    from rcp.runs.tasks.discuss import stream_discuss_run

    run = stream_work_run if request.mode == "work" else stream_discuss_run
    execution = _chat_task_execution(app, request, operation_id)
    execution.store.record_agent_task_receipt(
        operation_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in run(app.state.service, launcher, request, tmp_path / "data", execution)
    ]
    artifacts = [event.artifact.model_dump(mode="json") for event in events if event.artifact]
    execution.store.complete_agent_task(
        operation_id, applied_revision=None, result={"artifacts": artifacts}
    )
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["discuss", "work"])
async def test_a_later_turn_changing_an_earlier_artifact_publishes_its_next_version(
    manifest, tmp_path, mode
):
    from rcp.api.tasks import _read_agent_artifact_bytes
    from rcp.service import RunRequest

    from .helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    store = app.state.background_tasks.store
    first = RunRequest(
        provider="codex",
        reasoning="medium",
        model="",
        run_on="laptop",
        chat_scope="project",
        chat_id="0f6c2a6e-3b0d-4a4e-9a51-3d3f3c1f5b10",
        message="Draw it",
        mode="discuss",
        run_truth_scope=["repo-a"],
    )
    page = "turns/first/artifacts/page.html"
    events = await _turn(
        app, ScriptedLauncher([{page: "<p>v1</p>"}], message="Drew it."), first, "first", tmp_path
    )
    [artifact] = [event.artifact for event in events if event.event == "artifact"]
    later = first.model_copy(update={"mode": mode, "message": "Fix the label"})
    events = await _turn(
        app, ScriptedLauncher([{page: "<p>v2</p>"}], message="Fixed."), later, "second", tmp_path
    )
    assert not [event.text for event in events if event.event == "error"]
    assert [event.artifact.artifact_id for event in events if event.event == "artifact"] == [
        artifact.artifact_id
    ]
    assert store.read_artifact_bytes(artifact.artifact_id) == b"<p>v2</p>"
    # The turn that published the version serves it from its own card.
    _, data = _read_agent_artifact_bytes(
        store, app.state.default_project_id, "second", artifact.artifact_id, "download"
    )
    assert data == b"<p>v2</p>"
    # After Undo, the next turn edits what the human sees; an unchanged turn records nothing.
    store.undo_artifact(artifact.artifact_id)
    launcher = ScriptedLauncher([{}], message="Nothing to change.")
    await _turn(app, launcher, later.model_copy(update={"mode": "discuss"}), "third", tmp_path)
    assert (launcher.workspaces[0] / page).read_bytes() == b"<p>v1</p>"
    assert store.agent_task_artifact_edit_outcomes(["third"]) == {}
