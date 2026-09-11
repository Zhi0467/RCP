from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from rcp.artifacts import descriptor_for
from rcp.limits import AGENT_TASK_LIST_MAX_LIMIT
from rcp.storage import AgentTaskRecord

from .helpers import create_named_app
from .test_episode_api import create_terminal_auto_episode
from .test_project_membership import _create_project, _team_app


def create_saved_artifact(app, project_id: str, *, kept: bool = True):
    """Real persisted output and bytes for inventory and served-viewer checks."""
    store = app.state.background_tasks.store
    operation_id = str(uuid.uuid4())
    data = b"<!doctype html><h1>Saved comparison</h1>"
    now = store.now()
    artifact = descriptor_for(operation_id, "comparison.html", size_bytes=len(data))
    if kept:
        workspace = app.state.catalog.open(project_id).history.workspace
        filename = workspace.keep_artifact(
            source_name=artifact.name,
            project_name="Research",
            data=data,
            today=datetime.now(UTC).date(),
        )
        artifact = artifact.model_copy(update={"kept_filename": filename, "kept_at": now})
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="project_chat",
            status="succeeded",
            request={"chat_id": str(uuid.uuid4()), "chat_scope": "project"},
            result={"artifacts": [artifact.model_dump(mode="json")]},
            created_at=now,
            updated_at=now,
            status_message="Completed",
        )
    )
    store.record_agent_task_receipt(
        operation_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    return task, artifact


def test_inventory_reopens_old_saved_output_and_archived_episode_report(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    task, artifact = create_saved_artifact(app, project_id)
    create_saved_artifact(app, project_id, kept=False)
    episode, _, report = create_terminal_auto_episode(
        store,
        app.state.catalog.open(project_id).history,
        project_id,
        episode_id="saved-report",
        report_html="<!doctype html><h1>Durable episode report</h1>",
    )
    assert report is not None
    old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET created_at = ?, history_only = 1 WHERE operation_id = ?",
            (old, task.operation_id),
        )
    # Enough newer tasks to push the saved output outside the ordinary task list.
    for index in range(AGENT_TASK_LIST_MAX_LIMIT + 1):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=f"recent-{index}",
                project_id=project_id,
                kind="refresh",
                status="succeeded",
                request={},
                created_at=store.now(),
                updated_at=store.now(),
                status_message="Completed",
            )
        )
    with TestClient(app) as client:
        assert (
            client.post(
                f"/api/projects/{project_id}/episodes/{episode.episode_id}/archive",
                json={"archived": True},
            ).status_code
            == 200
        )
        response = client.get(f"/api/projects/{project_id}/artifacts")
        assert response.status_code == 200, response.text
        entries = response.json()
        assert len(entries) == 2
        saved = next(entry for entry in entries if entry["kind"] == "artifact")
        assert saved["operation_id"] == task.operation_id
        assert saved["artifact_id"] == artifact.artifact_id
        assert saved["path"] == f"artifacts/{artifact.kept_filename}"
        assert saved["can_open"] is True
        assert client.get(saved["viewer_url"]).status_code == 200
        content = client.get(saved["viewer_url"].replace("/viewer", "/content"))
        assert content.status_code == 200
        assert "Saved comparison" in content.text
        retained_report = next(entry for entry in entries if entry["kind"] == "report")
        assert retained_report["id"] == f"report:{report.report_id}"
        assert retained_report["episode_id"] == episode.episode_id
        assert retained_report["created_at"] == report.created_at
        assert store.project_episode_report_summaries(str(uuid.uuid4())) == []
        assert "Trace the strongest evidence" in retained_report["name"]
        assert client.get(retained_report["viewer_url"]).status_code == 200
        assert (
            client.post(
                f"/api/projects/{project_id}/episodes/{episode.episode_id}/report/save"
            ).status_code
            == 200
        )
        assert client.get(f"/api/projects/{project_id}/artifacts").json() == entries


def test_inventory_enforces_membership_and_never_lists_other_project_outputs(tmp_path):
    app, client, _, people, acting = _team_app(tmp_path)
    first = _create_project(client, tmp_path / "first", name="First")
    second = _create_project(client, tmp_path / "second", name="Second")
    task, _ = create_saved_artifact(app, first)
    assert client.get(f"/api/projects/{first}/artifacts").json()[0]["operation_id"] == (
        task.operation_id
    )
    assert client.get(f"/api/projects/{second}/artifacts").json() == []
    acting[0] = people[1].user_id
    assert client.get(f"/api/projects/{first}/artifacts").status_code == 404


def test_legacy_project_url_lists_saved_outputs_with_canonical_viewer_urls(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    create_saved_artifact(app, project_id)
    create_terminal_auto_episode(
        store,
        app.state.catalog.open(project_id).history,
        project_id,
        episode_id="aliased-report",
        report_html="<!doctype html><h1>Retained report</h1>",
    )
    alias = "legacy-project-url"
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO project_aliases(alias_id, canonical_project_id) VALUES (?, ?)",
            (alias, project_id),
        )
    # Reopen to load the durable alias into the catalog's request-path snapshot.
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with TestClient(app) as client:
        canonical = client.get(f"/api/projects/{project_id}/artifacts")
        legacy = client.get(f"/api/projects/{alias}/artifacts")
        assert canonical.status_code == legacy.status_code == 200
        assert legacy.json() == canonical.json()
        assert {entry["kind"] for entry in legacy.json()} == {"artifact", "report"}
        for entry in legacy.json():
            assert entry["viewer_url"].startswith(f"/api/projects/{project_id}/")
            assert client.get(entry["viewer_url"]).status_code == 200
