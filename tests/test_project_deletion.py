from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher
from rcp.core.models import AuthorizedHuman
from rcp.history import ProjectIdentityConflict
from rcp.projects import ProjectCatalog
from rcp.sources import ImportedProviderSourceStore, project_cache_roots
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    EpisodeRecord,
    ProjectRecord,
    WatcherContinuation,
    WatcherRecord,
)
from rcp.transport import RemoteRunStage


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_catalog_delete_reclaims_only_app_owned_files_and_persists(manifest, tmp_path) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    repository = Path(manifest.repository_map[manifest.state.repository].path)
    before = _tree_digest(repository)

    stage = data_dir / "run-stage" / "saved-operation"
    stage.mkdir(parents=True)
    (stage / "patch.json").write_text("{}", encoding="utf-8")
    (stage / "patch.json").chmod(0o400)
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="saved-operation",
            project_id=record.project_id,
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
            stage_host="",
            stage_root=str(stage),
        )
    )
    display = catalog._cached_snapshot_path(record.project_id)
    display.parent.mkdir(parents=True)
    display.write_text("display", encoding="utf-8")
    paper = catalog._paper_snapshot_path(record.project_id)
    paper.parent.mkdir(parents=True)
    paper.write_text("draft", encoding="utf-8")
    catalog._services[record.project_id] = object()  # type: ignore[assignment]

    result = catalog.delete(record.project_id)

    assert result.project_id == record.project_id
    assert result.removed_stages == 1
    assert result.removed_display_snapshot is True
    assert result.removed_paper_snapshot is True
    assert not stage.exists()
    assert not display.exists()
    assert not paper.exists()
    assert record.project_id not in catalog._services
    assert _tree_digest(repository) == before

    reopened_store = AppStore(store.path)
    reopened_catalog = ProjectCatalog(data_dir, reopened_store, AgentLauncher())
    assert reopened_store.project(record.project_id) is None
    with pytest.raises(KeyError):
        reopened_catalog.card(record.project_id)


def test_catalog_delete_warns_when_remote_cleanup_fails_after_commit(
    manifest, tmp_path, monkeypatch, caplog
) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="saved-remote-operation",
            project_id=record.project_id,
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
            stage_host="research-host",
            stage_root="/tmp/rcp-run.saved-remote-operation",
        )
    )
    monkeypatch.setattr(RemoteRunStage, "close", lambda self: False)

    result = catalog.delete(record.project_id)

    assert result.project_id == record.project_id
    assert result.removed_stages == 0
    assert store.project(record.project_id) is None
    assert store.agent_task("saved-remote-operation") is None
    assert record.project_id in caplog.text
    assert "research-host:/tmp/rcp-run.saved-remote-operation" in caplog.text
    assert "Could not remove saved run stage" in caplog.text


def test_catalog_delete_rejects_local_stage_outside_app_boundary(manifest, tmp_path) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    outside = tmp_path / "repository-owned"
    outside.mkdir()
    marker = outside / "must-remain"
    marker.write_text("source", encoding="utf-8")
    cache_root, _ = project_cache_roots(data_dir, record.project_id)
    cached = cache_root / "remote" / "saved.jsonl"
    cached.parent.mkdir(parents=True)
    cached.write_text("saved cache", encoding="utf-8")
    display = catalog._cached_snapshot_path(record.project_id)
    display.parent.mkdir(parents=True)
    display.write_text("saved display", encoding="utf-8")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="unsafe-stage",
            project_id=record.project_id,
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
            stage_host="",
            stage_root=str(outside),
        )
    )

    with pytest.raises(ValueError, match="outside the RCP staging boundary"):
        catalog.delete(record.project_id)

    assert marker.read_text(encoding="utf-8") == "source"
    assert cached.read_text(encoding="utf-8") == "saved cache"
    assert display.read_text(encoding="utf-8") == "saved display"
    assert os.path.exists(outside)
    assert store.project(record.project_id) is not None


def test_catalog_delete_validates_snapshot_before_removing_stage_or_cache(
    manifest, tmp_path
) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    stage = data_dir / "run-stage" / "saved-stage"
    stage.mkdir(parents=True)
    stage_marker = stage / "patch.json"
    stage_marker.write_text("saved stage", encoding="utf-8")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="saved-stage",
            project_id=record.project_id,
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
            stage_root=str(stage),
        )
    )
    cache_root, _ = project_cache_roots(data_dir, record.project_id)
    cached = cache_root / "remote" / "saved.jsonl"
    cached.parent.mkdir(parents=True)
    cached.write_text("saved cache", encoding="utf-8")
    external = tmp_path / "external-display"
    external.write_text("outside", encoding="utf-8")
    display = catalog._cached_snapshot_path(record.project_id)
    display.parent.mkdir(parents=True)
    display.symlink_to(external)

    with pytest.raises(ValueError, match="non-file project display snapshot"):
        catalog.delete(record.project_id)

    assert stage_marker.read_text(encoding="utf-8") == "saved stage"
    assert cached.read_text(encoding="utf-8") == "saved cache"
    assert external.read_text(encoding="utf-8") == "outside"
    assert display.is_symlink()
    assert store.project(record.project_id) is not None


def test_catalog_delete_validates_imported_sources_before_removing_files(
    manifest, tmp_path
) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    stage = data_dir / "run-stage" / "saved-stage"
    stage.mkdir(parents=True)
    stage_marker = stage / "patch.json"
    stage_marker.write_text("saved stage", encoding="utf-8")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="saved-stage",
            project_id=record.project_id,
            kind="refresh",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
            stage_root=str(stage),
        )
    )
    display = catalog._cached_snapshot_path(record.project_id)
    display.parent.mkdir(parents=True)
    display.write_text("saved display", encoding="utf-8")
    cache_root, _ = project_cache_roots(data_dir, record.project_id)
    cached = cache_root / "remote" / "saved.jsonl"
    cached.parent.mkdir(parents=True)
    cached.write_text("saved cache", encoding="utf-8")
    capture = tmp_path / "capture" / "provider-history"
    capture.mkdir(parents=True)
    imported = ImportedProviderSourceStore(data_dir, record.project_id)
    imported.publish(tmp_path / "capture", ())
    unrelated = imported.project_root / "unrelated"
    unrelated.write_text("must remain", encoding="utf-8")

    with pytest.raises(ValueError, match="contains unrelated state"):
        catalog.delete(record.project_id)

    assert stage_marker.read_text(encoding="utf-8") == "saved stage"
    assert display.read_text(encoding="utf-8") == "saved display"
    assert cached.read_text(encoding="utf-8") == "saved cache"
    assert unrelated.read_text(encoding="utf-8") == "must remain"
    assert store.project(record.project_id) is not None


def test_catalog_delete_accepts_legacy_non_uuid_project_id(tmp_path) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    project_id = "legacy-project"
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            locator=str(tmp_path / "legacy" / ".research" / "manifest.toml"),
            name="Legacy project",
            state_location=str(tmp_path / "legacy" / ".research"),
            state_remote=False,
            added_at=store.now(),
        )
    )

    assert catalog.card(project_id)["can_delete"] is True
    result = catalog.delete(project_id)

    assert result.project_id == project_id
    assert store.project(project_id) is None


def test_catalog_delete_refuses_live_episode(manifest, tmp_path) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    owner = store.local_owner
    assert owner is not None
    now = store.now()
    store.create_episode(
        EpisodeRecord(
            episode_id="live-episode",
            project_id=record.project_id,
            mode="experiment_loop",
            control_node_id="experiment",
            status="queued",
            invocation_ceiling=1,
            authorized_by=AuthorizedHuman(
                space_id=store.space_id,
                user_id=owner.user_id,
                display_name="Researcher",
            ),
            created_at=now,
            updated_at=now,
        )
    )

    with pytest.raises(ValueError, match="Use Stop"):
        catalog.delete(record.project_id)

    assert store.project(record.project_id) is not None
    assert store.episode("live-episode") is not None


def test_catalog_delete_refuses_active_watcher(manifest, tmp_path) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="watch-origin",
            project_id=record.project_id,
            kind="project_chat",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
        )
    )
    store.create_watchers(
        [
            WatcherRecord(
                watcher_id="live-watcher",
                project_id=record.project_id,
                origin_operation_id="watch-origin",
                origin_task_kind="project_chat",
                chat_id="chat",
                check_command="true",
                log_path="/tmp/live-watcher.log",
                cwd="/tmp",
                continuation=WatcherContinuation(
                    provider="codex",
                    run_on="local",
                ),
                created_at=now,
            )
        ]
    )

    with pytest.raises(ValueError, match="stop watching"):
        catalog.delete(record.project_id)

    assert store.project(record.project_id) is not None
    assert store.watcher("live-watcher") is not None


@pytest.mark.parametrize(("notified", "blocks_deletion"), [(False, True), (True, False)])
def test_catalog_delete_handles_degraded_watcher_delivery(
    manifest,
    tmp_path,
    notified: bool,
    blocks_deletion: bool,
) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="watch-origin",
            project_id=record.project_id,
            kind="project_chat",
            status="failed",
            request={},
            created_at=now,
            updated_at=now,
            status_message="failed",
        )
    )
    store.create_watchers(
        [
            WatcherRecord(
                watcher_id="degraded-watcher",
                project_id=record.project_id,
                origin_operation_id="watch-origin",
                origin_task_kind="project_chat",
                chat_id="chat",
                check_command="true",
                log_path="/tmp/degraded-watcher.log",
                cwd="/tmp",
                continuation=WatcherContinuation(
                    provider="codex",
                    run_on="local",
                ),
                status="degraded",
                created_at=now,
                notified=notified,
                last_error="temporary watcher failure",
                consecutive_error_count=1,
            )
        ]
    )

    if blocks_deletion:
        with pytest.raises(ValueError, match="stop watching"):
            catalog.delete(record.project_id)

        assert store.project(record.project_id) is not None
        assert store.watcher("degraded-watcher") is not None
    else:
        catalog.delete(record.project_id)

        assert store.project(record.project_id) is None
        assert store.watcher("degraded-watcher") is None


def test_deleted_tagged_project_reregisters_with_canonical_id_and_alias(
    manifest,
    tmp_path,
) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    first = catalog.register(str(manifest.path), identity_action="adopted")
    aliases = store.project_aliases()
    assert len(aliases) == 1
    old_project_id = next(iter(aliases))

    catalog.delete(first.project_id)
    restored = catalog.register(str(manifest.path))

    assert restored.project_id == first.project_id
    assert restored.home_space_id == store.space_id
    assert store.resolve_project_id(old_project_id) == first.project_id
    assert catalog.card(old_project_id)["id"] == first.project_id


def test_registration_waits_until_project_deletion_cleanup_finishes(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "app-data"
    store = AppStore(data_dir / "rcp.sqlite3")
    catalog = ProjectCatalog(data_dir, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    cleanup_started = threading.Event()
    finish_cleanup = threading.Event()
    delete_records = store.delete_project_records

    def pause_after_commit(project_id: str):
        result = delete_records(project_id)
        cleanup_started.set()
        if not finish_cleanup.wait(timeout=5):
            raise TimeoutError("test did not release project cleanup")
        return result

    monkeypatch.setattr(store, "delete_project_records", pause_after_commit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        deletion = executor.submit(catalog.delete, record.project_id)
        try:
            assert cleanup_started.wait(timeout=5)
            assert store.project(record.project_id) is None
            with pytest.raises(ProjectIdentityConflict, match="being deleted"):
                catalog.register(str(manifest.path))
            assert store.project(record.project_id) is None
        finally:
            finish_cleanup.set()
        assert deletion.result(timeout=5).project_id == record.project_id

    restored = catalog.register(str(manifest.path))
    assert restored.project_id == record.project_id
