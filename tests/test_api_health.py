from __future__ import annotations

from fastapi.testclient import TestClient

from rcp import __version__
from rcp.server_runtime import ServerMetadata, data_dir_identity

from .helpers import create_named_app

create_app = create_named_app


def test_health_separates_durable_space_process_and_data_directory_identity(tmp_path) -> None:
    original_dir = tmp_path / "original-data"
    first_metadata = ServerMetadata.create(
        original_dir.resolve(), host="127.0.0.1", port=8421, owner_kind="embedded"
    )
    first_app = create_named_app(data_dir=original_dir, instance_metadata=first_metadata)
    with TestClient(first_app) as client:
        first = client.get("/api/health").json()

    restarted_metadata = ServerMetadata.create(
        original_dir.resolve(), host="127.0.0.2", port=9443, owner_kind="embedded"
    )
    restarted_app = create_named_app(data_dir=original_dir, instance_metadata=restarted_metadata)
    with TestClient(restarted_app) as client:
        restarted = client.get("/api/health").json()

    assert restarted["space_id"] == first["space_id"] == first_app.state.space_id
    assert restarted["instance_id"] != first["instance_id"]
    assert restarted["data_dir_id"] == first["data_dir_id"]
    assert restarted["space_id"] not in {restarted["instance_id"], restarted["data_dir_id"]}

    relocated_dir = tmp_path / "relocated-data"
    original_dir.rename(relocated_dir)
    relocated_metadata = ServerMetadata.create(
        relocated_dir.resolve(), host="127.0.0.1", port=8421, owner_kind="embedded"
    )
    relocated_app = create_named_app(data_dir=relocated_dir, instance_metadata=relocated_metadata)
    with TestClient(relocated_app) as client:
        relocated = client.get("/api/health").json()

    assert relocated["space_id"] == first["space_id"]
    assert relocated["instance_id"] != restarted["instance_id"]
    assert relocated["data_dir_id"] != restarted["data_dir_id"]


def test_health_reports_the_server_identity_version_data_and_activity(tmp_path) -> None:
    data_dir = tmp_path / "data"
    metadata = ServerMetadata.create(
        data_dir,
        host="127.0.0.1",
        port=18421,
        owner_kind="desktop",
    )
    app = create_app(data_dir=data_dir, instance_metadata=metadata)

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "space_id": app.state.space_id,
        "space_kind": "personal",
        "space_name": None,
        "instance_id": metadata.instance_id,
        "pid": metadata.pid,
        "data_dir_id": data_dir_identity(data_dir),
        "owner_kind": "desktop",
        "active_agent_tasks": 0,
        "projects": 0,
        "agent_mode": "provider",
    }
