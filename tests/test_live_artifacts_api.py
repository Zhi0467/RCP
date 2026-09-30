from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from rcp.live_artifact_runtime import resolve_artifact_live_version

from .helpers import create_named_app
from .test_saved_artifacts_api import create_saved_artifact


def test_live_endpoint_pins_version_and_rechecks_project(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.catalog.store
    _, descriptor = create_saved_artifact(app, project_id)
    artifact = store.artifact(descriptor.artifact_id)
    metrics = Path(manifest.repositories[0].path) / "metrics.jsonl"
    metrics.write_text('{"loss": 1.0}\n')
    declaration = {
        "version": 1,
        "needs": [{"kind": "file", "path": str(metrics), "read": "tail", "format": "jsonl"}],
    }
    html = '<script type="application/json" id="rcp-live">' + json.dumps(declaration) + "</script>"
    version = store.publish_artifact_version(
        artifact.artifact_id,
        base_version=artifact.current_version,
        operation_id="live",
        data=html.encode(),
    )
    resolve_artifact_live_version(
        store, app.state.catalog.open(project_id), artifact.artifact_id, version.version_id
    )
    base = f"/api/projects/{project_id}/artifacts/{artifact.artifact_id}"
    live_url = f"{base}/versions/{quote(version.version_id, safe='')}/live"
    with TestClient(app) as client:
        result = client.get(live_url)
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["snapshots"][0]["rows"] == [{"loss": 1.0}]
        assert result.json()["final"] is False
        metrics.write_text('{"loss": 0.5}\n')
        assert client.get(live_url).json()["snapshots"][0]["rows"] == [{"loss": 0.5}]
        assert client.get(live_url.replace(project_id, "other-project")).status_code == 404
        assert client.get(f"{base}/versions/unknown/live").status_code == 404
        newer = store.publish_artifact_version(
            artifact.artifact_id,
            base_version=version.version_id,
            operation_id="static",
            data=b"<h1>Final static page</h1>",
        )
        content = client.get(f"{base}/content", params={"version_id": version.version_id})
        assert content.status_code == 200
        assert "Final static page" not in content.text
        assert client.get(live_url).json()["snapshots"][0]["rows"] == [{"loss": 0.5}]
        static = client.get(f"{base}/versions/{quote(newer.version_id, safe='')}/live")
        assert static.json()["static"] is True
        assert static.json()["final"] is False


def test_invalid_live_tag_is_visible_static_reason(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.catalog.store
    _, descriptor = create_saved_artifact(app, project_id)
    artifact = store.artifact(descriptor.artifact_id)
    version = store.publish_artifact_version(
        artifact.artifact_id,
        base_version=artifact.current_version,
        operation_id="invalid",
        data=b'<script type="application/json" id="rcp-live">{"version":999,"needs":[]}</script>',
    )
    resolve_artifact_live_version(
        store, app.state.catalog.open(project_id), artifact.artifact_id, version.version_id
    )
    url = f"/api/projects/{project_id}/artifacts/{artifact.artifact_id}/versions/{quote(version.version_id, safe='')}/live"
    with TestClient(app) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert response.json()["static"] is True
        assert response.json()["reason"]
        assert response.json()["snapshots"] == []
        assert response.json()["final"] is False
