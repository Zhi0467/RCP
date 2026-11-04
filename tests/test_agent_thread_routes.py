from __future__ import annotations

from pathlib import Path

import pytest

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
        assert rows[0]["capabilities"] and rows[0]["capabilities"] != rows[1]["capabilities"]
        assert client.get("/api/agent-threads").json()["threads"] == rows
        for row in rows:
            base = "/api/agent-threads/" + row["thread_id"]
            renamed = client.post(base + "/title", json={"title": "Scout"})
            assert renamed.status_code == 200
            assert renamed.json()["agent_name"] == "Scout"
            assert client.put(base + "/browser", json={"browser_requested": False}).json() == {
                "browser_requested": False
            }
            transcript = client.get(
                base + "/transcript"
                if row["kind"] == "space"
                else f"/api/projects/{row['project_id']}/chats/{row['thread_id']}"
            )
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
    """An ordinary finished reply only marks the thread unread; a failure pushes, and so does a
    schedule wake's reply because it is the reminder itself.
    """
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


def test_mark_read_through_the_reported_finish_clears_unread(manifest, tmp_path):
    """The turn's messages carry its creation time; it finishes later."""
    from datetime import datetime, timedelta

    from rcp.storage import AgentTaskRecord
    from tests.helpers import authorized_human

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    with signed_in_client(app) as client:
        row = client.get("/api/agent-threads").json()["threads"][0]
        started = datetime.fromisoformat(store.now())
        store.create_agent_task(
            AgentTaskRecord(
                operation_id="space-reply",
                project_id=store.space_id,
                kind="space_chat",
                status="succeeded",
                status_message="Done",
                created_at=started.isoformat(),
                updated_at=started.isoformat(),
                finished_at=(started + timedelta(seconds=30)).isoformat(),
                request={"chat_id": row["thread_id"], "chat_scope": "project", "mode": "work"},
                authorized_by=authorized_human(store),
            )
        )
        base = "/api/agent-threads/" + row["thread_id"]
        (unread,) = [
            item
            for item in client.get("/api/agent-threads").json()["threads"]
            if item["thread_id"] == row["thread_id"]
        ]
        assert unread["unread"]
        read = client.post(base + "/read", json={"read_through": unread["last_finished_at"]})
        assert read.status_code == 200, read.text
        assert not read.json()["unread"]


def test_unread_counts_only_the_threads_own_finished_turns(manifest, tmp_path):
    """A stopped turn has not finished, and an Experiment the thread started is not one of its
    turns.
    """
    from datetime import datetime, timedelta

    from rcp.storage import AgentTaskRecord
    from tests.helpers import authorized_human

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    with signed_in_client(app) as client:
        row = next(
            row
            for row in client.get("/api/agent-threads").json()["threads"]
            if row["kind"] == "project"
        )
        start = datetime.fromisoformat(store.now())

        def task(operation_id, kind, status, minute):
            at = (start + timedelta(minutes=minute)).isoformat()
            store.create_agent_task(
                AgentTaskRecord(
                    operation_id=operation_id,
                    project_id=row["project_id"],
                    kind=kind,
                    status=status,
                    status_message=status,
                    created_at=at,
                    updated_at=at,
                    finished_at=at,
                    request={"chat_id": row["thread_id"], "chat_scope": "project", "mode": "work"},
                    authorized_by=authorized_human(store),
                )
            )

        def listed():
            return next(
                item
                for item in client.get("/api/agent-threads").json()["threads"]
                if item["thread_id"] == row["thread_id"]
            )

        task("reply", "project_chat", "succeeded", 1)
        base = "/api/agent-threads/" + row["thread_id"]
        read = client.post(base + "/read", json={"read_through": listed()["last_finished_at"]})
        assert read.status_code == 200, read.text
        task("stopped", "project_chat", "paused", 2)
        task("experiment", "node_chat", "succeeded", 3)
        thread = listed()
        assert not thread["unread"]
        assert thread["latest_operation_id"] == "stopped"
        assert thread["live_state"] == "paused"


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
        (seed,) = [
            path
            for path in stage.rglob("*")
            if path.is_file() and b"prior-private-marker" in path.read_bytes()
        ]
        assert str(seed) in launcher.prompts[0] and b"prior-private-answer" in seed.read_bytes()


def test_queued_message_with_a_file_starts_as_the_members_turn_after_the_running_one(
    manifest, tmp_path
):
    """A removed message never starts, and its files can be sent again."""
    import asyncio
    import threading
    import uuid
    from typing import cast

    from rcp.agents import AgentLauncher
    from rcp.runs.agent_thread_queue import queued_turn_operation_id
    from rcp.runs.tasks.work import stream_work_run
    from tests.helpers import (
        append_fixture_patch,
        seed_patch,
        wait_for_task,
        wait_for_task_response,
        wait_until,
    )
    from tests.test_chat_prompt_protocol import _RecordingLauncher

    class HeldLauncher(_RecordingLauncher):
        def __init__(self) -> None:
            super().__init__("queue-session")
            self.started = threading.Event()
            self.release = threading.Event()

        async def stream(self, _provider, prompt, **kwargs):
            if not self.prompts:
                self.started.set()
                while not self.release.is_set():
                    await asyncio.sleep(0.01)
            async for event in super().stream(_provider, prompt, **kwargs):
                yield event

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    store = app.state.background_tasks.store
    append_fixture_patch(service, seed_patch())
    launcher = HeldLauncher()

    async def stream(_project_id, _kind, request, execution):
        async for frame in stream_work_run(
            service, cast(AgentLauncher, launcher), request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    with signed_in_client(app) as client:
        thread = client.get("/api/agent-threads").json()["threads"][1]
        project_id, thread_id = thread["project_id"], thread["thread_id"]
        base = f"/api/agent-threads/{thread_id}/queue"

        def upload(content: bytes) -> dict[str, str]:
            client_id = str(uuid.uuid4())
            response = client.post(
                f"/api/projects/{project_id}/chats/{thread_id}/attachments",
                files={"file": ("notes.txt", content, "text/plain")},
                data={"client_id": client_id},
            )
            assert response.status_code == 200, response.text
            return {
                "attachment_set_id": response.json()["attachment_set_id"],
                "attachment_client_id": client_id,
            }

        def queue(message: str, files: dict[str, str]):
            response = client.post(
                base,
                json={"message": message, **files},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            assert response.status_code == 202, response.text
            return response.json()

        running = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={
                "chat_id": thread_id,
                "chat_scope": "project",
                "message": "Start",
                "mode": "work",
            },
        )
        assert running.status_code == 202, running.text
        assert launcher.started.wait(10)
        queued = queue("Queued follow-up", upload(b"queued-file-marker"))
        assert queued["operation_id"] is None
        dropped_files = upload(b"dropped-file-marker")
        dropped = queue("Never mind", dropped_files)
        assert client.delete(f"{base}/{dropped['queue_id']}").status_code == 200
        resent = queue("Never mind", dropped_files)
        assert client.delete(f"{base}/{resent['queue_id']}").status_code == 200
        launcher.release.set()
        first = wait_for_task_response(client, project_id, running.json()["operation_id"])
        assert first["status"] == "succeeded", first
        operation_id = wait_until(
            lambda: (row := store.agent_thread_queue_item(queued["queue_id"])) and row.operation_id
        )
        assert operation_id == queued_turn_operation_id(queued["queue_id"])
        turn = wait_for_task(app, operation_id, expect="succeeded")
        assert turn.request["message"] == "Queued follow-up"
        assert turn.request["trigger"] == "human" and "source" not in turn.request
        assert turn.authorized_by is not None
        assert turn.authorized_by.user_id == store.private_chat_owner(thread_id)
        assert "Queued follow-up" in launcher.prompts[1]
        assert turn.stage_root is not None
        (staged,) = [
            path
            for path in Path(turn.stage_root).rglob("*")
            if path.is_file() and path.read_bytes() == b"queued-file-marker"
        ]
        assert str(staged) in launcher.prompts[1]
        assert len(launcher.prompts) == 2
        for removed in (dropped, resent):
            assert store.agent_task(queued_turn_operation_id(removed["queue_id"])) is None
        assert client.get(base).json()["items"] == []


def test_space_turn_route_captures_member_work(manifest, tmp_path, monkeypatch):
    """The row names the turn before its member message reaches the transcript."""
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
        rows = client.get("/api/agent-threads").json()["threads"]
        assert rows[0]["latest_operation_id"] == task.operation_id


@pytest.mark.parametrize("action", ["retry", "resume"])
def test_space_retry_and_resume_honour_the_request_key(manifest, tmp_path, monkeypatch, action):
    """Resume's own checkpoint rules are not under test; both share one admission."""
    import uuid

    from tests.helpers import fake_launch_admitted

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    tasks = app.state.background_tasks
    fake_launch_admitted(monkeypatch, tasks)
    monkeypatch.setattr(tasks, "resume", tasks.retry)
    with signed_in_client(app) as client:
        thread = client.get("/api/agent-threads").json()["threads"][0]
        base = "/api/agent-threads/" + thread["thread_id"]
        sent = client.post(base + "/turns", json={"message": "hello", "timezone": "UTC"})
        operation_id = sent.json()["operation_id"]
        tasks.store.fail_agent_task(operation_id, "try again")
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        url = f"{base}/tasks/{operation_id}/{action}"
        first = client.post(url, headers=headers, json={})
        assert first.status_code == 202, first.text
        again = client.post(url, headers=headers, json={})
        assert again.status_code == 202, again.text
        assert again.json()["operation_id"] == first.json()["operation_id"]


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


def _space_turn_artifact(app, thread_id: str):
    """A saved HTML artifact rebound as an earlier turn of this Space thread."""
    import json

    from tests.test_saved_artifacts_api import create_saved_artifact

    store = app.state.background_tasks.store
    task, artifact = create_saved_artifact(app, store.space_id, kept=False, with_chat=False)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='space_chat', request_json=?, authorized_space_id=?, "
            "authorized_user_id=?, authorized_display_name='Owner' WHERE operation_id=?",
            (
                json.dumps({"chat_id": thread_id}),
                store.space_id,
                store.local_owner.user_id,
                task.operation_id,
            ),
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
    return task, artifact


def _text_comment(operation_id: str, artifact_id: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "artifact_id": artifact_id,
        "selections": [{"kind": "text", "text": "Saved comparison", "comment": "Bigger"}],
    }


def test_space_turn_admits_a_comment_on_its_own_artifact(manifest, tmp_path, monkeypatch):
    from tests.helpers import fake_launch_admitted

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    store = app.state.background_tasks.store
    with signed_in_client(app) as client:
        thread_id = client.get("/api/agent-threads").json()["threads"][0]["thread_id"]
        task, artifact = _space_turn_artifact(app, thread_id)
        context = _text_comment(task.operation_id, artifact.artifact_id)
        response = client.post(
            f"/api/agent-threads/{thread_id}/turns",
            json={"message": "", "artifact_context": context},
        )
        assert response.status_code == 202, response.text
        stored = store.agent_task(response.json()["operation_id"])
        assert stored is not None
        assert stored.request["artifact_context"]["artifact_id"] == artifact.artifact_id
        assert stored.request["artifact_context"]["selections"][0]["comment"] == "Bigger"
        assert "Comment 1" in stored.request["message"]
    from types import SimpleNamespace

    from rcp.runs.space_chat import _artifact_comment_part
    from rcp.service import RunRequest

    stage = tmp_path / "stage"
    part = _artifact_comment_part(
        SimpleNamespace(
            owner=SimpleNamespace(store=store),
            request=RunRequest.model_validate(stored.request),
            stage=stage,
        )
    )
    commented = stage / "turns" / task.operation_id / "artifacts" / artifact.name
    assert str(commented) in part and '"comment": "Bigger"' in part


def test_space_turn_refuses_a_comment_outside_its_thread(manifest, tmp_path, monkeypatch):
    import uuid

    from tests.helpers import fake_launch_admitted

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    fake_launch_admitted(monkeypatch, app.state.background_tasks)
    with signed_in_client(app) as client:
        thread_id = client.get("/api/agent-threads").json()["threads"][0]["thread_id"]
        task, artifact = _space_turn_artifact(app, str(uuid.uuid4()))
        own, own_artifact = _space_turn_artifact(app, thread_id)
        for context in (
            _text_comment(task.operation_id, artifact.artifact_id),
            _text_comment(own.operation_id, "0" * 24),
        ):
            response = client.post(
                f"/api/agent-threads/{thread_id}/turns",
                json={"message": "fix", "artifact_context": context},
            )
            assert response.status_code == 422, response.text
        empty = {**_text_comment(own.operation_id, own_artifact.artifact_id), "selections": []}
        response = client.post(
            f"/api/agent-threads/{thread_id}/turns",
            json={"message": " ", "artifact_context": empty},
        )
        assert response.status_code == 422, response.text


def test_space_inline_viewer_enables_selection(manifest, tmp_path):
    import json
    import re

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        thread_id = client.get("/api/agent-threads").json()["threads"][0]["thread_id"]
        task, artifact = _space_turn_artifact(app, thread_id)
        base = (
            f"/api/agent-threads/{thread_id}/tasks/{task.operation_id}"
            f"/artifacts/{artifact.artifact_id}"
        )
        viewer = client.get(base + "/viewer", params={"presentation": "inline"})
        assert viewer.status_code == 200, viewer.text
        config = re.search(r"const inlineConfig=(\{.*?\});", viewer.text)
        assert config is not None and json.loads(config.group(1))["selectable"] is True
        content = client.get(
            base + "/content",
            params={"presentation": "inline", "theme": "default", "color_mode": "dark"},
        )
        assert content.status_code == 200, content.text


def test_space_artifact_reads_are_owner_only(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    client = signed_in_client(app)
    thread = client.get("/api/agent-threads").json()["threads"][0]
    task, artifact = _space_turn_artifact(app, thread["thread_id"])
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


def test_thread_pick_is_owner_only_and_drives_retry(manifest, tmp_path, monkeypatch):
    """The web names the backend machine by its id and a Space thread runs there anyway; retry
    follows the pick made after the failure.
    """
    from rcp.core.models import AuthorizedHuman
    from rcp.service import RunRequest
    from tests.helpers import fake_launch_admitted
    from tests.test_storage import _task_record

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    background = app.state.background_tasks
    store = background.store
    fake_launch_admitted(monkeypatch, background)
    monkeypatch.setattr(background, "admit_provider_task", lambda *args, **kwargs: None)
    with signed_in_client(app) as client:
        space = client.get("/api/agent-threads").json()["threads"][0]
        other = store.ensure_agent_threads("someone-else", [], default_names=("Pip",))[0]
        assert (
            client.post(
                f"/api/agent-threads/{other.thread_id}/agent", json={"provider": "claude"}
            ).status_code
            == 404
        )
        owner = store.local_owner
        store.create_agent_task(
            _task_record(
                store.now(),
                operation_id="failed",
                project_id=store.space_id,
                kind="space_chat",
                status="failed",
                authorized_by=AuthorizedHuman(
                    space_id=store.space_id, user_id=owner.user_id, display_name="Member"
                ),
                request=RunRequest(
                    provider="codex",
                    chat_scope="project",
                    chat_id=space["thread_id"],
                    mode="work",
                    message="Hi",
                ).model_dump(mode="json"),
            )
        )
        picked = client.post(
            f"/api/agent-threads/{space['thread_id']}/agent",
            json={"provider": "claude", "run_on": "backend-machine-id"},
        )
        assert picked.status_code == 200, picked.text
        assert picked.json()["agent"]["provider"] == "claude"
        retried = background.retry("failed", authorized_by=store.agent_task("failed").authorized_by)
        assert retried.request["provider"] == "claude"
        assert retried.request["run_on"] == store.backend_space_machine().machine_id


def test_member_pick_waits_for_sign_in_but_agent_pick_must_run(manifest, tmp_path, monkeypatch):
    from rcp.runs import auto_research_admission
    from rcp.runs.agent_thread_commands import store_thread_pick

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store, catalog = app.state.background_tasks.store, app.state.catalog
    project_id = app.state.default_project_id
    thread = next(
        row
        for row in store.ensure_agent_threads(
            store.local_owner.user_id, [project_id], default_names=("Pip",)
        )
        if row.kind == "project"
    )

    def unready(*args, **kwargs):
        raise auto_research_admission.AutoResearchChildProfileInvalid(
            "provider_unready", "not signed in"
        )

    monkeypatch.setattr(
        auto_research_admission, "require_auto_research_child_profile_pick", unready
    )
    with pytest.raises(ValueError):
        store_thread_pick(store, catalog, thread, provider="claude")
    store_thread_pick(store, catalog, thread, provider="claude", wait_for_sign_in=True)
    assert store.agent_thread(thread.thread_id).agent["provider"] == "claude"


def test_space_transcript_shows_when_each_message_happened(manifest, tmp_path, monkeypatch):
    """The member message keeps its first attempt's time across a Retry; the reply shows when its
    attempt finished; a steer keeps its own send time.
    """
    import uuid

    from rcp.runs.space_chat import SpaceThreadOwner, append_space_message
    from rcp.service import RunRequest
    from tests.helpers import fake_launch_admitted

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    tasks = app.state.background_tasks
    store = tasks.store
    fake_launch_admitted(monkeypatch, tasks)
    with signed_in_client(app) as client:
        thread = client.get("/api/agent-threads").json()["threads"][0]
        base = "/api/agent-threads/" + thread["thread_id"]
        first_id = client.post(base + "/turns", json={"message": "hello"}).json()["operation_id"]
        store.fail_agent_task(first_id, "try again")
        retry = client.post(f"{base}/tasks/{first_id}/retry", json={})
        assert retry.status_code == 202, retry.text
        retry_id = retry.json()["operation_id"]
        steer = {
            "message_id": str(uuid.uuid4()),
            "attempt": 1,
            "expected_turn_id": "not-running",
            "message": "new direction",
        }
        assert client.post(f"{base}/tasks/{retry_id}/steer", json=steer).status_code == 200
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET finished_at = ? WHERE operation_id = ?",
                ("2026-11-01T09:30:00+00:00", retry_id),
            )
        retried = store.agent_task(retry_id)
        first = store.agent_task(first_id)
        assert retried is not None and first is not None
        assert retried.parent_operation_id == first_id
        owner = SpaceThreadOwner(store, app.state.catalog, thread["thread_id"])
        request = RunRequest.model_validate(retried.request)
        append_space_message(owner, request, retry_id, role="user", text="hello")
        append_space_message(owner, request, retry_id, role="assistant", text="hi")
        messages = client.get(base + "/transcript").json()["messages"]
        assert [(message["text"], message.get("shown_at")) for message in messages] == [
            ("hello", first.created_at),
            ("new direction", None),
            ("hi", "2026-11-01T09:30:00+00:00"),
        ]


def _busy_thread_turn(store, actor, thread, operation_id="busy", **fields):
    from rcp.service import RunRequest
    from tests.test_storage import _task_record

    store.create_agent_task(
        _task_record(
            store.now(),
            **{
                "operation_id": operation_id,
                "project_id": thread["project_id"],
                "kind": "project_chat",
                "status": "running",
                "authorized_by": actor,
                "request": RunRequest(
                    chat_scope="project", chat_id=thread["thread_id"], mode="work"
                ).model_dump(mode="json"),
                **fields,
            },
        )
    )


def test_queue_routes_store_list_start_and_remove_member_messages(manifest, tmp_path, monkeypatch):
    import uuid

    from rcp.limits import AGENT_THREAD_QUEUE_MAX_CHARS, AGENT_THREAD_QUEUE_MAX_ITEMS
    from tests.helpers import authorized_human, fake_launch_admitted

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    tasks = app.state.background_tasks
    store = tasks.store
    fake_launch_admitted(monkeypatch, tasks)
    with signed_in_client(app) as client:
        space, thread = client.get("/api/agent-threads").json()["threads"]
        base = f"/api/agent-threads/{thread['thread_id']}/queue"
        actor = authorized_human(store)

        def post(message, key=None, url=base):
            return client.post(
                url,
                json={"message": message},
                headers={"Idempotency-Key": key or str(uuid.uuid4())},
            )

        _busy_thread_turn(store, actor, thread)
        space_queue = f"/api/agent-threads/{space['thread_id']}/queue"
        assert client.get(space_queue).status_code == 404
        assert post("x", url=space_queue).status_code == 404
        assert client.post(base, json={"message": "No key"}).status_code == 422
        assert post("").status_code == 422
        assert post("x" * (AGENT_THREAD_QUEUE_MAX_CHARS + 1)).status_code == 422
        key = str(uuid.uuid4())
        queued = post("Then check the logs", key)
        assert queued.status_code == 202, queued.text
        assert queued.json()["operation_id"] is None
        assert post("Then check the logs", key).json() == queued.json()
        assert post("Another message", key).status_code == 422
        listed = client.get(base).json()["items"]
        assert [(item["kind"], item["queue_id"]) for item in listed] == [
            ("message", queued.json()["queue_id"])
        ]
        extra = [post(f"More {index}") for index in range(1, AGENT_THREAD_QUEUE_MAX_ITEMS)]
        assert all(response.status_code == 202 for response in extra)
        assert post("One too many").status_code == 409
        for response in extra:
            removed = client.delete(f"{base}/{response.json()['queue_id']}")
            assert removed.status_code == 200, removed.text
        assert client.delete(f"{base}/{extra[0].json()['queue_id']}").status_code == 404
        store.complete_agent_task("busy", applied_revision=None, result={})
        later = post("Now go")
        assert later.status_code == 202, later.text
        first = store.agent_thread_queue_item(queued.json()["queue_id"])
        assert first is not None and first.operation_id is not None
        assert later.json()["operation_id"] is None
        assert client.delete(f"{base}/{queued.json()['queue_id']}").status_code == 409
        store.fail_agent_task(first.operation_id, "stopped")
        store.set_chat_archived(
            thread["project_id"], thread["thread_id"], actor.user_id, archived=True
        )
        assert post("Archived").status_code == 409


def test_queue_lists_ready_handoffs_by_readiness_with_the_space_agent_name(manifest, tmp_path):
    from rcp.service import RunRequest
    from tests.helpers import authorized_human
    from tests.test_storage import _task_record

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    with signed_in_client(app) as client:
        space, thread = client.get("/api/agent-threads").json()["threads"]
        actor = authorized_human(store)
        _busy_thread_turn(store, actor, thread)

        def handoff(operation_id: str) -> None:
            store.create_agent_task(
                _task_record(
                    store.now(),
                    operation_id=operation_id,
                    project_id=store.space_id,
                    kind="space_chat",
                    status="running",
                    authorized_by=actor,
                    request=RunRequest(
                        chat_scope="project", chat_id=space["thread_id"], mode="work"
                    ).model_dump(mode="json"),
                )
            )
            store.create_agent_handoff(
                source_thread_id=space["thread_id"],
                source_operation_id=operation_id,
                idempotency_key="key",
                target_thread_id=thread["thread_id"],
                message=f"From {operation_id}",
            )

        handoff("done-source")
        queued = store.queue_agent_thread_message(
            queue_id="queued",
            thread_id=thread["thread_id"],
            user_id=actor.user_id,
            idempotency_key="key",
            message="Mine",
        )
        store.complete_agent_task("done-source", applied_revision=None, result={})
        handoff("running-source")
        items = client.get(f"/api/agent-threads/{thread['thread_id']}/queue").json()["items"]
        assert [item["kind"] for item in items] == ["message", "handoff"]
        assert items[0]["queue_id"] == queued.queue_id
        assert items[1]["message"] == "From done-source"
        assert items[1]["source_agent_name"] == space["agent_name"]
