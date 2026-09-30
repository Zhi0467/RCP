from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from rcp.artifacts import classify_artifact_bytes, descriptor_for
from rcp.live_artifacts import ResolvedLiveVersion
from rcp.runs.session_master import record_session_master
from rcp.storage import AgentTaskRecord, Artifact, AutoResearchChildExperimentRecord, EpisodeRecord

from .helpers import create_named_app
from .test_project_membership import _create_project, _team_app


@pytest.fixture
def viewer_app(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    return app, TestClient(app), app.state.background_tasks.store


def _task(store, project_id, *, request=None, **fields):
    task = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=project_id,
        kind=fields.pop("kind", "project_chat"),
        status=fields.pop("status", "succeeded"),
        created_at=store.now(),
        updated_at=store.now(),
        status_message="Completed",
        request=request
        if request is not None
        else {
            "chat_scope": "project",
            "chat_id": str(uuid.uuid4()),
            "provider": "codex",
            "model": "",
            "reasoning": "medium",
            "run_on": "laptop",
        },
        **fields,
    )
    with store.connection() as connection:
        store._insert_agent_task(connection, task, continuation_cause="fresh")
    return task


def _episode(store, project_id, *, mode="experiment_loop", **fields):
    if mode == "experiment_loop":
        fields.setdefault("control_node_id", "experiment")
    episode = EpisodeRecord(
        episode_id=str(uuid.uuid4()),
        project_id=project_id,
        mode=mode,
        status="running",
        invocation_ceiling=5,
        created_at=store.now(),
        updated_at=store.now(),
        **fields,
    )
    with store.connection() as connection:
        store._insert_episode(connection, episode)
    return episode


def _artifact(store, task, *, name="result.md", data=b"Original", **fields):
    descriptor = descriptor_for(
        task.operation_id,
        name,
        media_type=classify_artifact_bytes(name, data),
        size_bytes=len(data),
    )
    return store.create_artifact(
        Artifact(
            artifact_id=descriptor.artifact_id,
            project_id=task.project_id,
            supplier=fields.pop("supplier", "turn"),
            supplier_id=task.operation_id,
            source_name=name,
            media_type=descriptor.media_type,
            origin_operation_id=task.operation_id,
            episode_id=task.episode_id,
            created_at=fields.pop("created_at", store.now()),
            **fields,
        ),
        data=data,
    )


def _state(client, artifact):
    response = client.get(
        f"/api/projects/{artifact.project_id}/artifacts/{artifact.artifact_id}/state"
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_state_version_undo_and_current_viewer(viewer_app):
    app, client, store = viewer_app
    task = _task(store, app.state.default_project_id)
    artifact = _artifact(store, task)
    original = _state(client, artifact)
    assert original["version_number"] == 1
    assert not original["can_undo"]
    assert original["fresh_session_required"] and original["can_comment"]
    second = store.publish_artifact_version(
        artifact.artifact_id,
        base_version=artifact.current_version,
        operation_id="edit-1",
        data=b"Second",
    )
    state = _state(client, artifact)
    assert state["current_version"] == second.version_id
    assert state["version_number"] == 2 and state["can_undo"]
    viewer = client.get(state["viewer_url"])
    assert viewer.status_code == 200
    assert second.version_id in unquote(viewer.text)
    assert "frame-ancestors 'self'" in viewer.headers["content-security-policy"]
    store.undo_artifact(artifact.artifact_id)
    reverted = _state(client, artifact)
    assert reverted["current_version"] == artifact.current_version
    assert reverted["version_number"] == 1 and not reverted["can_undo"]
    assert client.get(reverted["download_url"]).content == b"Original"


@pytest.mark.parametrize(
    "live,finished,expected",
    [
        (None, False, None),
        (ResolvedLiveVersion(invalid_reason="Invalid declaration"), False, None),
        (ResolvedLiveVersion(), False, "live"),
        (ResolvedLiveVersion(), True, "finished"),
    ],
)
def test_state_live_classification(viewer_app, live, finished, expected):
    app, client, store = viewer_app
    artifact = _artifact(store, _task(store, app.state.default_project_id), name="live.html")
    if live is not None:
        store.set_artifact_version_live(artifact.artifact_id, artifact.current_version, live)
    if finished:
        store.save_artifact_live_snapshot(artifact.artifact_id, artifact.current_version, b"{}")
    assert _state(client, artifact)["live"] == expected


@pytest.mark.parametrize(
    "status,active",
    [
        ("queued", True),
        ("running", True),
        ("pausing", True),
        ("succeeded", False),
        ("failed", False),
        ("interrupted", False),
    ],
)
def test_state_edit_operation_tracks_task_lifecycle(viewer_app, status, active):
    app, client, store = viewer_app
    origin = _task(store, app.state.default_project_id)
    artifact = _artifact(store, origin)
    edit = _task(
        store,
        origin.project_id,
        kind="artifact_edit",
        status=status,
        request={"artifact_edit": {"artifact_id": artifact.artifact_id}},
    )
    assert _state(client, artifact)["editing_operation_id"] == (
        edit.operation_id if active else None
    )


@pytest.mark.parametrize(
    "kind,report",
    [
        ("chat", False),
        ("experiment", False),
        ("experiment", True),
        ("auto_research", False),
        ("auto_research", True),
    ],
)
def test_state_reply_thread_and_reports(viewer_app, kind, report):
    app, client, store = viewer_app
    project_id = app.state.default_project_id
    episode = (
        None
        if kind == "chat"
        else _episode(
            store,
            project_id,
            mode="auto_research" if kind == "auto_research" else "experiment_loop",
            control_node_id="experiment" if kind == "experiment" else None,
        )
    )
    request = (
        {}
        if kind == "auto_research"
        else {
            "chat_id": str(uuid.uuid4()),
            "chat_scope": "node" if kind == "experiment" else "project",
        }
    )
    if kind == "experiment":
        request.update(node_id="experiment")
    origin = _task(
        store,
        project_id,
        kind="node_chat",
        episode_id=episode.episode_id if episode else None,
        request=request,
    )
    if episode:
        with store.connection() as connection:
            connection.execute(
                "UPDATE episodes SET root_operation_id = ? WHERE episode_id = ?",
                (origin.operation_id, episode.episode_id),
            )
    supplier = (
        _task(
            store,
            project_id,
            kind="episode_report",
            episode_id=episode.episode_id if episode else None,
            parent_operation_id=origin.operation_id,
            request={},
        )
        if report
        else origin
    )
    artifact = _artifact(store, supplier, supplier="episode_ending" if report else "turn")
    state = _state(client, artifact)
    query = parse_qs(urlsplit(state["thread_href"][1:]).query)
    if kind == "auto_research":
        assert query == {
            "view": ["runs"],
            "mode": ["auto_research"],
            "episode": [episode.episode_id],
        }
    else:
        assert query == {"view": ["chats"], "chat": [request["chat_id"]]}
    assert state["supplier"] == ("episode_ending" if report else "turn")
    assert client.get(state["viewer_url"]).status_code == 200


@pytest.mark.parametrize("name,data", [("paper.pdf", b"%PDF-1.7\n"), ("data.bin", b"\x00\xff")])
def test_state_download_only_has_no_viewer_or_comment(viewer_app, name, data):
    app, client, store = viewer_app
    artifact = _artifact(store, _task(store, app.state.default_project_id), name=name, data=data)
    state = _state(client, artifact)
    assert state["viewer_url"] is None
    assert not state["can_comment"]
    assert client.get(state["download_url"]).content == data


def test_state_session_reservation_matches_comment_admission(viewer_app, tmp_path):
    app, client, store = viewer_app
    stage = tmp_path / "stage"
    stage.mkdir()
    origin = _task(
        store, app.state.default_project_id, native_session_id="native", stage_root=str(stage)
    )
    artifact = _artifact(store, origin)
    record_session_master(store, origin.operation_id, "master bytes")
    assert _state(client, artifact)["can_comment"]
    _task(
        store,
        origin.project_id,
        native_session_id="native",
        stage_root=str(stage),
        status="running",
        request={**origin.request, "chat_id": str(uuid.uuid4())},
    )
    state = _state(client, artifact)
    assert not state["can_comment"] and not state["fresh_session_required"]
    response = client.post(
        f"/api/projects/{origin.project_id}/artifacts/{artifact.artifact_id}/comments",
        json={"message": "Update this"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == state["comment_unavailable_reason"]


def test_run_artifacts_includes_workers_child_experiments_and_retained_reports(viewer_app):
    app, client, store = viewer_app
    project_id = app.state.default_project_id
    episode = _episode(store, project_id, mode="auto_research")
    parent = _task(store, project_id, episode_id=episode.episode_id)
    worker = _task(store, project_id, episode_id=episode.episode_id)
    child = _episode(store, project_id)
    child_task = _task(store, project_id, episode_id=child.episode_id)
    now = datetime.fromisoformat(store.now())
    before = (now - timedelta(days=1)).isoformat()
    after = (now + timedelta(days=1)).isoformat()
    worker_id = str(uuid.uuid4())
    instruction = "# Summarize results\nDo the work."
    route = AutoResearchChildExperimentRecord(
        child_episode_id=child.episode_id,
        auto_research_episode_id=episode.episode_id,
        project_id=project_id,
        control_node_id="experiment",
        state="running",
        request={},
        parent_operation_id=parent.operation_id,
        created_at=store.now(),
        updated_at=store.now(),
    )
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO auto_research_child_work (worker_id, episode_id, project_id, control_node_id, root_operation_id, current_operation_id, admitted_by_operation_id, instruction, instruction_sha256, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                worker_id,
                episode.episode_id,
                project_id,
                "experiment",
                worker.operation_id,
                worker.operation_id,
                parent.operation_id,
                instruction,
                hashlib.sha256(instruction.encode()).hexdigest(),
                store.now(),
                store.now(),
            ),
        )
        connection.execute(
            "INSERT INTO auto_research_child_work_attempts (operation_id, worker_id, allocation_operation_id, created_at) VALUES (?, ?, ?, ?)",
            (worker.operation_id, worker_id, worker.operation_id, store.now()),
        )
        connection.execute(
            "INSERT INTO auto_research_child_experiments (child_episode_id, auto_research_episode_id, project_id, control_node_id, state, replaces_episode_id, request_json, goal_sha256, parent_operation_id, terminal_diagnostic, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            store._child_experiment_values(route),
        )
    report = _artifact(store, parent, name="report.md", supplier="episode_ending", created_at=after)
    work = _artifact(store, worker, name="work.md", created_at=before, expires_at=after)
    child_artifact = _artifact(store, child_task, name="child.md", created_at=store.now())
    child_report = _artifact(
        store,
        child_task,
        name="child-report.md",
        supplier="episode_ending",
        created_at=(now + timedelta(hours=1)).isoformat(),
    )
    kept = _artifact(
        store, parent, name="kept.md", created_at=after, expires_at=before, kept_at=before
    )
    _artifact(store, parent, name="expired.md", expires_at=before)
    _artifact(store, _task(store, project_id), name="unrelated.md")
    response = client.get(f"/api/projects/{project_id}/episodes/{episode.episode_id}/artifacts")
    assert response.status_code == 200, response.text
    entries = response.json()
    assert [entry["artifact_id"] for entry in entries] == [
        report.artifact_id,
        work.artifact_id,
        child_artifact.artifact_id,
        child_report.artifact_id,
        kept.artifact_id,
    ]
    assert entries[1]["worker_label"] == "Summarize results"
    assert all(entry["worker_label"] is None for index, entry in enumerate(entries) if index != 1)
    child_response = client.get(f"/api/projects/{project_id}/episodes/{child.episode_id}/artifacts")
    assert [entry["artifact_id"] for entry in child_response.json()] == [
        child_report.artifact_id,
        child_artifact.artifact_id,
    ]


@pytest.mark.parametrize("route", ["artifacts/{id}/state", "episodes/{id}/artifacts"])
def test_viewer_endpoints_enforce_project_membership(tmp_path, route):
    _, client, store, people, acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repository")
    episode = _episode(store, project_id)
    task = _task(store, project_id, episode_id=episode.episode_id)
    artifact = _artifact(store, task)
    resource_id = artifact.artifact_id if route.startswith("artifacts/") else episode.episode_id
    url = f"/api/projects/{project_id}/" + route.format(id=resource_id)
    assert client.get(url).status_code == 200
    acting[0] = people[1].user_id
    response = client.get(url)
    assert response.status_code == 404
