from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.session_master import record_session_master
from rcp.runs.tasks.artifact_edit import stream_artifact_edit_run
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord

from .helpers import create_named_app
from .test_unified_artifacts import _publish_artifact, _stored_artifact


def _setup(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    source = _stored_artifact(app, "origin", "chart.html", b"<p>original</p>")
    base = _publish_artifact(store, source.artifact_id, b"<p>base</p>")
    stage = tmp_path / "data" / "run-stage" / "stage"
    workspace = stage / "workspace"
    workspace.mkdir(parents=True)
    origin_request = RunRequest(
        provider="codex",
        reasoning="medium",
        model="",
        run_on="laptop",
        chat_scope="project",
        chat_id="chat-edit",
        message="Create chart",
        mode="discuss",
        session_id="native-session",
    )
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="origin",
            project_id=app.state.default_project_id,
            kind="project_chat",
            status="succeeded",
            request=origin_request.model_dump(mode="json"),
            native_session_id="native-session",
            stage_root=str(stage),
            created_at=now,
            updated_at=now,
            status_message="Done",
        )
    )
    store.record_chat_stage_layout("origin", stage_root=str(stage), workspace_root=str(workspace))
    request = origin_request.model_copy(update={"message": "Update the chart"})
    request = RunRequest.model_validate(
        {
            **request.model_dump(mode="json"),
            "artifact_context": {"operation_id": "origin", "artifact_id": source.artifact_id},
            "artifact_edit": {
                "artifact_id": source.artifact_id,
                "base_version": base.version_id,
                "source_name": source.name,
                "media_type": source.media_type,
                "operation_id": "edit",
                "staged_scope_id": "edit",
                "origin_operation_id": "origin",
                "launch_kind": "revoking",
                "stage_root": str(stage),
            },
        }
    )
    record = AgentTaskRecord(
        operation_id="edit",
        project_id=app.state.default_project_id,
        kind="artifact_edit",
        status="running",
        request=request.model_dump(mode="json"),
        native_session_id="native-session",
        stage_root=str(stage),
        created_at=now,
        updated_at=now,
        status_message="Editing",
    )
    store.create_artifact_edit_task(record)
    execution = AgentTaskExecution(
        operation_id="edit",
        store=store,
        control=AgentProcessControl(),
        stage_root=str(stage),
    )
    return app, request, execution, source, workspace


class _EditLauncher:
    def __init__(self, *, before_edit=None, fail=False, session="native-session", late_event=None):
        self.before_edit = before_edit
        self.fail = fail
        self.session = session
        self.late_event = late_event
        self.calls = []
        self.before = []

    async def stream(self, provider, prompt, **kwargs):
        self.calls.append((provider, kwargs))
        folder = Path(kwargs["cwd"]) / "turns" / "edit" / "artifacts"
        target = folder / "chart.html"
        self.before.append(target.read_bytes())
        if self.before_edit:
            self.before_edit()
        target.write_bytes(b"<p>edited</p>")
        (folder / "extra.txt").write_text("additional artifact")
        yield AgentEvent(event="session", session_id=self.session)
        if self.fail:
            yield AgentEvent(event="error", text="Provider failed")
            return
        yield AgentEvent(event="answer", text="Updated the chart.")
        if self.late_event:
            yield AgentEvent(event=self.late_event, text="Interrupted after answer")
        yield AgentEvent(event="done")


async def _run(app, request, execution, launcher, tmp_path):
    return [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_artifact_edit_run(
            app.state.service,
            launcher,
            request,
            tmp_path / "data",
            execution,
        )
    ]


@pytest.mark.parametrize("undo", [False, True])
@pytest.mark.asyncio
async def test_revoking_edit_uses_recorded_workspace_and_publishes_cas(
    manifest,
    tmp_path,
    monkeypatch,
    undo,
):
    import rcp.runs.tasks.artifact_edit as runtime

    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    store = execution.store
    calls = []
    compose = runtime.compose

    def capture_compose(*args, **kwargs):
        calls.append((args, kwargs))
        return compose(*args, **kwargs)

    monkeypatch.setattr(runtime, "compose", capture_compose)
    (workspace / "patch.json").write_text("stale patch")
    launcher = _EditLauncher(
        before_edit=(lambda: store.undo_artifact(source.artifact_id)) if undo else None
    )
    events = await _run(app, request, execution, launcher, tmp_path)
    assert not any(event.event == "error" for event in events)
    assert events[-1].event == "done"
    assert launcher.before == [b"<p>base</p>"]
    kwargs = launcher.calls[0][1]
    assert kwargs["cwd"] == workspace
    assert kwargs["capability"] == "discuss"
    assert kwargs["write_dirs"] == []
    assert kwargs["write_scope"] is None
    assert calls[0][0] == ("report",)
    assert calls[0][1]["master"] is None
    assert not (workspace / "patch.json").exists()
    assert store.agent_task_contract("edit", "session_master") is None
    artifacts = [event.artifact for event in events if event.event == "artifact"]
    assert {artifact.name for artifact in artifacts} == (
        {"extra.txt", "chart.html"} if undo else {"extra.txt"}
    )
    assert store.read_artifact_bytes(source.artifact_id) == (
        b"<p>original</p>" if undo else b"<p>edited</p>"
    )
    if undo:
        edited = next(artifact for artifact in artifacts if artifact.name == "chart.html")
        assert store.read_artifact_bytes(edited.artifact_id) == b"<p>edited</p>"


@pytest.mark.asyncio
async def test_failed_edit_keeps_staged_bytes_and_retry_reuses_them(manifest, tmp_path):
    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    failed = _EditLauncher(fail=True)
    events = await _run(app, request, execution, failed, tmp_path)
    assert any(event.event == "error" for event in events)
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>base</p>"
    assert (
        workspace / "turns" / "edit" / "artifacts" / source.name
    ).read_bytes() == b"<p>edited</p>"
    store = execution.store
    store.fail_agent_task(execution.operation_id, "Provider failed")
    parent = store.agent_task(execution.operation_id)
    record = parent.model_copy(
        update={
            "operation_id": "edit-retry",
            "status": "running",
            "attempt": 2,
            "parent_operation_id": parent.operation_id,
        }
    )
    store.create_artifact_edit_task(record, continuation_cause="retry")
    execution = AgentTaskExecution(
        operation_id="edit-retry",
        store=store,
        control=AgentProcessControl(),
        stage_root=execution.stage_root,
        continuation="retry",
    )
    retry = _EditLauncher()
    events = await _run(app, request, execution, retry, tmp_path)
    assert retry.before == [b"<p>edited</p>"]
    assert events[-1].event == "done"
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>edited</p>"


@pytest.mark.asyncio
async def test_mismatched_native_session_never_publishes(manifest, tmp_path):
    app, request, execution, source, _workspace = _setup(manifest, tmp_path)
    events = await _run(app, request, execution, _EditLauncher(session="wrong-session"), tmp_path)
    assert any(event.event == "error" for event in events)
    assert not any(event.event == "done" for event in events)
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>base</p>"


def test_background_edit_start_and_retry_preserve_admitted_binding(manifest, tmp_path, monkeypatch):
    from .helpers import fabricated_authorizer

    app, request, execution, _source, _workspace = _setup(manifest, tmp_path)
    tasks = app.state.background_tasks
    tasks.store.fail_agent_task(execution.operation_id, "Setup settled")
    request = request.model_copy(
        update={
            "artifact_edit": request.artifact_edit.model_copy(
                update={
                    "operation_id": "background-edit",
                    "staged_scope_id": "background-edit",
                }
            )
        }
    )
    monkeypatch.setattr(tasks, "admit_provider_task", lambda *_args, **_kwargs: None)
    spawned = []

    def spawn(record, admitted, *, continuation, parent=None):
        spawned.append((record.operation_id, continuation))
        return record

    monkeypatch.setattr(tasks, "_spawn_record", spawn)
    task = tasks.start(
        app.state.default_project_id,
        "artifact_edit",
        request,
        authorized_by=fabricated_authorizer("Editor"),
    )
    assert task.kind == "artifact_edit"
    assert task.native_session_id == request.session_id
    assert task.stage_root == execution.stage_root
    assert task.request["artifact_edit"] == request.artifact_edit.model_dump(mode="json")
    tasks.store.fail_agent_task(task.operation_id, "Provider failed")
    retry = tasks.retry(task.operation_id)
    assert retry.operation_id != task.operation_id
    assert retry.parent_operation_id == task.operation_id
    assert retry.stage_root == task.stage_root
    assert retry.request["artifact_edit"] == task.request["artifact_edit"]
    assert spawned == [(task.operation_id, "fresh"), (retry.operation_id, "retry")]


def test_comment_route_refuses_busy_session_but_undo_remains_available(manifest, tmp_path):

    from fastapi.testclient import TestClient

    app, _request, execution, source, _workspace = _setup(manifest, tmp_path)
    record_session_master(
        execution.store,
        "origin",
        "Operational master",
    )
    client = TestClient(app)
    path = f"/api/projects/{app.state.default_project_id}/artifacts/{source.artifact_id}"
    refused = client.post(path + "/comments", json={"message": "Change this chart"})
    assert refused.status_code == 409
    assert isinstance(refused.json()["detail"], str)
    undone = client.post(path + "/undo")
    assert undone.status_code == 200
    assert execution.store.read_artifact_bytes(source.artifact_id) == b"<p>original</p>"
    assert execution.store.agent_task(execution.operation_id).status == "running"
    original = client.post(path + "/undo")
    assert original.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("late_event", [None, "error", "paused"])
async def test_ordinary_discuss_edit_preserves_frozen_master(
    manifest, tmp_path, monkeypatch, late_event
):

    import rcp.runs.tasks.discuss as discuss
    from rcp.runs.session_master import session_master_label

    from .helpers import append_fixture_patch, seed_patch

    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    append_fixture_patch(app.state.service, seed_patch())
    store = execution.store
    store.fail_agent_task("edit", "Fixture replaced with Discuss admission")
    frozen = "Frozen child boundary and original chat context."
    digest = record_session_master(store, "origin", frozen)
    request = request.model_copy(
        update={
            "artifact_edit": request.artifact_edit.model_copy(
                update={
                    "operation_id": "discuss-edit",
                    "launch_kind": "discuss",
                    "master_operation_id": "origin",
                    "master_sha256": digest,
                    "master_path": session_master_label("chat-master", frozen),
                }
            )
        }
    )
    parent = store.agent_task("edit")
    store.create_artifact_edit_task(
        parent.model_copy(
            update={
                "operation_id": "discuss-edit",
                "kind": "project_chat",
                "status": "running",
                "request": request.model_dump(mode="json"),
            }
        )
    )
    execution = AgentTaskExecution(
        operation_id="discuss-edit",
        store=store,
        control=AgentProcessControl(),
        stage_root=execution.stage_root,
    )
    masters = []
    render = discuss.PromptFactory.discuss_turn_prompt

    def capture(**kwargs):
        masters.append(kwargs["master"])
        return render(**kwargs)

    monkeypatch.setattr(discuss.PromptFactory, "discuss_turn_prompt", capture)
    launcher = _EditLauncher(late_event=late_event)
    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in discuss.stream_discuss_run(
            app.state.service,
            launcher,
            request,
            tmp_path / "data",
            execution,
        )
    ]
    if late_event:
        assert not any(event.event == "done" for event in events)
    else:
        assert not [event.text for event in events if event.event == "error"]
        assert events[-1].event == "done"
    assert launcher.calls[0][1]["capability"] == "discuss"
    assert launcher.calls[0][1]["cwd"] == workspace
    assert masters and not masters[0].bootstrap
    assert Path(masters[0].path).read_text() == frozen
    assert store.agent_task_contract("origin", "session_master") == frozen
    assert store.agent_task_contract("discuss-edit", "session_master") is None
    assert store.read_artifact_bytes(source.artifact_id) == (
        b"<p>base</p>" if late_event else b"<p>edited</p>"
    )


@pytest.mark.asyncio
async def test_edit_of_revoking_edit_artifact_keeps_underlying_chat_workspace(manifest, tmp_path):
    app, request, execution, _source, workspace = _setup(manifest, tmp_path)
    store = execution.store
    first = await _run(app, request, execution, _EditLauncher(), tmp_path)
    extra = next(event.artifact for event in first if event.event == "artifact")
    store.complete_agent_task("edit", applied_revision=None, result={})
    artifact = store.artifact(extra.artifact_id)
    request = request.model_copy(
        update={
            "artifact_context": request.artifact_context.model_copy(
                update={
                    "operation_id": "edit",
                    "artifact_id": extra.artifact_id,
                }
            ),
            "artifact_edit": request.artifact_edit.model_copy(
                update={
                    "artifact_id": extra.artifact_id,
                    "base_version": artifact.current_version,
                    "source_name": extra.name,
                    "media_type": extra.media_type,
                    "operation_id": "edit-again",
                    "staged_scope_id": "edit-again",
                    "origin_operation_id": "edit",
                }
            ),
        }
    )
    prior = store.agent_task("edit")
    store.create_artifact_edit_task(
        prior.model_copy(
            update={
                "operation_id": "edit-again",
                "status": "running",
                "request": request.model_dump(mode="json"),
            }
        )
    )
    execution = AgentTaskExecution(
        operation_id="edit-again",
        store=store,
        control=AgentProcessControl(),
        stage_root=prior.stage_root,
    )

    class EditExtra:
        async def stream(self, _provider, _prompt, **kwargs):
            assert kwargs["cwd"] == workspace
            target = workspace / "turns" / "edit-again" / "artifacts" / "extra.txt"
            assert target.read_text() == "additional artifact"
            target.write_text("edited extra")
            yield AgentEvent(event="session", session_id="native-session")
            yield AgentEvent(event="answer", text="Edited extra artifact.")
            yield AgentEvent(event="done")

    events = await _run(app, request, execution, EditExtra(), tmp_path)
    assert events[-1].event == "done"
    assert store.read_artifact_bytes(extra.artifact_id) == b"edited extra"


def test_edit_origin_chain_stops_at_its_independent_fresh_stage(manifest, tmp_path):
    from rcp.runs.tasks.artifact_edit import _open_stage

    app, request, execution, _source, _workspace = _setup(manifest, tmp_path)
    store = execution.store
    store.fail_agent_task("edit", "Fixture replaced with fresh origin")
    fresh_root = tmp_path / "fresh-edit"
    fresh_root.mkdir()
    fresh_request = request.model_copy(
        update={
            "artifact_edit": request.artifact_edit.model_copy(
                update={
                    "operation_id": "fresh-origin",
                    "fresh_session": True,
                    "stage_root": None,
                }
            ),
        }
    )
    prior = store.agent_task("edit")
    store.create_artifact_edit_task(
        prior.model_copy(
            update={
                "operation_id": "fresh-origin",
                "status": "succeeded",
                "stage_root": str(fresh_root),
                "request": fresh_request.model_dump(mode="json"),
            }
        )
    )
    current = request.model_copy(
        update={
            "artifact_edit": request.artifact_edit.model_copy(
                update={
                    "operation_id": "after-fresh",
                    "origin_operation_id": "fresh-origin",
                    "stage_root": str(fresh_root),
                }
            ),
        }
    )
    store.create_artifact_edit_task(
        prior.model_copy(
            update={
                "operation_id": "after-fresh",
                "status": "running",
                "stage_root": str(fresh_root),
                "request": current.model_dump(mode="json"),
            }
        )
    )
    execution = AgentTaskExecution(
        operation_id="after-fresh",
        store=store,
        control=AgentProcessControl(),
        stage_root=str(fresh_root),
    )
    _local, _remote, workspace, _machine = _open_stage(
        app.state.service,
        current,
        tmp_path / "data",
        execution,
    )
    assert workspace == fresh_root
