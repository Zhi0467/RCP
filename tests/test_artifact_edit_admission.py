from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from rcp.runs.artifact_edit_admission import admit_artifact_edit, artifact_edit_availability
from rcp.runs.session_master import record_session_master
from rcp.service import RunRequest
from rcp.storage import AgentTaskAdmissionConflict, EpisodeRecord
from tests.helpers import signed_in_client

from .helpers import create_named_app
from .test_artifact_viewer_state import _task
from .test_unified_artifacts import _stored_artifact


@pytest.fixture
def edit_origin(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    stage = tmp_path / "stage"
    stage.mkdir()
    task = _task(
        store,
        app.state.default_project_id,
        native_session_id="native-session",
        stage_root=str(stage),
    )
    record_session_master(store, task.operation_id, "master bytes")
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
    admitted = admit_artifact_edit(
        store, app.state.service, task.project_id, _request(task, artifact, fresh=True)
    )
    assert admitted.artifact_edit.launch_kind == shape
    assert admitted.mode == "discuss"
    assert not admitted.artifact_edit.fresh_session
    assert admitted.session_id == task.native_session_id
    assert admitted.chat_id == task.request["chat_id"]
    assert (
        admitted.artifact_edit.base_version == store.artifact(artifact.artifact_id).current_version
    )


@pytest.mark.parametrize("missing", ["stage", "session", "history", "contract"])
def test_unresumable_origin_requires_explicit_fresh_session(edit_origin, missing):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "explanation.txt", b"Original")
    with store.connection() as connection:
        if missing == "stage":
            Path(task.stage_root).rmdir()
        elif missing == "contract":
            connection.execute(
                "DELETE FROM graph_run_contracts WHERE operation_id = ?", (task.operation_id,)
            )
        else:
            column = "native_session_id" if missing == "session" else "history_only"
            connection.execute(
                f"UPDATE graph_runs SET {column} = ? WHERE operation_id = ?",
                (None if missing == "session" else 1, task.operation_id),
            )
    availability = artifact_edit_availability(
        store, app.state.service, store.artifact(artifact.artifact_id)
    )
    assert availability.can_comment and availability.fresh_session_required
    response = signed_in_client(app).post(
        f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments",
        json={"comments": [{"text": "Update"}]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "fresh_session_required"
    admitted = admit_artifact_edit(
        store, app.state.service, task.project_id, _request(task, artifact, fresh=True)
    )
    assert admitted.session_id is None
    assert admitted.artifact_edit.stage_root is None
    assert admitted.artifact_edit.fresh_session
    assert admitted.chat_id == task.request["chat_id"]


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
    artifact = _stored_artifact(
        app,
        supplier.operation_id,
        "report.html",
        b"<h1>Original</h1>",
        supplier="episode_ending" if report else "turn",
        episode_id=episode_id,
    )
    admitted = admit_artifact_edit(
        store, app.state.service, task.project_id, _request(supplier, artifact)
    )
    assert admitted.artifact_edit.launch_kind == "revoking"
    assert admitted.artifact_edit.origin_operation_id == task.operation_id
    assert admitted.chat_id == (None if mode == "auto_research" else request["chat_id"])
    assert admitted.artifact_edit.reply_episode_id == (
        episode_id if mode == "auto_research" else None
    )

    client = signed_in_client(app)
    response = client.get(f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/state")
    assert response.status_code == 200
    state = response.json()
    expected = (
        {"view": ["runs"], "mode": ["auto_research"], "episode": [episode_id]}
        if mode == "auto_research"
        else {"view": ["chats"], "chat": [request["chat_id"]]}
    )
    assert parse_qs(urlsplit(state["thread_href"][1:]).query) == expected
    assert state["supplier"] == ("episode_ending" if report else "turn")
    assert client.get(state["viewer_url"]).status_code == 200


def test_text_edits_refuse_selection_gestures(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "data.json", b'{"value": 1}')
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


def test_selections_drawn_on_an_older_version_are_refused(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "page.html", b"<p>v1</p>")
    request = _request(task, artifact)
    context = request.artifact_context.model_dump(mode="python")
    stale = RunRequest.model_validate(
        {
            **request.model_dump(mode="python"),
            "artifact_context": {**context, "base_version": "older"},
        }
    )
    with pytest.raises(ValueError):
        admit_artifact_edit(store, app.state.service, task.project_id, stale)
    current = RunRequest.model_validate(
        {
            **request.model_dump(mode="python"),
            "artifact_context": {
                **context,
                "base_version": store.artifact(artifact.artifact_id).current_version,
            },
        }
    )
    admit_artifact_edit(store, app.state.service, task.project_id, current)


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


def test_viewer_session_collision_matches_comment_admission(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "artifact.md", b"Original")
    client = signed_in_client(app)
    url = f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}"
    assert client.get(url + "/state").json()["can_comment"]
    active = task.model_copy(
        update={
            "operation_id": str(uuid.uuid4()),
            "status": "running",
            "request": {**task.request, "chat_id": str(uuid.uuid4())},
        }
    )
    store.create_agent_task(active)
    response = client.get(url + "/state")
    assert response.status_code == 200
    state = response.json()
    assert not state["can_comment"] and not state["fresh_session_required"]
    response = client.post(url + "/comments", json={"comments": [{"text": "Update this"}]})
    assert response.status_code == 409


def test_viewer_defers_master_integrity_check_to_admission(edit_origin, monkeypatch):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "artifact.md", b"Original")
    monkeypatch.setattr(store, "agent_task_contract", lambda *_: "corrupt bytes")
    result = artifact_edit_availability(
        store, app.state.service, store.artifact(artifact.artifact_id)
    )
    assert result.can_comment
    assert not result.fresh_session_required
    with pytest.raises(AgentTaskAdmissionConflict):
        admit_artifact_edit(
            store, app.state.service, task.project_id, _request(task, artifact, fresh=True)
        )


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
    with pytest.raises(ValueError):
        admit_artifact_edit(
            store, app.state.service, task.project_id, _request(task, artifact, fresh=True)
        )


def test_availability_uses_no_remote_or_content_reads(edit_origin, monkeypatch):
    from rcp.transport import RemoteRunStage

    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "artifact.md", b"Original")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET stage_host = 'remote' WHERE operation_id = ?",
            (task.operation_id,),
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("Availability performed expensive verification")

    monkeypatch.setattr(RemoteRunStage, "directory_exists", forbidden)
    monkeypatch.setattr(store, "read_artifact_bytes", forbidden)
    assert artifact_edit_availability(
        store, app.state.service, store.artifact(artifact.artifact_id)
    ).can_comment


@pytest.mark.parametrize(
    ("state", "expected"), [("pending", 409), ("unavailable", 409), ("missing", 410)]
)
def test_pending_import_is_transient(edit_origin, monkeypatch, state, expected):
    app, store, task = edit_origin
    monkeypatch.setattr(
        store,
        "artifact_import_status",
        lambda artifact_id: {
            "project_id": task.project_id,
            "state": state,
            "reason": None,
        },
    )
    response = signed_in_client(app).get(
        f"/api/projects/{task.project_id}/artifacts/{'a' * 24}/download"
    )
    assert response.status_code == expected


def test_every_comment_is_one_object_on_one_route(edit_origin, monkeypatch):
    from types import SimpleNamespace

    from rcp.agents.prompts import _attachment_items

    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "report.html", b"<p>Score</p>")
    started = []
    monkeypatch.setattr(
        "rcp.api.artifacts.start_artifact_edit",
        lambda _tasks, _project, admitted, **_: (
            started.append(admitted)
            or SimpleNamespace(model_dump=lambda **_: {"operation_id": "edit"})
        ),
    )
    box = {
        "kind": "box",
        "rect": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.3},
        "viewport": {"width": 800, "height": 600},
        "elements": [{"path": "p", "label": "", "text": "Score"}],
    }
    url = f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments"
    client = signed_in_client(app)
    # A box with only its own comment is a complete request, sent at once.
    response = client.post(
        url,
        json={
            "comments": [{"text": "Label these rows", "selection": box}, {"text": "Shorter"}],
            "edit_now": True,
        },
    )
    assert response.status_code == 202, response.text
    (admitted,) = started
    context = admitted.artifact_context
    assert context.edit_now
    assert [selection.comment for selection in context.selections] == ["Label these rows"]
    for text in ("Label these rows", "Shorter"):
        assert admitted.message.count(text) == 1
    assert client.post(url, json={"comments": [{"text": " ", "selection": box}]}).status_code == 422

    pointer = {"name": "report.html", "media_type": "text/html", "path": "/a/report.html"}
    pointer |= {"source_artifact_id": artifact.artifact_id, "selections": [box]}
    queued, now = (_attachment_items([{**pointer, "edit_now": flag}]) for flag in (False, True))
    assert len(now.splitlines()) == len(queued.splitlines()) + 1
