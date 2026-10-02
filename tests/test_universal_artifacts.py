from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.artifact_comments import supports_comments
from rcp.artifacts import artifact_view, classify_artifact_bytes, descriptor_for
from rcp.runs.chat import _local_chat_artifact_directory
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, Artifact

from .helpers import create_named_app


@pytest.mark.parametrize(
    ("name", "data", "media_type", "view"),
    [
        ("unknown.bin", b"bytes", "application/octet-stream", "file"),
        ("bad.png", b"not PNG", "application/octet-stream", "file"),
        ("bad.html", b"\xff", "application/octet-stream", "file"),
        ("nul.md", b"a\0b", "application/octet-stream", "file"),
        ("bad.svg", b"<root/>", "application/octet-stream", "file"),
        ("bad.pdf", b"not PDF", "application/octet-stream", "file"),
        ("notes.md", b"# Notes", "text/markdown", "markdown"),
        ("results.csv", b"a,b\n1,2", "text/csv", "text"),
        ("paper.pdf", b"%PDF-1.7", "application/pdf", "pdf"),
        ("notebook.ipynb", b"{}", "text/plain", "text"),
        ("data.JSON", b"{}", "application/json", "text"),
    ],
)
def test_soft_classification(name, data, media_type, view):
    assert classify_artifact_bytes(name, data) == media_type
    assert artifact_view(media_type) == view
    assert supports_comments(media_type) == (view not in {"pdf", "file"})


def _seed(app, tmp_path: Path, name: str, data: bytes, *, media_type=None, chat=True):
    store = app.state.background_tasks.store
    operation_id = str(uuid.uuid4())
    descriptor = descriptor_for(
        operation_id,
        name,
        media_type=media_type or classify_artifact_bytes(name, data),
        size_bytes=len(data),
    )
    if not chat:
        descriptor = descriptor.model_copy(update={"kept_at": store.now()})
    stored = store.create_artifact(
        Artifact(
            artifact_id=descriptor.artifact_id,
            project_id=app.state.default_project_id,
            supplier="turn",
            supplier_id=operation_id,
            origin_operation_id=operation_id,
            source_name=name,
            media_type=descriptor.media_type,
            created_at=store.now(),
            kept_at=descriptor.kept_at,
        ),
        data=data,
    )
    request = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Inspect",
        mode="discuss",
    ).model_dump(mode="json")
    if not chat:
        request.pop("chat_id")
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=app.state.default_project_id,
            kind="project_chat",
            status="succeeded",
            request=request,
            result={"messages": ["answer"], "artifacts": [descriptor.model_dump(mode="json")]},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Completed",
            stage_root=str(tmp_path / operation_id),
            native_session_id="session",
        )
    )
    store.record_agent_task_receipt(
        operation_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    directory = (
        _local_chat_artifact_directory(store, task, operation_id) if chat else tmp_path / "unused"
    )
    directory.mkdir(parents=True)
    path = directory / name
    path.write_bytes(data)
    base = (
        f"/api/projects/{task.project_id}/tasks/{operation_id}/artifacts/{descriptor.artifact_id}"
    )
    version = store.artifact_versions(stored.artifact_id)[0]
    return task, descriptor, store.artifact_file_path(version), base


@pytest.mark.parametrize(
    ("name", "data"),
    [
        (
            "notes.md",
            b"# Notes\n<script>alert(1)</script>\n[label](https://example.org)\n![alt](https://example.org/image)",
        ),
        ("results.csv", b"a,b\n<script>,2"),
        ("paper.pdf", b"%PDF-1.7"),
        ("raw.bin", b"\x00\xff"),
        ("view.html", b"<h1>view</h1>"),
        ("image.png", b"\x89PNG\r\n\x1a\n"),
        ("image.jpg", b"\xff\xd8\xff"),
        ("image.gif", b"GIF89a"),
        ("image.webp", b"RIFF0000WEBP"),
        ("image.svg", b"<svg/>"),
    ],
)
def test_routes_capabilities_and_server_context_gate(tmp_path, manifest, name, data):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    task, descriptor, _path, base = _seed(app, tmp_path, name, data)
    with TestClient(app) as client:
        result = client.get(f"/api/projects/{task.project_id}/tasks/{task.operation_id}").json()[
            "result"
        ]
        projected = result["artifacts"][0]
        commentable = supports_comments(descriptor.media_type)
        assert projected["can_discuss"] is commentable
        assert "can_revise" not in projected
        assert projected["can_download"] and projected["can_keep"]
        download = client.get(base + "/download")
        assert download.status_code == 200 and download.content == data
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers["content-type"].split(";")[0] == descriptor.media_type
        content = client.get(base + "/content")
        viewer = client.get(base + "/viewer")
        if projected["view"] in {"file", "pdf"}:
            assert content.status_code == viewer.status_code == 404
            assert not projected["can_open"]
        else:
            assert content.status_code == viewer.status_code == 200
            assert ('id="composer"' in viewer.text) is commentable
            if projected["view"] in {"markdown", "text"}:
                assert "<script" not in content.text and "<img" not in content.text
                assert "href=" not in content.text and "&lt;script&gt;" in content.text
                assert "sandbox" in content.headers["content-security-policy"]
                assert "allow-scripts" not in content.headers["content-security-policy"]
        if not commentable:
            request = {
                **task.request,
                "artifact_context": {
                    "source": "task",
                    "operation_id": task.operation_id,
                    "artifact_id": descriptor.artifact_id,
                    "selections": [{"kind": "text", "text": "a", "comment": "Inspect"}],
                },
            }
            response = client.post(
                f"/api/projects/{task.project_id}/tasks/project_chat",
                json=request,
            )
            assert response.status_code == 422
        kept = client.post(base + "/keep")
        assert kept.status_code == 200
        saved = client.get(f"/api/projects/{task.project_id}/artifacts").json()[0]
        assert saved["view"] == projected["view"]
        assert saved["can_download"] and saved["available"]
        assert saved["download_url"] == base + "/download"
        assert saved["can_open"] == projected["can_open"]


def test_stored_type_pinning_and_changed_typed_bytes(tmp_path, manifest):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, _, _, plain = _seed(
        app, tmp_path, "plain.html", b"<p>valid now</p>", media_type="application/octet-stream"
    )
    _, _, typed_path, typed = _seed(app, tmp_path, "typed.html", b"<p>valid</p>")
    typed_path.write_bytes(b"\xff")
    with TestClient(app) as client:
        assert client.get(plain + "/content").status_code == 404
        response = client.get(plain + "/download")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert client.get(typed + "/content").status_code == 410
        assert client.get(typed + "/download").status_code == 410


def test_viewer_without_chat_uses_stored_artifact_admission(tmp_path, manifest):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, _, _, base = _seed(app, tmp_path, "view.html", b"<p>view</p>", chat=False)
    with TestClient(app) as client:
        viewer = client.get(base + "/viewer")
        assert viewer.status_code == 200 and 'id="message"' in viewer.text
        content = client.get(base + "/content")
        assert content.status_code == 200
        assert "installArtifactSelection" in content.text
        assert "rcp-reference" in content.text
