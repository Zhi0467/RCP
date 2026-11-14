from __future__ import annotations

import uuid

import pytest

from tests.helpers import create_named_app, fake_launch_admitted, signed_in_client
from tests.test_artifact_viewer_state import _artifact, _task


@pytest.fixture
def send_app(manifest, tmp_path, monkeypatch):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    store = app.state.background_tasks.store
    task = _task(store, app.state.default_project_id)
    return app, signed_in_client(app), store, task


def _thread(store, task, *, kind="reply", target=None, text="Please revise"):
    return store.create_comment_thread(
        task.project_id,
        target_kind=kind,
        target_id=target or task.operation_id,
        anchor={"kind": "whole"},
        drawn_version=None,
        text=text,
        user_id=store.local_owner.user_id,
    )


def _send(client, task, threads, **changes):
    return client.post(
        f"/api/projects/{task.project_id}/tasks/project_chat",
        headers={"Idempotency-Key": changes.pop("key", str(uuid.uuid4()))},
        json={"chat_id": task.request["chat_id"], "comment_thread_ids": threads, **changes},
    )


def test_reply_send_and_replay_mark_threads_without_storing_ids(send_app):
    _, client, store, task = send_app
    thread = _thread(store, task)
    key = str(uuid.uuid4())
    first = _send(client, task, [thread["thread_id"]], key=key)
    assert first.status_code == 202, first.text
    operation = first.json()["operation_id"]
    saved = store.agent_task(operation)
    assert "Please revise" in saved.request["message"]
    assert "comment_thread_ids" not in saved.request
    assert (
        store.get_comment_thread(task.project_id, thread["thread_id"])["sent_operation_id"]
        == operation
    )
    replay = _send(client, task, [thread["thread_id"]], key=key)
    assert replay.status_code == 202, replay.text
    assert replay.json()["operation_id"] == operation


@pytest.mark.parametrize(
    "refusal", ["resolved", "wrong-chat", "no-chat", "budget", "two-artifacts", "private"]
)
def test_send_refusals_leave_threads_unchanged(send_app, refusal):
    _, client, store, task = send_app
    changes = {}
    thread = _thread(store, task)
    if refusal == "private":
        private = next(
            t
            for t in store.ensure_agent_threads("other", [task.project_id], default_names=("Pip",))
            if t.project_id == task.project_id
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET request_json=json_set(request_json, '$.chat_id', ?) WHERE operation_id=?",
                (private.thread_id, task.operation_id),
            )
        task = store.agent_task(task.operation_id)
    threads = [thread["thread_id"]]
    expected = 409
    if refusal == "resolved":
        store.resolve_comment_thread(
            task.project_id, thread["thread_id"], user_id=store.local_owner.user_id
        )
    elif refusal == "wrong-chat":
        changes["chat_id"] = str(uuid.uuid4())
    elif refusal == "no-chat":
        changes["chat_id"] = ""
        expected = 422
    elif refusal == "budget":
        changes["message"] = "x" * 32000
        expected = 422
    elif refusal == "private":
        expected = 404
    else:
        threads = [
            _thread(
                store, task, kind="artifact", target=_artifact(store, task, name=name).artifact_id
            )["thread_id"]
            for name in ("a.txt", "b.txt")
        ]
    response = _send(client, task, threads, **changes)
    assert response.status_code == expected, response.text
    for thread_id in threads:
        saved = store.get_comment_thread(task.project_id, thread_id)
        assert saved["sent_operation_id"] is None
        assert saved["state"] == ("resolved" if refusal == "resolved" else "open")


def test_artifact_threads_make_server_owned_selections(send_app):
    _, client, store, task = send_app
    artifact = _artifact(store, task)
    thread = _thread(store, task, kind="artifact", target=artifact.artifact_id)
    response = _send(client, task, [thread["thread_id"]])
    assert response.status_code == 202, response.text
    context = store.agent_task(response.json()["operation_id"]).request["artifact_context"]
    assert context["operation_id"] == task.operation_id
    assert context["selections"][0]["kind"] == "whole"
    assert "Please revise" in context["selections"][0]["comment"]


def test_client_selections_are_rejected(send_app):
    _, client, store, task = send_app
    artifact = _artifact(store, task)
    response = _send(
        client,
        task,
        [],
        message="Update",
        artifact_context={
            "operation_id": task.operation_id,
            "artifact_id": artifact.artifact_id,
            "selections": [{"kind": "whole", "comment": "forged"}],
        },
    )
    assert response.status_code == 422, response.text


def test_replay_marks_threads_left_open_after_admission(send_app):
    _, client, store, task = send_app
    thread = _thread(store, task)
    key = str(uuid.uuid4())
    first = _send(client, task, [thread["thread_id"]], key=key)
    assert first.status_code == 202, first.text
    store.reopen_comment_thread(
        task.project_id, thread["thread_id"], user_id=store.local_owner.user_id
    )
    replay = _send(client, task, [thread["thread_id"]], key=key)
    assert replay.status_code == 202, replay.text
    assert replay.json()["operation_id"] == first.json()["operation_id"]
    assert (
        store.get_comment_thread(task.project_id, thread["thread_id"])["sent_operation_id"]
        == first.json()["operation_id"]
    )


def test_paper_thread_send_uses_coach_message(send_app):
    _, client, store, task = send_app
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_files VALUES (?, 'paper-file', 'draft.md', 'text', ?, NULL, NULL)",
            (task.project_id, store.now()),
        )
    thread = _thread(store, task, kind="paper", target="paper-file")
    response = client.post(
        f"/api/projects/{task.project_id}/tasks/paper_coach",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"comment_thread_ids": [thread["thread_id"]]},
    )
    assert response.status_code == 202, response.text
    saved = store.agent_task(response.json()["operation_id"])
    assert "Please revise" in saved.request["message"]
    assert "comment_thread_ids" not in saved.request


def test_admission_refusal_and_missing_key_keep_thread_open(send_app, monkeypatch):
    from rcp.storage import AgentTaskAdmissionConflict

    app, client, store, task = send_app
    thread = _thread(store, task)
    response = client.post(
        f"/api/projects/{task.project_id}/tasks/project_chat",
        json={"chat_id": task.request["chat_id"], "comment_thread_ids": [thread["thread_id"]]},
    )
    assert response.status_code == 422, response.text

    def refuse(*args, **kwargs):
        raise AgentTaskAdmissionConflict("Unavailable")

    monkeypatch.setattr(app.state.background_tasks, "start", refuse)
    response = _send(client, task, [thread["thread_id"]])
    assert response.status_code == 409, response.text
    assert store.get_comment_thread(task.project_id, thread["thread_id"])["state"] == "open"


def test_replayed_key_cannot_mark_a_different_thread(send_app):
    _, client, store, task = send_app
    first_thread = _thread(store, task, text="First request")
    other_thread = _thread(store, task, text="Unsent request")
    key = str(uuid.uuid4())
    first = _send(client, task, [first_thread["thread_id"]], key=key)
    assert first.status_code == 202, first.text
    replay = _send(client, task, [other_thread["thread_id"]], key=key)
    assert replay.status_code == 409, replay.text
    assert store.get_comment_thread(task.project_id, other_thread["thread_id"])["state"] == "open"
