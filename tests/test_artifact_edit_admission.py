from __future__ import annotations

import io
import json
import uuid

import pytest
from PIL import Image

from rcp.runs.artifact_edit_admission import admit_artifact_edit, artifact_edit_availability
from rcp.runs.session_master import record_session_master
from rcp.service import RunRequest
from rcp.storage import AgentTaskAdmissionConflict, AgentTaskRecord, EpisodeRecord

from .helpers import create_named_app
from .test_unified_artifacts import _stored_artifact


@pytest.fixture
def edit_origin(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    stage = tmp_path / "stage"
    stage.mkdir()
    task = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=app.state.default_project_id,
        kind="project_chat",
        status="succeeded",
        created_at=store.now(),
        updated_at=store.now(),
        status_message="Completed",
        native_session_id="native-session",
        stage_root=str(stage),
        request={
            "chat_scope": "project",
            "chat_id": str(uuid.uuid4()),
            "provider": "codex",
            "model": "",
            "reasoning": "medium",
            "run_on": "laptop",
        },
    )
    store.create_agent_task(task)
    return app, store, task


def _request(task, artifact, *, fresh=False):
    return RunRequest(
        message="Update the explanation.",
        artifact_context={
            "operation_id": task.operation_id,
            "artifact_id": artifact.artifact_id,
            "fresh_session": fresh,
            "selections": [],
        },
    )


@pytest.mark.parametrize(
    ("kind", "patch_kind", "shape"),
    [
        ("project_chat", "work", "discuss"),
        ("node_chat", "work", "discuss"),
        ("node_chat", "experiment_loop", "revoking"),
        ("auto_research", None, "revoking"),
    ],
)
def test_master_owner_decides_edit_shape(edit_origin, kind, patch_kind, shape):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "explanation.md", b"Original")
    request = {**task.request, "patch_kind": patch_kind} if patch_kind else task.request
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind = ?, request_json = ? WHERE operation_id = ?",
            (kind, json.dumps(request), task.operation_id),
        )
    record_session_master(store, task.operation_id, "master bytes")
    admitted = admit_artifact_edit(
        store, app.state.service, task.project_id, _request(task, artifact)
    )
    assert admitted.artifact_edit.launch_kind == shape
    assert admitted.mode == "discuss"
    assert admitted.session_id == task.native_session_id
    assert admitted.chat_id == task.request["chat_id"]
    assert (
        admitted.artifact_edit.base_version == store.artifact(artifact.artifact_id).current_version
    )


@pytest.mark.parametrize("missing", ["stage", "session", "history"])
def test_unresumable_origin_requires_explicit_fresh_session(edit_origin, missing):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "explanation.txt", b"Original")
    column = {"stage": "stage_root", "session": "native_session_id", "history": "history_only"}[
        missing
    ]
    with store.connection() as connection:
        connection.execute(
            f"UPDATE graph_runs SET {column} = ? WHERE operation_id = ?",
            (1 if missing == "history" else None, task.operation_id),
        )
    with pytest.raises(AgentTaskAdmissionConflict):
        admit_artifact_edit(store, app.state.service, task.project_id, _request(task, artifact))
    admitted = admit_artifact_edit(
        store, app.state.service, task.project_id, _request(task, artifact, fresh=True)
    )
    assert admitted.session_id is None
    assert admitted.artifact_edit.stage_root is None
    assert admitted.artifact_edit.fresh_session
    assert admitted.chat_id == task.request["chat_id"]


def test_pdf_is_refused_even_with_fresh_session(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "paper.pdf", b"%PDF-1.7\n")
    with pytest.raises(ValueError):
        admit_artifact_edit(
            store, app.state.service, task.project_id, _request(task, artifact, fresh=True)
        )


@pytest.mark.parametrize("mode", ["experiment_loop", "auto_research"])
@pytest.mark.parametrize("report", [False, True])
def test_episode_reply_thread_is_resolved_from_origin(edit_origin, mode, report):
    app, store, task = edit_origin
    episode_id = str(uuid.uuid4())
    request = dict(task.request)
    if mode == "auto_research":
        request.pop("chat_id")
    else:
        request.update(chat_scope="node", node_id="experiment", patch_kind="experiment_loop")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET episode_id = ?, request_json = ?, kind = ? "
            "WHERE operation_id = ?",
            (
                episode_id,
                json.dumps(request),
                "auto_research" if mode == "auto_research" else task.kind,
                task.operation_id,
            ),
        )
        store._insert_episode(
            connection,
            EpisodeRecord(
                episode_id=episode_id,
                project_id=task.project_id,
                mode=mode,
                control_node_id="experiment" if mode == "experiment_loop" else None,
                root_operation_id=task.operation_id,
                status="running",
                invocation_ceiling=3,
                created_at=store.now(),
                updated_at=store.now(),
            ),
        )
    record_session_master(store, task.operation_id, "master bytes")
    supplier = task
    if report:
        supplier = task.model_copy(
            update={
                "operation_id": str(uuid.uuid4()),
                "kind": "episode_report",
                "episode_id": episode_id,
                "parent_operation_id": task.operation_id,
                "request": {
                    **{
                        key: task.request[key]
                        for key in ("provider", "model", "reasoning", "run_on")
                    },
                    "episode_id": episode_id,
                },
            }
        )
        with store.connection() as connection:
            store._insert_agent_task(connection, supplier, continuation_cause="fresh")
    artifact = _stored_artifact(app, supplier.operation_id, "report.html", b"<h1>Original</h1>")
    admitted = admit_artifact_edit(
        store, app.state.service, task.project_id, _request(supplier, artifact)
    )
    assert admitted.artifact_edit.launch_kind == "revoking"
    assert admitted.artifact_edit.origin_operation_id == task.operation_id
    assert admitted.chat_id == (None if mode == "auto_research" else request["chat_id"])
    assert admitted.artifact_edit.reply_episode_id == (
        episode_id if mode == "auto_research" else None
    )


def test_text_edits_refuse_selection_gestures(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "data.json", b'{"value": 1}')
    record_session_master(store, task.operation_id, "master bytes")
    request = _request(task, artifact)
    request = RunRequest.model_validate(
        {
            **request.model_dump(mode="python"),
            "artifact_context": {
                **request.artifact_context.model_dump(mode="python"),
                "selections": [{"kind": "text", "text": "value", "comment": "Update"}],
            },
        }
    )
    with pytest.raises(ValueError):
        admit_artifact_edit(store, app.state.service, task.project_id, request)


@pytest.mark.parametrize("suffix", ["html", "png", "svg", "md", "txt", "json", "py"])
def test_viewer_offers_supported_edits_without_creating_tasks(edit_origin, suffix):
    app, store, task = edit_origin
    data = b"Original"
    if suffix == "png":
        output = io.BytesIO()
        Image.new("RGB", (1, 1)).save(output, format="PNG")
        data = output.getvalue()
    elif suffix == "svg":
        data = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    artifact = _stored_artifact(app, task.operation_id, f"artifact.{suffix}", data)
    record_session_master(store, task.operation_id, "master bytes")
    with store.connection() as connection:
        before = connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    result = artifact_edit_availability(
        store, app.state.service, store.artifact(artifact.artifact_id)
    )
    assert result.can_comment
    assert result.comment_unavailable_reason is None
    assert not result.fresh_session_required
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == before


def test_viewer_session_collision_uses_reservation_reason(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "artifact.md", b"Original")
    record_session_master(store, task.operation_id, "master bytes")
    active = task.model_copy(update={"operation_id": str(uuid.uuid4()), "status": "running"})
    store.create_agent_task(active)
    result = artifact_edit_availability(
        store, app.state.service, store.artifact(artifact.artifact_id)
    )
    assert not result.can_comment
    probe = task.model_copy(update={"operation_id": str(uuid.uuid4()), "status": "queued"})
    assert result.comment_unavailable_reason == store.session_launch_unavailable_reason(probe)
    assert not result.fresh_session_required


def test_viewer_missing_master_offers_explicit_fresh_session(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "artifact.md", b"Original")
    result = artifact_edit_availability(
        store, app.state.service, store.artifact(artifact.artifact_id)
    )
    assert result.can_comment
    assert result.fresh_session_required
    assert result.comment_unavailable_reason is None


def test_viewer_corrupt_master_does_not_offer_fresh_session(edit_origin, monkeypatch):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "artifact.md", b"Original")
    record_session_master(store, task.operation_id, "master bytes")
    monkeypatch.setattr(store, "agent_task_contract", lambda *_: "corrupt bytes")
    result = artifact_edit_availability(
        store, app.state.service, store.artifact(artifact.artifact_id)
    )
    assert not result.can_comment
    assert result.comment_unavailable_reason
    assert not result.fresh_session_required


@pytest.mark.parametrize("suffix", ["pdf", "bin"])
def test_viewer_does_not_offer_edits_for_unsupported_types(edit_origin, suffix):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, f"artifact.{suffix}", b"Original")
    result = artifact_edit_availability(
        store, app.state.service, store.artifact(artifact.artifact_id)
    )
    assert not result.can_comment
    assert result.comment_unavailable_reason
    assert not result.fresh_session_required
