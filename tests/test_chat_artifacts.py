from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from rcp.artifacts import classify_artifact_bytes, descriptor_for
from rcp.storage import AgentTaskRecord, Artifact
from tests.helpers import create_named_app, signed_in_client


def _app(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    return app, signed_in_client(app), app.state.background_tasks.store


def _turn(store, project_id, chat_id: str | None):
    now = store.now()
    request = {"chat_scope": "project", "chat_id": chat_id} if chat_id else {}
    return store.create_agent_task(
        AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id=project_id,
            kind="project_chat" if chat_id else "refresh",
            status="succeeded",
            request=request,
            created_at=now,
            updated_at=now,
            status_message="Completed",
        )
    )


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
            created_at=created_at,
            expires_at=expires_at,
        ),
        data=data,
    )


def _at(store, minutes: int) -> str:
    return (datetime.fromisoformat(store.now()) + timedelta(minutes=minutes)).isoformat()


def _listed(client, project_id, chat_id):
    response = client.get(f"/api/projects/{project_id}/chats/{chat_id}/artifact-list")
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

    assert _listed(client, project_id, chat_id) == [newer.artifact_id, older.artifact_id]
    assert len(_listed(client, project_id, other_chat)) == 1
    assert _listed(client, project_id, str(uuid.uuid4())) == []


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
    response = client.get(f"/api/projects/{project_id}/chats/{thread.thread_id}/artifact-list")
    assert response.status_code == 404
