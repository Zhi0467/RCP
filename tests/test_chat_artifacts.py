from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from rcp.artifacts import classify_artifact_bytes, descriptor_for
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.storage import AgentTaskRecord, Artifact, EpisodeRecord
from tests.helpers import create_named_app, signed_in_client


def _app(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    return app, signed_in_client(app), app.state.background_tasks.store


def _turn(store, project_id, chat_id: str | None, **fields):
    now = store.now()
    request = {"chat_scope": "project", "chat_id": chat_id} if chat_id else {}
    record = {
        "operation_id": str(uuid.uuid4()),
        "project_id": project_id,
        "kind": "project_chat" if chat_id else "refresh",
        "status": "succeeded",
        "request": {**request, **fields.pop("request", {})},
        "created_at": now,
        "updated_at": now,
        "status_message": "Completed",
    }
    task = AgentTaskRecord.model_validate({**record, **fields})
    with store.connection() as connection:
        store._insert_agent_task(connection, task, continuation_cause="fresh")
    return task


def _artifact(store, task, name: str, *, created_at: str, expires_at: str | None = None):
    data = b"<!doctype html><p>out</p>"
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
            supplier="turn",
            supplier_id=task.operation_id,
            source_name=name,
            media_type=descriptor.media_type,
            origin_operation_id=task.operation_id,
            episode_id=task.episode_id,
            created_at=created_at,
            expires_at=expires_at,
        ),
        data=data,
    )


def _at(store, minutes: int) -> str:
    return (datetime.fromisoformat(store.now()) + timedelta(minutes=minutes)).isoformat()


def _listed(client, agent_id):
    response = client.get(f"/api/agent-threads/{agent_id}/artifact-list")
    assert response.status_code == 200, response.text
    return [entry["artifact_id"] for entry in response.json()]


def test_chat_lists_its_own_unexpired_artifacts_newest_first(manifest, tmp_path):
    app, client, store = _app(manifest, tmp_path)
    project_id = app.state.default_project_id
    chat_id, other_chat = str(uuid.uuid4()), str(uuid.uuid4())
    first = _turn(store, project_id, chat_id)
    second = _turn(store, project_id, chat_id)
    older = _artifact(store, first, "older.html", created_at=_at(store, -10))
    newer = _artifact(store, second, "newer.md", created_at=_at(store, -1))
    _artifact(store, first, "gone.html", created_at=_at(store, -20), expires_at=_at(store, -5))
    _artifact(store, _turn(store, project_id, other_chat), "other.html", created_at=_at(store, -2))
    _artifact(store, _turn(store, project_id, None), "ingest.html", created_at=_at(store, -3))

    assert _listed(client, chat_id) == [newer.artifact_id, older.artifact_id]
    assert len(_listed(client, other_chat)) == 1
    assert client.get(f"/api/agent-threads/{uuid.uuid4()}/artifact-list").status_code == 404


def test_another_members_private_chat_is_refused(manifest, tmp_path):
    app, client, store = _app(manifest, tmp_path)
    project_id = app.state.default_project_id
    thread = next(
        t
        for t in store.ensure_agent_threads("another-member", [project_id], default_names=("Pip",))
        if t.project_id == project_id
    )
    _artifact(
        store, _turn(store, project_id, thread.thread_id), "private.html", created_at=_at(store, 0)
    )
    response = client.get(f"/api/agent-threads/{thread.thread_id}/artifact-list")
    assert response.status_code == 404


def test_paper_coach_chat_lists_its_turn_artifacts(manifest, tmp_path):
    app, client, store = _app(manifest, tmp_path)
    project_id = app.state.default_project_id
    chat_id = str(uuid.uuid4())
    turn = _turn(store, project_id, chat_id, request={"focus": "paper"})
    draft = _artifact(store, turn, "draft.html", created_at=_at(store, 0))

    assert _listed(client, chat_id) == [draft.artifact_id]


def test_space_thread_lists_its_turn_artifacts(manifest, tmp_path):
    app, client, store = _app(manifest, tmp_path)
    threads = client.get("/api/agent-threads").json()["threads"]
    thread_id = next(thread["thread_id"] for thread in threads if thread["kind"] == "space")
    turn = _turn(store, store.space_id, thread_id)
    note = _artifact(store, turn, "note.html", created_at=_at(store, 0))
    _artifact(
        store,
        _turn(store, app.state.default_project_id, thread_id),
        "x.html",
        created_at=_at(store, 0),
    )

    assert _listed(client, thread_id) == [note.artifact_id]


def test_orchestrator_lists_its_episode_artifacts_under_its_branch(manifest, tmp_path):
    app, client, store = _app(manifest, tmp_path)
    project_id = app.state.default_project_id
    episode_id, root_id = str(uuid.uuid4()), str(uuid.uuid4())
    branch = GraphTargetRef(kind="branch", branch_id=episode_id)
    with store.connection() as connection:
        store._insert_episode(
            connection,
            EpisodeRecord(
                episode_id=episode_id,
                project_id=project_id,
                mode="auto_research",
                status="running",
                invocation_ceiling=5,
                root_operation_id=root_id,
                graph_target=branch,
                graph_base_head=GraphHeadRef(revision=0),
                created_at=store.now(),
                updated_at=store.now(),
            ),
        )
    root = _turn(
        store,
        project_id,
        None,
        operation_id=root_id,
        episode_id=episode_id,
        graph_target=branch,
    )
    plot = _artifact(store, root, "plot.html", created_at=_at(store, 0))

    assert _listed(client, episode_id) == [plot.artifact_id]
