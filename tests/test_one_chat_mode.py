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
