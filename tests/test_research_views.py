from __future__ import annotations

import logging
import socket
import sqlite3
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.limits import RESEARCH_VIEW_PIN_LIMIT
from rcp.storage import AppStore, EpisodeRecord
from rcp.storage.research_views import ResearchViewConflict
from rcp.transfer.records import TRANSFER_EXCLUDED_PROJECT_TABLES
from tests.helpers import create_named_app, signed_in_client, wait_until
from tests.test_artifact_viewer_state import _artifact, _state, _task
from tests.test_project_membership import _create_project, _team_app


@pytest.fixture
def views(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project = app.state.default_project_id
    client = signed_in_client(app)
    return store, project, client, f"/api/projects/{project}/research-views"


@pytest.fixture
def served_views(manifest, monkeypatch, caplog):
    with tempfile.TemporaryDirectory(prefix="rcp-pin-api-") as data_dir:
        monkeypatch.setenv("RCP_DATA_DIR", data_dir)
        app = create_named_app(str(manifest.path), data_dir=Path(data_dir))
        store = app.state.background_tasks.store
        project = app.state.default_project_id
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True))
            worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
            worker.start()
            try:
                wait_until(lambda: server.started)
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False
                ) as client:
                    assert (
                        client.post(
                            "/api/owner/redeem", json={"code": store.create_owner_sign_in_code()}
                        ).status_code
                        == 200
                    )
                    yield store, project, client, f"/api/projects/{project}/research-views"
            finally:
                server.should_exit = True
                worker.join(timeout=10)
                assert not worker.is_alive()
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def page(store, project, branch=None, **fields):
    if branch:
        with store.connection() as connection:
            store._insert_episode(
                connection,
                EpisodeRecord(
                    episode_id=branch,
                    project_id=project,
                    mode="auto_research",
                    status="running",
                    invocation_ceiling=1,
                    created_at=store.now(),
                    updated_at=store.now(),
                    graph_target=GraphTargetRef(kind="branch", branch_id=branch),
                    graph_base_head=GraphHeadRef(target=GraphTargetRef(), revision=0),
                ),
            )
    task = _task(
        store,
        project,
        graph_target=GraphTargetRef(kind="branch" if branch else "main", branch_id=branch),
    )
    return _artifact(
        store,
        task,
        name="board.html",
        data=b"<!doctype html><title>Board</title>",
        expires_at=(datetime.fromisoformat(store.now()) + timedelta(days=1)).isoformat(),
        **fields,
    )


def generation(store, project):
    with store.connection() as connection:
        row = connection.execute(
            "SELECT generation FROM resource_versions WHERE scope=? AND resource='artifacts' AND entity=''",
            (project,),
        ).fetchone()
    return row[0] if row else 0


def test_pin_keep_current_version_undo_and_unpin(served_views):
    store, project, client, url = served_views
    artifact = page(store, project)
    assert _state(client, artifact)["can_pin"]
    assert _state(client, artifact)["pin_branch_name"] == "main"
    before = generation(store, project)
    result = client.post(url, json={"artifact_id": artifact.artifact_id, "branch_id": None})
    assert result.status_code == 200
    assert result.json()["pins"][0]["artifact_id"] == artifact.artifact_id
    assert generation(store, project) > before
    assert store.artifact(artifact.artifact_id).expires_at is None
    assert _state(client, artifact)["pinned"]
    version = store.publish_artifact_version(
        artifact.artifact_id,
        base_version=artifact.current_version,
        operation_id="edit",
        data=b"edited",
    )
    assert _state(client, artifact)["current_version"] == version.version_id
    store.undo_artifact(artifact.artifact_id)
    assert _state(client, artifact)["current_version"] == artifact.current_version
    before = generation(store, project)
    for _ in range(2):
        assert client.delete(f"{url}/{artifact.artifact_id}?branch_id=").json() == {"pins": []}
    assert generation(store, project) > before
    assert store.artifact(artifact.artifact_id).kept_at is not None


def test_personal_target_and_membership_isolation(tmp_path):
    _app, client, store, people, acting = _team_app(tmp_path)
    project = _create_project(client, tmp_path / "repo")
    artifact = page(store, project, "branch-a")
    url = f"/api/projects/{project}/research-views"
    assert _state(client, artifact)["pin_branch_id"] == "branch-a"
    store.rename_branch(project, "branch-a", "matrix-run")
    assert _state(client, artifact)["pin_branch_name"] == "matrix-run"
    assert (
        client.post(url, json={"artifact_id": artifact.artifact_id}).json()["detail"]["code"]
        == "target_mismatch"
    )
    assert (
        client.post(
            url, json={"artifact_id": artifact.artifact_id, "branch_id": "branch-a"}
        ).status_code
        == 200
    )
    assert client.get(url).json() == {"pins": []}
    acting[0] = people[1].user_id
    assert client.get(url).status_code == 404
    store.seat_project_member(project, people[1].user_id)
    assert client.get(url + "?branch_id=branch-a").json() == {"pins": []}
    assert not _state(client, artifact)["pinned"]
    with store.connection() as connection:
        connection.execute(
            "DELETE FROM project_members WHERE project_id=? AND user_id=?", (project, acting[0])
        )
    for action in (store.pin_research_view, store.unpin_research_view):
        with pytest.raises(ResearchViewConflict) as error:
            action(project, acting[0], artifact.artifact_id, "branch-a")
        assert error.value.status == 404


@pytest.mark.parametrize("invalid,code", [("origin", "origin_unavailable"), ("type", "not_html")])
def test_ineligible_artifact_never_kept(views, invalid, code):
    store, project, client, url = views
    artifact = page(store, project)
    with store.connection() as connection:
        updated = artifact.model_copy(
            update={"origin_operation_id": None}
            if invalid == "origin"
            else {"media_type": "text/plain"}
        )
        connection.execute(
            "UPDATE artifacts SET metadata=? WHERE artifact_id=?",
            (updated.model_dump_json(), artifact.artifact_id),
        )
    assert not _state(client, artifact)["can_pin"]
    response = client.post(url, json={"artifact_id": artifact.artifact_id})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    assert store.artifact(artifact.artifact_id).kept_at is None


def test_limit_is_atomic_and_retry_is_idempotent(views):
    store, project, client, url = views
    artifacts = [page(store, project) for _ in range(RESEARCH_VIEW_PIN_LIMIT + 1)]
    user = store.local_owner.user_id

    def pin(artifact):
        try:
            store.pin_research_view(project, user, artifact.artifact_id, None)
            return artifact
        except ResearchViewConflict as exc:
            assert exc.code == "pin_limit_reached"
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        kept = [a for a in pool.map(pin, artifacts) if a is not None]
    assert len(kept) == RESEARCH_VIEW_PIN_LIMIT
    retry = client.post(url, json={"artifact_id": kept[0].artifact_id, "branch_id": ""})
    assert retry.status_code == 200
    assert len(retry.json()["pins"]) == RESEARCH_VIEW_PIN_LIMIT
    assert len({pin["position"] for pin in retry.json()["pins"]}) == RESEARCH_VIEW_PIN_LIMIT
    refused = next(a for a in artifacts if a not in kept)
    assert store.artifact(refused.artifact_id).kept_at is None
    assert not _state(client, refused)["can_pin"]


def test_failed_insert_rolls_back_keep(views):
    store, project, _client, _url = views
    artifact = page(store, project)
    before = generation(store, project)
    with store.connection() as connection:
        connection.execute(
            "CREATE TRIGGER reject_pin BEFORE INSERT ON research_view_pins BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.pin_research_view(project, store.local_owner.user_id, artifact.artifact_id, None)
    assert store.artifact(artifact.artifact_id) == artifact
    assert generation(store, project) == before


def test_migration_cleanup_and_transfer_classification(views):
    store, project, _client, _url = views
    assert "research_view_pins" in TRANSFER_EXCLUDED_PROJECT_TABLES
    with store.connection() as connection:
        connection.execute("DROP TABLE research_view_pins")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=50")
    path = store.path
    store.close()
    store = AppStore(path)
    artifact = page(store, project)
    store.pin_research_view(project, store.local_owner.user_id, artifact.artifact_id, None)
    assert AppStore(path).research_view_pins(project, store.local_owner.user_id)
    migrated = str(uuid.uuid4())
    store.migrate_project_identity(project, migrated, store.space_id)
    assert (
        store.research_view_pins(migrated, store.local_owner.user_id)[0].artifact_id
        == artifact.artifact_id
    )
    store.delete_project_records(migrated)
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM research_view_pins").fetchone()[0] == 0


def test_expired_artifact_pins_only_while_an_edit_protects_it(views):
    store, project, client, url = views
    artifact = page(store, project)
    with store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=? WHERE artifact_id=?",
            (
                artifact.model_copy(update={"expires_at": store.now()}).model_dump_json(),
                artifact.artifact_id,
            ),
        )
    body = {"artifact_id": artifact.artifact_id}
    expired = store.artifact(artifact.artifact_id)
    assert not store.research_view_offers(expired, store.local_owner.user_id).can_pin
    assert client.post(url, json=body).status_code == 410
    _task(
        store,
        project,
        status="running",
        request={"artifact_edit": {"artifact_id": artifact.artifact_id}},
    )
    assert client.post(url, json=body).status_code == 200
    assert store.artifact(artifact.artifact_id).kept_at
