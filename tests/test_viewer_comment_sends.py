from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from tests.helpers import fake_launch_admitted, signed_in_client
from tests.test_artifact_edit_admission import edit_origin as edit_origin
from tests.test_artifact_viewer_state import _task
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


def _busy_chat(edit_origin, monkeypatch, *texts):
    """A chat whose turn runs, with one saved thread per text on its artifact."""
    app, store, task = edit_origin
    artifact = store.artifact(
        _stored_artifact(app, task.operation_id, "report.md", b"Original").artifact_id
    )
    client = signed_in_client(app)
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    running = _task(store, task.project_id, request=task.request, status="running")
    threads = [_thread(client, task.project_id, artifact, text) for text in texts]
    return client, artifact, running, threads


def _drained(edit_origin, running, queue_id):
    from rcp.runs.agent_thread_queue import queued_turn_operation_id
    from tests.helpers import wait_until

    app, store, _task = edit_origin
    store.complete_agent_task(running.operation_id, applied_revision=None, result={})
    app.state.reconcile_thread_handoffs()
    return wait_until(lambda: store.agent_task(queued_turn_operation_id(queue_id)))


def _state(store, task, thread_id):
    thread = store.get_comment_thread(task.project_id, thread_id)
    return thread["state"], thread["sent_operation_id"]


def test_viewer_send_queues_while_the_chat_turn_runs(edit_origin, monkeypatch):
    _app, store, task = edit_origin
    client, artifact, running, (thread_id,) = _busy_chat(edit_origin, monkeypatch, "Revise")
    url = f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments"
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    response = client.post(url, json={"thread_ids": [thread_id]}, headers=headers)
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["queued"] is True and queued["operation_id"] is None
    assert _state(store, task, thread_id)[0] == "resolved"
    replay = client.post(url, json={"thread_ids": [thread_id]}, headers=headers)
    assert replay.json()["queue_id"] == queued["queue_id"]

    turn = _drained(edit_origin, running, queued["queue_id"])
    assert turn.request["chat_id"] == task.request["chat_id"]
    (selection,) = turn.request["artifact_context"]["selections"]
    assert (selection["kind"], selection["drawn_version"]) == ("whole", artifact.current_version)
    assert "Revise" in selection["comment"]
    assert _state(store, task, thread_id) == ("resolved", turn.operation_id)


def test_composer_queue_carries_comment_threads_and_removal_reopens(edit_origin, monkeypatch):
    _app, store, task = edit_origin
    client, _artifact, running, (kept, dropped) = _busy_chat(
        edit_origin, monkeypatch, "Keep", "Drop"
    )
    base = f"/api/agent-threads/{task.request['chat_id']}/queue"

    def queue(body):
        response = client.post(base, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
        assert response.status_code == 202, response.text
        return response.json()["queue_id"]

    queued = queue({"message": "Also check", "comment_thread_ids": [kept]})
    removed = queue({"comment_thread_ids": [dropped]})
    assert _state(store, task, dropped)[0] == "resolved"
    assert client.delete(f"{base}/{removed}").status_code == 200
    assert _state(store, task, dropped) == ("open", None)

    turn = _drained(edit_origin, running, queued)
    assert turn.request["message"].startswith("Also check")
    (selection,) = turn.request["artifact_context"]["selections"]
    assert "Keep" in selection["comment"]
    assert _state(store, task, kept) == ("resolved", turn.operation_id)


def test_queue_refuses_an_unadmittable_artifact_comment_at_once(edit_origin, monkeypatch):
    from rcp.runs import artifact_edit_admission
    from rcp.storage.models import AgentTaskAdmissionConflict

    _app, store, task = edit_origin
    client, artifact, _running, (thread_id,) = _busy_chat(edit_origin, monkeypatch, "Revise")

    def expired(*_args, **_kwargs):
        raise AgentTaskAdmissionConflict("The artifact has expired.")

    monkeypatch.setattr(artifact_edit_admission, "admit_chat_artifact_comment", expired)
    for url, body in (
        (
            f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments",
            {"thread_ids": [thread_id]},
        ),
        (
            f"/api/agent-threads/{task.request['chat_id']}/queue",
            {"comment_thread_ids": [thread_id]},
        ),
    ):
        response = client.post(url, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
        assert response.status_code == 409, response.text
    assert _state(store, task, thread_id) == ("open", None)
    assert store.agent_thread_queue(task.request["chat_id"]) == []


def test_a_queued_send_cannot_take_a_thread_another_send_took(edit_origin, monkeypatch):
    from rcp.storage.comments import CommentError

    _app, store, task = edit_origin
    _client, _artifact, _running, (thread_id,) = _busy_chat(edit_origin, monkeypatch, "Revise")
    chat_id, user_id = task.request["chat_id"], store.local_owner.user_id
    renders = [
        store.sendable_threads(task.project_id, user_id, [thread_id], key)[0][0]
        for key in ("first", "second")
    ]

    def queue(key, rendered):
        return store.queue_agent_thread_message(
            queue_id=f"{key}-row",
            thread_id=chat_id,
            user_id=user_id,
            idempotency_key=key,
            message="",
            request_sha256=key,
            rendered_through={thread_id: rendered["rendered_through"]},
            sent_operation_id=f"{key}-turn",
        )

    queue("first", renders[0])
    with pytest.raises(CommentError) as refused:
        queue("second", renders[1])
    assert refused.value.status_code == 409
    assert [row.idempotency_key for row in store.agent_thread_queue(chat_id)] == ["first"]
    assert _state(store, task, thread_id) == ("resolved", "first-turn")


def test_viewer_queued_send_drains_when_the_turn_settled_before_it_queued(edit_origin, monkeypatch):
    from rcp.api import artifacts as artifact_routes
    from rcp.runs.agent_thread_queue import queued_turn_operation_id
    from tests.helpers import wait_until

    _app, store, task = edit_origin
    client, artifact, running, (thread_id,) = _busy_chat(edit_origin, monkeypatch, "Revise")
    store.complete_agent_task(running.operation_id, applied_revision=None, result={})
    monkeypatch.setattr(artifact_routes, "agent_turn_busy", lambda *_: True)
    response = client.post(
        f"/api/projects/{task.project_id}/artifacts/{artifact.artifact_id}/comments",
        json={"thread_ids": [thread_id]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 202, response.text
    operation_id = queued_turn_operation_id(response.json()["queue_id"])
    assert wait_until(lambda: store.agent_task(operation_id)).request["artifact_context"]


def test_a_reply_does_not_free_a_queued_thread_for_another_send(edit_origin, monkeypatch):
    _app, store, task = edit_origin
    client, _artifact, running, (thread_id,) = _busy_chat(edit_origin, monkeypatch, "Revise")
    base = f"/api/agent-threads/{task.request['chat_id']}/queue"
    body = {"comment_thread_ids": [thread_id]}
    first = client.post(base, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert first.status_code == 202, first.text
    store.add_comment_post(
        task.project_id, thread_id, user_id=store.local_owner.user_id, text="One more"
    )
    second = client.post(base, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert second.status_code == 409, second.text
    turn = _drained(edit_origin, running, first.json()["queue_id"])
    assert "Revise" in turn.request["artifact_context"]["selections"][0]["comment"]
