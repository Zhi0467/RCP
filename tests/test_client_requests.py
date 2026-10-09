from __future__ import annotations

import uuid

import pytest

from rcp.agents import AgentEvent

from .helpers import signed_in_client, wait_for_task
from .test_api import _experiment_project
from .test_episode_api import settling_auto_research_stream
from .test_project_membership import _create_project, _team_app


@pytest.fixture
def request_app(manifest, tmp_path):
    app, _ = _experiment_project(manifest, tmp_path)

    async def stream(*_args):
        yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"

    app.state.background_tasks.stream = stream
    return app


def chat_request():
    return {
        "chat_id": str(uuid.uuid4()),
        "message": "Explain the project.",
        "run_truth_scope": ["repo-a"],
    }


@pytest.mark.parametrize("route", ["tasks/project_chat", "experiments/run", "episodes"])
def test_client_request_replay_admits_once_and_lookup_returns_id(request_app, tmp_path, route):
    app = request_app
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    path = route
    body = chat_request()
    result_id = "operation_id"
    if route == "experiments/run":
        path = "experiments/exp%2Fbounded-loop/run"
        body = {"chat_id": str(uuid.uuid4()), "run_truth_scope": ["repo-a"]}
    elif route == "episodes":
        stage = tmp_path / "auto-stage"
        stage.mkdir()
        app.state.background_tasks.stream = settling_auto_research_stream(stage)
        body = {
            "mode": "auto_research",
            "invocation_ceiling": 3,
            "starting_instruction": "Investigate the evidence.",
        }
        result_id = "episode_id"
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    with signed_in_client(app) as client:
        url = f"/api/projects/{project_id}/{path}"
        first = client.post(url, json=body, headers=headers)
        assert first.status_code == 202, first.text
        operation_id = first.json().get("operation_id", first.json().get("root_operation_id"))
        wait_for_task(store, operation_id, expect="succeeded")
        before = {task.operation_id for task in store.agent_tasks(project_id)}
        repeated = client.post(url, json=body, headers=headers)
        assert repeated.status_code == first.status_code
        assert repeated.json().keys() == first.json().keys()
        assert repeated.json()[result_id] == first.json()[result_id]
        assert {task.operation_id for task in store.agent_tasks(project_id)} == before
        found = client.get(f"/api/projects/{project_id}/client-requests/{key}")
        assert found.status_code == 200
        expected = {"route": route, result_id: first.json()[result_id]}
        if route == "experiments/run":
            # The started episode comes back too, so Resume watches the whole loop.
            expected["episode_id"] = store.agent_task(operation_id).episode_id
            assert expected["episode_id"]
        assert found.json() == expected
        assert (
            client.get(f"/api/projects/{project_id}/client-requests/{uuid.uuid4()}").status_code
            == 404
        )


@pytest.mark.parametrize(
    "route", ["tasks/project_chat", "experiments/exp%2Fbounded-loop/run", "episodes"]
)
# A fixed version-1 UUID keeps test ids identical across xdist workers.
@pytest.mark.parametrize("key", ["invalid", "c232ab00-9414-11ec-b3c8-9e6bdeced846"])
def test_client_request_headers_require_uuid4(request_app, route, key):
    project_id = request_app.state.default_project_id
    body = chat_request()
    if route.startswith("experiments/"):
        body = {"chat_id": str(uuid.uuid4()), "run_truth_scope": ["repo-a"]}
    elif route == "episodes":
        body = {"mode": "auto_research", "invocation_ceiling": 3}
    with signed_in_client(request_app) as client:
        response = client.post(
            f"/api/projects/{project_id}/{route}", json=body, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 422
    assert request_app.state.background_tasks.store.agent_tasks(project_id) == []


def test_client_request_key_cannot_cross_routes(request_app):
    project_id = request_app.state.default_project_id
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    with signed_in_client(request_app) as client:
        first = client.post(
            f"/api/projects/{project_id}/tasks/project_chat", json=chat_request(), headers=headers
        )
        assert first.status_code == 202
        collision = client.post(
            f"/api/projects/{project_id}/episodes",
            json={"mode": "auto_research", "invocation_ceiling": 3},
            headers=headers,
        )
        assert collision.status_code == 409


def test_client_request_key_is_private_and_cannot_be_reused_by_another_member(tmp_path):
    app, client, store, people, acting = _team_app(tmp_path)
    project_id = _create_project(client, tmp_path / "repo")
    store.seat_project_member(project_id, people[1].user_id)

    async def stream(*_args):
        yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"

    app.state.background_tasks.stream = stream
    key = str(uuid.uuid4())
    body = {**chat_request(), "run_truth_scope": ["paper-repo"]}
    first = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 202, first.text
    acting[0] = people[1].user_id
    assert client.get(f"/api/projects/{project_id}/client-requests/{key}").status_code == 404
    repeated = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert repeated.status_code == 409
    assert len(store.agent_tasks(project_id)) == 1


def test_failed_admission_rolls_back_request_and_task(request_app, monkeypatch):
    import rcp.storage.agent_tasks as task_storage

    store = request_app.state.background_tasks.store
    project_id = request_app.state.default_project_id
    record_request = task_storage.record_client_request
    key = str(uuid.uuid4())

    def fail_after_request(*args):
        record_request(*args)
        raise RuntimeError("Simulated interruption before commit")

    monkeypatch.setattr(task_storage, "record_client_request", fail_after_request)
    with signed_in_client(request_app, raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json=chat_request(),
            headers={"Idempotency-Key": key},
        )
    assert response.status_code == 500
    assert store.client_request(project_id, key) is None
    assert store.agent_tasks(project_id) == []


def test_an_old_key_still_returns_its_original_admission(request_app):
    store = request_app.state.background_tasks.store
    project_id = request_app.state.default_project_id
    key = str(uuid.uuid4())
    with signed_in_client(request_app) as client:
        url = f"/api/projects/{project_id}/tasks/project_chat"
        first = client.post(url, json=chat_request(), headers={"Idempotency-Key": key})
        assert first.status_code == 202
        # Far past any transcript window, a resumed receipt's re-ask still matches.
        with store.connection() as connection:
            connection.execute(
                "UPDATE client_requests SET created_at = '2000-01-01T00:00:00+00:00'"
            )
        other = client.post(
            url, json=chat_request(), headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        assert other.status_code == 202
        again = client.post(url, json=chat_request(), headers={"Idempotency-Key": key})
        assert again.json()["operation_id"] == first.json()["operation_id"]
        lookup = client.get(f"/api/projects/{project_id}/client-requests/{key}")
        assert lookup.status_code == 200
