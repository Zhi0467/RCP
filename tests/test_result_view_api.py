from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from rcp.storage import Artifact

from .helpers import create_named_app


def test_legacy_urls_use_artifact_storage_and_routes(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    artifact = store.create_artifact(
        Artifact(
            artifact_id=uuid.uuid4().hex[:24],
            project_id=project_id,
            supplier="turn",
            supplier_id="legacy-turn",
            source_name="result.html",
            media_type="text/html",
            created_at=store.now(),
        ),
        data=b"<h1>Retained result</h1>",
    )
    base = f"/api/projects/{project_id}"
    with TestClient(app) as client:
        legacy = client.get(
            f"{base}/result-views/{artifact.artifact_id}/preview", follow_redirects=False
        )
        assert legacy.status_code == 307
        assert legacy.headers["location"] == f"{base}/artifacts/{artifact.artifact_id}/viewer"
        content = client.get(f"{base}/artifacts/{artifact.artifact_id}/content")
        assert content.status_code == 200
        assert "Retained result" in content.text
        assert 'sandbox="allow-scripts"' in content.text
        assert (
            client.post(f"{base}/result-views/{artifact.artifact_id}/keep", json={}).status_code
            == 200
        )
        assert store.artifact(artifact.artifact_id).kept_at is not None
        assert not (manifest.path.parent / "artifacts").exists()
