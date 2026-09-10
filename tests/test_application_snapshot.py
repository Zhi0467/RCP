from __future__ import annotations

import hashlib
import json
import stat
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from rcp.runs.shared import checkpoint_local_recovery_stages
from rcp.server_ops.application_snapshot import (
    ApplicationSnapshotPolicy,
    ApplicationSnapshotRefused,
    _snapshot_tree,
)
from rcp.storage.models import (
    ProjectTransferUploadCompleteReceipt,
    ProjectTransferUploadRecord,
    RunStageLifecycleRecord,
)
from rcp.transfer.target import target_transfer_archive_path

BASE_COMMIT = "a" * 40
CANDIDATE_COMMIT = "b" * 40
WEB_BUILD_ID = "sha256:" + ("c" * 64)


class _StageLifecycleStore:
    def __init__(self, *records: RunStageLifecycleRecord) -> None:
        self.records = records

    def run_stage_lifecycles(self) -> tuple[RunStageLifecycleRecord, ...]:
        return self.records


def test_local_recovery_stage_inventory_uses_the_task_ledger_and_ignores_remote(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    local = data_dir / "run-stage" / "local-stage"
    local.mkdir(parents=True)
    local_operation = str(uuid.uuid4())
    local_episode = str(uuid.uuid4())
    remote_view = uuid.uuid4().hex[:24]
    store = _StageLifecycleStore(
        RunStageLifecycleRecord(
            stage_host="",
            stage_root=str(local),
            owner_refs=(
                f"experiment_episode_state:{local_episode}",
                f"graph_runs:{local_operation}",
            ),
            must_exist=True,
            protect_from_cleanup=True,
        ),
        RunStageLifecycleRecord(
            stage_host="gpu.example",
            stage_root="/tmp/rcp-run.remote-stage",
            owner_refs=(f"result_views:{remote_view}",),
            must_exist=False,
            protect_from_cleanup=True,
        ),
    )

    inventory = checkpoint_local_recovery_stages(store, data_dir)

    assert [item.root for item in inventory] == [local]
    assert inventory[0].owner_refs == (
        f"experiment_episode_state:{local_episode}",
        f"graph_runs:{local_operation}",
    )


def test_local_recovery_stage_inventory_ignores_retention_swept_history(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    missing_task = data_dir / "run-stage" / "old-task"
    missing_view = data_dir / "run-stage" / "old-result-view"
    store = _StageLifecycleStore(
        RunStageLifecycleRecord(
            stage_host="",
            stage_root=str(missing_task),
            owner_refs=(f"graph_runs:{uuid.uuid4()}",),
            must_exist=False,
            protect_from_cleanup=False,
        ),
        RunStageLifecycleRecord(
            stage_host="",
            stage_root=str(missing_view),
            owner_refs=(f"result_views:{uuid.uuid4().hex[:24]}",),
            must_exist=False,
            protect_from_cleanup=False,
        ),
    )

    assert checkpoint_local_recovery_stages(store, data_dir) == ()


def test_local_recovery_stage_inventory_requires_active_episode_stage(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    episode_id = str(uuid.uuid4())
    missing = data_dir / "run-stage" / "active-episode"
    store = _StageLifecycleStore(
        RunStageLifecycleRecord(
            stage_host="",
            stage_root=str(missing),
            owner_refs=(f"experiment_episode_state:{episode_id}",),
            must_exist=True,
            protect_from_cleanup=True,
        )
    )

    with pytest.raises(ValueError, match="recovery-critical local run stage"):
        checkpoint_local_recovery_stages(store, data_dir)


class _TransferUploadSnapshot:
    def __init__(self, row: dict[str, object], request: object) -> None:
        self._row = row
        self._request = request

    def project_transfer_request(self, _request_id: str):
        return self._request

    def target_project_transfer_uploads(self):
        receipt_json = self._row["receipt_json"]
        receipt = (
            None
            if receipt_json is None
            else ProjectTransferUploadCompleteReceipt.model_validate_json(receipt_json)
        )
        return [
            ProjectTransferUploadRecord(
                **{
                    **{key: value for key, value in self._row.items() if key != "receipt_json"},
                    "receipt": receipt,
                },
            )
        ]


def _transfer_upload_capture_fixture(tmp_path: Path, *, status: str = "complete"):
    data_dir = tmp_path / "server" / "data"
    data_dir.mkdir(parents=True, mode=0o700)
    data_dir.chmod(0o700)
    inbox = data_dir / "transfer-inbox"
    inbox.mkdir(mode=0o700)
    inbox.chmod(0o700)
    request_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    payload = b"one exact transfer archive\n"
    digest = hashlib.sha256(payload).hexdigest()
    final = target_transfer_archive_path(data_dir, request_id)
    final.write_bytes(payload)
    final.chmod(0o600)
    timestamp = datetime.now(UTC).isoformat()
    receipt = {
        "request_id": request_id,
        "project_id": project_id,
        "archive_sha256": digest,
        "archive_size_bytes": len(payload),
        "lease_boundary_sha256": "a" * 64,
        "completed_at": timestamp,
    }
    row = {
        "request_id": request_id,
        "project_id": project_id,
        "archive_sha256": digest,
        "archive_size_bytes": len(payload),
        "lease_boundary_sha256": "a" * 64,
        "status": status,
        "receipt_json": json.dumps(receipt) if status in {"complete", "consumed"} else None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "invalidated_at": None,
    }
    request = SimpleNamespace(
        side="target",
        project_id=project_id,
        archive_sha256=digest,
        archive_size_bytes=len(payload),
    )
    destination = tmp_path / "checkpoint" / "app-data"
    destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)
    coordinator = ApplicationSnapshotPolicy(data_dir)
    return coordinator, _TransferUploadSnapshot(row, request), destination, final, payload


def test_checkpoint_captures_only_receipt_backed_complete_transfer_archive(
    tmp_path: Path,
) -> None:
    coordinator, snapshot, destination, final, payload = _transfer_upload_capture_fixture(tmp_path)

    directories, files = coordinator._copy_transfer_inbox(snapshot, destination)  # noqa: SLF001

    assert directories == {"transfer-inbox"}
    assert [item.relative_path for item in files] == [f"transfer-inbox/{final.name}"]
    copied = destination / "transfer-inbox" / final.name
    assert copied.read_bytes() == payload
    assert stat.S_IMODE(copied.stat().st_mode) == 0o400


def test_checkpoint_ignores_consumed_transfer_upload_without_an_inbox_archive(
    tmp_path: Path,
) -> None:
    coordinator, snapshot, destination, final, _payload = _transfer_upload_capture_fixture(
        tmp_path,
        status="consumed",
    )
    final.unlink()

    directories, files = coordinator._copy_transfer_inbox(snapshot, destination)  # noqa: SLF001

    assert directories == set()
    assert files == []


@pytest.mark.parametrize(
    ("status", "mutation", "message"),
    [
        ("active", lambda _path: None, "durable complete boundary"),
        ("consumed", lambda _path: None, "no typed completed-upload proof"),
        ("complete", lambda path: path.unlink(), "missing its archive file"),
        ("complete", lambda path: path.write_bytes(b"wrong"), "differs from its receipt"),
        ("complete", lambda path: path.chmod(0o644), "unsafe ownership, mode, or type"),
    ],
)
def test_checkpoint_refuses_unfinished_or_unsafe_transfer_archive(
    tmp_path: Path,
    status: str,
    mutation: Callable[[Path], None],
    message: str,
) -> None:
    coordinator, snapshot, destination, final, _payload = _transfer_upload_capture_fixture(
        tmp_path,
        status=status,
    )
    mutation(final)

    with pytest.raises(ApplicationSnapshotRefused, match=message):
        coordinator._copy_transfer_inbox(snapshot, destination)  # noqa: SLF001


def test_checkpoint_refuses_extra_transfer_partial_beside_complete_archive(
    tmp_path: Path,
) -> None:
    coordinator, snapshot, destination, final, _payload = _transfer_upload_capture_fixture(tmp_path)
    (final.parent / ".unexpected.partial").write_bytes(b"partial")
    (final.parent / ".unexpected.partial").chmod(0o600)

    with pytest.raises(ApplicationSnapshotRefused, match="unknown, partial, or untyped"):
        coordinator._copy_transfer_inbox(snapshot, destination)  # noqa: SLF001


def test_stage_checkpoints_leave_agent_symlinks_out_while_owned_trees_still_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "run-stage" / "chat-1"
    workspace = stage / "workspace" / "pytest-0"
    workspace.mkdir(parents=True)
    (stage / "patch.json").write_text("{}")
    (workspace / "test_a0").mkdir()
    (workspace / "test_a0" / "log.txt").write_text("ran")
    (workspace / "test_acurrent").symlink_to(workspace / "test_a0")
    (workspace / "python").symlink_to("/usr/bin/python3")

    with pytest.raises(ApplicationSnapshotRefused, match="contains a link"):
        _snapshot_tree(
            stage, tmp_path / "refused", relative_prefix=PurePosixPath("run-stage/chat-1")
        )

    directories, files = _snapshot_tree(
        stage,
        tmp_path / "copied",
        relative_prefix=PurePosixPath("run-stage/chat-1"),
        skip_links=True,
    )
    assert sorted(item.relative_path for item in files) == [
        "run-stage/chat-1/patch.json",
        "run-stage/chat-1/workspace/pytest-0/test_a0/log.txt",
    ]
    assert "run-stage/chat-1/workspace/pytest-0/test_a0" in directories
    assert not any("current" in entry or entry.endswith("python") for entry in directories)
    copied_links = [path for path in (tmp_path / "copied").rglob("*") if path.is_symlink()]
    assert copied_links == []

    # Skipped links still count toward the inventory bound.
    monkeypatch.setattr("rcp.server_ops.application_snapshot.BACKUP_INVENTORY_MAX_ENTRIES", 40)
    flood = tmp_path / "run-stage" / "chat-2"
    flood.mkdir(parents=True)
    (flood / "target").write_text("x")
    for index in range(40):
        (flood / f"link-{index}").symlink_to(flood / "target")
    with pytest.raises(ApplicationSnapshotRefused, match="inventory bound"):
        _snapshot_tree(
            flood,
            tmp_path / "flooded",
            relative_prefix=PurePosixPath("run-stage/chat-2"),
            skip_links=True,
        )
