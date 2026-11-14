from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import create_named_app, signed_in_client
from tests.paper_helpers import start_paper


def test_git_routes_require_member_and_create_pending_link(manifest, tmp_path: Path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project = app.state.default_project_id
    start_paper(app.state.catalog.paper_live)
    base = f"/api/projects/{project}/paper/git"
    with TestClient(app) as anonymous:
        assert anonymous.post(
            base + "/key", json={"repository": "owner/repo", "branch": "main"}
        ).status_code in {401, 403}
        assert anonymous.post(base + "/branch", json={"branch": "draft"}).status_code in {401, 403}
    with signed_in_client(app) as client:
        status = client.get(base).json()
        assert status["state"] == "none"
        assert set(status) == {
            "state",
            "repository",
            "branch",
            "public_key",
            "key_source",
            "head",
            "unpushed",
            "conflicts",
            "skipped",
            "last_error",
            "checked_at",
        }
        response = client.post(base + "/key", json={"repository": "owner/repo", "branch": "main"})
        assert response.status_code == 200
        assert response.json()["state"] == "pending"
        assert client.get(base + "/branches").status_code == 409
        assert client.post(base + "/branch", json={"branch": "draft"}).status_code == 409
        assert client.delete(base).status_code == 204
        assert client.get(base).json()["state"] == "none"


def test_coach_route_refuses_git_group(manifest, tmp_path: Path):
    from rcp.paper.changes import read_paper_changes
    from tests.paper_helpers import saved, wire_saved_paper

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project = app.state.default_project_id
    service = app.state.catalog.open(project)
    live = wire_saved_paper(service)
    output = tmp_path / "changes"
    (output / "files").mkdir(parents=True)
    (output / "files/paper.md").write_text("remote proposal")
    group = service.paper_changes.collect(
        project, "paper-git", saved(live, project), read_paper_changes(output)
    )[0]
    with signed_in_client(app) as client:
        response = client.post(
            f"/api/projects/{project}/paper/changes/{group.group_id}/accept", json={}
        )
        assert response.status_code == 409
        response = client.post(
            f"/api/projects/{project}/paper/changes/undo-all", json={"task_id": "paper-git"}
        )
        assert response.status_code == 409
    assert service.paper_changes.group(project, group.group_id).state == "pending"
