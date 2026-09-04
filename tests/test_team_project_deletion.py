from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from rcp.sources import ImportedProviderSourceStore, project_cache_roots
from rcp.storage import AgentTaskRecord
from rcp.transfer import TransferArchiveEntry

from .helpers import create_named_app as create_app
from .test_project_membership import _create_project, _team_app


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_team_delete_removes_rcp_state_and_preserves_checkout_and_key(tmp_path: Path) -> None:
    app, client, store, _people, acting = _team_app(tmp_path, members=1)
    repository = tmp_path / "managed-checkout"
    project_id = _create_project(client, repository)
    data_dir = store.path.parent

    checkout_marker = repository / "source.txt"
    checkout_marker.write_text("managed checkout\n", encoding="utf-8")
    repository_before = _tree_digest(repository)
    deploy_key = tmp_path / "server-credentials" / project_id / "paper-repo" / "id_ed25519"
    deploy_key.parent.mkdir(parents=True)
    deploy_key.write_text("private key marker\n", encoding="utf-8")

    source_cache, _session_cache = project_cache_roots(data_dir, project_id)
    cache_entry = source_cache / "remote" / "cached-source.jsonl"
    cache_entry.parent.mkdir(parents=True)
    cache_entry.write_text('{"source":"rebuildable"}\n', encoding="utf-8")
    imported_payload = b'{"type":"assistant","text":"retained"}\n'
    imported_digest = hashlib.sha256(imported_payload).hexdigest()
    imported_capture = (
        tmp_path / "imported-capture" / "provider-history" / "codex" / imported_digest
    )
    imported_capture.parent.mkdir(parents=True)
    imported_capture.write_bytes(imported_payload)
    imported_sources = ImportedProviderSourceStore(data_dir, project_id)
    imported_sources.publish(
        tmp_path / "imported-capture",
        (
            TransferArchiveEntry(
                archive_path=f"provider-history/codex/{imported_digest}",
                group="provider_history",
                sha256=imported_digest,
                size_bytes=len(imported_payload),
            ),
        ),
    )
    assert imported_sources.project_root.exists()

    stage = data_dir / "run-stage" / "retained-team-task"
    stage.mkdir(parents=True)
    stage_marker = stage / "patch.json"
    stage_marker.write_text("{}\n", encoding="utf-8")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="retained-team-task",
            project_id=project_id,
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
            stage_root=str(stage),
        )
    )
    display = app.state.catalog._cached_snapshot_path(project_id)
    display.parent.mkdir(parents=True, exist_ok=True)
    display.write_text("display snapshot\n", encoding="utf-8")
    paper = app.state.catalog._paper_snapshot_path(project_id)
    paper.parent.mkdir(parents=True, exist_ok=True)
    paper.write_text("paper snapshot\n", encoding="utf-8")

    [card] = client.get("/api/projects").json()
    assert card["id"] == project_id
    assert card["can_delete"] is True
    assert card["delete_unavailable_reason"] is None

    deleted = client.delete(f"/api/projects/{project_id}")

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["project_id"] == project_id
    assert store.project(project_id) is None
    assert store.project_members(project_id) == []
    assert store.agent_task("retained-team-task") is None
    assert not stage.exists()
    assert not display.exists()
    assert not paper.exists()
    assert not source_cache.parent.exists()
    assert not imported_sources.project_root.exists()
    assert _tree_digest(repository) == repository_before
    assert deploy_key.read_text(encoding="utf-8") == "private key marker\n"

    restarted = TestClient(
        create_app(
            data_dir=data_dir,
            trusted_principal_resolver=lambda _request, opened: opened.space_user(acting[0]),
        )
    )
    assert restarted.get("/api/projects").json() == []


def test_team_delete_refuses_an_active_task_before_touching_the_checkout(tmp_path: Path) -> None:
    app, client, store, _people, _acting = _team_app(tmp_path, members=1)
    repository = tmp_path / "managed-checkout"
    project_id = _create_project(client, repository)
    before = _tree_digest(repository)
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="active-team-delete-test",
            project_id=project_id,
            kind="seed",
            status="queued",
            request={},
            created_at=now,
            updated_at=now,
            status_message="queued",
        )
    )

    refused = client.delete(f"/api/projects/{project_id}")

    assert refused.status_code == 409
    assert refused.json()["detail"] == ("Pause the active agent task before deleting this project.")
    assert store.project(project_id) is not None
    assert _tree_digest(repository) == before
