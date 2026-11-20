from __future__ import annotations

from uuid import uuid4

from rcp.api.dependencies import get_graph_service
from rcp.runs.tasks.work import stream_work_run
from tests.helpers import create_named_app, signed_in_client, wait_for_task_response
from tests.test_chat_prompt_protocol import _RecordingLauncher


def test_private_project_agent_runs_on_a_graph_branch(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    launcher = _RecordingLauncher("private-branch-session")

    async def stream(_project_id, _kind, request, execution):
        task = store.agent_task(execution.operation_id)
        service = get_graph_service(app.state.catalog, project_id, task.graph_target.branch_id)
        async for frame in stream_work_run(
            service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    with signed_in_client(app) as client:
        thread = next(
            row
            for row in client.get("/api/agent-threads").json()["threads"]
            if row["project_id"] == project_id
        )
        branch_id = str(uuid4())
        fork = client.post(
            f"/api/projects/{project_id}/graph-refs",
            json={"request_id": branch_id, "at": "main@0"},
        )
        assert fork.status_code == 201, fork.text
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            params={"branch_id": branch_id},
            json={"chat_id": thread["thread_id"], "message": "Work on this branch", "mode": "work"},
        )
        assert response.status_code == 202, response.text
        task = wait_for_task_response(client, project_id, response.json()["operation_id"])
        assert task["status"] == "succeeded", task
        record = store.agent_task(task["operation_id"])
        assert record.graph_target.branch_id == branch_id
        assert record.authorized_by.user_id == store.private_chat_owner(thread["thread_id"])
        branch = client.get(
            f"/api/projects/{project_id}/chats/{thread['thread_id']}",
            params={"branch_id": branch_id},
        )
        assert branch.status_code == 200, branch.text
        assert len(branch.json()["messages"]) == 2
        main = client.get(f"/api/projects/{project_id}/chats/{thread['thread_id']}")
        assert main.status_code == 200, main.text
        assert main.json()["messages"] == []
