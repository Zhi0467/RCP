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
            "skipped",
            "last_error",
            "checked_at",
        }
        response = client.post(base + "/key", json={"repository": "owner/repo", "branch": "main"})
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "pending"
        assert client.get(base + "/branches").status_code == 409
        assert client.post(base + "/branch", json={"branch": "draft"}).status_code == 409
        assert client.delete(base).status_code == 204
        assert client.get(base).json()["state"] == "none"
