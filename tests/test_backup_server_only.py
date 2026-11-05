from __future__ import annotations

import os
import pwd
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.config import load_manifest
from rcp.history import HistoryManager
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.projects import (
    complete_restored_project_publication,
    rebind_restored_project_registration,
    restored_project_owners,
)
from rcp.server_ops import restore
from rcp.server_ops.backup import project_capture_problems, project_capture_warnings
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.backup_project_files import BackupProjectFileCaptureCoordinator
from rcp.setup import render_prepared_team_manifest
from rcp.storage import AppStore, ProjectProvisioningRequestRecord
from rcp.storage.provisioning import project_provisioning_review_digest
from rcp.transport.remote_backup_checkout import (
    restore_server_only,
)
from tests.helpers import unchecked_write_authority
from tests.test_backup_capture import _git, _initialize_git_repository, _metadata
from tests.test_backup_manifest import _completed_registration


def test_mixed_backup_restore_backup(tmp_path, monkeypatch):
    """A real server-only project can have code the restore must not recover. The server-only alias
    goes first to exercise its coordinator step before GitHub's separately tested deploy-key stop,
    and a completed connect supersedes the restore proof for this alias only.
    """
    data = tmp_path / "data"
    store, _ = AppStore.initialize_team_space(data / "rcp.sqlite3", "Backup lab")
    record, original = _completed_registration(tmp_path)
    account = pwd.getpwuid(os.geteuid()).pw_name
    store.ensure_space_machines([("", account, "Backend")])
    root = tmp_path / "projects"
    github = root / record.project_id / "repositories" / "paper"
    server = github.with_name("notes")
    commit = _initialize_git_repository(github)
    for directory in (root, github.parent.parent, github.parent):
        directory.chmod(0o700)
    restore_server_only(os_account=account, central_root=str(root), repository_path=str(server))
    (server / "code.py").write_text("print('not archived')\n")
    _git(server, "add", "code.py")
    _git(
        server,
        "-c",
        "user.name=Member",
        "-c",
        "user.email=member@example.test",
        "commit",
        "-m",
        "member work",
    )
    old_commit = _git(server, "rev-parse", "HEAD")
    layout = SimpleNamespace(service_account=account, service_home=Path.home(), projects_root=root)
    monkeypatch.setattr("rcp.storage.models.DEFAULT_SERVER_LAYOUT", layout)
    monkeypatch.setattr("rcp.storage.provisioning.DEFAULT_SERVER_LAYOUT", layout)
    monkeypatch.setattr("rcp.server_ops.layout.DEFAULT_SERVER_LAYOUT", layout)
    values = original.model_dump()
    values["target_space_id"] = store.space_id
    values["authorized_by"]["space_id"] = store.space_id
    values["machines"][0].update(
        machine_id=BACKEND_MACHINE_ID, central_root=str(root), resolved_central_root=str(root)
    )
    values["repositories"][0].update(
        machine_id=BACKEND_MACHINE_ID, intended_path=str(github), resolved_path=str(github)
    )
    values["repositories"][0]["git_check"].update(
        commit=commit, deploy_key_label=f"rcp:{store.space_id}:{record.project_id}:paper"
    )
    for check in values["provider_checks"]:
        check.update(machine_id=BACKEND_MACHINE_ID, execution_account=account)

    def reviewed(payload):
        draft = ProjectProvisioningRequestRecord.model_validate(payload)
        return draft.model_copy(
            update={"final_review_digest": project_provisioning_review_digest(draft)}
        )

    creation = reviewed(values)
    added = creation.model_dump()
    added.update(
        request_id=str(uuid.uuid4()), kind="add_repository", target_project_id=record.project_id
    )
    added["repositories"] = [
        dict(
            alias="notes",
            repository=None,
            machine_id=BACKEND_MACHINE_ID,
            intended_path=str(server),
            resolved_path=str(server),
            checkout_disposition="request_created",
            git_check=dict(status="ready", commit=old_commit, checked_at=creation.completed_at),
        )
    ]
    added.update(
        state_repository="notes", project_truth_scope=["notes"], default_run_truth_scope=["notes"]
    )
    addition = reviewed(added)
    manifest_path = server / ".research" / "manifest.toml"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        render_prepared_team_manifest(
            creation.model_copy(
                update={
                    "repositories": (*creation.repositories, *addition.repositories),
                    "state_repository": "notes",
                    "project_truth_scope": ["paper", "notes"],
                }
            )
        )
    )
    manifest = load_manifest(manifest_path)
    history = HistoryManager(
        manifest, expected_space_id=store.space_id, write_authority=unchecked_write_authority()
    )
    history.claim_project_identity("created", project_id=record.project_id)
    record = record.model_copy(
        update=dict(
            home_space_id=store.space_id,
            locator=str(manifest_path),
            state_machine_id=BACKEND_MACHINE_ID,
            state_path=str(manifest.research_dir),
        )
    )
    store.upsert_project(record)
    with store.connection() as connection:
        for request in (creation, addition):
            store._insert_project_provisioning_request(connection, request)

    def backup(owner, directory):
        sqlite = BackupCaptureCoordinator(owner, directory, _metadata(directory)).capture_sqlite()
        files = BackupProjectFileCaptureCoordinator(directory).capture(
            sqlite.receipt_path, expected_sha256=sqlite.receipt_sha256
        )
        assert files.receipt.status == "complete"
        return sqlite, files, files.receipt.projects[0]

    sqlite, files, capture = backup(store, data)
    assert {r.alias for r in capture.recovery.repositories} == {"paper", "notes"}
    assert project_capture_problems((capture,)) == ()
    assert len(project_capture_warnings((capture,))) == 1
    assert all(e.source_relative_path.startswith(".research/") for e in capture.files)
    restored = tmp_path / "restored"
    restored.mkdir()
    shutil.copyfile(sqlite.receipt_path.parent / "rcp.sqlite3", restored / "rcp.sqlite3")
    restored_store = AppStore(restored / "rcp.sqlite3")
    shutil.rmtree(manifest.research_dir)
    shutil.rmtree(server)
    machine = restore._recovery_machine(
        capture.recovery.machines[0], {card.machine_id: card for card in store.space_machines()}
    )
    from rcp.server_ops.project_checkout import ProjectCheckoutManager

    checkouts = ProjectCheckoutManager(layout)
    repository = capture.recovery.repositories[1]
    server_first = capture.model_copy(
        update={
            "recovery": capture.recovery.model_copy(
                update={"repositories": tuple(reversed(capture.recovery.repositories))}
            )
        }
    )
    progress = restore._recover_repositories(
        SimpleNamespace(progress=()),
        SimpleNamespace(projects=(server_first,), space_id=store.space_id),
        None,
        (),
        {card.machine_id: card for card in store.space_machines()},
    )
    assert progress["status"] == "continue"
    saved = restore.RestoreRepositoryRecovery.model_validate(progress["progress"][0])
    new_commit = saved.checkout_commit
    assert restore._recover_server_only(checkouts, machine, repository, saved, {}) == new_commit
    from rcp.server_ops.project_checkout import ProjectCheckoutRefused

    retained = server / ".research"
    retained.mkdir(mode=0o700)
    (retained / "manifest.toml").write_text("conflicting research")
    with pytest.raises(ProjectCheckoutRefused) as refused:
        restore._recover_server_only(checkouts, machine, repository, saved, {})
    assert refused.value.kind == "retained_research"
    shutil.rmtree(retained)
    assert new_commit != old_commit
    assert _git(server, "ls-tree", "HEAD") == ""
    assert _git(server, "branch", "--show-current") == "main"
    assert _git(server, "show", "-s", "--format=%an <%ae>") == "RCP <rcp@rcp.invalid>"
    assert not (server / "code.py").exists()
    rebind_restored_project_registration(
        restored_store,
        capture,
        repository_paths={"paper": str(github), "notes": str(server)},
        data_dir=restored,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    canonical = {
        e.source_relative_path.removeprefix(".research/"): (
            files.receipt_path.parent / e.archive_path,
            e.sha256,
            e.size_bytes,
        )
        for e in capture.files
    }
    owners = restored_project_owners(
        restored_store,
        capture,
        archived_manifest=canonical["manifest.toml"][0],
        data_dir=restored,
        local_home=Path.home(),
    )
    materialization = owners.history.restore_canonical_history(
        canonical,
        expected_main_head=capture.main_head,
        expected_branch_heads=capture.branch_heads,
    )
    complete_restored_project_publication(restored_store, capture, owners, materialization)
    restore._record_restored_checkout_proofs(restored_store, restored, capture, (saved,))
    _, _, recaptured = backup(restored_store, restored)
    assert recaptured.main_head == capture.main_head
    assert recaptured.recovery.repositories[1].git_commit == new_commit
    assert restored_store.completed_project_provisioning_requests(
        record.project_id
    ) == store.completed_project_provisioning_requests(record.project_id)
    from datetime import datetime, timedelta

    connected = addition.model_dump()
    connected.update(
        request_id=str(uuid.uuid4()),
        kind="connect_repository",
        completed_at=(
            datetime.fromisoformat(addition.completed_at) + timedelta(seconds=1)
        ).isoformat(),
    )
    from rcp.server_ops.github import parse_github_repository_ref

    source = parse_github_repository_ref("git@github.com:example/notes.git")
    connected["repositories"][0]["repository"] = source.model_dump()
    connected["repositories"][0]["git_check"].update(
        commit=new_commit,
        write_verified=True,
        deploy_key_label=f"rcp:{store.space_id}:{record.project_id}:notes",
        public_key_fingerprint="SHA256:" + "B" * 43,
    )
    with restored_store.connection() as connection:
        restored_store._insert_project_provisioning_request(connection, reviewed(connected))
    _git(server, "remote", "add", "origin", source.ssh_clone_url)
    _, _, connected_capture = backup(restored_store, restored)
    assert connected_capture.recovery.repositories[1].repository == source
    assert project_capture_warnings((connected_capture,)) == ()


def test_adopted_folder_capture_ignores_its_remote_but_not_its_commit(tmp_path):
    """The folder has an origin RCP did not set. Archives written before adoption existed keep their
    exact bytes.
    """
    from rcp.server_ops.backup_models import BackupRecoveryRepository
    from rcp.transport.remote_backup_checkout import main

    account = pwd.getpwuid(os.geteuid()).pw_name
    folder = tmp_path / "anywhere" / "notes"
    commit = _initialize_git_repository(folder)
    arguments = ["helper", "inspect", account, str(folder)]
    assert main([*arguments, commit]) == 0
    assert main([*arguments, "0" * 40]) == 3
    repository = BackupRecoveryRepository(
        alias="notes", repository=None, machine_id=BACKEND_MACHINE_ID, resolved_path=str(folder),
        git_commit=commit, deploy_key_label=None, public_key_fingerprint=None,
    )  # fmt: skip
    assert "adopted" not in repository.model_dump()
