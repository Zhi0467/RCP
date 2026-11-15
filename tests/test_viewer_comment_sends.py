from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from tests.helpers import fake_launch_admitted, signed_in_client
from tests.test_artifact_edit_admission import edit_origin as edit_origin
from tests.test_unified_artifacts import _stored_artifact


def _thread(client, project, artifact, text="Please revise"):
    response = client.post(
        f"/api/projects/{project}/comment-threads",
        json={
            "target": {"kind": "artifact", "id": artifact.artifact_id},
            "anchor": {"kind": "whole"},
            "drawn_version": artifact.current_version,
            "text": text,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["thread"]["thread_id"]


@pytest.mark.parametrize("lost_mark", [False, True])
def test_viewer_thread_send_and_replay(edit_origin, monkeypatch, lost_mark):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "report.md", b"Original")
    artifact = store.artifact(artifact.artifact_id)
    client = signed_in_client(app)
    thread_id = _thread(client, task.project_id, artifact)
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    url = f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments"
    key = str(uuid.uuid4())
    mark_sent = store.mark_threads_sent
    if lost_mark:
        monkeypatch.setattr(store, "mark_threads_sent", lambda *args: None)
    response = client.post(url, json={"thread_ids": [thread_id]}, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    sent = store.agent_task(response.json()["operation_id"])
    selection = sent.request["artifact_context"]["selections"][0]
    assert selection["kind"] == "whole"
    assert "Please revise" in selection["comment"]
    assert selection["drawn_version"] == artifact.current_version
    thread = store.get_comment_thread(task.project_id, thread_id)
    assert thread["state"] == ("open" if lost_mark else "resolved")
    monkeypatch.setattr(store, "mark_threads_sent", mark_sent)
    replay = client.post(url, json={"thread_ids": [thread_id]}, headers={"Idempotency-Key": key})
    assert replay.status_code == 202, replay.text
    assert replay.json()["operation_id"] == sent.operation_id
    thread = store.get_comment_thread(task.project_id, thread_id)
    assert thread["state"] == "resolved"
    assert thread["sent_operation_id"] == sent.operation_id
    refused = client.post(
        url, json={"thread_ids": [thread_id]}, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert refused.status_code == 409


def test_viewer_refused_admission_keeps_thread_open(edit_origin, monkeypatch):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "report.md", b"Original")
    client = signed_in_client(app)
    thread_id = _thread(client, task.project_id, store.artifact(artifact.artifact_id))

    def refuse(*args, **kwargs):
        raise HTTPException(status_code=409, detail="Admission refused")

    monkeypatch.setattr("rcp.api.artifacts.start_ordinary_task", refuse)
    response = client.post(
        f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments",
        json={"thread_ids": [thread_id]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 409, response.text
    assert store.get_comment_thread(task.project_id, thread_id)["state"] == "open"


def test_viewer_requires_key_and_rejects_another_artifact(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "report.md", b"Original")
    other = _stored_artifact(app, task.operation_id, "other.md", b"Other")
    client = signed_in_client(app)
    thread_id = _thread(client, task.project_id, store.artifact(other.artifact_id))
    url = f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments"
    assert client.post(url, json={"thread_ids": [thread_id]}).status_code == 422
    response = client.post(
        url, json={"thread_ids": [thread_id]}, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 409, response.text
    assert store.get_comment_thread(task.project_id, thread_id)["state"] == "open"


def test_viewer_budget_refusal_keeps_all_threads_open(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "report.md", b"Original")
    artifact = store.artifact(artifact.artifact_id)
    client = signed_in_client(app)
    ids = []
    for _ in range(8):
        thread_id = _thread(client, task.project_id, artifact, "x" * 2048)
        response = client.post(
            f"/api/projects/{task.project_id}/comment-threads/{thread_id}/posts",
            json={"text": "y" * 2048},
        )
        assert response.status_code == 200, response.text
        ids.append(thread_id)
    response = client.post(
        f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments",
        json={"thread_ids": ids},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422, response.text
    assert all(store.get_comment_thread(task.project_id, item)["state"] == "open" for item in ids)


def test_viewer_private_chat_nonowner_cannot_send(edit_origin):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "report.md", b"Original")
    client = signed_in_client(app)
    thread_id = _thread(client, task.project_id, store.artifact(artifact.artifact_id))
    private_chat = next(
        chat
        for chat in store.ensure_agent_threads(
            "another-member", [task.project_id], default_names=("Pip",)
        )
        if chat.project_id == task.project_id
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json, '$.chat_id', ?) "
            "WHERE operation_id=?",
            (private_chat.thread_id, task.operation_id),
        )
    response = client.post(
        f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments",
        json={"thread_ids": [thread_id]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 404, response.text
    assert store.get_comment_thread(task.project_id, thread_id)["state"] == "open"


def test_viewer_replay_cannot_mark_a_different_thread(edit_origin, monkeypatch):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "report.md", b"Original")
    artifact = store.artifact(artifact.artifact_id)
    client = signed_in_client(app)
    first = _thread(client, task.project_id, artifact, "First change")
    other = _thread(client, task.project_id, artifact, "Unsent change")
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    url = f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments"
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    assert client.post(url, json={"thread_ids": [first]}, headers=headers).status_code == 202
    response = client.post(url, json={"thread_ids": [other]}, headers=headers)
    assert response.status_code == 409, response.text
    assert store.get_comment_thread(task.project_id, other)["state"] == "open"
