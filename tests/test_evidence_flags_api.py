from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from rcp.agents import AgentEvent
from rcp.api.consolidation import evidence_flag_message
from rcp.core.models import Patch
from tests.helpers import append_fixture_patch, create_named_app, signed_in_client, wait_for_task
from tests.test_api_tasks import _event_frame
from tests.test_consolidation_work import _admit


@pytest.fixture
def flagged(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(
        app.state.service,
        Patch(
            kind="seed",
            author="agent",
            summary="Observation",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "ev/result",
                            "type": "evidence",
                            "title": "Measured result",
                            "observation": "Measured result",
                            "origin": "analytic",
                        }
                    ],
                }
            ],
        ),
    )
    _, execution = _admit(app)
    store = execution.store
    project = app.state.default_project_id
    flag = store.execute_evidence_flag_command(
        project,
        execution.operation_id,
        key="claim",
        evidence_id="ev/result",
        validate_evidence=lambda: app.state.service.history.state().revision,
        kind="overclaim",
        finding="The record supports a narrower claim.",
        source="repo-a/results.txt:3",
    )["flag"]
    return app, signed_in_client(app), store, project, flag


def test_flags_get_and_dismiss_disposition(flagged):
    _, client, _, project, flag = flagged
    base = f"/api/projects/{project}/consolidation"
    assert client.get(base).json()["evidence_flags"] == [flag]
    url = f"{base}/evidence-flags/{flag['flag_id']}"
    assert client.post(url + "/dismiss", json={}).json()["flag"]["state"] == "dismissed"
    assert client.get(base).json()["evidence_flags"] == []
    assert client.post(url + "/dismiss", json={}).status_code == 409
    assert client.post(url + "/address", json={"request_id": "closed"}).status_code == 409
    assert client.post(f"{base}/evidence-flags/missing/dismiss", json={}).status_code == 404


def test_address_admits_one_ordinary_work_turn_and_replays(flagged):
    app, client, store, project, flag = flagged
    launches = []

    async def stream(project_id, kind, request, execution):
        launches.append((project_id, kind, request, execution))
        yield _event_frame(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    base = f"/api/projects/{project}/consolidation"
    url = f"{base}/evidence-flags/{flag['flag_id']}/address"
    request_id = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: client.post(url, json={"request_id": request_id}), range(2))
        )
    assert [response.status_code for response in responses] == [200, 200]
    result = responses[0].json()
    assert responses[1].json() == result
    task = wait_for_task(store, result["operation_id"], expect="succeeded")
    assert task.kind == "node_chat"
    assert task.request["mode"] == "work"
    assert task.request["node_id"] == flag["evidence_id"]
    assert task.request["message"] == evidence_flag_message(flag)
    assert task.request["chat_id"] == result["chat_id"]
    assert task.authorized_by.user_id == store.local_owner.user_id
    assert task.dispatch_authority.task_contract == "work_auto"
    assert task.graph_target.branch_id is None
    assert len(launches) == 1
    # The finished turn closed the flag; only the original request still replays.
    assert client.get(base).json()["evidence_flags"] == []
    assert client.post(url, json={"request_id": "another-click"}).status_code == 409
    # A lost response replays to the bound chat even after the Evidence leaves main.
    with store.connection() as connection:
        connection.execute(
            "UPDATE evidence_flags SET evidence_id='ev/missing' WHERE flag_id=?",
            (flag["flag_id"],),
        )
    replay = client.post(url, json={"request_id": request_id})
    assert replay.status_code == 200
    assert replay.json()["chat_id"] == result["chat_id"]
    assert len(launches) == 1


def test_a_failed_address_turn_reopens_the_flag_for_another(flagged):
    app, client, store, project, flag = flagged
    outcomes = iter(["error", "error", "done"])

    async def stream(project_id, kind, request, execution):
        yield _event_frame(AgentEvent(event=next(outcomes), text="provider failed"))

    app.state.background_tasks.stream = stream
    base = f"/api/projects/{project}/consolidation"
    url = f"{base}/evidence-flags/{flag['flag_id']}/address"

    def address(request_id, expect):
        result = client.post(url, json={"request_id": request_id}).json()
        wait_for_task(store, result["operation_id"], expect=expect)
        return result

    first = address("first", "failed")
    [reopened] = client.get(base).json()["evidence_flags"]
    assert (reopened["state"], reopened["chat_id"]) == ("open", first["chat_id"])
    # Each request keeps its own turn across later attempts and their outcomes.
    second = address("second", "failed")
    assert address("first", "failed")["operation_id"] == first["operation_id"]
    third = address("third", "succeeded")
    assert client.get(base).json()["evidence_flags"] == []
    assert address("first", "failed")["chat_id"] == first["chat_id"]
    assert len({first["operation_id"], second["operation_id"], third["operation_id"]}) == 3


def test_a_retried_address_turn_carries_the_flag(flagged):
    app, client, store, project, flag = flagged
    outcomes = iter(["error", "done"])

    async def stream(project_id, kind, request, execution):
        yield _event_frame(AgentEvent(event=next(outcomes), text="provider failed"))

    app.state.background_tasks.stream = stream
    base = f"/api/projects/{project}/consolidation"
    url = f"{base}/evidence-flags/{flag['flag_id']}/address"
    first = client.post(url, json={"request_id": "first"}).json()
    wait_for_task(store, first["operation_id"], expect="failed")
    retried = client.post(f"/api/projects/{project}/tasks/{first['operation_id']}/retry", json={})
    wait_for_task(store, retried.json()["operation_id"], expect="succeeded")
    assert client.get(base).json()["evidence_flags"] == []


def test_a_request_answered_by_a_running_turn_replays_it(flagged):
    app, client, store, project, flag = flagged
    release = threading.Event()
    launches = []

    async def stream(project_id, kind, request, execution):
        launches.append(execution)
        await asyncio.to_thread(release.wait, 10)
        yield _event_frame(AgentEvent(event="error", text="provider failed"))

    app.state.background_tasks.stream = stream
    url = f"/api/projects/{project}/consolidation/evidence-flags/{flag['flag_id']}/address"
    try:
        first = client.post(url, json={"request_id": "first"}).json()
        second = client.post(url, json={"request_id": "second"}).json()
        assert second["operation_id"] == first["operation_id"]
    finally:
        release.set()
    wait_for_task(store, first["operation_id"], expect="failed")
    # The flag reopened, yet a retry of the second request still returns its turn.
    replay = client.post(url, json={"request_id": "second"}).json()
    assert replay["operation_id"] == first["operation_id"]
    assert len(launches) == 1


def test_a_restart_reopens_an_addressing_flag(flagged):
    app, client, store, project, flag = flagged
    release = threading.Event()

    async def stream(project_id, kind, request, execution):
        await asyncio.to_thread(release.wait, 10)
        yield _event_frame(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    base = f"/api/projects/{project}/consolidation"
    url = f"{base}/evidence-flags/{flag['flag_id']}/address"
    try:
        result = client.post(url, json={"request_id": "before-restart"}).json()
        store.interrupt_active_agent_tasks()
        [reopened] = client.get(base).json()["evidence_flags"]
        assert (reopened["state"], reopened["chat_id"]) == ("open", result["chat_id"])
    finally:
        release.set()


@pytest.mark.parametrize(("outcome", "state"), [("done", None), ("error", "open")])
def test_startup_settles_a_flag_an_older_release_left_addressing(flagged, outcome, state):
    app, client, store, project, flag = flagged

    async def stream(project_id, kind, request, execution):
        yield _event_frame(AgentEvent(event=outcome, text="provider failed"))

    app.state.background_tasks.stream = stream
    base = f"/api/projects/{project}/consolidation"
    url = f"{base}/evidence-flags/{flag['flag_id']}/address"
    result = client.post(url, json={"request_id": "old-release"}).json()
    wait_for_task(store, result["operation_id"])
    # An older release never settled the flag when its turn ended.
    with store.connection() as connection:
        connection.execute(
            "UPDATE evidence_flags SET state='addressing' WHERE flag_id=?", (flag["flag_id"],)
        )
    store.interrupt_active_agent_tasks()
    assert [item["state"] for item in client.get(base).json()["evidence_flags"]] == (
        [state] if state else []
    )


def test_address_refuses_a_consolidation_notice(flagged):
    app, client, store, project, flag = flagged
    launches = []

    async def stream(project_id, kind, request, execution):
        launches.append(kind)
        yield _event_frame(AgentEvent(event="done"))

    app.state.background_tasks.stream = stream
    with store.connection() as connection:
        connection.execute(
            "UPDATE evidence_flags SET kind='consolidation_added' WHERE flag_id=?",
            (flag["flag_id"],),
        )
    url = f"/api/projects/{project}/consolidation/evidence-flags/{flag['flag_id']}/address"
    response = client.post(url, json={"request_id": str(uuid.uuid4())})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "flag_not_addressable"
    assert launches == []
    assert store.list_evidence_flags(project)[0]["state"] == "open"


@pytest.mark.parametrize("evidence", ["missing", "retired"])
def test_address_refuses_absent_or_retired_evidence(flagged, evidence):
    app, client, store, project, flag = flagged
    evidence_id = f"ev/{evidence}"
    if evidence == "retired":
        append_fixture_patch(
            app.state.service,
            Patch(
                kind="seed",
                author="agent",
                summary="Retired observation",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[
                    {
                        "op": "create_nodes",
                        "nodes": [
                            {
                                "id": evidence_id,
                                "type": "evidence",
                                "title": "Old result",
                                "observation": "Old result",
                                "origin": "analytic",
                                "validity": "superseded",
                            }
                        ],
                    }
                ],
            ),
        )
    # An operational flag can outlive the current graph node it named.
    with store.connection() as connection:
        connection.execute(
            "UPDATE evidence_flags SET evidence_id=? WHERE flag_id=?",
            (evidence_id, flag["flag_id"]),
        )
    response = client.post(
        f"/api/projects/{project}/consolidation/evidence-flags/{flag['flag_id']}/address",
        json={"request_id": "stale-node"},
    )
    assert response.status_code == 409
    current = store.evidence_flag(project, flag["flag_id"])
    assert current["state"] == "open" and current["chat_id"] is None


@pytest.mark.parametrize("action", ["address", "dismiss"])
@pytest.mark.parametrize("guard", ["membership", "identity", "admission"])
def test_flag_actions_use_human_write_guards(flagged, monkeypatch, action, guard):
    _, client, store, project, flag = flagged
    from fastapi import HTTPException

    from rcp.api.identity import IdentityAccess

    if guard == "membership":
        monkeypatch.setattr(store, "is_project_member", lambda *args: False)
        status = 404
    elif guard == "identity":

        def refuse(*args):
            raise HTTPException(status_code=428)

        monkeypatch.setattr(IdentityAccess, "require_patch_capable_identity", refuse)
        status = 428
    else:

        def refuse(*args):
            raise ValueError("closed")

        monkeypatch.setattr(store, "require_project_accepts_new_work", refuse)
        status = 409
    response = client.post(
        f"/api/projects/{project}/consolidation/evidence-flags/{flag['flag_id']}/{action}",
        json={"request_id": "guard"} if action == "address" else {},
    )
    assert response.status_code == status
    assert store.evidence_flag(project, flag["flag_id"])["state"] == "open"
