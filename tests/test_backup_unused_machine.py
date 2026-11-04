from __future__ import annotations

import os
import pwd
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.config import load_manifest
from rcp.history import HistoryManager
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.projects import (
    complete_restored_project_publication,
    inspect_backup_project_registration,
    rebind_restored_project_registration,
    restored_project_owners,
)
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.backup_project_files import BackupProjectFileCaptureCoordinator
from rcp.setup import render_prepared_team_manifest
from rcp.storage import AppStore, ProjectProvisioningRequestRecord
from rcp.storage.provisioning import project_provisioning_review_digest
from tests.helpers import unchecked_write_authority
from tests.test_backup_capture import _initialize_git_repository, _metadata
from tests.test_backup_manifest import _completed_registration


@pytest.mark.parametrize("added_after_setup", [False, True])
def test_capture_and_restore_preserve_machine_without_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, added_after_setup: bool
) -> None:
    """Matches the shipped case: an extra local machine never resolved by checkout preparation
    because no repository is assigned to it. A machine added in Project Settings was never seen by
    the provisioning record.
    """
    data_dir = tmp_path / "data"
    store, _ = AppStore.initialize_team_space(data_dir / "rcp.sqlite3", "Backup lab")
    record, request = _completed_registration(tmp_path, unused_machine=True)
    account = pwd.getpwuid(os.geteuid()).pw_name
    store.ensure_space_machines([("", account, "Backend")])
    unused = request.machines[1].machine_id
    root = tmp_path / "projects"
    repository = root / record.project_id / "repositories" / "paper"
    commit = _initialize_git_repository(repository)
    monkeypatch.setattr(
        "rcp.storage.models.DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(service_account=account, projects_root=root),
    )
    values = request.model_dump(mode="python")
    values["target_space_id"] = store.space_id
    values["authorized_by"]["space_id"] = store.space_id
    values["machines"][0].update(
        machine_id=BACKEND_MACHINE_ID, central_root=str(root), resolved_central_root=str(root)
    )
    values["machines"][1].update(central_root=str(root))
    values["repositories"][0].update(
        machine_id=BACKEND_MACHINE_ID, intended_path=str(repository), resolved_path=str(repository)
    )
    values["repositories"][0]["git_check"].update(
        commit=commit,
        deploy_key_label=f"rcp:{store.space_id}:{record.project_id}:paper",
    )
    for check in values["provider_checks"]:
        check.update(machine_id=BACKEND_MACHINE_ID, execution_account=account)
    draft = ProjectProvisioningRequestRecord.model_validate(values)
    values["final_review_digest"] = project_provisioning_review_digest(draft)
    request = ProjectProvisioningRequestRecord.model_validate(values)
    manifest_path = repository / ".research" / "manifest.toml"
    manifest_path.parent.mkdir()
    manifest_path.write_text(render_prepared_team_manifest(request))
    manifest = load_manifest(manifest_path)
    history = HistoryManager(
        manifest, expected_space_id=store.space_id, write_authority=unchecked_write_authority()
    )
    history.claim_project_identity("created", project_id=record.project_id)
    added = store.create_space_machine(name="added", host="added.example", os_account="alice")
    if added_after_setup:
        manifest = history.add_machine(added.machine_id)
    original_head = history.head_ref()
    record = record.model_copy(
        update={
            "home_space_id": store.space_id,
            "locator": str(manifest_path),
            "state_machine_id": BACKEND_MACHINE_ID,
            "state_path": str(manifest.research_dir),
        }
    )
    store.upsert_project(record)
    with store.connection() as connection:
        store._insert_project_provisioning_request(connection, request)

    sqlite = BackupCaptureCoordinator(store, data_dir, _metadata(data_dir)).capture_sqlite()
    assert sqlite.receipt.status == "complete"
    files = BackupProjectFileCaptureCoordinator(data_dir).capture(
        sqlite.receipt_path,
        expected_sha256=sqlite.receipt_sha256,
    )
    assert files.receipt.status == "complete"
    (capture,) = files.receipt.projects
    assert capture.status == "captured"
    assert capture.recovery is not None
    expected_roots = {BACKEND_MACHINE_ID: str(root), unused: None}
    if added_after_setup:
        expected_roots[added.machine_id] = None
    assert {
        item.machine_id: item.resolved_central_root for item in capture.recovery.machines
    } == expected_roots
    assert set(capture.recovery.configuration.machines) == set(expected_roots)
    assert all(item.group == "canonical" for item in capture.files)

    restored_data = tmp_path / "restored-data"
    restored_data.mkdir()
    shutil.copyfile(sqlite.receipt_path.parent / "rcp.sqlite3", restored_data / "rcp.sqlite3")
    restored_store = AppStore(restored_data / "rcp.sqlite3")
    shutil.rmtree(manifest.research_dir)
    rebound = rebind_restored_project_registration(
        restored_store,
        capture,
        repository_paths={"paper": str(repository)},
        data_dir=restored_data,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    assert not rebound.reachable
    canonical = {
        item.source_relative_path.removeprefix(".research/"): (
            files.receipt_path.parent / item.archive_path,
            item.sha256,
            item.size_bytes,
        )
        for item in capture.files
    }
    owners = restored_project_owners(
        restored_store,
        capture,
        archived_manifest=canonical["manifest.toml"][0],
        data_dir=restored_data,
        local_home=Path.home(),
    )
    materialization = owners.history.restore_canonical_history(
        canonical,
        expected_main_head=capture.main_head,
        expected_branch_heads=capture.branch_heads,
    )
    published = complete_restored_project_publication(
        restored_store, capture, owners, materialization
    )
    assert published.reachable and published.error is None
    assert owners.history.head_ref() == original_head
    assert load_manifest(published.locator).machines == manifest.machines
    recovered = inspect_backup_project_registration(
        published,
        store=restored_store,
        data_dir=restored_data,
        provisioning_requests=restored_store.completed_project_provisioning_requests(
            record.project_id
        ),
    )
    assert recovered.recovery == capture.recovery
