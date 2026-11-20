from __future__ import annotations

import uuid

from tests.helpers import (
    append_fixture_patch,
    create_named_app,
    fake_launch_admitted,
    seed_patch,
    signed_in_client,
)


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


def test_a_node_agents_send_continues_its_node_whatever_scope_the_client_names(
    manifest, tmp_path, monkeypatch
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    tasks = app.state.background_tasks
    fake_launch_admitted(monkeypatch, tasks)
    project = app.state.default_project_id
    agent = str(uuid.uuid4())
    node = "hyp/replanning-restores-plasticity"
    with signed_in_client(app) as client:
        first = client.post(
            f"/api/projects/{project}/tasks/node_chat",
            json={"node_id": node, "chat_id": agent, "message": "first", "mode": "work"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert first.status_code == 202, first.text
        tasks.store.fail_agent_task(first.json()["operation_id"], "settled for the test")
        sent = client.post(
            f"/api/agent-threads/{agent}/turns",
            params={"project_id": project},
            json={"chat_id": agent, "chat_scope": "project", "mode": "work", "message": "next"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert sent.status_code == 202, sent.text
        record = tasks.store.agent_task(sent.json()["operation_id"])
        assert record.kind == "node_chat"
        assert record.request["node_id"] == node


def _node_chat_split_by_a_project_turn(manifest, tmp_path, monkeypatch, *, paused=True):
    """A 0.4.49-shaped chat: a node turn on one session, then a project turn on another."""
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    tasks = app.state.background_tasks
    fake_launch_admitted(monkeypatch, tasks)
    store = tasks.store
    agent = str(uuid.uuid4())
    node = "hyp/replanning-restores-plasticity"
    profile = {"provider": "codex", "model": "", "reasoning": "medium", "run_on": "backend"}
    for operation_id, scope, session in (
        ("node-a", {"chat_scope": "node", "node_id": node}, "session-a"),
        ("project-b", {"chat_scope": "project"}, "session-b"),
    ):
        turn = _chat_task_execution(
            app, RunRequest(chat_id=agent, message=operation_id, **scope, **profile), operation_id
        )
        turn.checkpoint_stage("backend", str(tmp_path / operation_id))
        store.checkpoint_agent_task(operation_id, native_session_id=session)
        if operation_id == "node-a" or not paused:
            store.complete_agent_task(operation_id, applied_revision=None, result={})
    if paused:
        store.pause_agent_task("project-b")
    return app, store, agent, node, profile


def test_a_split_node_chat_is_one_conversation(manifest, tmp_path, monkeypatch):
    """Session, overlap and paused closure span the chat's node and project turns."""
    from rcp.runs.agent_threads import agent_turn_busy

    app, store, agent, node, profile = _node_chat_split_by_a_project_turn(
        manifest, tmp_path, monkeypatch
    )
    project = app.state.default_project_id
    assert store.has_resumable_paused_chat_task(project, "node_chat", agent)
    with signed_in_client(app) as client:
        sent = client.post(
            f"/api/agent-threads/{agent}/turns",
            params={"project_id": project},
            json={"chat_scope": "project", "mode": "work", "message": "next", **profile},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert sent.status_code == 202, sent.text
        record = store.agent_task(sent.json()["operation_id"])
        assert record.kind == "node_chat"
        assert record.request["node_id"] == node
        assert record.request["session_id"] == "session-b"
        resumed = client.post(
            f"/api/agent-threads/{agent}/tasks/project-b/resume",
            json={},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert resumed.status_code == 409, resumed.text
        assert store.has_active_chat_task(project, "project_chat", agent)
        store.complete_agent_task(record.operation_id, applied_revision=None, result={})
        assert not store.has_resumable_paused_chat_task(project, "project_chat", agent)
        assert not agent_turn_busy(store, agent)


def test_a_queued_message_after_a_mislabelled_project_turn_stays_on_the_node(
    manifest, tmp_path, monkeypatch
):
    app, store, agent, node, _profile = _node_chat_split_by_a_project_turn(
        manifest, tmp_path, monkeypatch, paused=False
    )
    with signed_in_client(app) as client:
        queued = client.post(
            f"/api/agent-threads/{agent}/queue",
            json={"message": "queued step"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert queued.status_code == 202, queued.text
        app.state.reconcile_thread_handoffs()
    row = store.agent_thread_queue_item(queued.json()["queue_id"])
    task = store.agent_task(row.operation_id)
    assert task.kind == "node_chat"
    assert task.request["node_id"] == node


def test_a_resumed_session_is_seeded_with_the_exchanges_it_missed(manifest, tmp_path, monkeypatch):
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.agent_threads import thread_history_seed
    from rcp.runs.chat import _append_chat_exchange
    from rcp.service import RunRequest
    from tests.test_api import _chat_task_execution

    app, store, agent, node, profile = _node_chat_split_by_a_project_turn(
        manifest, tmp_path, monkeypatch, paused=False
    )
    service = app.state.service
    for operation_id in ("node-a", "project-b"):
        turn = store.agent_task(operation_id)
        _append_chat_exchange(
            service,
            RunRequest.model_validate(turn.request),
            f"answer in {operation_id}",
            turn.native_session_id,
            None,
            execution=AgentTaskExecution(
                operation_id=operation_id, store=store, control=AgentProcessControl()
            ),
        )

    def resume(session_id, operation_id):
        request = RunRequest(
            chat_id=agent, chat_scope="node", node_id=node, session_id=session_id, **profile
        )
        _chat_task_execution(app, request, operation_id)
        seed = thread_history_seed(service, request, operation_id)
        store.complete_agent_task(operation_id, applied_revision=None, result={})
        return seed

    assert resume("session-b", "resumes-b") == ""
    seed = resume("session-a", "resumes-a")
    assert "answer in project-b" in seed
    assert "answer in node-a" not in seed
