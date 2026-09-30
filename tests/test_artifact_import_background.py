from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.api import app as app_module
from rcp.artifacts import descriptor_for
from rcp.background import StartupEffectFence
from rcp.runs.chat import _local_chat_artifact_directory
from rcp.server_runtime import ServerMetadata
from rcp.storage import AgentTaskRecord, AppStore

from .helpers import create_named_app, wait_until


def _app(manifest, tmp_path: Path, *, fence=None, control=False):
    data_dir = tmp_path / "data"
    if control:
        AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Import test team")
    metadata = ServerMetadata.create(
        data_dir,
        host="127.0.0.1",
        port=18422,
        owner_kind="cli" if control else "embedded",
        control_socket=Path(f"/private/tmp/import-{uuid.uuid4().hex}.sock") if control else None,
    )
    return create_named_app(
        str(manifest.path),
        data_dir=data_dir,
        instance_metadata=metadata,
        startup_effect_fence=fence,
    )


@pytest.mark.parametrize("fenced", [False, True])
def test_import_waits_for_fence_and_does_not_block_startup_or_another_project(
    manifest, tmp_path, monkeypatch, fenced
):
    fence = StartupEffectFence("artifact import test") if fenced else None
    app = _app(manifest, tmp_path, fence=fence)
    store = app.state.catalog.store
    first_id = app.state.default_project_id
    second_id = str(uuid.uuid4())
    foreign_id = str(uuid.uuid4())
    for project_id, home_id in ((second_id, store.space_id), (foreign_id, str(uuid.uuid4()))):
        store.upsert_project(
            store.project(first_id).model_copy(
                update={
                    "project_id": project_id,
                    "home_space_id": home_id,
                    "locator": str(tmp_path / f"{project_id}.toml"),
                }
            )
        )
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def import_pass(store, project_id, *, workspace):
        seen.append(project_id)
        if project_id == first_id:
            entered.set()
            assert release.wait(10)
        return None

    monkeypatch.setattr(app_module, "import_project_artifacts", import_pass)
    with TestClient(app, base_url="http://testserver:18422") as client:
        try:
            assert client.get("/api/health").status_code == 200
            if fenced:
                assert not seen
                fence.release()
            assert entered.wait(5)
            wait_until(lambda: second_id in seen)
            assert foreign_id not in seen
        finally:
            release.set()


def test_import_shutdown_waits_for_active_pass(manifest, tmp_path, monkeypatch):
    app = _app(manifest, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def import_pass(store, project_id, *, workspace):
        entered.set()
        assert release.wait(10)
        finished.set()
        return 0

    monkeypatch.setattr(app_module, "import_project_artifacts", import_pass)
    client = TestClient(app, base_url="http://testserver:18422")
    client.__enter__()
    assert entered.wait(5)
    with ThreadPoolExecutor(max_workers=1) as executor:
        shutdown = executor.submit(client.__exit__, None, None, None)
        try:
            assert not finished.wait(0.1)
            assert not shutdown.done()
        finally:
            release.set()
        shutdown.result(timeout=5)
    assert finished.is_set()


def test_maintenance_pauses_import_and_resumes_one_worker(manifest, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module.ServerControlServer, "start", lambda self: None)
    monkeypatch.setattr(app_module.ServerControlServer, "stop", lambda self: None)
    app = _app(manifest, tmp_path, control=True)
    calls = []

    def import_pass(store, project_id, *, workspace):
        calls.append(project_id)
        return 3600

    monkeypatch.setattr(app_module, "import_project_artifacts", import_pass)
    with TestClient(app, base_url="http://testserver:18422"):
        wait_until(lambda: len(calls) == 1)
        gate = app.state.background_admission_gate
        coordinator = app.state.maintenance_coordinator
        gate.close_and_wait(timeout=5)
        coordinator.pause_runtime_owners(5)
        assert len(calls) == 1
        gate.reopen()
        coordinator.resume_runtime_owners()
        coordinator.resume_runtime_owners()
        wait_until(lambda: len(calls) == 2)
        gate.close_and_wait(timeout=5)
        coordinator.pause_runtime_owners(5)
        assert len(calls) == 2


@pytest.mark.parametrize("missing", [False, True])
def test_background_import_serves_legacy_artifact_or_durable_reason(manifest, tmp_path, missing):
    app = _app(manifest, tmp_path)
    store = app.state.catalog.store
    operation_id = str(uuid.uuid4())
    data = b"<!doctype html><p>Imported legacy artifact</p>"
    descriptor = descriptor_for(
        operation_id, "legacy.html", media_type="text/html", size_bytes=len(data)
    )
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=app.state.default_project_id,
            kind="project_chat",
            status="succeeded",
            request={"chat_id": str(uuid.uuid4()), "chat_scope": "project", "mode": "discuss"},
            result={"artifacts": [descriptor.model_dump(mode="json")]},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Completed",
            stage_root=str(tmp_path / "stage"),
        )
    )
    store.record_agent_task_receipt(
        operation_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    directory = _local_chat_artifact_directory(store, task, operation_id)
    directory.mkdir(parents=True)
    if not missing:
        (directory / descriptor.name).write_bytes(data)
    base = (
        f"/api/projects/{task.project_id}/tasks/{operation_id}/artifacts/{descriptor.artifact_id}"
    )
    repository_artifacts = app.state.catalog.open(task.project_id).history.workspace.root.parent / (
        "artifacts"
    )
    assert store.artifact(descriptor.artifact_id) is None
    with TestClient(app, base_url="http://testserver:18422") as client:
        if missing:
            status = wait_until(lambda: store.artifact_import_status(descriptor.artifact_id))
            response = client.get(base + "/content")
            assert response.status_code == 410
            assert response.json()["detail"] == status["reason"]
            assert status["state"] == "missing"
            return
        wait_until(lambda: store.artifact(descriptor.artifact_id))
        content = client.get(base + "/content")
        assert content.status_code == 200
        assert b"Imported legacy artifact" in content.content
        download = client.get(base + "/download")
        assert download.status_code == 200
        assert download.content == data
        kept = client.post(base + "/keep")
        assert kept.status_code == 200
        artifact = store.artifact(descriptor.artifact_id)
        assert artifact.kept_at is not None and artifact.expires_at is None
        assert store.read_artifact_bytes(descriptor.artifact_id) == data
        assert not repository_artifacts.exists()
