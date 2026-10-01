from __future__ import annotations

from fastapi.testclient import TestClient

from .helpers import create_named_app


def test_paper_endpoints_cover_snapshot_create_save_and_sessions(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id

    assert client.get(f"/api/projects/{project_id}").json()["paper"]["sync_state"] == "not_created"
    initial = client.get(f"/api/projects/{project_id}/paper")
    assert initial.status_code == 200
    assert initial.json()["sync_state"] == "not_created"

    created = client.post(f"/api/projects/{project_id}/paper/create")
    assert created.status_code == 200
    assert created.json()["sync_state"] == "unsynced"

    saved = client.put(
        f"/api/projects/{project_id}/paper",
        json={
            "content": "# API introduction\n",
            "base_hash": created.json()["base_hash"],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["content"] == "# API introduction\n"
    assert saved.json()["sync_state"] == "synced"
    # The project snapshot, which a restarted app opens Paper from, follows the save.
    restarted = TestClient(create_named_app(str(manifest.path), data_dir=tmp_path / "data"))
    assert restarted.get(f"/api/projects/{project_id}").json()["paper"] == saved.json()

    sessions = client.get(f"/api/projects/{project_id}/paper/sessions")
    assert sessions.status_code == 200
    assert sessions.json() == []

    removed_conflict_route = client.post(
        f"/api/projects/{project_id}/paper/conflict",
        json={"strategy": "use_canonical"},
    )
    assert removed_conflict_route.status_code == 405


def test_paper_save_survives_a_concurrent_cache_update(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    catalog = app.state.services.catalog
    assert client.get(f"/api/projects/{project_id}").json()["paper"]["sync_state"] == "not_created"
    commit = catalog.commit_cached_snapshot
    raced = []

    def commit_after_a_newer_freshness_update(pid, snapshot, *, generation, **kwargs):
        if not raced and snapshot["paper"]["sync_state"] != "not_created":
            raced.append(True)
            winner = catalog.cached_snapshot(pid)
            winner["snapshot_freshness"] = "reconciling"
            newer = catalog.reserve_cached_snapshot_generation(pid)
            assert commit(pid, winner, generation=newer)
        return commit(pid, snapshot, generation=generation, **kwargs)

    catalog.commit_cached_snapshot = commit_after_a_newer_freshness_update
    created = client.post(f"/api/projects/{project_id}/paper/create").json()

    cached = client.get(f"/api/projects/{project_id}").json()
    assert raced
    assert cached["paper"] == created
    assert cached["snapshot_freshness"] == "reconciling"
