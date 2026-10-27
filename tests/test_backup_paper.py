from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib

import pytest

from rcp.server_ops import backup_paper
from rcp.server_ops.backup_paper import (
    PaperSection,
    capture_paper_section,
    paper_projection_sources,
    restore_paper_section,
    verify_paper_section_records,
)


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    from rcp.storage.base import AppStoreBase

    AppStoreBase._migrate_paper_live(connection)
    return connection


def _saved_paper(connection, monkeypatch, project_id="p"):
    text = "saved unpublished text"
    previous = b"named ancestor"
    figure = b"\x89PNG\r\n\x1a\nfigure"
    digest = hashlib.sha256(figure).hexdigest()
    text_digest = hashlib.sha256(previous).hexdigest()
    files = [
        {"file_id": "text", "path": "sections/renamed.tex", "kind": "text", "text": text},
        {"file_id": "image", "path": "figures/result.png", "kind": "binary", "blob_sha256": digest},
    ]
    connection.executemany(
        "INSERT INTO paper_files (project_id,file_id,path,kind,created_at,deleted_at) VALUES ('p', ?, ?, ?, 'now', NULL)",
        [(f["file_id"], f["path"], f["kind"]) for f in files],
    )
    connection.execute(
        "INSERT INTO paper_updates VALUES ('p', 'text', 2, ?, 4, 'human', 'now')", (b"crdt-tail",)
    )
    connection.execute(
        "INSERT INTO paper_doc_states VALUES ('p', 'text', ?, 1, 3)", (b"crdt-prefix",)
    )
    connection.execute("INSERT INTO paper_revisions VALUES ('p', 4, 'now', 'human', 'rename')")
    connection.execute(
        "INSERT INTO paper_publication VALUES ('p', 4, 2, 'now', NULL, 'legacy-divergence', 'now')"
    )
    manifest = [
        {"file_id": "text", "path": "old.tex", "kind": "text", "text_sha256": text_digest},
        {"file_id": "image", "path": "figures/result.png", "kind": "binary", "blob_sha256": digest},
    ]
    connection.execute(
        "INSERT INTO paper_versions VALUES ('p', 'version', 2, 'named', 'Milestone', 'human', 'now', ?)",
        (json.dumps(manifest),),
    )
    connection.execute("INSERT INTO paper_blobs VALUES (?, ?, ?)", (digest, figure, len(figure)))
    connection.execute(
        "INSERT INTO paper_version_texts VALUES (?, ?)", (text_digest, zlib.compress(previous))
    )
    # Shared content owned only by another project must not leak into export.
    connection.execute("INSERT INTO paper_blobs VALUES ('unrelated', x'00', 1)")

    saved_hash = hashlib.sha256(text.encode()).hexdigest()
    connection.execute(
        "INSERT INTO paper_version_texts VALUES (?, ?)", (saved_hash, zlib.compress(text.encode()))
    )
    saved_manifest = [dict(file) for file in files]
    saved_manifest[0].pop("text")
    saved_manifest[0]["text_sha256"] = saved_hash
    connection.execute(
        "INSERT INTO paper_saved_snapshots VALUES ('p', 4, ?)", (json.dumps(saved_manifest),)
    )
    connection.execute("UPDATE paper_files SET blob_sha256=? WHERE file_id='image'", (digest,))
    connection.execute("INSERT INTO paper_apply_receipts VALUES ('p','key','hash','{}')")
    for table in backup_paper.PAPER_PROJECT_TABLES:
        if table not in {"paper_change_groups", "paper_changes", "paper_change_receipts"}:
            connection.execute(f'UPDATE "{table}" SET project_id=?', (project_id,))

    return figure


def test_saved_section_roundtrips_one_revision_and_all_recovery_state(monkeypatch):
    source = _database()
    target = _database()
    try:
        figure = _saved_paper(source, monkeypatch)
        section = capture_paper_section(source, "p")
        assert section is not None
        section = PaperSection.model_validate_json(section.model_dump_json())
        with target:
            restore_paper_section(target, section)
        verify_paper_section_records(target, section)
        assert target.execute("SELECT bytes FROM paper_blobs").fetchall() == [(figure,)]
        assert target.execute(
            "SELECT saved_revision, published_revision, hold FROM paper_publication"
        ).fetchone() == (4, 2, "legacy-divergence")
        assert target.execute('SELECT "update" FROM paper_updates').fetchone() == (b"crdt-tail",)
        assert target.execute("SELECT state FROM paper_doc_states").fetchone() == (b"crdt-prefix",)
        assert capture_paper_section(target, "p") == section
        target.execute("UPDATE paper_publication SET published_revision=4")
        with pytest.raises(ValueError):
            verify_paper_section_records(target, section)
    finally:
        source.close()
        target.close()


def test_saved_section_refuses_missing_snapshot_or_corrupt_content(monkeypatch):
    connection = _database()
    try:
        _saved_paper(connection, monkeypatch)
        connection.execute("UPDATE paper_blobs SET bytes=x'00' WHERE sha256 != 'unrelated'")
        with pytest.raises(ValueError):
            capture_paper_section(connection, "p")
        connection.execute("DELETE FROM paper_saved_snapshots")
        with pytest.raises(KeyError):
            capture_paper_section(connection, "p")
        assert capture_paper_section(connection, "legacy") is None
    finally:
        connection.close()


def test_projection_is_recursive_binary_safe_and_rejects_links(tmp_path):
    paper = tmp_path / "paper"
    (paper / "sections").mkdir(parents=True)
    (paper / "sections/one.tex").write_text("published")
    (paper / "figure.png").write_bytes(b"\x00\xff")
    assert {path.relative_to(paper).as_posix() for path in paper_projection_sources(tmp_path)} == {
        "sections/one.tex",
        "figure.png",
    }
    (paper / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        paper_projection_sources(tmp_path)


def test_restore_schema_requires_exact_registered_paper_shape(tmp_path, monkeypatch):
    from rcp.server_ops import restore
    from rcp.server_ops.backup_integrity import database_schema_sha256
    from rcp.server_ops.restore import _supported_restore_schema

    path = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE projects(project_id TEXT)")
        base = database_schema_sha256(connection)
        monkeypatch.setattr(restore, "SUPPORTED_RESTORE_DATABASE_SCHEMAS", frozenset({base}))
        connection.execute(
            "CREATE TABLE paper_publication(project_id, saved_revision, published_revision, pending_since, last_error, hold)"
        )
        shape = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE tbl_name='paper_publication' ORDER BY type,name,tbl_name"
            )
        )
        digest = database_schema_sha256(connection)
    assert not _supported_restore_schema(path, digest)
    monkeypatch.setattr(backup_paper, "PAPER_RESTORE_SCHEMA_VARIANTS", (shape,))
    assert _supported_restore_schema(path, digest)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE paper_publication ADD COLUMN unclassified")
        digest = database_schema_sha256(connection)
    assert not _supported_restore_schema(path, digest)


def test_backup_binds_saved_sqlite_revision_separately_from_projection(tmp_path, monkeypatch):
    import uuid

    from rcp.server_ops.backup_capture import (
        BackupCaptureCoordinator,
        write_immutable_backup_receipt,
    )
    from rcp.server_ops.backup_project_files import BackupProjectFileCaptureCoordinator
    from rcp.storage import AppStore
    from tests.test_backup_capture import _inventory_for_space, _metadata, _project_inventory

    project_id = str(uuid.uuid4())
    inventory, repository = _project_inventory(
        tmp_path, project_id=project_id, task_id=str(uuid.uuid4()), with_files=False
    )
    paper = repository / ".research/paper"
    paper.mkdir(exist_ok=True)
    (paper / "published.md").write_text("older published tree")
    data_dir = tmp_path / "data"
    store, _ = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Paper backup")
    seed = _database()
    try:
        figure = _saved_paper(seed, monkeypatch, project_id=project_id)
        with store.connection() as connection:
            for statement in seed.iterdump():
                if statement.startswith("INSERT INTO"):
                    connection.execute(statement)
        publication = BackupCaptureCoordinator(
            store, data_dir, _metadata(data_dir)
        ).capture_sqlite()
        inventory = _inventory_for_space(inventory, space_id=store.space_id).model_copy(
            update={"paper_revision": 4}
        )
        receipt = publication.receipt.model_copy(update={"projects": (inventory,)})
        publication.receipt_path.chmod(0o600)
        publication.receipt_path.unlink()
        digest = write_immutable_backup_receipt(publication.receipt_path, receipt)
        # Publication and live rows may advance after SQLite capture.
        with store.connection() as connection:
            connection.execute("UPDATE paper_publication SET saved_revision=5")
            connection.execute("DELETE FROM paper_blobs")
        captured = BackupProjectFileCaptureCoordinator(data_dir).capture(
            publication.receipt_path, expected_sha256=digest
        )
        assert captured.receipt.status == "complete"
        project = captured.receipt.projects[0]
        saved = next(entry for entry in project.files if entry.group == "paper_saved")
        section = PaperSection.model_validate_json(
            (publication.receipt_path.parent / saved.archive_path).read_bytes()
        )
        assert section.revision == 4
        assert backup_paper.decode_cell(section.tables["paper_blobs"][0]["bytes"]) == figure
        projection = [entry for entry in project.files if entry.group == "paper_projection"]
        assert [entry.source_relative_path for entry in projection] == [
            ".research/paper/published.md"
        ]
    finally:
        seed.close()
        store.close()


def test_restore_allowlist_matches_fresh_and_upgraded_migration_62(tmp_path):
    from rcp.storage import AppStore
    from rcp.storage.base import AppStoreBase

    fresh = AppStore(tmp_path / "fresh.sqlite3")
    upgraded = sqlite3.connect(":memory:")
    AppStoreBase._migrate_paper_live(upgraded)
    try:
        with fresh.connection() as connection:
            for database in (connection, upgraded):
                tables = set(backup_paper.PAPER_PROJECT_TABLES + backup_paper.PAPER_SHARED_TABLES)
                shape = tuple(
                    tuple(row)
                    for row in database.execute(
                        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
                    )
                    if row[2] in tables
                )
                assert shape in backup_paper.PAPER_RESTORE_SCHEMA_VARIANTS
    finally:
        fresh.close()
        upgraded.close()


def test_deleted_binary_without_retained_history_does_not_require_pruned_bytes(
    tmp_path, monkeypatch
):
    import asyncio

    from rcp import limits
    from rcp.paper import PaperLive, PaperOp
    from rcp.storage import AppStore
    from rcp.transport import LocalStateWorkspace

    monkeypatch.setattr(limits, "PAPER_SAVED_REVISION_LIMIT", 1)
    store = AppStore(tmp_path / "live.sqlite3")
    root = tmp_path / "state"
    root.mkdir()
    live = PaperLive(store, lambda _: LocalStateWorkspace(root, str(root)))

    async def scenario():
        created = await live.apply(
            "p", [PaperOp(kind="create", path="figure.png", file_kind="binary", data=b"image")], "h"
        )
        await live.apply("p", [PaperOp(kind="delete", file_id=created.file_ids[0])], "h")
        with store.connection() as connection:
            assert connection.execute("SELECT count(*) FROM paper_blobs").fetchone()[0] == 0
            section = capture_paper_section(connection, "p")
        assert section is not None and not section.files
        await live.close()

    try:
        asyncio.run(scenario())
    finally:
        store.close()
