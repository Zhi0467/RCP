from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, get_args

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from rcp.api import dependencies
from rcp.storage import AppStore


class SyncDouble:
    def __init__(self):
        self.demands = []

    def note_demand(self, client_id: str, open_project_ids: set[str], activated: str | None):
        self.demands.append((client_id, open_project_ids, activated))

    def sync_health(self, project_id: str) -> dict:
        return {"last_completed_at": None, "status": "local", "error": None}


def _sync(request: Request) -> SyncDouble:
    return request.app.state.coordinator


# S1 owns this dependency; this isolated slice supplies only a test double.
if not hasattr(dependencies, "SyncCoordinatorDependency"):
    dependencies.SyncCoordinatorDependency = Annotated[SyncDouble, Depends(_sync)]
watermarks = importlib.import_module("rcp.api.resource_watermarks")


class ContentDouble:
    def __init__(self):
        self.calls = []
        self.generation = "published-1"

    def content_generations(self, project_id: str, families: Iterable[str]) -> dict[str, str]:
        names = set(families)
        self.calls.append((project_id, names))
        return dict.fromkeys(names, self.generation)

    def open(self, *args, **kwargs):
        pytest.fail("watermark opened a service")


@pytest.fixture(params=["personal", "team"])
def endpoint(tmp_path, monkeypatch, request):
    store = AppStore(tmp_path / "rcp.sqlite3", space_kind=request.param)
    owner = store.local_owner if request.param == "personal" else None
    member = owner.user_id if owner else str(uuid.uuid4())
    other_member = str(uuid.uuid4())
    project = str(uuid.uuid4())
    forbidden = str(uuid.uuid4())
    now = datetime.now(UTC)
    with store.connection() as connection:
        for user_id in (member, other_member):
            if owner is not None and user_id == owner.user_id:
                continue
            connection.execute(
                "INSERT INTO space_users(user_id, identity_kind, created_at, updated_at) VALUES(?, 'team_member', ?, ?)",
                (user_id, now.isoformat(), now.isoformat()),
            )
        for project_id in (project, forbidden):
            connection.execute(
                "INSERT INTO projects(project_id, locator, name, state_location, state_remote, added_at) VALUES(?, ?, 'test', 'local', 0, ?)",
                (project_id, project_id, now.isoformat()),
            )
        connection.execute(
            "INSERT INTO project_members(project_id, user_id, seated_at) VALUES(?, ?, ?)",
            (project, member, now.isoformat()),
        )
        connection.execute(
            "INSERT INTO project_aliases(alias_id, canonical_project_id) VALUES('alias', ?)",
            (project,),
        )
    catalog = ContentDouble()
    coordinator = SyncDouble()
    app = FastAPI()
    app.state.coordinator = coordinator
    app.dependency_overrides[get_args(dependencies.SyncCoordinatorDependency)[1].dependency] = _sync
    app.include_router(watermarks.router)
    app.dependency_overrides[dependencies.get_store] = lambda: store
    app.dependency_overrides[dependencies.get_catalog] = lambda: catalog
    app.dependency_overrides[dependencies.get_identity_access] = lambda: SimpleNamespace(
        acting_user=lambda request: SimpleNamespace(user_id=member)
    )
    monkeypatch.setattr(store, "now", lambda: now.isoformat())
    with TestClient(app) as client:
        yield SimpleNamespace(
            store=store,
            member=member,
            other_member=other_member,
            project=project,
            forbidden=forbidden,
            catalog=catalog,
            coordinator=coordinator,
            client=client,
            now=now,
        )
    store.close()


def beat(endpoint, resources=None, project=None):
    project = project or endpoint.project
    response = endpoint.client.post(
        "/api/resource-watermarks",
        json={
            "client_id": "test",
            "open": [
                {
                    "project_id": project,
                    "current_ref": "main",
                    "resources": resources or [{"name": "tasks.status"}],
                }
            ],
            "due": [project],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_authorization_aliases_revocation_and_demand(endpoint):
    ids = ["alias", endpoint.forbidden, "unknown"]
    response = endpoint.client.post(
        "/api/resource-watermarks",
        json={
            "client_id": "test",
            "open": [{"project_id": value, "resources": []} for value in ids],
            "due": ids,
            "activated": "alias",
        },
    ).json()
    assert [(item["requested_id"], item["project_id"]) for item in response["projects"]] == [
        ("alias", endpoint.project)
    ]
    assert response["unavailable"] == [endpoint.forbidden, "unknown"]
    assert endpoint.coordinator.demands[-1] == ("test", {endpoint.project}, endpoint.project)
    assert (
        beat(endpoint, project="alias")["projects"][0]["resources"]
        == beat(endpoint)["projects"][0]["resources"]
    )
    with endpoint.store.connection() as connection:
        connection.execute(
            "UPDATE space_users SET removal_started_at = ? WHERE user_id = ?",
            (endpoint.now.isoformat(), endpoint.member),
        )
    assert beat(endpoint)["unavailable"] == [endpoint.project]
    assert endpoint.coordinator.demands[-1] == ("test", set(), None)
    empty = endpoint.client.post(
        "/api/resource-watermarks", json={"client_id": "test", "open": [], "due": []}
    )
    assert empty.status_code == 200
    assert empty.json()["projects"] == []
    assert empty.json()["unavailable"] == []


def test_entity_and_member_counters_do_not_cross_scopes(endpoint):
    keys = [{"name": "task", "entity": "same-id"}, {"name": "chats"}]
    before = beat(endpoint, keys)["projects"]
    with endpoint.store.connection() as connection:
        connection.executemany(
            "INSERT INTO resource_versions(scope,resource,entity,generation) VALUES(?,?,?,1)",
            [
                (endpoint.forbidden, "task", "same-id"),
                (endpoint.project, "chats", f"member:{endpoint.other_member}"),
            ],
        )
    assert beat(endpoint, keys)["projects"] == before
    with endpoint.store.connection() as connection:
        connection.execute(
            "INSERT INTO resource_versions(scope,resource,entity,generation) VALUES(?, 'chats', ?, 1)",
            (endpoint.project, f"member:{endpoint.member}"),
        )
    after = beat(endpoint, keys)["projects"][0]["resources"]
    assert after[0] == before[0]["resources"][0]
    assert after[1]["version"] != before[0]["resources"][1]["version"]


def test_deadline_crossing_and_epoch_fence(endpoint, monkeypatch):
    families = [
        "tasks.status",
        "tasks.progress",
        "questions",
        "artifacts",
        "consolidation",
        "settings",
    ]
    deadline = endpoint.now + timedelta(seconds=2)
    with endpoint.store.connection() as connection:
        connection.executemany(
            "INSERT INTO resource_deadlines(scope,resource,entity,owner,deadline) VALUES(?, ?, '', 'test', ?)",
            [
                ("" if family == "settings" else endpoint.project, family, deadline.timestamp())
                for family in families
            ],
        )
    keys = [{"name": family} for family in families]
    before = beat(endpoint, keys)
    assert all(
        item["valid_until"] == deadline.isoformat() for item in before["projects"][0]["resources"]
    )
    monkeypatch.setattr(endpoint.store, "now", lambda: deadline.isoformat())
    after = beat(endpoint, keys)
    for previous, current in zip(
        before["projects"][0]["resources"], after["projects"][0]["resources"], strict=True
    ):
        assert previous["version"] != current["version"]
        assert current["valid_until"] is None
    with endpoint.store.connection() as connection:
        connection.execute("UPDATE resource_watermark_epoch SET epoch = ?", (uuid.uuid4().hex,))
    assert beat(endpoint)["epoch"] != after["epoch"]


def test_batched_heartbeat_is_four_reads_without_payload_work(endpoint, monkeypatch):
    queries = []
    connection_owner = endpoint.store.connection

    @contextmanager
    def traced():
        with connection_owner() as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(endpoint.store, "connection", traced)
    keys = [{"name": "task", "entity": f"task-{index}"} for index in range(250)]
    keys += [
        {"name": "graph", "ref": "main"},
        {"name": "graph", "ref": "branch:b"},
        {"name": "chat", "entity": "c"},
    ]
    beat(endpoint, keys)
    reads = [query for query in queries if query.lstrip().upper().startswith(("SELECT", "WITH"))]
    # Authorization, counters, deadlines, and running tasks for timed progress.
    assert len(reads) == 4
    assert endpoint.catalog.calls == [
        (endpoint.project, {"graph:main", "graph:branch:b", "chat:c"})
    ]
    assert "resource_deadlines" in reads[2]
    with endpoint.store.connection() as connection:
        connection.executemany(
            "INSERT INTO resource_versions(scope,resource,entity,generation) VALUES(?, 'task', ?, 1)",
            [(endpoint.project, f"history-{index}") for index in range(1000)],
        )
        connection.executemany(
            "INSERT INTO resource_deadlines(scope,resource,entity,owner,deadline) VALUES(?, 'tasks.status', '', ?, ?)",
            [
                (endpoint.project, f"history-{index}", endpoint.now.timestamp() - index)
                for index in range(1000)
            ],
        )
    queries.clear()
    beat(endpoint, keys)
    assert (
        len([query for query in queries if query.lstrip().upper().startswith(("SELECT", "WITH"))])
        == 4
    )


def test_registered_route_uses_identity_and_published_content(manifest, tmp_path, monkeypatch):
    from tests.helpers import create_named_app, signed_in_client

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    coordinator = SyncDouble()
    app.dependency_overrides[get_args(dependencies.SyncCoordinatorDependency)[1].dependency] = (
        lambda: coordinator
    )
    content = ContentDouble()
    monkeypatch.setattr(
        app.state.services.catalog,
        "content_generations",
        content.content_generations,
        raising=False,
    )
    client = signed_in_client(app)
    project = app.state.default_project_id
    body = {
        "client_id": "test",
        "open": [
            {
                "project_id": project,
                "resources": [{"name": "graph", "ref": "main"}, {"name": "terminals"}],
            }
        ],
        "due": [project],
    }
    response = client.post("/api/resource-watermarks", json=body)
    assert response.status_code == 200, response.text
    before = response.json()["projects"][0]["resources"]
    content.generation = "published-2"
    after = client.post("/api/resource-watermarks", json=body).json()["projects"][0]["resources"]
    assert all(old["version"] != new["version"] for old, new in zip(before, after, strict=True))
    assert coordinator.demands[-1] == ("test", {project}, None)
    assert TestClient(app).post("/api/resource-watermarks", json=body).status_code == 401


def test_artifact_expiry_invalidates_task_consumers(endpoint, monkeypatch):
    deadline = endpoint.now + timedelta(seconds=2)
    keys = [{"name": name} for name in ("artifacts", "tasks.status", "tasks.progress")]
    keys += [{"name": "task", "entity": "origin"}]
    with endpoint.store.connection() as connection:
        connection.execute(
            "INSERT INTO artifacts VALUES('artifact', ?, ?)",
            (
                endpoint.project,
                json.dumps({"origin_operation_id": "origin", "expires_at": deadline.isoformat()}),
            ),
        )
    before = beat(endpoint, keys)["projects"][0]["resources"]
    assert all(item["valid_until"] is not None for item in before)
    monkeypatch.setattr(
        endpoint.store, "now", lambda: (deadline + timedelta(seconds=1)).isoformat()
    )
    after = beat(endpoint, keys)["projects"][0]["resources"]
    assert all(old["version"] != new["version"] for old, new in zip(before, after, strict=True))
    with endpoint.store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=json_set(metadata, '$.kept_at', ?)",
            (endpoint.store.now(),),
        )
        assert not connection.execute(
            "SELECT 1 FROM resource_deadlines WHERE owner='artifacts:artifact'"
        ).fetchall()


def test_running_task_progress_moves_with_the_clock(endpoint, monkeypatch):
    from rcp.limits import TASK_PROGRESS_REFRESH_SECONDS

    keys = [{"name": "tasks.status"}, {"name": "tasks.progress"}]
    later = endpoint.now + timedelta(seconds=TASK_PROGRESS_REFRESH_SECONDS)

    def versions(at):
        monkeypatch.setattr(endpoint.store, "now", lambda: at.isoformat())
        return [item["version"] for item in beat(endpoint, keys)["projects"][0]["resources"]]

    assert versions(later) == versions(endpoint.now)
    with endpoint.store.connection() as connection:
        _insert_running_task(connection, endpoint.project, endpoint.now)
    running, running_later = versions(endpoint.now), versions(later)
    assert running_later[0] == running[0]
    assert running_later[1] != running[1]


def _insert_running_task(connection, project_id, at):
    connection.execute(
        "INSERT INTO graph_runs(operation_id, project_id, kind, status, request_json, "
        "created_at, updated_at, status_message) VALUES(?, ?, 'node_chat', 'running', '{}', ?, ?, '')",
        (str(uuid.uuid4()), project_id, at.isoformat(), at.isoformat()),
    )


def test_graph_token_moves_with_canonical_reachability(endpoint, monkeypatch):
    keys = [{"name": "graph", "ref": "main"}, {"name": "paper"}]
    healthy = beat(endpoint, keys)["projects"][0]["resources"]
    with monkeypatch.context() as patch:
        patch.setattr(
            endpoint.coordinator,
            "sync_health",
            lambda project_id: {"last_completed_at": None, "status": "error", "error": "offline"},
        )
        offline = beat(endpoint, keys)["projects"][0]["resources"]
    # The snapshot embeds reachability; the Paper read does not.
    assert offline[0]["version"] != healthy[0]["version"]
    assert offline[1]["version"] == healthy[1]["version"]
    assert beat(endpoint, keys)["projects"][0]["resources"] == healthy
