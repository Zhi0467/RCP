from __future__ import annotations

import uuid

import pytest

from rcp.agents import AgentEvent
from rcp.runs.task_policy import load_stored_request
from rcp.service import RunRequest

from .helpers import fabricated_authorizer, signed_in_client, wait_for_task
from .test_client_requests import chat_request, request_app

__all__ = ["request_app"]


def test_stored_chat_request_without_mode_decodes_as_discuss() -> None:
    stored = {"chat_id": "legacy", "message": "Why?", "run_truth_scope": ["repo-a"]}

    assert load_stored_request(RunRequest, stored).mode == "discuss"


def test_chat_route_admits_an_omitted_mode_as_work(request_app):
    project_id = request_app.state.default_project_id
    store = request_app.state.background_tasks.store
    with signed_in_client(request_app) as client:
        started = client.post(f"/api/projects/{project_id}/tasks/project_chat", json=chat_request())

    assert started.status_code == 202, started.text
    assert store.agent_task(started.json()["operation_id"]).request["mode"] == "work"


@pytest.mark.parametrize("kind", ["node_chat", "project_chat"])
def test_chat_routes_refuse_discuss(request_app, kind):
    project_id = request_app.state.default_project_id
    with signed_in_client(request_app) as client:
        response = client.post(
            f"/api/projects/{project_id}/tasks/{kind}",
            json={**chat_request(), "mode": "discuss"},
        )

    assert response.status_code == 422, response.text
    assert request_app.state.background_tasks.store.agent_tasks(project_id) == []


def _failed_turn(app, *, mode: str) -> tuple[str, str]:
    background = app.state.background_tasks
    chat_id = str(uuid.uuid4())

    async def fail(*_args):
        yield f"data: {AgentEvent(event='error', text='provider disconnected').model_dump_json()}\n\n"

    background.stream = fail
    record = background.start(
        app.state.default_project_id,
        "project_chat",
        RunRequest(
            chat_scope="project",
            chat_id=chat_id,
            message="Why is this proposed?",
            run_truth_scope=["repo-a"],
            mode=mode,
            provider="codex",
            reasoning="medium",
            run_on="backend",
        ),
        authorized_by=fabricated_authorizer(),
    )
    wait_for_task(background.store, record.operation_id, expect="failed")

    async def done(*_args):
        yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"

    background.stream = done
    return record.operation_id, chat_id


def test_discuss_turn_retry_is_resent_as_work_and_supersedes_the_old_turn(request_app):
    project_id = request_app.state.default_project_id
    store = request_app.state.background_tasks.store
    old_id, chat_id = _failed_turn(request_app, mode="discuss")
    with signed_in_client(request_app) as client:
        retry = client.post(f"/api/projects/{project_id}/tasks/{old_id}/retry", json={})
        assert retry.status_code == 409, retry.text

        elsewhere = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={**chat_request(), "supersedes": old_id},
        )
        assert elsewhere.status_code == 422, elsewhere.text

        resent = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={
                **chat_request(),
                "chat_id": chat_id,
                "chat_scope": "project",
                "supersedes": old_id,
            },
        )
    assert resent.status_code == 202, resent.text
    assert store.agent_task(resent.json()["operation_id"]).request["mode"] == "work"
    assert store.agent_task_has_receipt(old_id, "transport_auto_retry_superseded")


def test_supersedes_names_only_a_discuss_turn(request_app):
    project_id = request_app.state.default_project_id
    store = request_app.state.background_tasks.store
    old_id, chat_id = _failed_turn(request_app, mode="work")
    with signed_in_client(request_app) as client:
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={
                **chat_request(),
                "chat_id": chat_id,
                "chat_scope": "project",
                "supersedes": old_id,
            },
        )

    assert response.status_code == 422, response.text
    assert not store.agent_task_has_receipt(old_id, "transport_auto_retry_superseded")


def test_old_writer_discuss_rows_recover_as_discuss_after_upgrade(manifest, tmp_path):
    from rcp.runs.provider_login import auth_retry_candidates
    from rcp.storage.question_models import QuestionOrigin

    from .helpers import create_named_app, discuss_turn

    data_dir = tmp_path / "data"

    async def stream(_project_id, _kind, request, execution):
        stage = tmp_path / execution.operation_id
        stage.mkdir()
        execution.checkpoint_stage("backend", str(stage))
        session = AgentEvent(event="session", session_id=execution.operation_id)
        yield f"data: {session.model_dump_json()}\n\n"
        if request.message == "finish":
            yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"
        else:
            yield f"data: {AgentEvent(event='error', text='disconnected').model_dump_json()}\n\n"

    old = create_named_app(str(manifest.path), data_dir=data_dir)
    old.state.background_tasks.stream = stream
    project_id = old.state.default_project_id
    store = old.state.background_tasks.store
    client = signed_in_client(old)

    def turn(message: str, expect: str) -> str:
        response = discuss_turn(
            client,
            f"/api/projects/{project_id}/tasks/project_chat",
            {"chat_id": str(uuid.uuid4()), "message": message, "run_truth_scope": ["repo-a"]},
        )
        assert response.status_code == 202, response.json()
        return wait_for_task(store, response.json()["operation_id"], expect=expect).operation_id

    finished = turn("finish", "succeeded")
    lost = turn("lose", "failed")
    parked = turn("park", "failed")
    with store.connection() as connection:
        for operation_id, kind in ((lost, "transport_lost"), (parked, "provider_auth")):
            connection.execute(
                "UPDATE graph_runs SET failure_kind=? WHERE operation_id=?", (kind, operation_id)
            )
    store.record_agent_task_receipt(
        lost, "transport_auto_retry", {"attempt": 1, "delay_seconds": 5}, tier="summary"
    )
    origin = store.agent_task(finished)
    question = store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="chat",
            project_id=project_id,
            owner_id=origin.request["chat_id"],
            operation_id=origin.operation_id,
            provider=origin.request["provider"],
            native_session_id=origin.native_session_id,
            stage_root=origin.stage_root,
            stage_machine_id=origin.stage_machine_id,
            capability=origin.dispatch_authority.task_contract,
            write_scope_fingerprint=origin.write_scope_fingerprint,
            graph_target=origin.graph_target,
        ),
        key="route",
        question="Which route?",
    )

    upgraded = create_named_app(str(manifest.path), data_dir=data_dir)
    upgraded.state.background_tasks.stream = stream
    background = upgraded.state.background_tasks
    store = background.store
    assert {task.request["mode"] for task in store.agent_tasks(project_id)} == {"discuss"}

    background._run_transport_retry(lost, attempt=1)
    assert auth_retry_candidates(store, operation_id=parked)
    background.retry(parked, authorized_by=store.agent_task(parked).authorized_by)
    store.answer_question(
        question.question_id, answer="Take route A", resolved_by=origin.authorized_by
    )
    upgraded.state.reconcile_question_answers(project_id)
    followup = store.get_question(question.question_id).followup_operation_id
    assert followup is not None
    recovered = [
        task for task in store.agent_tasks(project_id) if task.parent_operation_id in {lost, parked}
    ]
    for task in [*recovered, store.agent_task(followup)]:
        assert task.request["mode"] == "discuss"
        assert task.dispatch_authority.task_contract == "discuss"
    assert len(recovered) == 2

    retry = signed_in_client(upgraded).post(
        f"/api/projects/{project_id}/tasks/{finished}/retry", json={}
    )
    assert retry.status_code == 409, retry.text


@pytest.mark.parametrize("mode", [None, "discuss", "work"])
def test_voice_receipts_decode_with_or_without_a_send_mode(mode):
    from rcp.service_connections import VoiceActionReceipt

    receipt = {
        "tool": "rcp_send_conversation_message",
        "target": {
            "project_id": "p",
            "project_name": "P",
            "graph_target": {"kind": "main", "branch_id": None},
        },
        "call_id": "call",
        "argument_fingerprint": "a" * 64,
        "task_id": "task",
        "outcome": "accepted",
    }
    if mode is not None:
        receipt["mode"] = mode

    assert VoiceActionReceipt.model_validate(receipt).mode == mode


def test_a_paused_discuss_turn_is_resumed_not_sent_again(request_app):
    import sqlite3

    project_id = request_app.state.default_project_id
    store = request_app.state.background_tasks.store
    old_id, chat_id = _failed_turn(request_app, mode="discuss")
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE graph_runs SET status='paused' WHERE operation_id=?", (old_id,))
    with signed_in_client(request_app) as client:
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={
                **chat_request(),
                "chat_id": chat_id,
                "chat_scope": "project",
                "supersedes": old_id,
            },
        )

    assert response.status_code == 422, response.text
    assert not store.agent_task_has_receipt(old_id, "transport_auto_retry_superseded")
