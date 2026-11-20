from __future__ import annotations

import uuid

from tests.helpers import create_named_app, fake_launch_admitted, signed_in_client


def test_project_agent_new_and_legacy_routes_share_turn_and_transcript(
    manifest, tmp_path, monkeypatch
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    with signed_in_client(app) as client:
        thread = client.get("/api/agent-threads").json()["threads"][1]
        project = thread["project_id"]
        agent = thread["thread_id"]
        base = f"/api/agent-threads/{agent}"
        body = {"chat_id": agent, "chat_scope": "project", "message": "hello"}
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        old = client.post(f"/api/projects/{project}/tasks/project_chat", json=body, headers=headers)
        new = client.post(base + "/turns", json=body, headers=headers)
        assert old.status_code == new.status_code == 202, new.text
        assert old.json() == new.json()
        operation = old.json()["operation_id"]
        old_task = client.get(f"/api/projects/{project}/tasks/{operation}")
        new_task = client.get(f"{base}/tasks/{operation}")
        assert old_task.status_code == new_task.status_code == 200, new_task.text
        assert old_task.json() == new_task.json()
        old_transcript = client.get(f"/api/projects/{project}/chats/{agent}")
        new_transcript = client.get(base + "/transcript")
        assert old_transcript.status_code == new_transcript.status_code == 200
        assert old_transcript.json() == new_transcript.json()


def test_unborn_agent_uses_validated_project_hint_and_keeps_its_identity(
    manifest, tmp_path, monkeypatch
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    tasks = app.state.background_tasks
    fake_launch_admitted(monkeypatch, tasks)
    project = app.state.default_project_id
    agent = str(uuid.uuid4())
    base = f"/api/agent-threads/{agent}"
    hint = {"project_id": project}
    with signed_in_client(app) as client:
        assert client.get(base + "/browser").status_code == 404
        assert client.get(base + "/browser", params=hint).status_code == 200
        assert (
            client.put(base + "/browser", params=hint, json={"browser_requested": True}).status_code
            == 200
        )
        assert tasks.store.agent(agent) is None
        started = client.post(
            base + "/turns",
            params=hint,
            json={"chat_id": None, "chat_scope": "project", "message": "hello"},
        )
        assert started.status_code == 202, started.text
        assert tasks.store.agent(agent).project_id == project
        assert client.get(base + "/browser").json() == {"browser_requested": True}
        assert client.get(base + "/browser", params={"project_id": "other"}).status_code == 404
        operation = started.json()["operation_id"]
        other = client.get("/api/agent-threads").json()["threads"][1]["thread_id"]
        assert client.get(f"/api/agent-threads/{other}/tasks/{operation}").status_code == 404
        assert (
            client.post(
                base + "/turns", json={"chat_id": other, "message": "wrong agent"}
            ).status_code
            == 422
        )
