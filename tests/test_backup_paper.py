from __future__ import annotations

import hashlib
import json
import os
import pwd
import sqlite3
import subprocess
import sys
import zlib

import pytest

from rcp.paper.folder import LocalPaperFolder, RemotePaperFolder, read_paper_folder
from rcp.paper.folder_io import paper_root
from rcp.server_ops import backup_paper
from rcp.server_ops.backup_paper import (
    PaperSection,
    capture_paper_section,
    restore_paper_section,
    section_files,
)
from rcp.transport.run_stage import RemoteRunStage

FIGURE = b"\x89PNG\r\n\x1a\nfigure"
OLD_FIGURE = b"\x89PNG\r\n\x1a\nolder figure"
FOLDER = {
    "sections/renamed.tex": b"saved text",
    "figures/result.png": FIGURE,
    "notes/new.md": b"written by an agent while nobody watched",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    from rcp.storage.base import AppStoreBase

    connection.execute(
        "CREATE TABLE resource_versions(scope TEXT, resource TEXT, entity TEXT, "
        "generation INTEGER, PRIMARY KEY(scope, resource, entity))"
    )
    connection.execute("CREATE TABLE graph_runs(dispatch_authority_json TEXT)")
    AppStoreBase._migrate_paper_live(connection)
    connection.execute(
        "CREATE TABLE paper_state (project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
        "turn_operation_id TEXT, turn_version_id TEXT)"
    )
    return connection


def _folder_paper(connection, project_id="p"):
    """Identity and History beside a folder; an unrelated body must not leak."""
    previous = b"named ancestor"
    rows = [
        ("text", "sections/renamed.tex", "text", None),
        ("image", "figures/result.png", "binary", None),
        ("gone", "old.md", "text", "deleted"),
    ]
    connection.executemany(
        "INSERT INTO paper_files (project_id,file_id,path,kind,created_at,deleted_at) "
        "VALUES (?, ?, ?, ?, 'now', ?)",
        [(project_id, *row) for row in rows],
    )
    manifest = [
        {"file_id": "text", "path": "old.tex", "kind": "text", "text_sha256": _sha(previous)},
        {
            "file_id": "image",
            "path": "figures/result.png",
            "kind": "binary",
            "blob_sha256": _sha(OLD_FIGURE),
        },
    ]
    connection.execute(
        "INSERT INTO paper_versions VALUES (?, 'version', 2, 'named', 'Milestone', 'human', "
        "'now', ?)",
        (project_id, json.dumps(manifest)),
    )
    connection.execute(
        "INSERT INTO paper_blobs VALUES (?, ?, ?)", (_sha(OLD_FIGURE), OLD_FIGURE, len(OLD_FIGURE))
    )
    connection.execute("INSERT INTO paper_blobs VALUES ('unrelated', x'00', 1)")
    connection.execute(
        "INSERT INTO paper_version_texts VALUES (?, ?)", (_sha(previous), zlib.compress(previous))
    )
    connection.execute("INSERT INTO paper_state VALUES (?, 7, NULL, NULL)", (project_id,))
    connection.execute(
        "INSERT INTO paper_doc_states (project_id,file_id,state,through_seq) "
        "VALUES (?, 'text', x'00', 0)",
        (project_id,),
    )


def test_section_carries_the_folder_with_identity_and_history_only():
    source, target = _database(), _database()
    try:
        _folder_paper(source)
        section = capture_paper_section(source, "p", FOLDER)
        assert section is not None
        section = PaperSection.model_validate_json(section.model_dump_json())
        assert set(section.tables) == {
            "paper_files",
            "paper_versions",
            "paper_blobs",
            "paper_version_texts",
        }
        assert {file.path: file.file_id for file in section.files}["figures/result.png"] == "image"
        assert section.revision == 7
        with target:
            restore_paper_section(target, section)
        assert section_files(section) == FOLDER
        assert {row[0] for row in target.execute("SELECT sha256 FROM paper_blobs")} == {
            _sha(FIGURE),
            _sha(OLD_FIGURE),
        }
        assert target.execute("SELECT revision FROM paper_state").fetchone() == (7,)
        assert capture_paper_section(target, "p", FOLDER) == section
    finally:
        source.close()
        target.close()


def test_section_refuses_corrupt_history_and_is_absent_without_paper():
    connection = _database()
    try:
        _folder_paper(connection)
        connection.execute("UPDATE paper_blobs SET bytes=x'00' WHERE sha256 != 'unrelated'")
        with pytest.raises(ValueError):
            capture_paper_section(connection, "p", FOLDER)
        assert capture_paper_section(connection, "other", {}) is None
        assert capture_paper_section(connection, "other", {"a.md": b"a"}) is not None
    finally:
        connection.close()


def _fake_ssh(home):
    stage = RemoteRunStage("example.invalid")

    def remote(arguments, *, input_data=None, **kwargs):
        return subprocess.run(
            [sys.executable, "-I", *arguments[1:]],
            input=input_data,
            capture_output=True,
            env={"HOME": str(home)},
        )

    home.mkdir(parents=True)
    stage._ssh_bytes = remote
    return stage


@pytest.mark.parametrize("remote", [False, True])
def test_paper_folder_backup_round_trips_local_and_ssh(tmp_path, remote):
    from rcp.server_ops.backup_project_files import capture_paper_folder
    from rcp.server_ops.restore import _restore_paper_folder

    def folder(name):
        if remote:
            return RemotePaperFolder(_fake_ssh(tmp_path / name), "p")
        return LocalPaperFolder(paper_root(tmp_path / name, "p"))

    source = folder("source")
    for path, data in FOLDER.items():
        source.write(path, data, None)
    payload = tmp_path / "archive"
    (payload / "projects/p").mkdir(parents=True)
    entries = capture_paper_folder(source, payload / "projects/p", "p")
    assert {entry.group for entry in entries} == {"paper_folder"}

    target = folder("target")
    _restore_paper_folder(target, entries, payload)
    assert read_paper_folder(target) == FOLDER
    target.write("notes/new.md", b"kept newer text", None)
    _restore_paper_folder(target, entries, payload)
    assert read_paper_folder(target)["notes/new.md"] == b"kept newer text"


def test_backup_captures_a_migrated_old_writer_paper_folder(tmp_path):
    """The old writer's Paper reaches the backup through the migration and the folder."""
    import uuid

    from rcp.paper.migration import migrate_paper
    from rcp.server_ops.backup_capture import (
        BackupCaptureCoordinator,
        write_immutable_backup_receipt,
    )
    from rcp.server_ops.backup_project_files import BackupProjectFileCaptureCoordinator
    from rcp.storage import AppStore
    from tests.paper_legacy_seed import legacy_tree, seed_legacy_paper
    from tests.test_backup_capture import _inventory_for_space, _metadata, _project_inventory

    project_id = str(uuid.uuid4())
    inventory, repository = _project_inventory(
        tmp_path, project_id=project_id, task_id=str(uuid.uuid4()), with_files=False
    )
    data_dir = tmp_path / "data"
    store, _ = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Paper backup")
    store.ensure_space_machines([("", pwd.getpwuid(os.geteuid()).pw_name, "Backend")])
    try:
        seed_legacy_paper(store, project_id, repository / ".research")
        folder = LocalPaperFolder(paper_root(data_dir, project_id))
        assert migrate_paper(store, project_id, folder, lambda: None)
        publication = BackupCaptureCoordinator(
            store, data_dir, _metadata(data_dir)
        ).capture_sqlite()
        inventory = _inventory_for_space(inventory, space_id=store.space_id)
        receipt = publication.receipt.model_copy(update={"projects": (inventory,)})
        publication.receipt_path.chmod(0o600)
        publication.receipt_path.unlink()
        digest = write_immutable_backup_receipt(publication.receipt_path, receipt)
        captured = BackupProjectFileCaptureCoordinator(data_dir).capture(
            publication.receipt_path, expected_sha256=digest
        )
        assert captured.receipt.status == "complete"
        [project] = captured.receipt.projects
        paper = {
            entry.source_relative_path: entry
            for entry in project.files
            if entry.group == "paper_folder"
        }
        assert set(paper) == {f"paper/{path}" for path in legacy_tree()}
        assert all(entry.sha256 == _sha(legacy_tree()[path[6:]]) for path, entry in paper.items())
        assert not any(entry.group == "paper_projection" for entry in project.files)
    finally:
        store.close()


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "notes/.publish",
        "normal.md",
    ],
)
def test_backup_and_live_share_path_validation(path):
    from rcp.paper.models import paper_path
    from rcp.server_ops.backup_models import BackupFileEntry
    from rcp.transfer.archive import TransferArchiveEntry

    entries = (
        lambda: BackupFileEntry(
            archive_path=f"project/.research/paper/{path}",
            source_relative_path=f".research/paper/{path}",
            group="paper_projection",
            sha256="a" * 64,
            size_bytes=0,
        ),
        lambda: BackupFileEntry(
            archive_path=f"project/paper/{path}",
            source_relative_path=f"paper/{path}",
            group="paper_folder",
            sha256="a" * 64,
            size_bytes=0,
        ),
        lambda: TransferArchiveEntry(
            archive_path=f"paper/projection/{path}",
            group="paper_projection",
            sha256="a" * 64,
            size_bytes=0,
        ),
        lambda: backup_paper.PaperSavedFile(file_id="text", path=path, kind="text", text=""),
    )
    for entry in entries:
        if path in {".git/config", "notes/.publish"}:
            with pytest.raises(ValueError):
                entry()
        else:
            assert paper_path(path) == path
            entry()
