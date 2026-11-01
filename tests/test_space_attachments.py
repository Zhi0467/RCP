from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.space_chat import SpaceThreadOwner, stream_space_chat_run
from rcp.service import RunRequest
from tests.helpers import create_named_app, fake_launch_admitted, signed_in_client


@pytest.fixture
def space(manifest, tmp_path, monkeypatch):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    tasks = app.state.background_tasks
    fake_launch_admitted(monkeypatch, tasks)
    client = signed_in_client(app)
    with tasks.store.connection() as connection:
        connection.execute("DELETE FROM project_members")
    thread = client.get("/api/agent-threads").json()["threads"][0]
    base = f"/api/agent-threads/{thread['thread_id']}"
    client.put(base + "/browser", json={"browser_requested": False})
    return app, client, base, thread["thread_id"]


def upload(client, base, client_id):
    response = client.post(
        base + "/attachments",
        data={"client_id": client_id, "chat_id": str(uuid.uuid4())},
        files={"file": ("notes.txt", b"private attachment", "text/plain")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_space_upload_send_stage_transcript_and_retry(space, tmp_path, monkeypatch):
    app, client, base, thread_id = space
    store = app.state.background_tasks.store
    client_id = str(uuid.uuid4())
    uploaded = upload(client, base, client_id)
    body = dict(
        message="Read this",
        attachment_client_id=client_id,
        attachment_set_id=uploaded["attachment_set_id"],
    )
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    sent = client.post(base + "/turns", json=body, headers=headers)
    assert sent.status_code == 202, sent.text
    assert client.post(base + "/turns", json=body, headers=headers).json() == sent.json()
    task = store.agent_task(sent.json()["operation_id"])
    assert task is not None
    request = RunRequest.model_validate(task.request)
    store.record_agent_task_receipt(
        task.operation_id,
        "operation_created",
        {"kind": "space_chat", "attempt": task.attempt, "has_parent": False, "resumed": False},
    )
    assert request.attachment_batch_id == uploaded["attachment_set_id"]
    assert request.attachment_set_id is None and request.attachment_client_id is None
    monkeypatch.setattr("rcp.runs.space_chat.install_space_hidden_read_scope", lambda scope: None)
    seen = []

    async def provider(_provider, prompt, **kwargs):
        files = [p for root in kwargs["read_dirs"] for p in Path(root).rglob("*.txt")]
        assert len(files) == 1 and files[0].read_bytes() == b"private attachment"
        assert files[0].is_relative_to(kwargs["cwd"])
        assert str(files[0]) in prompt and uploaded["attachment"]["name"] in prompt
        assert (
            str(tmp_path / "data/chat-attachments")
            in kwargs["hidden_read_scope"].hidden_directories
        )
        seen.append(files[0])
        (kwargs["cwd"] / "outcome.json").write_text('{"status":"done"}')
        yield AgentEvent(event="done")

    monkeypatch.setattr(app.state.launcher, "stream", provider)
    owner = SpaceThreadOwner(store, app.state.catalog, thread_id)

    async def run():
        execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
        return [
            frame
            async for frame in stream_space_chat_run(
                owner, app.state.launcher, request, tmp_path / "data", execution
            )
        ]

    asyncio.run(run())
    assert seen
    transcript = client.get(base + "/transcript").json()
    assert transcript["messages"][0]["attachments"] == task.request["attachments"]
    store.fail_agent_task(task.operation_id, "retry attachment")
    retried = client.post(base + f"/tasks/{task.operation_id}/retry", json={})
    assert retried.status_code == 202, retried.text
    retry = store.agent_task(retried.json()["operation_id"])
    assert retry is not None
    assert retry.request["attachment_batch_id"] == request.attachment_batch_id
    assert retry.request["attachments"] == task.request["attachments"]


def test_space_attachment_owner_and_path_scope(space):
    app, client, base, thread_id = space
    store = app.state.background_tasks.store
    client_id = str(uuid.uuid4())
    uploaded = upload(client, base, client_id)
    params = dict(client_id=client_id, attachment_set_id=uploaded["attachment_set_id"])
    foreign = store.ensure_agent_threads("another-member", [], default_names=("Pip",))[0]
    foreign_base = f"/api/agent-threads/{foreign.thread_id}"
    assert (
        client.post(
            foreign_base + "/attachments", data=params, files={"file": ("note.txt", b"x")}
        ).status_code
        == 404
    )
    assert (
        client.delete(
            foreign_base + "/attachments/" + uploaded["attachment"]["attachment_id"], params=params
        ).status_code
        == 404
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE agent_threads SET user_id=? WHERE thread_id=?", ("third-member", thread_id)
        )
    body = dict(
        message="Read",
        attachment_client_id=client_id,
        attachment_set_id=uploaded["attachment_set_id"],
    )
    assert client.post(base + "/turns", json=body).status_code == 404
    own = client.get("/api/agent-threads").json()["threads"][0]
    assert client.post(f"/api/agent-threads/{own['thread_id']}/turns", json=body).status_code == 409


@pytest.mark.parametrize("recorded", [False, True])
def test_space_attachment_validation_and_claim_release(space, monkeypatch, recorded):
    app, client, base, _thread_id = space
    client_id = str(uuid.uuid4())
    uploaded = upload(client, base, client_id)
    fields = dict(attachment_client_id=client_id, attachment_set_id=uploaded["attachment_set_id"])
    for field, value in fields.items():
        assert (
            client.post(base + "/turns", json={"message": "Read", field: value}).status_code == 422
        )
    assert client.post(base + "/turns", json={"message": "", **fields}).status_code == 422

    start = app.state.background_tasks.start

    def refuse(*args, **kwargs):
        if recorded:
            start(*args, **kwargs)
        raise ValueError("admission refused")

    with monkeypatch.context() as patch:
        patch.setattr(app.state.background_tasks, "start", refuse)
        assert client.post(base + "/turns", json={"message": "Read", **fields}).status_code == 409
    removed = client.delete(
        base + "/attachments/" + uploaded["attachment"]["attachment_id"],
        params={"client_id": client_id, "attachment_set_id": uploaded["attachment_set_id"]},
    )
    assert removed.status_code == (422 if recorded else 200)
