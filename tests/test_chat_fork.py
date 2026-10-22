from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from rcp.api.dependencies import get_graph_service
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.tasks.discuss import stream_discuss_run
from rcp.runs.tasks.work import stream_work_run
from tests.helpers import changed_values, create_named_app, signed_in_client, wait_for_task_response
from tests.test_chat_prompt_protocol import _RecordingLauncher


@pytest.mark.parametrize("mode", ["discuss", "work"])
@pytest.mark.parametrize("fail_fork", [False, True])
def test_branch_chat_defers_fork_and_keeps_source_unchanged(manifest, tmp_path, mode, fail_fork):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id

    class Launcher(_RecordingLauncher):
        fail_next_fork = False
        failed_kwargs = None

        async def stream(self, provider, prompt, **kwargs):
            if self.fail_next_fork and kwargs.get("fork_session_id"):
                self.fail_next_fork = False
                self.failed_kwargs = kwargs
                raise RuntimeError("Fork transport unavailable")
            async for event in super().stream(provider, prompt, **kwargs):
                yield event

    launcher = Launcher("source-session")
    redirect_host = False

    async def stream(_project_id, _kind, request, execution):
        service = get_graph_service(
            app.state.catalog,
            project_id,
            store.agent_task(execution.operation_id).graph_target.branch_id,
        )
        run = stream_work_run if request.mode == "work" else stream_discuss_run
        machine = service.manifest.machine_map[request.run_on]
        original_host = machine.host
        if redirect_host:
            machine.host = "changed-execution-host"
        try:
            async for frame in run(
                service, launcher, request, tmp_path / "data", execution=execution
            ):
                yield frame
        finally:
            machine.host = original_host

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    chat_id = str(uuid4())

    def send(target_chat, branch_id=None, *, fail=False, conflicting_profile=False):
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={
                "chat_id": target_chat,
                "message": "Investigate this",
                "mode": mode,
                **(
                    {
                        "provider": "opencode",
                        "model": "wrong-model",
                        "reasoning": "wrong",
                        "run_on": "missing-machine",
                    }
                    if conflicting_profile
                    else {}
                ),
            },
            params={"branch_id": branch_id} if branch_id else {},
        )
        assert response.status_code == 202, response.text
        task = wait_for_task_response(client, project_id, response.json()["operation_id"])
        if fail:
            assert task["status"] == "failed", task
            assert task["can_retry"]
        else:
            assert task["status"] == "succeeded", task.get("error")
        return store.agent_task(task["operation_id"])

    source = send(chat_id)
    assert source is not None
    source_before = source.model_dump()
    source_transcript = client.get(f"/api/projects/{project_id}/chats/{chat_id}").json()
    source_stage = Path(source.stage_root)
    source_files = {
        str(path.relative_to(source_stage)): path.read_bytes()
        for path in source_stage.rglob("*")
        if path.is_file()
    }
    branch_id = str(uuid4())
    fork_graph = client.post(
        f"/api/projects/{project_id}/graph-refs",
        json={"request_id": branch_id, "at": "main@0"},
    )
    assert fork_graph.status_code == 201, fork_graph.text
    target = {"kind": "branch", "branch_id": branch_id}
    response = client.post(
        f"/api/projects/{project_id}/chats/{chat_id}/branch", json={"graph_target": target}
    )
    assert response.status_code == 200, response.text
    branched = response.json()
    assert branched["messages"] == []
    assert branched["chat_id"] != chat_id
    assert branched["graph_target"] == target
    assert branched["origin"]["source_chat_id"] == chat_id
    assert branched["origin"]["source_operation_id"] == source.operation_id
    assert branched["origin"]["run_on"] == source.request["run_on"]
    assert len(launcher.prompts) == 1
    child_id = branched["chat_id"]
    assert (
        client.get(
            f"/api/projects/{project_id}/chats/{child_id}", params={"branch_id": branch_id}
        ).json()
        == branched
    )
    assert child_id in {
        item["chat_id"]
        for item in client.get(
            f"/api/projects/{project_id}/chats", params={"branch_id": branch_id}
        ).json()["items"]
    }
    assert (
        client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={"chat_id": child_id, "message": "Wrong target"},
        ).status_code
        == 422
    )
    launcher.native_session_id = "forked-session"
    launcher.fail_next_fork = fail_fork
    forked = send(child_id, branch_id, fail=fail_fork, conflicting_profile=True)
    if fail_fork:
        assert launcher.failed_kwargs["fork_session_id"] == "source-session"
        assert launcher.failed_kwargs.get("session_id") is None
        assert forked.native_session_id is None
        redirect_host = True
        refused_retry = client.post(
            f"/api/projects/{project_id}/tasks/{forked.operation_id}/retry", json={}
        )
        assert refused_retry.status_code == 202, refused_retry.text
        refused = wait_for_task_response(client, project_id, refused_retry.json()["operation_id"])
        redirect_host = False
        assert refused["status"] == "failed", refused
        assert refused["can_retry"]
        assert refused["native_session_id"] is None
        assert len(launcher.prompts) == 1
        forked = store.agent_task(refused["operation_id"])
        retry = client.post(
            f"/api/projects/{project_id}/tasks/{forked.operation_id}/retry", json={}
        )
        assert retry.status_code == 202, retry.text
        finished = wait_for_task_response(client, project_id, retry.json()["operation_id"])
        assert finished["status"] == "succeeded", finished.get("error")
        forked = store.agent_task(finished["operation_id"])
    assert forked.graph_target.model_dump() == target
    delta = changed_values(launcher.prompts[1])
    assert delta["conversation_fork.graph_target.kind"] == "branch"
    assert delta["conversation_fork.graph_target.branch_id"] == branch_id
    assert launcher.launch_kwargs[1]["fork_session_id"] == "source-session"
    assert launcher.sessions[1] is None
    assert launcher.workspaces[1] != launcher.workspaces[0]
    assert launcher.launch_kwargs[1]["fork_source_cwd"] == str(launcher.workspaces[0])
    assert store.agent_task(source.operation_id).model_dump() == source_before
    assert client.get(f"/api/projects/{project_id}/chats/{chat_id}").json() == source_transcript
    assert source_files == {
        str(path.relative_to(source_stage)): path.read_bytes()
        for path in source_stage.rglob("*")
        if path.is_file()
    }
    send(child_id, branch_id)
    assert launcher.sessions[2] == "forked-session"
    assert launcher.launch_kwargs[2].get("fork_session_id") is None
    # Only the fork turn moves the conversation; later turns carry no fork fact.
    assert not any(
        key.startswith("conversation_fork.") for key in changed_values(launcher.prompts[2])
    )
    if mode == "discuss" and not fail_fork:
        back_to_main = client.post(
            f"/api/projects/{project_id}/chats/{child_id}/branch",
            json={"graph_target": {"kind": "main"}},
        )
        assert back_to_main.status_code == 200, back_to_main.text
        assert back_to_main.json()["graph_target"]["kind"] == "main"
        assert back_to_main.json()["origin"]["source_graph_target"] == target
        assert len(launcher.prompts) == 3
        # Leaving a branch, the fork turn names main and forks the branch chat's own session.
        launcher.native_session_id = "main-session"
        send(back_to_main.json()["chat_id"])
        assert launcher.launch_kwargs[3]["fork_session_id"] == "forked-session"
        moved = changed_values(launcher.prompts[3])
        assert moved["conversation_fork.graph_target.kind"] == "main"
        assert "conversation_fork.graph_target.branch_id" not in moved


def test_branch_chat_rejects_missing_source_session(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    with pytest.raises(ValueError):
        store.create_chat_fork(app.state.default_project_id, str(uuid4()), GraphTargetRef())


def test_node_chat_branches_to_project_scope_when_target_lacks_node(manifest, tmp_path):
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    source_id = str(uuid4())
    execution = _chat_task_execution(
        app,
        RunRequest(
            chat_id=source_id,
            chat_scope="node",
            node_id="rq/source-only",
            provider="codex",
            model="",
            reasoning="medium",
            run_on="laptop",
        ),
        "source-node-turn",
    )
    execution.checkpoint_stage("", str(tmp_path / "source-stage"))
    store.checkpoint_agent_task(execution.operation_id, native_session_id="node-session")
    store.complete_agent_task(execution.operation_id, applied_revision=None, result={})
    client = signed_in_client(app)
    branch_id = str(uuid4())
    response = client.post(
        f"/api/projects/{project_id}/graph-refs",
        json={"request_id": branch_id, "at": "main@0"},
    )
    assert response.status_code == 201
    response = client.post(
        f"/api/projects/{project_id}/chats/{source_id}/branch",
        json={"graph_target": {"kind": "branch", "branch_id": branch_id}},
    )
    assert response.status_code == 200, response.text
    assert (response.json()["kind"], response.json()["node_id"]) == ("project_chat", None)
    assert store.agent_task(execution.operation_id).request["node_id"] == "rq/source-only"
