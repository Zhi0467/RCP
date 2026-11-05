"""Routes the web polls stay read-only.

`fixtures/polled_routes.json` lists each polled route. Every route is called once
on a seeded app under `trace_requests`, and must show no `BEGIN IMMEDIATE`, DML,
subprocess, outbound HTTP or remote read. Today's violators sit in the file's
allowlist with a reason, and an entry whose route has become clean must go.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
import uuid
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.core.models import AuthorizedHuman
from rcp.paper.engines import EngineStore
from rcp.storage import AgentTaskRecord, AppStore
from rcp.transport.state import LocalStateWorkspace

from .helpers import (
    authorized_human,
    create_named_app,
    signed_in_client,
    trace_requests,
)
from .test_project_provisioning_api import _payload
from .test_route_inventory import _FROZEN_ROUTE_INVENTORY

POLLED_ROUTES: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "polled_routes.json").read_text(encoding="utf-8")
)
ALLOWED = {
    (entry["method"], entry["path"]): entry["reason"] for entry in POLLED_ROUTES["allowlist"]
}

RouteKey = tuple[str, str]


def _fill(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format_map(ids)
    if isinstance(value, list):
        return [_fill(item, ids) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, ids) for key, item in value.items()}
    return value


def _routes(space: str) -> list[dict[str, Any]]:
    return [route for route in POLLED_ROUTES["routes"] if route.get("space", "personal") == space]


def _trace_routes(
    app: FastAPI, client: TestClient, space: str, ids_for: Any
) -> dict[RouteKey, dict[str, list[Any]]]:
    """Each route's effects."""

    found: dict[RouteKey, dict[str, list[Any]]] = {}
    for route in _routes(space):
        ids = ids_for(route)
        with trace_requests(app) as trace:
            response = client.request(
                route["method"], _fill(route["path"], ids), json=_fill(route.get("json"), ids)
            )
        assert response.status_code == route.get("status", 200), (route["path"], response.text)
        found[(route["method"], route["path"])] = trace.effects()
    return found


def _assert_matches_allowlist(found: dict[RouteKey, dict[str, list[Any]]]) -> None:
    unlisted = {key: effects for key, effects in found.items() if effects and key not in ALLOWED}
    assert not unlisted, (
        f"Polled routes did work; make them read-only or allowlist them: {unlisted}"
    )
    clean = [key for key in found if key in ALLOWED and not found[key]]
    assert not clean, f"These routes are read-only now; remove their allowlist entries: {clean}"


def _task(store: AppStore, human: AuthorizedHuman, project_id: str, kind: str, chat_id: str) -> str:
    operation_id = str(uuid.uuid4())
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind=kind,
            status="running",
            request={"chat_id": chat_id, "message": "Keep going."},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="",
            authorized_by=human,
        )
    )
    return operation_id


def test_trace_requests_catches_each_effect_on_a_sample_route(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    workspace = LocalStateWorkspace(tmp_path / "state", str(tmp_path / "state"))
    app = FastAPI()
    app.state.background_tasks = SimpleNamespace(store=store)

    @app.get("/clean")
    def clean() -> None:
        with store.connection() as connection:
            connection.execute("SELECT 1")

    @app.get("/effects")
    def effects() -> None:
        with store.connection() as connection:
            connection.execute("CREATE TEMP TABLE probe (value TEXT)")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO probe VALUES ('x')")
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        with suppress(OSError):
            urllib.request.urlopen("http://rcp.invalid/")
        with suppress(httpx.ConnectError):
            httpx.get("http://rcp.invalid/")
        workspace.refresh_if_stale()

    client = TestClient(app)
    with trace_requests(app) as trace:
        assert client.get("/clean").status_code == 200
    assert "SELECT 1" in trace.statements
    assert trace.effects() == {}

    with trace_requests(app) as trace:
        assert client.get("/effects").status_code == 200
    found = trace.effects()
    assert found["writes"] == ["BEGIN IMMEDIATE", "INSERT INTO probe VALUES ('x')"]
    assert ["git", "--version"] in found["subprocesses"]
    assert found["http"] == ["http://rcp.invalid/", "http://rcp.invalid/"]
    assert found["remote_reads"] == ["StateWorkspace.refresh_if_stale"]


def test_polled_routes_and_allowlist_name_real_routes() -> None:
    served = {(method, path) for methods, path in _FROZEN_ROUTE_INVENTORY for method in methods}
    listed = {(route["method"], route["path"]) for route in POLLED_ROUTES["routes"]}
    assert listed <= served, listed - served
    assert set(ALLOWED) <= listed, set(ALLOWED) - listed
    assert {route.get("space", "personal") for route in POLLED_ROUTES["routes"]} == {
        "personal",
        "team",
    }


def test_personal_polled_routes_stay_read_only(manifest, tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    app.state.paper_engines = EngineStore(data_dir)
    client = signed_in_client(app)
    project_id = client.get("/api/projects").json()[0]["id"]
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    threads = client.get("/api/agent-threads").json()["threads"]
    space_thread = next(thread for thread in threads if thread["kind"] == "space")
    project_thread = next(thread for thread in threads if thread["kind"] == "project")
    machine = client.get("/api/space/machines").json()["machines"][0]
    store = app.state.background_tasks.store
    human = authorized_human(app)
    space_task = _task(store, human, store.space_id, "space_chat", space_thread["thread_id"])
    project_task = _task(store, human, project_id, "project_chat", project_thread["thread_id"])
    ids = {
        "project_id": project_id,
        "thread_id": space_thread["thread_id"],
        "chat_id": project_thread["thread_id"],
        "machine_id": machine["machine_id"],
        "provider": "claude",
        "attempt_id": str(uuid.uuid4()),
    }
    monkeypatch.delenv("RCP_UPDATE_CHECK", raising=False)

    def ids_for(route: dict[str, Any]) -> dict[str, str]:
        space_route = route["path"].startswith("/api/agent-threads/")
        return {**ids, "operation_id": space_task if space_route else project_task}

    _assert_matches_allowlist(_trace_routes(app, client, "personal", ids_for))


def test_team_polled_routes_stay_read_only(tmp_path) -> None:
    data_dir = tmp_path / "team"
    store, _bootstrap = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Team Lab")
    store.ensure_space_machines([("", "rcp", "Server")])
    member = store.preprovision_team_member("Alice")
    app = create_app(
        data_dir=data_dir,
        trusted_principal_resolver=lambda _request, opened: opened.space_user(member.user_id),
    )
    client = signed_in_client(app)
    provisioning = client.post("/api/project-provisioning/requests", json=_payload())
    assert provisioning.status_code == 201, provisioning.text
    pairing = client.post("/api/team/devices/pairings", json={})
    assert pairing.status_code == 200, pairing.text
    ids = {
        "request_id": provisioning.json()["request_id"],
        "pairing_id": pairing.json()["pairing_id"],
    }

    found = _trace_routes(app, client, "team", lambda _route: ids)
    _assert_matches_allowlist(found)
