from __future__ import annotations

from uuid import uuid4

import pytest

from rcp.service import RunRequest
from tests.helpers import authorized_human, create_named_app, fake_launch_admitted, signed_in_client
from tests.test_storage import _task_record


@pytest.mark.parametrize("single_entry", [False, True])
def test_agent_queue_starts_with_the_sending_members_authority(
    manifest, tmp_path, monkeypatch, single_entry
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    tasks = app.state.background_tasks
    store = tasks.store
    fake_launch_admitted(monkeypatch, tasks)
    with signed_in_client(app) as client:
        actor = authorized_human(store)
        threads = client.get("/api/agent-threads").json()["threads"]
        agent_id = threads[0]["thread_id"] if single_entry else str(uuid4())
        project_id = store.space_id if single_entry else app.state.default_project_id
        kind = "space_chat" if single_entry else "project_chat"
        store.create_agent_task(
            _task_record(
                store.now(),
                operation_id="busy",
                project_id=project_id,
                kind=kind,
                status="running",
                authorized_by=actor,
                request=RunRequest(chat_id=agent_id, chat_scope="project", mode="work").model_dump(
                    mode="json"
                ),
            )
        )
        base = f"/api/agent-threads/{agent_id}"
        response = client.post(
            base + "/queue",
            json={"message": "Next step"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202, response.text
        row = store.agent_thread_queue_item(response.json()["queue_id"])
        assert row.user_id == actor.user_id
        assert row.operation_id is None
        assert len(client.get(base + "/queue").json()["items"]) == 1
        store.complete_agent_task("busy", applied_revision=None, result={})
        app.state.reconcile_thread_handoffs()
        row = store.agent_thread_queue_item(row.queue_id)
        task = store.agent_task(row.operation_id)
        assert task.authorized_by == actor
        assert task.request["chat_id"] == agent_id
        assert task.request["message"] == "Next step"
        assert task.kind == kind
