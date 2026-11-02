from __future__ import annotations

from pathlib import Path

from tests.helpers import create_named_app, signed_in_client


def test_threads_created_once_with_names_browser_and_owner_controls(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        response = client.get("/api/agent-threads")
        assert response.status_code == 200
        rows = response.json()["threads"]
        assert [row["kind"] for row in rows] == ["space", "project"]
        assert [row["agent_name"] for row in rows] == ["Pip", "Puck"]
        assert [row["latest_operation_id"] for row in rows] == [None, None]
        assert all(row["browser_requested"] for row in rows)
        assert client.get("/api/agent-threads").json()["threads"] == rows
        for row in rows:
            base = "/api/agent-threads/" + row["thread_id"]
            renamed = client.post(base + "/title", json={"title": "Scout"})
            assert renamed.status_code == 200
            assert renamed.json()["agent_name"] == "Scout"
            assert client.put(base + "/browser", json={"browser_requested": False}).json() == {
                "browser_requested": False
            }
            transcript = client.get(base + "/transcript")
            assert transcript.status_code == 200
            assert transcript.json()["messages"] == []


def test_private_exchange_and_fresh_seed_stay_with_the_thread(manifest, tmp_path):
    import json

    from rcp.runs.agent_threads import thread_history_seed
    from rcp.runs.chat import _append_chat_exchange
    from rcp.service import RunRequest

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        row = next(
            row
            for row in client.get("/api/agent-threads").json()["threads"]
            if row["kind"] == "project"
        )
        request = RunRequest(
            chat_id=row["thread_id"],
            chat_scope="project",
            message="earlier private question",
            source="handoff",
        )
        service = app.state.service
        _append_chat_exchange(service, request, "earlier private answer", None, None)
        paths = list((service.history.workspace.root / "chat").glob("*.jsonl"))
        assert len(paths) == 1 and paths[0].name.startswith("private-")
        records = [json.loads(line) for line in paths[0].read_text().splitlines()]
        assert all(record["privateOwner"] for record in records)
        assert records[0]["source"] == "handoff"
        assert "earlier private answer" in thread_history_seed(service, request)
        assert (
            thread_history_seed(service, request.model_copy(update={"session_id": "existing"}))
            == ""
        )


def test_thread_admission_refuses_discuss(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        row = next(
            row
            for row in client.get("/api/agent-threads").json()["threads"]
            if row["kind"] == "project"
        )
        response = client.post(
            "/api/projects/" + row["project_id"] + "/tasks/project_chat",
            json={
                "chat_scope": "project",
                "chat_id": row["thread_id"],
                "message": "hello",
                "mode": "discuss",
            },
        )
        assert response.status_code == 422


def test_private_reply_notification_is_unread_and_owner_bound(manifest, tmp_path):
    from datetime import datetime, timedelta

    from rcp.storage import AgentTaskRecord
    from tests.helpers import authorized_human

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    device = store.register_notification_device(store.local_owner.user_id)
    with signed_in_client(app) as client:
        row = next(
            row
            for row in client.get("/api/agent-threads").json()["threads"]
            if row["kind"] == "project"
        )
        now = store.now()
        task = AgentTaskRecord(
            operation_id="thread-reply",
            project_id=row["project_id"],
            kind="project_chat",
            status="succeeded",
            status_message="Done",
            created_at=now,
            updated_at=now,
            finished_at=now,
            request={
                "chat_id": row["thread_id"],
                "chat_scope": "project",
                "message": "hello",
                "mode": "work",
            },
            authorized_by=authorized_human(store),
        )
        store.create_agent_task(task)
        sender = app.state.notification_sender
        sender.run_pass()
        # An ordinary finished reply only marks the thread unread; a failure pushes.
        assert not [n for n in store.notification_outbox() if n["kind"] == "agent_thread"]
        store.create_agent_task(
            task.model_copy(update={"operation_id": "thread-failure", "status": "failed"})
        )
        sender.run_pass()
        notices = [
            notice for notice in store.notification_outbox() if notice["kind"] == "agent_thread"
        ]
        assert len(notices) == 1
        assert notices[0]["device_id"] == device["device_id"]
        pending = client.get(f"/api/notifications/devices/{device['device_id']}/pending")
        assert pending.status_code == 200
        assert [item["reason"] for item in pending.json()] == ["agent_thread"]
        assert pending.json()[0]["deep_link"] == f"#/agent/{row['thread_id']}"
        sender.run_pass()
        assert (
            len(
                [
                    notice
                    for notice in store.notification_outbox()
                    if notice["kind"] == "agent_thread"
                ]
            )
            == 1
        )
        store.mark_chat_read(
            row["project_id"],
            row["thread_id"],
            store.local_owner.user_id,
            datetime.fromisoformat(now),
        )
        assert not store.guard_notification_delivery(
            device["device_id"], notices[0]["notification_id"]
        )
        # A schedule wake's reply is the reminder itself, so it pushes.
        later = (datetime.fromisoformat(now) + timedelta(seconds=1)).isoformat()
        store.create_agent_task(
            task.model_copy(
                update={
                    "operation_id": "schedule-reply",
                    "created_at": later,
                    "updated_at": later,
                    "finished_at": later,
                    "request": {**task.request, "trigger": "schedule"},
                }
            )
        )
        sender.run_pass()
        assert later in {
            notice["created_at"]
            for notice in store.notification_outbox()
            if notice["kind"] == "agent_thread"
        }


def test_fresh_thread_seed_is_staged_in_its_private_stage(manifest, tmp_path):
    from typing import cast

    from rcp.agents import AgentLauncher
    from rcp.core.models import HiddenReadScope
    from rcp.runs.chat import _append_chat_exchange
    from rcp.runs.tasks.work import stream_work_run
    from rcp.service import RunRequest
    from tests.helpers import append_fixture_patch, seed_patch, wait_for_task_response
    from tests.test_chat_prompt_protocol import _RecordingLauncher

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    launcher = _RecordingLauncher("thread-session")

    async def stream(_project_id, _kind, request, execution):
        async for frame in stream_work_run(
            service, cast(AgentLauncher, launcher), request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    with signed_in_client(app) as client:
        thread = next(
            row
            for row in client.get("/api/agent-threads").json()["threads"]
            if row["kind"] == "project"
        )
        _append_chat_exchange(
            service,
            RunRequest(
                chat_id=thread["thread_id"], chat_scope="project", message="prior-private-marker"
            ),
            "prior-private-answer",
            None,
            None,
        )
        response = client.post(
            f"/api/projects/{thread['project_id']}/tasks/project_chat",
            json={
                "chat_id": thread["thread_id"],
                "chat_scope": "project",
                "message": "Continue",
                "mode": "work",
                "run_truth_scope": ["repo-a"],
            },
        )
        assert response.status_code == 202, response.text
        result = wait_for_task_response(
            client, thread["project_id"], response.json()["operation_id"]
        )
        assert result["status"] == "succeeded", result
        assert "prior-private-marker" not in launcher.prompts[0]
        task = app.state.background_tasks.store.agent_task(response.json()["operation_id"])
        assert task is not None and task.stage_root is not None
        stage = Path(task.stage_root)
        owner = app.state.background_tasks.store.private_chat_owner(thread["thread_id"])
        assert owner is not None
        assert stage.parent == tmp_path / "data/agent-threads" / owner / "stages"
        hidden = launcher.launch_kwargs[0]["hidden_read_scope"]
        assert isinstance(hidden, HiddenReadScope)
        assert not any(stage.is_relative_to(root) for root in hidden.hidden_directories)
        # The history is one staged file in the owner's private stage, behind a pointer.
        (seed,) = [
            path
            for path in stage.rglob("*")
            if path.is_file() and b"prior-private-marker" in path.read_bytes()
        ]
        assert str(seed) in launcher.prompts[0] and b"prior-private-answer" in seed.read_bytes()


def test_space_turn_route_captures_member_work(manifest, tmp_path, monkeypatch):
    import uuid

    from tests.helpers import fake_launch_admitted

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    with signed_in_client(app) as client:
        thread = client.get("/api/agent-threads").json()["threads"][0]
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        response = client.post(
            "/api/agent-threads/" + thread["thread_id"] + "/turns",
            json={"message": "List my projects", "timezone": "UTC"},
            headers=headers,
        )
        assert response.status_code == 202, response.text
        replay = client.post(
            "/api/agent-threads/" + thread["thread_id"] + "/turns",
            json={"message": "List my projects", "timezone": "UTC"},
            headers=headers,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json() == response.json()
        store = app.state.background_tasks.store
        assert len(store.agent_tasks(store.space_id)) == 1
        task = store.agent_task(response.json()["operation_id"])
        assert task is not None
        assert task.kind == "space_chat" and task.project_id == store.space_id
        assert task.authorized_by.user_id == store.local_owner.user_id
        assert task.request["mode"] == "work"
        status = client.get(
            "/api/agent-threads/" + thread["thread_id"] + "/tasks/" + task.operation_id
        )
        assert status.status_code == 200, status.text
        # The row names the turn before its member message reaches the transcript.
        rows = client.get("/api/agent-threads").json()["threads"]
        assert rows[0]["latest_operation_id"] == task.operation_id


def test_space_steer_refusal_is_durable_and_idempotent(manifest, tmp_path, monkeypatch):
    import uuid

    from tests.helpers import fake_launch_admitted

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    with signed_in_client(app) as client:
        thread = client.get("/api/agent-threads").json()["threads"][0]
        base = "/api/agent-threads/" + thread["thread_id"]
        admitted = client.post(base + "/turns", json={"message": "hello"})
        assert admitted.status_code == 202, admitted.text
        operation_id = admitted.json()["operation_id"]
        body = {
            "message_id": str(uuid.uuid4()),
            "attempt": 1,
            "expected_turn_id": "not-running",
            "message": "new direction",
        }
        first = client.post(base + "/tasks/" + operation_id + "/steer", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["steering"]["status"] == "refused"
        second = client.post(base + "/tasks/" + operation_id + "/steer", json=body)
        assert second.json() == first.json()
        transcript = client.get(base + "/transcript").json()
        assert [message["text"] for message in transcript["messages"]] == ["hello", "new direction"]


def test_space_artifact_reads_are_owner_only(manifest, tmp_path):
    import json

    from tests.test_saved_artifacts_api import create_saved_artifact

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    client = signed_in_client(app)
    thread = client.get("/api/agent-threads").json()["threads"][0]
    task, artifact = create_saved_artifact(app, store.space_id, kept=False, with_chat=False)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='space_chat', request_json=? WHERE operation_id=?",
            (json.dumps({"chat_id": thread["thread_id"]}), task.operation_id),
        )
        connection.execute(
            "UPDATE graph_run_receipts SET payload_json=? WHERE operation_id=? "
            "AND category='operation_created'",
            (
                json.dumps(
                    {"kind": "space_chat", "attempt": 1, "has_parent": False, "resumed": False}
                ),
                task.operation_id,
            ),
        )
    base = (
        f"/api/agent-threads/{thread['thread_id']}/tasks/{task.operation_id}"
        f"/artifacts/{artifact.artifact_id}"
    )
    assert client.get(base + "/content").status_code == 200
    assert client.get(base + "/download").status_code == 200
    viewer = client.get(base + "/viewer")
    assert viewer.status_code == 200
    assert base + "/content" in viewer.text
    assert client.head(base + "/content").content == b""
    with store.connection() as connection:
        connection.execute(
            "UPDATE agent_threads SET user_id='another-member' WHERE thread_id=?",
            (thread["thread_id"],),
        )
    for action in ("content", "download", "viewer"):
        assert client.get(base + "/" + action).status_code == 404


def test_thread_task_projects_latest_session_resolution(manifest, tmp_path):
    from rcp.storage import AgentTaskRecord
    from tests.helpers import authorized_human

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    with signed_in_client(app) as client:
        for thread in client.get("/api/agent-threads").json()["threads"]:
            operation_id = f"resolution-{thread['kind']}"
            project_id = thread["project_id"] or store.space_id
            store.create_agent_task(
                AgentTaskRecord(
                    operation_id=operation_id,
                    project_id=project_id,
                    kind="space_chat" if thread["kind"] == "space" else "project_chat",
                    status="succeeded",
                    status_message="Done",
                    created_at=store.now(),
                    updated_at=store.now(),
                    request={"chat_id": thread["thread_id"], "mode": "work"},
                    authorized_by=authorized_human(store),
                )
            )
            base = (
                f"/api/agent-threads/{thread['thread_id']}"
                if thread["kind"] == "space"
                else f"/api/projects/{project_id}"
            )
            url = f"{base}/tasks/{operation_id}"
            assert client.get(url).json()["chat_session_resolution"] is None
            for outcome, reason in (
                ("fresh", "provider_switch_tail"),
                ("continued", "current_session"),
            ):
                resolution = {"outcome": outcome, "reason_code": reason}
                store.record_agent_task_receipt(
                    operation_id, "chat_session_resolution", {**resolution, "session_id": None}
                )
                response = client.get(url)
                assert response.status_code == 200
                assert response.json()["chat_session_resolution"] == resolution
                if thread["kind"] == "project":
                    tasks = client.get(base + "/tasks").json()
                    task = next(task for task in tasks if task["operation_id"] == operation_id)
                    assert task["chat_session_resolution"] == resolution


def test_corrupt_space_transcript_is_a_visible_conflict(manifest, tmp_path):
    from rcp.service import canonical_space_chat_path

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    client = signed_in_client(app)
    thread = next(
        t for t in client.get("/api/agent-threads").json()["threads"] if not t.get("project_id")
    )
    owner = store.agent_thread(thread["thread_id"]).user_id
    path = canonical_space_chat_path(store.path.parent, owner, thread["thread_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{\n")
    response = client.get(f"/api/agent-threads/{thread['thread_id']}/transcript")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "thread_transcript_invalid"
