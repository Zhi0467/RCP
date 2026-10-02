from __future__ import annotations

import threading

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

    created = client.post(f"/api/projects/{project_id}/paper/create", json={})
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


def test_paper_cache_update_holds_the_snapshot_lock(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    catalog = app.state.services.catalog
    assert client.get(f"/api/projects/{project_id}").json()["paper"]["sync_state"] == "not_created"
    commit = catalog._commit_cached_snapshot_locked
    racers: list[threading.Thread] = []

    def commit_while_a_freshness_update_waits(pid, snapshot, **kwargs):
        if not racers and snapshot["paper"]["sync_state"] != "not_created":
            racer = threading.Thread(
                target=catalog.update_cached_snapshot_freshness, args=(pid, "reconciling")
            )
            racers.append(racer)
            racer.start()
            racer.join(0.2)
            assert racer.is_alive()  # It cannot read until this update commits.
        return commit(pid, snapshot, **kwargs)

    catalog._commit_cached_snapshot_locked = commit_while_a_freshness_update_waits
    created = client.post(f"/api/projects/{project_id}/paper/create", json={}).json()
    racers[0].join(10)

    cached = catalog.cached_snapshot(project_id)
    assert cached["paper"] == created
    assert cached["snapshot_freshness"] == "reconciling"


def test_a_failed_cache_refresh_does_not_fail_the_save(manifest, tmp_path, monkeypatch) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    created = client.post(f"/api/projects/{project_id}/paper/create", json={}).json()

    def too_large(*_args):
        raise ValueError("Project display snapshot exceeds its size limit")

    monkeypatch.setattr(app.state.services.catalog, "update_cached_snapshot_paper", too_large)
    saved = client.put(
        f"/api/projects/{project_id}/paper",
        json={"content": "# Kept\n", "base_hash": created["base_hash"]},
    )
    assert saved.status_code == 200
    assert client.get(f"/api/projects/{project_id}/paper").json()["content"] == "# Kept\n"


def test_a_late_cache_refresh_keeps_the_newer_paper(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    catalog = app.state.services.catalog
    client.get(f"/api/projects/{project_id}")
    created = client.post(f"/api/projects/{project_id}/paper/create", json={}).json()
    refresh = catalog.update_cached_snapshot_paper
    delayed = []

    def delay_the_first_refresh(*args):
        if not delayed:
            delayed.append(args)
            return True
        return refresh(*args)

    catalog.update_cached_snapshot_paper = delay_the_first_refresh
    first = client.put(
        f"/api/projects/{project_id}/paper",
        json={"content": "# First\n", "base_hash": created["base_hash"]},
    ).json()
    second = client.put(
        f"/api/projects/{project_id}/paper",
        json={"content": "# Second\n", "base_hash": first["base_hash"]},
    ).json()
    refresh(*delayed[0])

    assert catalog.cached_snapshot(project_id)["paper"]["content"] == second["content"]
