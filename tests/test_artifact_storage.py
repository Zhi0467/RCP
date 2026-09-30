from __future__ import annotations

import gzip
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rcp.storage import AppStore, Artifact, ArtifactVersionConflict
from tests.helpers import wait_until


def _artifact(store: AppStore) -> Artifact:
    return store.create_artifact(
        Artifact(
            artifact_id="one",
            project_id="project",
            supplier="turn",
            supplier_id="turn",
            source_name="page.html",
            media_type="text/html",
            created_at=store.now(),
            expires_at=store.now(),
        ),
        data=b"original",
    )


def test_versions_compare_and_set_retry_retention_and_keep(tmp_path, monkeypatch):
    import rcp.storage.artifacts as module

    monkeypatch.setattr(module, "ARTIFACT_RECENT_VERSIONS", 2)
    monkeypatch.setattr(module, "ARTIFACT_MAX_VERSION_BYTES", 30)
    store = AppStore(tmp_path / "rcp.sqlite3")
    original = _artifact(store)
    base = original.current_version
    orphan = tmp_path / "artifacts" / "one" / ("f" * 64)
    orphan.write_bytes(b"uncommitted version")
    for index in range(4):
        version = store.publish_artifact_version(
            "one",
            base_version=base,
            operation_id="original" if index == 0 else str(index),
            data=str(index).encode(),
        )
        base = version.version_id
    assert len(store.artifact_versions("one")) == 3
    assert store.read_artifact_bytes("one", original.current_version) == b"original"
    assert (
        store.publish_artifact_version("one", base_version="stale", operation_id="3", data=b"3")
        == version
    )
    with pytest.raises(ArtifactVersionConflict):
        store.publish_artifact_version(
            "one", base_version=original.current_version, operation_id="stale", data=b"lost"
        )
    with pytest.raises(ValueError):
        store.publish_artifact_version(
            "one", base_version=base, operation_id="huge", data=b"x" * 30
        )
    assert store.keep_artifact("one").expires_at is None
    assert store.artifact("one").kept_at
    assert {p.name for p in (tmp_path / "artifacts" / "one").iterdir()} == {
        v.file_id for v in store.artifact_versions("one")
    }


def test_capture_allows_reads_and_creation_but_holds_pruned_files(tmp_path, monkeypatch):
    import rcp.storage.artifacts as module

    monkeypatch.setattr(module, "ARTIFACT_RECENT_VERSIONS", 1)
    store = AppStore(tmp_path / "rcp.sqlite3")
    artifact = _artifact(store)
    captured_version = store.publish_artifact_version(
        "one", base_version=artifact.current_version, operation_id="first", data=b"first"
    )
    captured_path = store.artifact_file_path(captured_version)
    # Another store for the same directory must share the capture guard.
    writer = AppStore(store.path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        with store.artifact_capture():
            snapshot_path = tmp_path / "snapshot.sqlite3"
            store.online_snapshot(snapshot_path)
            snapshot = AppStore.open_read_only_snapshot(snapshot_path)
            inventory = snapshot.artifact_inventory()
            publish = pool.submit(
                writer.publish_artifact_version,
                "one",
                base_version=captured_version.version_id,
                operation_id="second",
                data=b"second",
            )
            wait_until(
                lambda: writer.artifact("one").current_version != captured_version.version_id,
                detail="publishing must advance metadata during capture",
            )
            read = pool.submit(writer.read_artifact_bytes, "one")
            create = pool.submit(
                writer.create_artifact,
                artifact.model_copy(update={"artifact_id": "two"}),
                data=b"new artifact",
            )
            assert read.result(timeout=5) == b"second"
            assert create.result(timeout=5).artifact_id == "two"
            assert not publish.done()
            assert captured_path.read_bytes() == b"first"
            assert {item.file_id for item in inventory} == {
                artifact.current_version,
                captured_version.file_id,
            }
            for item in inventory:
                assert len(store.artifact_file_path(item).read_bytes()) == item.size_bytes
        publish.result(timeout=5)
    assert not captured_path.exists()
    assert writer.read_artifact_bytes("two") == b"new artifact"


def test_migration_check_uses_throwaway_files_and_relocation(tmp_path):
    fixture = (
        Path(__file__).parent
        / "fixtures/server_upgrade/pre-artifacts-v15-ff090e1/data/rcp.sqlite3.gz"
    )
    path = tmp_path / "rcp.sqlite3"
    path.write_bytes(gzip.decompress(fixture.read_bytes()))
    with sqlite3.connect(path) as connection:
        # A legacy view has all its bytes in SQLite; migration must not inspect its stage.
        now = AppStore.now()
        connection.execute(
            "INSERT INTO result_views VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "a" * 24,
                "project",
                "experiment",
                "chat",
                "origin",
                "latest",
                "codex",
                "model",
                "",
                "local",
                "session",
                "",
                "/missing/stage",
                "page.html",
                "0" * 64,
                4,
                "page",
                now,
                now,
                now,
                None,
                None,
            ),
        )
    connection.close()
    before = path.read_bytes()
    AppStore.open_read_only_snapshot(path).check_storage_schema_migrations()
    assert path.read_bytes() == before
    assert not (tmp_path / "artifacts").exists()
    store = AppStore(path)
    assert store.read_artifact_bytes("a" * 24) == b"page"
    with store.connection() as connection:
        assert "html" not in {
            row[1] for row in connection.execute("PRAGMA table_info(episode_reports)")
        }
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    path.rename(relocated / path.name)
    (tmp_path / "artifacts").rename(relocated / "artifacts")
    assert AppStore(relocated / path.name).read_artifact_bytes("a" * 24) == b"page"


def test_expiry_preserves_kept_artifact_and_clears_inventory(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    _artifact(store)
    assert store.expire_artifacts() == 1
    assert store.artifact("one") is None
    assert store.artifact_inventory() == []
    assert not (tmp_path / "artifacts" / "one").exists()
    _artifact(store)
    store.keep_artifact("one")
    assert store.expire_artifacts() == 0
    assert store.read_artifact_bytes("one") == b"original"


@pytest.mark.parametrize(
    "field,value",
    [
        ("supplier", "other"),
        ("media_type", "invalid"),
        ("expires_at", "invalid"),
        ("kept_at", "2000-01-01T00:00:00"),
    ],
)
def test_artifact_rejects_invalid_imported_metadata(tmp_path, field, value):
    store = AppStore(tmp_path / "rcp.sqlite3")
    fields = _artifact(store).model_dump()
    with pytest.raises(ValueError):
        Artifact.model_validate({**fields, field: value})


def test_live_snapshot_metadata_integrity_and_relocation(tmp_path):
    from rcp.live_artifacts import ResolvedLiveVersion
    from rcp.storage.artifact_models import ArtifactVersion

    store = AppStore(tmp_path / "rcp.sqlite3")
    artifact = _artifact(store)
    version_id = artifact.current_version
    live = ResolvedLiveVersion(invalid_reason="Invalid declaration")
    store.set_artifact_version_live("one", version_id, live)
    assert store.artifact_versions("one")[0].live == live
    assert store.read_artifact_live_snapshot("one", version_id) is None
    data = b'{"final":true,"needs":[]}'
    entry = store.save_artifact_live_snapshot("one", version_id, data)
    assert store.save_artifact_live_snapshot("one", version_id, data) == entry
    assert store.read_artifact_live_snapshot("one", version_id) == data
    with pytest.raises(ArtifactVersionConflict):
        store.save_artifact_live_snapshot("one", version_id, b"different")
    version = store.artifact_versions("one")[0]
    with pytest.raises(ValueError, match="another artifact"):
        ArtifactVersion.model_validate(
            {
                **version.model_dump(),
                "live_snapshot": {**entry.model_dump(), "artifact_id": "other"},
            }
        )
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    store.path.rename(relocated / "rcp.sqlite3")
    (tmp_path / "artifacts").rename(relocated / "artifacts")
    store = AppStore(relocated / "rcp.sqlite3")
    assert store.read_artifact_live_snapshot("one", version_id) == data
    store.artifact_file_path(entry).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="inventory"):
        store.read_artifact_live_snapshot("one", version_id)


def test_snapshot_pruning_waits_for_capture_and_preserves_retained_versions(tmp_path, monkeypatch):
    import rcp.storage.artifacts as module

    monkeypatch.setattr(module, "ARTIFACT_RECENT_VERSIONS", 1)
    store = AppStore(tmp_path / "rcp.sqlite3")
    original = _artifact(store)
    original_snapshot = store.save_artifact_live_snapshot(
        "one", original.current_version, b"original data"
    )
    first = store.publish_artifact_version(
        "one", base_version=original.current_version, operation_id="first", data=b"first"
    )
    snapshot = store.save_artifact_live_snapshot("one", first.version_id, b"first data")
    path = store.artifact_file_path(snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.artifact_capture():
            inventory = store.artifact_inventory()
            publish = pool.submit(
                store.publish_artifact_version,
                "one",
                base_version=first.version_id,
                operation_id="second",
                data=b"second",
            )
            wait_until(
                lambda: store.artifact("one").current_version != first.version_id,
                detail="publication updates metadata before capture releases files",
            )
            assert snapshot in inventory
            assert not publish.done()
            assert path.read_bytes() == b"first data"
        publish.result(timeout=5)
    assert not path.exists()
    assert store.artifact_file_path(original_snapshot).read_bytes() == b"original data"
    assert set(p.name for p in path.parent.iterdir()) == {
        v.file_id for v in store.artifact_inventory()
    }


def test_live_policy_migration_covers_report_creation_before_lifecycle_commit(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    ordinary = _artifact(store)
    report = store.create_artifact(
        ordinary.model_copy(update={"artifact_id": "unbound-report", "supplier": "episode_ending"}),
        data=b"report captured before lifecycle commit",
    )
    assert "live_data_allowed" not in report.model_dump()
    with store.connection() as connection:
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version = 32")
    reopened = AppStore(store.path)
    assert reopened.artifact(report.artifact_id).live_data_allowed is False
    assert reopened.artifact(ordinary.artifact_id).live_data_allowed is True
