from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from rcp.api.dependencies import get_graph_service
from rcp.runs.tasks.discuss import stream_discuss_run
from tests.helpers import create_named_app, discuss_turn, signed_in_client, wait_for_task_response
from tests.test_chat_prompt_protocol import _RecordingLauncher

SESSION = "019a0000-0000-7000-8000-0000000000aa"


def test_a_swept_chat_starts_fresh_and_refuses_branching(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    launcher = _RecordingLauncher(SESSION)

    async def stream(_project_id, _kind, request, execution):
        service = get_graph_service(app.state.catalog, project_id, None)
        async for frame in stream_discuss_run(
            service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    chat_id = str(uuid4())
    url = f"/api/projects/{project_id}/tasks/project_chat"

    def send():
        response = discuss_turn(client, url, {"chat_id": chat_id, "message": "Look"})
        assert response.status_code == 202, response.text
        task = wait_for_task_response(client, project_id, response.json()["operation_id"])
        assert task["status"] == "succeeded", task.get("error")
        return task

    send()
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET created_at = ?, updated_at = ?", (old, old))
    targets = store.provider_session_sweep_targets(store.now())
    assert [target.session_id for target in targets] == [SESSION]
    assert store.mark_provider_sessions_swept(targets, store.now())

    branch_id = str(uuid4())
    created = client.post(
        f"/api/projects/{project_id}/graph-refs", json={"request_id": branch_id, "at": "main@0"}
    )
    assert created.status_code == 201, created.text
    probe = client.get(f"/api/projects/{project_id}/chats/{chat_id}/branch")
    branched = client.post(
        f"/api/projects/{project_id}/chats/{chat_id}/branch",
        json={
            "graph_target": {"kind": "branch", "branch_id": branch_id},
            "request_id": str(uuid4()),
        },
    )
    for response in (probe, branched):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "chat_session_expired"

    launcher.native_session_id = "019a0000-0000-7000-8000-0000000000bb"
    send()
    assert launcher.sessions == [None, None]
    with store.connection() as connection:
        statuses = [row[0] for row in connection.execute("SELECT status FROM graph_runs")]
    assert statuses == ["succeeded", "succeeded"]
