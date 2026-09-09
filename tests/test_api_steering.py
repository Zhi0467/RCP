from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent, AgentProcessControl, ProviderReadiness
from rcp.providers import ProviderSteeringState, ProviderSteerReceipt
from rcp.runs.chat import _append_chat_exchange
from rcp.runs.steering import chat_steering_state, chat_steering_visible
from rcp.service import (
    canonical_chat_backup_sources,
    iter_canonical_chat_backup_prefix,
    iter_canonical_chat_transfer,
)

from .helpers import TASK_SETTLE_TIMEOUT, create_named_app, wait_until


@pytest.fixture
def running_chat(manifest, tmp_path, monkeypatch, request):
    provider = getattr(request, "param", "codex")
    runtime = "claude.stream-json.v1" if provider == "claude" else "codex.app-server-stdio.v1"
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    background = app.state.background_tasks
    project_id = app.state.default_project_id
    done = threading.Event()
    ready = threading.Event()
    receipts: list[tuple[str, str, str]] = []
    pending: Future[ProviderSteerReceipt] | None = None

    def steering_state(_self):
        return ProviderSteeringState(True, None, "owned-turn")

    def steer(_self, expected_turn_id, message_id, text):
        receipts.append((expected_turn_id, message_id, text))
        if pending is not None:
            return pending
        future = Future()
        future.set_result(ProviderSteerReceipt("delivered"))
        return future

    monkeypatch.setattr(AgentProcessControl, "steering_state", steering_state)
    monkeypatch.setattr(AgentProcessControl, "steer", steer)

    async def stream(_project_id, _kind, request, execution):
        yield f"data: {AgentEvent(event='runtime', text=runtime).model_dump_json()}\n\n"
        ready.set()
        assert await asyncio.to_thread(done.wait, TASK_SETTLE_TIMEOUT)
        _append_chat_exchange(
            app.state.service, request, "Finished.", None, None, execution=execution
        )
        yield f"data: {AgentEvent(event='answer', text='Finished.').model_dump_json()}\n\n"
        yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"

    background.stream = stream
    chat_id = str(uuid.uuid4())
    response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": "Original prompt.",
            "mode": "discuss",
            "provider": provider,
        },
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert {
        "steer_visible",
        "can_steer",
        "steer_unavailable_reason",
        "steer_turn_id",
        "steer_action_label",
    } <= accepted.keys()
    assert accepted["steer_visible"]
    operation_id = accepted["operation_id"]
    assert ready.wait(TASK_SETTLE_TIMEOUT)
    url = f"/api/projects/{project_id}/tasks/{operation_id}"

    def finish():
        done.set()
        return wait_until(
            lambda: value if (value := client.get(url).json())["settled"] else None,
            timeout=TASK_SETTLE_TIMEOUT,
            detail="steering test turn did not settle",
        )

    def defer():
        nonlocal pending
        pending = Future()
        return pending

    yield SimpleNamespace(
        app=app,
        client=client,
        background=background,
        project_id=project_id,
        chat_id=chat_id,
        operation_id=operation_id,
        url=url,
        receipts=receipts,
        finish=finish,
        defer=defer,
    )
    done.set()
    if pending is not None and not pending.done():
        pending.set_result(ProviderSteerReceipt("unknown", "Test transport closed."))
    finish()
    client.close()


def _body(**changes):
    return {
        "message_id": str(uuid.uuid4()),
        "attempt": 1,
        "expected_turn_id": "owned-turn",
        "message": "Use the revised framing.",
        **changes,
    }


def test_delivered_steer_is_one_durable_human_message_without_changing_mode(running_chat):
    run = running_chat
    original = run.background.store.agent_task(run.operation_id)
    listed = run.client.get(f"/api/projects/{run.project_id}/tasks").json()
    assert listed[0]["steer_action_label"] == "Steer running turn"
    assert listed[0]["can_steer"]
    assert listed[0]["steer_visible"]
    assert listed[0]["steer_turn_id"] == "owned-turn"
    body = _body(message="Switch to Work and edit everything.")
    response = run.client.post(run.url + "/steer", json=body)
    assert response.status_code == 200, response.text
    message = response.json()
    assert message["role"] == "user"
    assert message["operation_id"] == run.operation_id
    assert message["mode"] == "discuss"
    assert message["steering"] == {
        "attempt": 1,
        "turn_id": "owned-turn",
        "status": "delivered",
        "label": "Delivered",
        "reason": None,
    }
    assert run.client.post(run.url + "/steer", json=body).json() == message
    assert len(run.receipts) == 1
    current = run.background.store.agent_task(run.operation_id)
    assert current.request == original.request
    assert current.dispatch_authority == original.dispatch_authority
    assert current.graph_target == original.graph_target
    transcript = run.app.state.service.chat_transcript(run.chat_id)
    assert [item.text for item in transcript.messages] == ["Original prompt.", body["message"]]
    run.finish()
    transcript = run.app.state.service.chat_transcript(run.chat_id)
    assert [item.text for item in transcript.messages] == [
        "Original prompt.",
        body["message"],
        "Finished.",
    ]
    assert transcript.message_count == 3
    assert transcript.title == "Original prompt."
    assert run.app.state.service.chat_summaries().items[0].message_count == 3
    # Receipt snapshots survive transfer and backup as canonical chat, not SQLite decorations.
    source = canonical_chat_backup_sources(run.app.state.service.history.workspace.root)[0]
    backed_up = list(
        iter_canonical_chat_backup_prefix(
            source, project_id=run.project_id, operation_projects={run.operation_id: run.project_id}
        )
    )
    assert len(backed_up) == 4  # prompt, reservation, acknowledgment, answer
    target_operation = str(uuid.uuid4())
    transferred = [
        json.loads(line)
        for line in iter_canonical_chat_transfer(
            source, operation_id_map={run.operation_id: target_operation}
        )
    ]
    assert transferred[2]["steering"]["status"] == "delivered"
    assert transferred[2]["operationId"] == target_operation


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"attempt": 2}, "attempt does not match"),
        ({"expected_turn_id": "other-turn"}, "turn does not match"),
    ],
)
def test_stale_attempt_or_turn_is_stored_as_refused(running_chat, changes, reason):
    run = running_chat
    body = _body(**changes)
    response = run.client.post(run.url + "/steer", json=body)
    assert response.status_code == 200
    assert response.json()["steering"]["status"] == "refused"
    assert reason in response.json()["steering"]["reason"]
    assert run.receipts == []
    assert (
        run.app.state.service.chat_transcript(run.chat_id).messages[-1].message_id
        == body["message_id"]
    )


def test_completed_attempt_refuses_without_starting_a_turn(running_chat):
    run = running_chat
    run.finish()
    response = run.client.post(run.url + "/steer", json=_body())
    assert response.status_code == 200
    assert response.json()["steering"]["status"] == "refused"
    assert "not running" in response.json()["steering"]["reason"]
    assert run.receipts == []
    assert len(run.background.store.agent_tasks(run.project_id)) == 1


def test_message_uuid_reuse_with_changed_payload_is_rejected(running_chat):
    run = running_chat
    body = _body()
    assert run.client.post(run.url + "/steer", json=body).status_code == 200
    for changes in (
        {"message": "A different message"},
        {"attempt": 2},
        {"expected_turn_id": "different"},
    ):
        response = run.client.post(run.url + "/steer", json={**body, **changes})
        assert response.status_code == 409
        assert "already used" in response.json()["detail"]
    assert len(run.receipts) == 1


def test_unknown_reservation_survives_restart_and_overlapping_retry_without_resend(
    running_chat, monkeypatch
):
    run = running_chat
    pending = run.defer()
    body = _body()
    response = []
    thread = threading.Thread(
        target=lambda: response.append(run.client.post(run.url + "/steer", json=body))
    )
    thread.start()
    try:
        wait_until(lambda: run.receipts, timeout=TASK_SETTLE_TIMEOUT, detail="steer not dispatched")
        duplicate = run.client.post(run.url + "/steer", json=body)
        assert duplicate.status_code == 200
        assert duplicate.json()["steering"]["status"] == "unknown"
        # A fresh process has no controls and can only read the reservation. Neither
        # retry nor recovery interprets it as a queued provider instruction.
        from rcp.background import BackgroundAgentTasks
        from rcp.runs.steering import begin_chat_steer, finish_chat_steer

        restarted = BackgroundAgentTasks(run.background.store, run.background.stream)
        record = run.background.store.agent_task(run.operation_id)
        assert not chat_steering_state(restarted, record).can_steer
        delivery = begin_chat_steer(
            run.app.state.service,
            restarted,
            record,
            message_id=body["message_id"],
            attempt=1,
            expected_turn_id="owned-turn",
            text=body["message"],
        )
        stored = finish_chat_steer(run.app.state.service, record, delivery)
        assert stored.steering.status == "unknown"
        assert len(run.receipts) == 1
        pending.set_result(ProviderSteerReceipt("unknown", "Connection lost."))
    finally:
        if not pending.done():
            pending.set_result(ProviderSteerReceipt("unknown", "Test cleanup."))
        thread.join(TASK_SETTLE_TIMEOUT)
    assert not thread.is_alive()
    assert response[0].json()["steering"]["reason"] == "Connection lost."
    assert run.client.post(run.url + "/steer", json=body).json()["steering"]["status"] == "unknown"
    assert len(run.receipts) == 1


def test_unacknowledged_steer_times_out_without_resending_or_stopping_turn(
    running_chat, monkeypatch
):
    run = running_chat
    pending = run.defer()
    monkeypatch.setattr("rcp.runs.steering.PROVIDER_STEER_ACK_TIMEOUT_SECONDS", 0.05)
    body = _body()
    response = run.client.post(run.url + "/steer", json=body)
    assert response.status_code == 200
    assert response.json()["steering"]["status"] == "unknown"
    assert "timed out" in response.json()["steering"]["reason"]
    assert pending.cancelled()
    assert not run.client.get(run.url).json()["settled"]
    duplicate = run.client.post(run.url + "/steer", json=body)
    assert duplicate.json() == response.json()
    assert len(run.receipts) == 1
    run.finish()
    assert run.client.post(run.url + "/steer", json=body).json() == response.json()


def test_runtime_and_human_conversation_boundaries_come_from_backend(running_chat, monkeypatch):
    run = running_chat
    record = run.background.store.agent_task(run.operation_id)
    exec_record = record.model_copy(
        update={"runtime_id": "codex.exec-json.v1", "runtime_label": "Codex exec"}
    )
    assert chat_steering_visible(run.background.store, exec_record)
    assert not chat_steering_state(run.background, exec_record).can_steer
    assert "exec" in chat_steering_state(run.background, exec_record).reason
    for changes in (
        {"episode_id": "episode"},
        {"request": {**record.request, "trigger": "orchestrator"}},
        {"kind": "seed"},
    ):
        unavailable = record.model_copy(update=changes)
        assert not chat_steering_visible(run.background.store, unavailable)
        assert not chat_steering_state(run.background, unavailable).can_steer
    monkeypatch.setattr(
        run.background.store, "auto_research_child_work_for_operation", lambda _operation: object()
    )
    response = run.client.post(run.url + "/steer", json=_body())
    assert response.status_code == 409
    assert run.receipts == []


@pytest.mark.parametrize(
    "changes",
    [
        {"message": " "},
        {"message_id": "not-uuid"},
        {"attempt": 0},
        {"mode": "work"},
        {"graph_target": {"kind": "main"}},
    ],
)
def test_steering_request_cannot_smuggle_authority_or_invalid_input(running_chat, changes):
    run = running_chat
    response = run.client.post(run.url + "/steer", json=_body(**changes))
    assert response.status_code == 422
    assert run.receipts == []


def test_steer_requires_named_human_identity_before_any_delivery(manifest, tmp_path):
    from rcp.api.app import create_app

    app = create_app(str(manifest.path), data_dir=tmp_path / "unnamed-data")
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{app.state.default_project_id}/tasks/{uuid.uuid4()}/steer",
            json=_body(),
        )
    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "identity_name_required"


def test_steer_hides_non_member_project_before_any_delivery(running_chat):
    run = running_chat
    with run.background.store.connection() as connection:
        connection.execute("DELETE FROM project_members WHERE project_id = ?", (run.project_id,))
    try:
        response = run.client.post(run.url + "/steer", json=_body())
        assert response.status_code == 404
        assert response.json() == {"detail": "Project not found"}
        assert run.receipts == []
    finally:
        # Restore only this fixture's membership so its ordinary teardown can read the task.
        with run.background.store.connection() as connection:
            connection.execute(
                "INSERT INTO project_members (project_id, user_id, seated_at) VALUES (?, ?, ?)",
                (
                    run.project_id,
                    run.background.store.local_owner.user_id,
                    run.background.store.now(),
                ),
            )


def test_receipt_snapshot_cannot_rewrite_the_human_message(running_chat):
    run = running_chat
    assert run.client.post(run.url + "/steer", json=_body()).status_code == 200
    run.finish()
    source = canonical_chat_backup_sources(run.app.state.service.history.workspace.root)[0]
    raw = [json.loads(line) for line in source.path.read_text().splitlines()]
    raw[2]["text"] = "Changed message in acknowledgment"
    source.path.write_text("".join(json.dumps(item) + "\n" for item in raw))
    assert run.app.state.service.chat_transcript(run.chat_id) is None


def test_receipt_persistence_failure_keeps_unknown_and_never_resends(running_chat, monkeypatch):
    from rcp.runs import steering

    run = running_chat
    original_append = steering._append_chat_records

    def fail_acknowledgment(service, path, records, **kwargs):
        if any(item.get("steering", {}).get("status") == "delivered" for item in records):
            raise OSError("Receipt storage unavailable.")
        return original_append(service, path, records, **kwargs)

    monkeypatch.setattr(steering, "_append_chat_records", fail_acknowledgment)
    body = _body()
    response = run.client.post(run.url + "/steer", json=body)
    assert response.status_code == 503
    assert response.json()["detail"] == "Receipt storage unavailable."
    assert len(run.receipts) == 1
    assert run.client.post(run.url + "/steer", json=body).json()["steering"]["status"] == "unknown"
    assert len(run.receipts) == 1
    run.finish()
    transcript = run.app.state.service.chat_transcript(run.chat_id)
    assert [item.text for item in transcript.messages] == [
        "Original prompt.",
        body["message"],
        "Finished.",
    ]


def test_unknown_persisted_provider_disables_steering_without_hiding_task(running_chat):
    run = running_chat
    original = run.background.store.agent_task(run.operation_id)
    unavailable_request = {**original.request, "provider": "retired-provider"}
    with run.background.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json = ? WHERE operation_id = ?",
            (json.dumps(unavailable_request), run.operation_id),
        )
    try:
        detail = run.client.get(run.url)
        listing = run.client.get(f"/api/projects/{run.project_id}/tasks")
        assert detail.status_code == listing.status_code == 200
        for task in (detail.json(), listing.json()[0]):
            assert task["steer_visible"]
            assert not task["can_steer"]
            assert task["steer_turn_id"] is None
            assert (
                task["steer_unavailable_reason"] == "The recorded provider runtime is unavailable."
            )
    finally:
        with run.background.store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET request_json = ? WHERE operation_id = ?",
                (json.dumps(original.request), run.operation_id),
            )


@pytest.mark.parametrize("running_chat", ["claude"], indirect=True)
def test_claude_receipt_is_durably_queued_with_captured_capability(running_chat):
    run = running_chat
    task = run.client.get(run.url).json()
    assert task["steer_action_label"] == "Queue a follow-up turn"
    original = run.background.store.agent_task(run.operation_id)
    body = _body(message="Switch to Work and edit everything.")
    response = run.client.post(run.url + "/steer", json=body)
    assert response.status_code == 200, response.text
    message = response.json()
    assert message["mode"] == "discuss"
    assert message["steering"]["status"] == "delivered"
    assert message["steering"]["label"] == "Queued"
    assert "after the current turn" in message["steering"]["reason"]
    current = run.background.store.agent_task(run.operation_id)
    assert current.request == original.request
    assert current.dispatch_authority == original.dispatch_authority
    assert current.graph_target == original.graph_target
    run.finish()
    transcript = run.app.state.service.chat_transcript(run.chat_id)
    queued = next(item for item in transcript.messages if item.message_id == body["message_id"])
    assert queued.steering.model_dump() == message["steering"]
    assert run.client.post(run.url + "/steer", json=body).json() == message
    assert len(run.receipts) == 1


@pytest.mark.parametrize("provider_fails", [False, True])
def test_claude_real_launcher_preserves_joined_chat_answer_and_provider_failure(
    manifest, tmp_path, monkeypatch, provider_fails
):
    binary = tmp_path / "claude-fixture"
    capture = tmp_path / "provider-input.jsonl"
    reason = "sandbox required but unavailable: bubblewrap (bwrap) not installed"
    binary.write_text(
        f"""#!{sys.executable}
import json, sys, time
from pathlib import Path

def emit(value):
    print(json.dumps(value), flush=True)

def receive():
    line = sys.stdin.readline()
    with Path({str(capture)!r}).open('a') as file:
        file.write(line)
    return json.loads(line)

initial = receive()
if {provider_fails!r}:
    print({reason!r}, file=sys.stderr, flush=True)
    emit({{'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
          'result': None, 'error': None, 'message': None, 'num_turns': 0}})
else:
    emit({{'type': 'command_lifecycle', 'state': 'started', 'command_uuid': initial['uuid']}})
    emit({{'type': 'system', 'subtype': 'init', 'session_id': 'fixture-session'}})
    follow_up = receive()
    emit({{'type': 'command_lifecycle', 'state': 'queued', 'command_uuid': follow_up['uuid']}})
    for message, answer in ((initial, 'First answer.'), (follow_up, 'Follow-up answer.')):
        emit({{'type': 'result', 'result': answer, 'session_id': 'fixture-session',
              'user_message_uuids': [message['uuid']], 'uuid': message['uuid'],
              'usage': {{'input_tokens': 3, 'output_tokens': 2}}}})
        emit({{'type': 'command_lifecycle', 'state': 'completed', 'command_uuid': message['uuid']}})
        if message is initial:
            emit({{'type': 'command_lifecycle', 'state': 'started', 'command_uuid': follow_up['uuid']}})
            emit({{'type': 'system', 'subtype': 'init', 'session_id': 'fixture-session'}})
# The launcher must terminate at the last result, without waiting for stdin EOF.
time.sleep(120)
"""
    )
    binary.chmod(0o755)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "integration-data")
    monkeypatch.setattr(
        app.state.launcher,
        "readiness",
        lambda *args, **kwargs: ProviderReadiness(
            provider="claude",
            installed=True,
            authenticated=True,
            binary_path=str(binary),
            version="2.1.263",
        ),
    )
    project_id = app.state.default_project_id
    chat_id = str(uuid.uuid4())
    client = TestClient(app)
    try:
        accepted = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={
                "chat_id": chat_id,
                "message": "Original prompt.",
                "mode": "discuss",
                "provider": "claude",
            },
        )
        assert accepted.status_code == 202, accepted.text
        operation_id = accepted.json()["operation_id"]
        url = f"/api/projects/{project_id}/tasks/{operation_id}"
        if not provider_fails:
            live = wait_until(
                lambda: value if (value := client.get(url).json())["can_steer"] else None,
                timeout=TASK_SETTLE_TIMEOUT,
                detail="fixture Claude command did not become ready",
            )
            receipt = client.post(
                url + "/steer",
                json=_body(expected_turn_id=live["steer_turn_id"], message="Follow up, please."),
            )
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["steering"]["label"] == "Queued"
        settled = wait_until(
            lambda: value if (value := client.get(url).json())["finished"] else None,
            timeout=TASK_SETTLE_TIMEOUT,
            detail="fixture Claude task did not settle",
        )
        store = app.state.background_tasks.store
        assert len(store.agent_tasks(project_id)) == 1
        if provider_fails:
            assert reason in settled["error"]
            assert settled["status"] == "failed"
            assert store.agent_task(operation_id).stage_root is not None
        else:
            assert settled["error"] is None
            transcript = app.state.service.chat_transcript(chat_id)
            assert [(item.role, item.text) for item in transcript.messages] == [
                ("user", "Original prompt."),
                ("user", "Follow up, please."),
                ("assistant", "First answer.\n\nFollow-up answer."),
            ]
            answers = [item for item in transcript.messages if item.role == "assistant"]
            assert answers[0].operation_id == operation_id
            assert len(capture.read_text().splitlines()) == 2
    finally:
        client.close()
