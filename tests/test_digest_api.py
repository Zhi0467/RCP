from __future__ import annotations

from fastapi.testclient import TestClient

from rcp.digest import digest_counts
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

    app.state.digest_projector.run_pass()
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
    app.state.digest_projector.run_pass()
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


def test_get_waits_for_projector_before_baselining(manifest, tmp_path, monkeypatch):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    url = f"/api/projects/{project_id}/digest"
    signals = []
    monkeypatch.setattr(app.state.digest_projector, "signal", signals.append)

    def no_history(*args, **kwargs):
        raise AssertionError("Requests must not read history")

    with monkeypatch.context() as patch:
        patch.setattr(app.state.catalog, "open", no_history)
        patch.setattr(HistoryManager, "accepted_patch_boundaries", no_history)
        response = client.get(url)
        assert response.status_code == 200
        assert response.json() == dict(
            mark=None,
            cursor=0,
            count=0,
            needs_you=[],
            changed=[],
            branches=[],
            ran=[],
            changed_node_ids=[],
        )
        assert signals == [project_id]
        assert client.post(f"{url}/caught-up", json={"seq": 0}).status_code == 409
        assert digest_counts(store, [project_id], store.local_owner.user_id) == {project_id: 0}
        with store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM digest_marks").fetchone()[0] == 0
    app.state.digest_projector.run_pass()
    with monkeypatch.context() as patch:
        patch.setattr(HistoryManager, "accepted_patch_boundaries", no_history)
        response = client.get(url)
        assert response.status_code == 200
        assert response.json()["mark"] is not None
        assert response.json()["count"] == 0
