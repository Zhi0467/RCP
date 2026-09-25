from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.limits import BACKUP_RETAINED_FAILED_CAPTURES
from rcp.server_ops.backup import (
    BackupRunRefused,
    apply_backup_retention,
    discard_backup_temporary_files,
    discard_orphaned_backup_receipts,
    plan_backup_retention,
    protect_backup_archive,
    prune_backup_capture_roots,
)
from rcp.server_ops.config import ServerBackupConfig

from .test_backup_encryption import (
    CAPTURED_AT,
    INSTALLATION_ID,
    _capture,
    _fake_age,
    _installed,
    _manifest,
)


def _protect(
    tmp_path: Path,
    destination: Path,
    age: Path,
    *,
    captured_at: datetime,
    status: str,
):
    capture_id = str(uuid.uuid4())
    capture_root, database = _capture(tmp_path / capture_id, capture_id=capture_id)
    manifest = _manifest(database, captured_at=captured_at)
    if status == "partial":
        manifest = manifest.model_copy(
            update={
                "uncaptured_app_data_entries": ("unknown-root",),
                "status": "partial",
            }
        )
    return protect_backup_archive(
        installed=_installed(destination),
        manifest=manifest,
        capture_root=capture_root,
        age_version="1.1.1",
        age_executable=str(age),
        protected_at=captured_at + timedelta(minutes=1),
    )


def test_retention_keeps_the_window_and_newest_complete_archive(tmp_path: Path) -> None:
    age = _fake_age(tmp_path)
    destination = tmp_path / "backups"
    destination.mkdir()
    old_complete = _protect(
        tmp_path,
        destination,
        age,
        captured_at=CAPTURED_AT,
        status="complete",
    )
    old_partial = _protect(
        tmp_path,
        destination,
        age,
        captured_at=CAPTURED_AT + timedelta(days=1),
        status="partial",
    )
    new_partial = _protect(
        tmp_path,
        destination,
        age,
        captured_at=CAPTURED_AT + timedelta(days=2),
        status="partial",
    )
    newest_partial = _protect(
        tmp_path,
        destination,
        age,
        captured_at=CAPTURED_AT + timedelta(days=3),
        status="partial",
    )
    config = ServerBackupConfig(
        destination=str(destination),
        age_recipient=_installed(destination).backup.age_recipient,
        retention=2,
    )

    plan = plan_backup_retention(
        config,
        installation_id=INSTALLATION_ID,
        expected_uid=os.geteuid(),
    )

    assert set(plan.kept_archives) == {
        old_complete.receipt.archive_name,
        new_partial.receipt.archive_name,
        newest_partial.receipt.archive_name,
    }
    assert plan.delete_archives == (old_partial.receipt.archive_name,)
    assert old_partial.archive_path.exists()

    deleted = apply_backup_retention(
        plan,
        installation_id=INSTALLATION_ID,
        expected_uid=os.geteuid(),
    )

    assert deleted == plan.delete_archives
    assert not old_partial.archive_path.exists()
    assert not old_partial.receipt_path.exists()
    assert old_complete.archive_path.exists()
    assert new_partial.archive_path.exists()
    assert newest_partial.archive_path.exists()


def test_retention_ignores_unproven_files_and_rechecks_before_deletion(tmp_path: Path) -> None:
    age = _fake_age(tmp_path)
    destination = tmp_path / "backups"
    destination.mkdir()
    older = _protect(
        tmp_path,
        destination,
        age,
        captured_at=CAPTURED_AT,
        status="complete",
    )
    _protect(
        tmp_path,
        destination,
        age,
        captured_at=CAPTURED_AT + timedelta(days=1),
        status="complete",
    )
    unknown = destination / "human-notes.tar.age"
    unknown.write_bytes(b"do not delete\n")
    forged = (
        destination
        / "rcp-team-backup-v1-20260829T120000000000Z-9c59550a-9787-466a-9435-1e59f0a9803f.tar.age"
    )
    forged.write_bytes(b"not age\n")
    config = ServerBackupConfig(
        destination=str(destination),
        age_recipient=_installed(destination).backup.age_recipient,
        retention=1,
    )
    plan = plan_backup_retention(
        config,
        installation_id=INSTALLATION_ID,
        expected_uid=os.geteuid(),
    )
    assert plan.delete_archives == (older.receipt.archive_name,)

    with older.archive_path.open("ab") as stream:
        stream.write(b"changed after preview")
    with pytest.raises(BackupRunRefused):
        apply_backup_retention(
            plan,
            installation_id=INSTALLATION_ID,
            expected_uid=os.geteuid(),
        )

    assert older.archive_path.exists()
    assert older.receipt_path.exists()
    assert unknown.read_bytes() == b"do not delete\n"
    assert forged.read_bytes() == b"not age\n"


def test_failed_capture_retention_preserves_task_scratch_then_success_clears_backups(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "run-stage"
    stage.mkdir(mode=0o700)
    task = stage / str(uuid.uuid4())
    task.mkdir()
    (task / "patch.json").write_text("task evidence")
    captures = []
    for index in range(BACKUP_RETAINED_FAILED_CAPTURES + 3):
        capture = stage / f"backup-{uuid.uuid4()}"
        capture.mkdir(mode=0o700)
        (capture / "rcp.sqlite3").write_bytes(b"failed capture")
        os.utime(capture, ns=(index, index))
        captures.append(capture)

    prune_backup_capture_roots(tmp_path, keep=BACKUP_RETAINED_FAILED_CAPTURES)

    assert {path for path in captures if path.exists()} == set(
        captures[-BACKUP_RETAINED_FAILED_CAPTURES:]
    )
    prune_backup_capture_roots(tmp_path, keep=0)
    assert not any(path.exists() for path in captures)
    assert (task / "patch.json").read_text() == "task evidence"


def test_backup_retention_refuses_symlinked_capture_boundary(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    capture = outside / f"backup-{uuid.uuid4()}"
    capture.mkdir(mode=0o700)
    (tmp_path / "run-stage").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BackupRunRefused, match="staging boundary is unsafe"):
        prune_backup_capture_roots(tmp_path, keep=0)
    assert capture.exists()


def test_backup_retry_removes_only_recognized_private_interrupted_writes(tmp_path: Path) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    server_root = tmp_path / "server"
    server_root.mkdir()
    archive = (
        "rcp-team-backup-v1-20260829T120000000000Z-9c59550a-9787-466a-9435-1e59f0a9803f.tar.age"
    )
    temporary_files = [
        destination / f".{archive}.{'a' * 32}.partial",
        destination / f".{archive}.receipt.json.{'b' * 32}.partial",
        destination
        / f"..rcp-backup-publication-9c59550a-9787-466a-9435-1e59f0a9803f.json.{'c' * 32}.partial",
        server_root / ".backup-status.json.abcdefgh",
        server_root / ".backup-diagnostics.json.12345678",
    ]
    for path in temporary_files:
        path.write_bytes(b"interrupted")
        path.chmod(0o600)
    unrelated = destination / ".human-notes.partial"
    unrelated.write_text("keep")

    discard_backup_temporary_files(SimpleNamespace(server_root=server_root), destination)

    assert not any(path.exists() for path in temporary_files)
    assert unrelated.read_text() == "keep"


def test_backup_retry_finishes_interrupted_receipt_retention(tmp_path: Path) -> None:
    age = _fake_age(tmp_path)
    destination = tmp_path / "backups"
    destination.mkdir()
    protected = _protect(tmp_path, destination, age, captured_at=CAPTURED_AT, status="complete")
    protected.archive_path.unlink()  # A process stop between archive and receipt deletion.
    unrelated = destination / "human.tar.age.receipt.json"
    unrelated.write_text("keep")

    discard_orphaned_backup_receipts(_installed(destination))

    assert not protected.receipt_path.exists()
    assert unrelated.read_text() == "keep"
