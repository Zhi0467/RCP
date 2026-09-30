from __future__ import annotations

import gzip
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from rcp.storage import AppStore, Artifact, ArtifactVersionConflict


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


def test_capture_holds_files_and_database_pointer_together(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    artifact = _artifact(store)
    started = Event()
    finished = Event()

    def publish():
        started.set()
        store.publish_artifact_version(
            "one", base_version=artifact.current_version, operation_id="edit", data=b"edited"
        )
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.artifact_capture():
            future = pool.submit(publish)
            assert started.wait(1)
            assert not finished.is_set()
            inventory = store.artifact_inventory()
            assert len(inventory) == 1
            assert store.artifact_file_path(inventory[0]).read_bytes() == b"original"
        future.result(timeout=5)
    assert finished.is_set()


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
