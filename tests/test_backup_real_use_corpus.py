from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.api import create_app
from rcp.server_ops.backup import _write_deterministic_archive, build_archive_manifest
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.backup_project_files import BackupProjectFileCaptureCoordinator
from rcp.server_ops.restore import RestorePrepareRequest, prepare_restore
from rcp.storage import AppStore
from rcp.transfer import TransferArchiveActor, TransferArchiveAttribution
from rcp.transfer.project_files import capture_project_transfer_files
from tests.real_use_corpus import seed_real_use_corpus
from tests.test_application_deployment import use_captured_layout
from tests.test_application_restore import _confirm
from tests.test_backup_capture import _metadata


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path
    use_captured_layout(monkeypatch, root)
    state = seed_real_use_corpus(root / "sk-learn-exps", root / "projects")
    return root, state


def test_backup_and_restore_accept_real_writer_corpus(corpus):
    root, state = corpus
    data = root / "sk-learn-exps"
    store = AppStore(data / "rcp.sqlite3")
    sqlite = BackupCaptureCoordinator(store, data, _metadata(data)).capture_sqlite()
    assert sqlite.receipt.status == "complete"
    assert all(p.status == "capturable" for p in sqlite.receipt.projects)
    assert state["operation_id"] in sqlite.receipt.projects[0].task_operation_ids
    files = BackupProjectFileCaptureCoordinator(data).capture(
        sqlite.receipt_path,
        expected_sha256=sqlite.receipt_sha256,
    )
    assert files.receipt.status == "complete"
    installed = SimpleNamespace(
        installation_id=str(uuid.uuid4()),
        backup=SimpleNamespace(age_recipient="age1" + "q" * 58),
    )
    manifest = build_archive_manifest(
        installed=installed,
        sqlite_receipt=sqlite.receipt,
        project_publication=files,
    )
    archive = root / "archive.tar"
    with archive.open("wb") as stream:
        _write_deterministic_archive(stream, manifest, files.receipt_path.parent)
    archive.chmod(0o600)
    value = dict(
        version=1,
        data_dir=str(data),
        output_dir=str(root / "review"),
        plaintext_path=str(archive),
        plaintext_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        recipient_fingerprint=manifest.encryption_recipient_fingerprint,
        source_commit="a" * 40,
        preparation_id=str(uuid.uuid4()),
        detached_at=datetime.now(UTC),
        confirmed_data_dir=str(data),
        confirmed_by="test operator",
        previous_roots=(),
    )
    review = prepare_restore(RestorePrepareRequest(**value))
    result = prepare_restore(_confirm(value, review, root / "restored"))
    assert result["status"] == "prepared"
    restored_data = next(Path(r["payload"]) for r in result["roots"] if r["live"] == str(data))
    restored = AppStore(restored_data / "rcp.sqlite3")
    assert restored.agent_task(state["operation_id"]) is not None
    assert restored.read_artifact_bytes(state["artifact_id"]) == store.read_artifact_bytes(
        state["artifact_id"]
    )


def test_transfer_accepts_real_writer_corpus(corpus):
    root, state = corpus
    app = create_app(data_dir=root / "sk-learn-exps")
    service = app.state.catalog.open(state["project_id"])
    store = app.state.catalog.store
    attribution = TransferArchiveAttribution(
        archive_actor_id=str(uuid.uuid4()),
        source_actor=TransferArchiveActor.capture(state["actor"]),
    )
    records = store.export_project_transfer_records(
        state["project_id"], attributions=(attribution,)
    )
    capture = capture_project_transfer_files(service, records, root / "transfer")
    assert state["operation_id"] in {t.operation_id for t in capture.records.tasks}
    assert state["artifact_id"] in {a.artifact_id for a in capture.artifacts}


def test_legacy_project_identity_does_not_abort_other_projects(corpus):
    root, state = corpus
    data = root / "sk-learn-exps"
    store = AppStore(data / "rcp.sqlite3")
    project = store.project(state["project_id"])
    legacy = store.upsert_project(
        project.model_copy(
            update={
                "project_id": "legacy-project",
                "home_space_id": None,
                "locator": str(root / "legacy" / "manifest.toml"),
            }
        )
    )
    capture = BackupCaptureCoordinator(store, data, _metadata(data)).capture_sqlite()
    assert capture.receipt.status == "partial"
    assert {p.project_id: p.status for p in capture.receipt.projects} == {
        state["project_id"]: "capturable",
        legacy.project_id: "uncaptured",
    }
