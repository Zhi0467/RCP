from __future__ import annotations

from fastapi.testclient import TestClient

from rcp.history.manager import HistoryManager
from rcp.storage.digest import append_digest_event

from .helpers import create_named_app


def test_landing_counts_batch_events_without_history(manifest, tmp_path, monkeypatch):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    user_id = store.local_owner.user_id

    def no_history(*args, **kwargs):
        raise AssertionError("Landing counts must not read graph history")

    monkeypatch.setattr(app.state.catalog, "open", no_history)
    monkeypatch.setattr(HistoryManager, "accepted_patch_boundaries", no_history)
    assert client.get("/api/projects").json()[0]["digest_count"] == 0
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM digest_marks").fetchone()[0] == 0
    store.digest_snapshot(project_id, user_id)
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        append_digest_event(
            conn,
            project_id=project_id,
            kind="job_ended",
            item_id="job",
            created_at=store.now(),
            payload={"title": "Finished job", "status": "exited"},
        )
    response = client.get("/api/projects")
    assert response.status_code == 200
    assert response.json()[0]["digest_count"] == 1


def test_caught_up_keeps_events_committed_after_displayed_cursor(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    url = f"/api/projects/{project_id}/digest"
    initial = client.get(url)
    assert initial.status_code == 200
    assert initial.json()["count"] == 0

    def finish(identifier):
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return append_digest_event(
                conn,
                project_id=project_id,
                kind="job_ended",
                item_id=identifier,
                created_at=store.now(),
                payload={"title": identifier, "status": "exited"},
            )

    first = finish("first")
    displayed = client.get(url)
    assert displayed.status_code == 200
    assert displayed.json()["cursor"] == first
    second = finish("second")
    caught = client.post(f"{url}/caught-up", json={"seq": first})
    assert caught.status_code == 200
    remaining = client.get(url)
    assert remaining.status_code == 200
    assert remaining.json()["cursor"] == second
    assert [item["item_id"] for item in remaining.json()["ran"]] == ["second"]
    assert client.post(f"{url}/caught-up", json={"seq": second + 1}).status_code == 409
    assert client.post(f"{url}/caught-up", json={"seq": True}).status_code == 422
