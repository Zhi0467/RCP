from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from rcp.api.dependencies import get_graph_service
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.session_master import recorded_master_values
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
    branch_request = {"graph_target": target, "request_id": str(uuid4())}
    response = client.post(
        f"/api/projects/{project_id}/chats/{chat_id}/branch", json=branch_request
    )
    assert response.status_code == 200, response.text
    branched = response.json()
    replayed = client.post(
        f"/api/projects/{project_id}/chats/{chat_id}/branch", json=branch_request
    )
    assert replayed.json() == branched
    assert branched["messages"] == []
    assert branched["chat_id"] != chat_id
    assert branched["graph_target"] == target
    assert branched["origin"]["source_chat_id"] == chat_id
    assert branched["origin"]["source_operation_id"] == source.operation_id
    assert branched["origin"]["run_on"] == source.request["run_on"]
    assert len(launcher.prompts) == 1
    child_id = branched["chat_id"]
    # A chat without its own native session refuses before any graph branch is made.
    branchable = f"/api/projects/{project_id}/chats/{{}}/branch"
    assert client.get(branchable.format(chat_id)).status_code == 204
    assert client.get(branchable.format(child_id)).status_code == 422
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
            json={"graph_target": {"kind": "main"}, "request_id": str(uuid4())},
        )
        assert back_to_main.status_code == 200, back_to_main.text
        assert back_to_main.json()["graph_target"]["kind"] == "main"
        assert back_to_main.json()["origin"]["source_graph_target"] == target
        assert len(launcher.prompts) == 3
        # Leaving a branch, the fork turn names main and forks the branch chat's own session.
        launcher.native_session_id = "main-session"
        on_main = send(back_to_main.json()["chat_id"])
        assert launcher.launch_kwargs[3]["fork_session_id"] == "forked-session"
        # The replacing master itself states the new target, so it survives a compaction.
        current = (recorded_master_values(store, on_main.operation_id) or {}).get("current")
        assert isinstance(current, dict) and current["graph_target"] == "main"
        moved = changed_values(launcher.prompts[3])
        assert moved["conversation_fork.graph_target.kind"] == "main"
        assert "conversation_fork.graph_target.branch_id" not in moved


def test_branch_chat_rejects_missing_source_session(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    with pytest.raises(ValueError):
        store.create_chat_fork(
            app.state.default_project_id, str(uuid4()), GraphTargetRef(), str(uuid4())
        )


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
        json={
            "graph_target": {"kind": "branch", "branch_id": branch_id},
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 200, response.text
    assert (response.json()["kind"], response.json()["node_id"]) == ("project_chat", None)
    assert store.agent_task(execution.operation_id).request["node_id"] == "rq/source-only"


def test_first_fork_turn_records_the_remote_source_workspace(manifest, tmp_path):
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    profile = {"provider": "codex", "model": "", "reasoning": "medium", "run_on": "laptop"}
    source_id = str(uuid4())
    source = _chat_task_execution(
        app, RunRequest(chat_id=source_id, chat_scope="project", **profile), "remote-source"
    )
    source.checkpoint_stage("gpu-host", "/remote/stage")
    store.checkpoint_agent_task(source.operation_id, native_session_id="remote-session")
    store.complete_agent_task(source.operation_id, applied_revision=None, result={})
    fork = store.create_chat_fork(project_id, source_id, GraphTargetRef(), str(uuid4()))
    _chat_task_execution(
        app, RunRequest(chat_id=fork.chat_id, chat_scope="project", **profile), "fork-turn"
    )
    request = store.agent_task("fork-turn").request
    assert request["fork_session_id"] == "remote-session"
    assert request["fork_source_host"] == "gpu-host"
    assert request["fork_source_cwd"] == "/remote/stage/workspace"


def test_fork_turn_retry_after_pause_continues_and_first_turn_rechecks_source(manifest, tmp_path):
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution, _named_test_authorizer

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    profile = {"provider": "codex", "model": "", "reasoning": "medium", "run_on": "laptop"}
    source_id = str(uuid4())
    source = _chat_task_execution(
        app, RunRequest(chat_id=source_id, chat_scope="project", **profile), "source-turn"
    )
    source.checkpoint_stage("", str(tmp_path / "source-stage"))
    store.checkpoint_agent_task(source.operation_id, native_session_id="source-session")
    store.complete_agent_task(source.operation_id, applied_revision=None, result={})
    fork, unused = (
        store.create_chat_fork(project_id, source_id, GraphTargetRef(), str(uuid4()))
        for _ in range(2)
    )
    first = _chat_task_execution(
        app, RunRequest(chat_id=fork.chat_id, chat_scope="project", **profile), "fork-turn"
    )
    first.checkpoint_stage("", str(tmp_path / "fork-stage"))
    store.checkpoint_agent_task(first.operation_id, native_session_id="forked-session")
    store.pause_agent_task(first.operation_id)

    async def idle(*_args, **_kwargs):
        return
        yield

    app.state.background_tasks.stream = idle
    retried = app.state.background_tasks.retry(
        first.operation_id, authorized_by=_named_test_authorizer(store)
    )
    assert retried.request["session_id"] == "forked-session"
    assert retried.request["fork_session_id"] is None

    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET history_only = 1 WHERE operation_id = ?",
            (source.operation_id,),
        )
    with pytest.raises(ValueError):
        _chat_task_execution(
            app, RunRequest(chat_id=unused.chat_id, chat_scope="project", **profile), "late-turn"
        )


def _settled_source_and_fork(app, tmp_path, profile):
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution

    store = app.state.catalog.store
    source_id = str(uuid4())
    source = _chat_task_execution(
        app, RunRequest(chat_id=source_id, chat_scope="project", **profile), "source-turn"
    )
    source.checkpoint_stage("", str(tmp_path / "source-stage"))
    store.checkpoint_agent_task(source.operation_id, native_session_id="source-session")
    store.complete_agent_task(source.operation_id, applied_revision=None, result={})
    fork = store.create_chat_fork(
        app.state.default_project_id, source_id, GraphTargetRef(), str(uuid4())
    )
    return source_id, fork


@pytest.mark.parametrize("change", ["source_moved", "source_gone"])
def test_a_failed_first_fork_never_reforks_a_changed_source(manifest, tmp_path, change):
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution, _named_test_authorizer

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    profile = {"provider": "codex", "model": "", "reasoning": "medium", "run_on": "laptop"}
    source_id, fork = _settled_source_and_fork(app, tmp_path, profile)
    first = _chat_task_execution(
        app, RunRequest(chat_id=fork.chat_id, chat_scope="project", **profile), "fork-turn"
    )
    assert store.agent_task("fork-turn").request["fork_source_operation_id"] == "source-turn"
    store.fail_agent_task(first.operation_id, "Fork transport unavailable")
    if change == "source_moved":
        later = _chat_task_execution(
            app, RunRequest(chat_id=source_id, chat_scope="project", **profile), "source-later"
        )
        store.checkpoint_agent_task(later.operation_id, native_session_id="source-session")
        store.complete_agent_task(later.operation_id, applied_revision=None, result={})
    else:
        store.mark_agent_tasks_history_only(["source-turn"])
    launched = []

    async def record_launch(*args, **_kwargs):
        launched.append(args)
        return
        yield

    app.state.background_tasks.stream = record_launch
    authorizer = _named_test_authorizer(store)
    refused = app.state.background_tasks.retry(first.operation_id, authorized_by=authorizer)

    assert (refused.status, refused.failure_kind) == ("failed", "session_refused")
    assert refused.error == (
        "chat_fork_source_moved" if change == "source_moved" else "chat_fork_source_unavailable"
    )
    assert refused.request["fork_session_id"] == "source-session"
    assert not store.agent_task(refused.operation_id).can_retry
    assert launched == []
    # A new message refuses the same way; it never starts a fresh session instead.
    _chat_task_execution(
        app, RunRequest(chat_id=fork.chat_id, chat_scope="project", **profile), "next-message"
    )
    assert store.agent_task("next-message").error == refused.error


def test_a_queued_fork_rechecks_its_source_at_launch(manifest, tmp_path):
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    profile = {"provider": "codex", "model": "", "reasoning": "medium", "run_on": "laptop"}
    source_id, fork = _settled_source_and_fork(app, tmp_path, profile)
    _chat_task_execution(
        app, RunRequest(chat_id=fork.chat_id, chat_scope="project", **profile), "fork-turn"
    )
    assert store.chat_fork_launch_refusal("fork-turn") is None
    later = _chat_task_execution(
        app, RunRequest(chat_id=source_id, chat_scope="project", **profile), "source-later"
    )
    store.checkpoint_agent_task(later.operation_id, native_session_id="source-session")
    store.complete_agent_task(later.operation_id, applied_revision=None, result={})

    assert store.chat_fork_launch_refusal("fork-turn") == "chat_fork_source_moved"


def test_restore_detaches_unfinished_forks_to_fresh_sessions(manifest, tmp_path):
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    profile = {"provider": "codex", "model": "", "reasoning": "medium", "run_on": "laptop"}
    source_id, failed_fork = _settled_source_and_fork(app, tmp_path, profile)
    unsent_fork = store.create_chat_fork(
        app.state.default_project_id, source_id, GraphTargetRef(), str(uuid4())
    )
    first = _chat_task_execution(
        app, RunRequest(chat_id=failed_fork.chat_id, chat_scope="project", **profile), "fork-turn"
    )
    store.fail_agent_task(first.operation_id, "Fork transport unavailable")

    store.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")

    for chat_id, operation_id in (
        (failed_fork.chat_id, "after-failed"),
        (unsent_fork.chat_id, "after-unsent"),
    ):
        _chat_task_execution(
            app, RunRequest(chat_id=chat_id, chat_scope="project", **profile), operation_id
        )
        request = store.agent_task(operation_id).request
        assert (request["fork_session_id"], request["session_id"]) == (None, None)
    origins = {
        summary["chat_id"]: summary.get("origin")
        for summary in store.chat_inventory(app.state.default_project_id)
    }
    assert origins[unsent_fork.chat_id]["source_chat_id"] == source_id
