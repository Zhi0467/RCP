from __future__ import annotations

import hashlib
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from rcp_supervisor.checkpoint import SnapshotRoot, create_checkpoint, restore_checkpoint

from rcp.api import create_app
from rcp.server_ops.backup import _write_deterministic_archive, build_archive_manifest
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.backup_models import BackupArchiveManifest
from rcp.server_ops.backup_project_files import BackupProjectFileCapturePublication
from rcp.server_ops.deployment import (
    ApplicationProof,
    ValidateRequest,
    prepare,
    validate,
    verify_live_application,
)
from rcp.server_ops.maintenance import MaintenanceIdentity
from rcp.server_ops.restore import RestorePrepareRequest, RestoreRefused, prepare_restore
from rcp.storage import AppStore
from rcp.storage.digest import append_digest_event
from tests.legacy_artifacts import insert_legacy_view
from tests.test_application_deployment import captured, socket_root  # noqa: F401


def _archive_manifest(value):
    with tarfile.open(value["plaintext_path"]) as archive:
        return BackupArchiveManifest.model_validate_json(
            archive.extractfile("manifest.json").read()
        )


def _write_archive(value, manifest, root):
    path = Path(value["plaintext_path"])
    with path.open("wb") as stream:
        _write_deterministic_archive(stream, manifest, root)
    value["plaintext_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def restore_request(captured, tmp_path):  # noqa: F811 - imported shared fixture
    request, state, metadata = captured
    store = AppStore(Path(request.data_dir) / "rcp.sqlite3")
    project_id = store.projects()[0].project_id
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        seq = append_digest_event(
            connection,
            project_id=project_id,
            kind="job_ended",
            item_id="restored-job",
            created_at=store.now(),
            payload={"title": "Finished job", "status": "exited"},
        )
        user_id = store.project_members(project_id)[0].user_id
        connection.execute(
            "INSERT INTO digest_marks VALUES(?,?,?,?)", (project_id, user_id, seq, store.now())
        )
        connection.execute(
            "INSERT INTO digest_heads VALUES(?,?,?,?)", (project_id, "main", 1, "restored-head")
        )
    capture = BackupCaptureCoordinator(store, Path(request.data_dir), metadata).capture_sqlite()
    store.close()
    request = request.model_copy(
        update={
            "sqlite_receipt_path": str(capture.receipt_path),
            "sqlite_receipt_sha256": capture.receipt_sha256,
        }
    )
    previous = prepare(request)
    proof = ApplicationProof.model_validate_json(Path(previous["proof_path"]).read_bytes())
    recipient = "age1" + "q" * 58
    publication = BackupProjectFileCapturePublication(
        receipt=proof.project_receipt,
        receipt_path=Path(proof.capture_root) / "project-files.json",
        receipt_sha256=proof.project_receipt_sha256,
    )
    installed = SimpleNamespace(
        installation_id=str(uuid.uuid4()), backup=SimpleNamespace(age_recipient=recipient)
    )
    manifest = build_archive_manifest(
        installed=installed, sqlite_receipt=proof.sqlite_receipt, project_publication=publication
    )
    archive = tmp_path / "archive.tar"
    with archive.open("wb") as stream:
        _write_deterministic_archive(stream, manifest, Path(proof.capture_root))
    archive.chmod(0o600)
    value = dict(
        version=1,
        data_dir=request.data_dir,
        output_dir=str(tmp_path / "restore-review"),
        plaintext_path=str(archive),
        plaintext_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        recipient_fingerprint=manifest.encryption_recipient_fingerprint,
        source_commit="a" * 40,
        preparation_id=str(uuid.uuid4()),
        detached_at=datetime.now(UTC),
        confirmed_data_dir=request.data_dir,
        confirmed_by="test operator",
        previous_roots=tuple(previous["roots"]),
    )
    return value, state, previous


def test_restore_reviews_then_prepares_exact_candidate_without_changing_live(
    restore_request, tmp_path
):
    value, state, previous = restore_request
    data = Path(value["data_dir"])
    original = (data / "rcp.sqlite3").read_bytes()
    research = Path(state["research"])
    before = {p.relative_to(research): p.read_bytes() for p in research.rglob("*") if p.is_file()}
    artifacts = research.parent / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "unrelated").symlink_to("missing-agent-output")
    views = research.parent / "views"
    assert not views.exists()
    review = prepare_restore(RestorePrepareRequest(**value))
    assert review["status"] == "operator_action_needed"
    fields = {item["name"]: item["value"] for item in review["fields"]}
    assert len(fields["old_authority_boundary"]) == 64
    assert len(fields["member_roster_boundary"]) == 64
    value.update(
        output_dir=str(tmp_path / "restore-ready"),
        old_authority_disposition="old-machine-fenced-and-credentials-revoked",
        confirm_old_authority=fields["old_authority_boundary"],
        confirm_member_roster=fields["member_roster_boundary"],
    )
    result = prepare_restore(RestorePrepareRequest(**value))
    assert result["status"] == "prepared"
    restored_data = next(
        Path(root["payload"]) for root in result["roots"] if root["live"] == str(data)
    )
    with AppStore.open_read_only_snapshot(restored_data / "rcp.sqlite3").connection() as connection:
        assert (
            connection.execute("SELECT item_id FROM digest_events").fetchone()[0] == "restored-job"
        )
        assert connection.execute("SELECT seq FROM digest_marks").fetchone()[0] == 1
        assert (
            connection.execute("SELECT patch_id FROM digest_heads").fetchone()[0] == "restored-head"
        )
    assert (data / "rcp.sqlite3").read_bytes() == original
    assert {
        p.relative_to(research): p.read_bytes() for p in research.rglob("*") if p.is_file()
    } == before
    assert {r["live"] for r in result["roots"]} >= {str(data), str(research)}
    assert not (Path(result["roots"][0]["payload"]) / "run-stage").exists()
    assert Path(result["proof_path"]).is_file()
    assert not views.exists()
    assert {root["live"] for root in result["preserve_roots"]} >= {str(artifacts)}
    assert {tuple(root) for root in result["extra_previous_roots"]} == {("live",)}
    assert (artifacts / "unrelated").is_symlink()


def test_restore_rejects_changed_archive_before_any_candidate_publication(restore_request):
    value, _, _ = restore_request
    Path(value["plaintext_path"]).write_bytes(b"changed")
    with pytest.raises(RestoreRefused):
        prepare_restore(RestorePrepareRequest(**value))
    assert not Path(value["output_dir"]).exists()


def _confirm(value, review, output):
    fields = {item["name"]: item["value"] for item in review["fields"]}
    return RestorePrepareRequest(
        **{
            **value,
            "output_dir": str(output),
            "old_authority_disposition": "old-machine-fenced-and-credentials-revoked",
            "confirm_old_authority": fields["old_authority_boundary"],
            "confirm_member_roster": fields["member_roster_boundary"],
        }
    )


@pytest.mark.parametrize("verification", ["candidate", "live"])
def test_restore_preserves_uncaptured_project_as_visible_unavailable(
    restore_request, tmp_path, verification
):

    from rcp.server_ops.backup_models import BackupProjectCapture
    from rcp.storage import AppStore

    value, state, previous = restore_request
    proof = ApplicationProof.model_validate_json(Path(previous["proof_path"]).read_bytes())
    manifest = _archive_manifest(value)
    captured_project = manifest.projects[0]
    uncaptured = BackupProjectCapture(
        project_id=captured_project.project_id,
        home_space_id=captured_project.home_space_id,
        locator=captured_project.locator,
        status="uncaptured",
        unavailable_kind="capture_failure",
        unavailable_reason="Remote owner was unavailable",
        unavailable_at=manifest.captured_at,
        total_bytes=0,
    )
    manifest = manifest.model_copy(
        update={
            "projects": (uncaptured,),
            "imported_sources": (),
            "status": "partial",
            "total_bytes": manifest.sqlite_snapshot.size_bytes,
        }
    )
    _write_archive(value, manifest, Path(proof.capture_root))
    review = prepare_restore(RestorePrepareRequest(**value))
    result = prepare_restore(_confirm(value, review, tmp_path / "partial-ready"))
    candidate = AppStore(Path(result["roots"][0]["payload"]) / "rcp.sqlite3")
    record = candidate.project(state["project_id"])
    assert record.reachable is False
    proof = ApplicationProof.model_validate_json(Path(result["proof_path"]).read_bytes())
    assert proof.read_model.projects[0].status == "not_replay_verified"

    if verification == "candidate":
        checked = validate(
            ValidateRequest(
                version=1,
                proof_path=result["proof_path"],
                proof_sha256=result["proof_sha256"],
                output_dir=str(tmp_path / "different-candidate-directory"),
            )
        )
        assert checked["status"] == "verified"
        result.update(checked)
    checkpoint = create_checkpoint(
        tmp_path / "replacement",
        tuple(SnapshotRoot(Path(r["live"]), Path(r["payload"])) for r in result["roots"]),
        boundary_sha256=result["boundary_sha256"],
    )
    restore_checkpoint(checkpoint)
    app = create_app(
        data_dir=Path(value["data_dir"]),
        maintenance_identity=MaintenanceIdentity(str(uuid.uuid4()), result["boundary_sha256"]),
    )
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 503
        digest = verify_live_application(
            Path(result["proof_path"]),
            proof_sha256=result["proof_sha256"],
            background=app.state.background_tasks,
            catalog=app.state.catalog,
            store=AppStore(Path(value["data_dir"]) / "rcp.sqlite3"),
        )
        assert len(digest) == 64


def test_restore_refuses_wrong_recorded_transition(restore_request, tmp_path):

    value, _, previous = restore_request
    proof = ApplicationProof.model_validate_json(Path(previous["proof_path"]).read_bytes())
    manifest = _archive_manifest(value)
    project = manifest.projects[0]
    project = project.model_copy(
        update={"main_head": project.main_head.model_copy(update={"transition_id": "e" * 64})}
    )
    manifest = manifest.model_copy(update={"projects": (project,)})
    _write_archive(value, manifest, Path(proof.capture_root))
    review = prepare_restore(RestorePrepareRequest(**value))
    with pytest.raises(ValueError):
        prepare_restore(_confirm(value, review, tmp_path / "wrong-head"))


def test_fresh_checkout_pauses_and_resumes_same_key_after_durable_progress(
    restore_request, monkeypatch
):
    import os
    import pwd

    from rcp.server_ops import git_credentials, layout, project_checkout, restore
    from rcp.server_ops.models import ExternalAction
    from rcp.storage import AppStore

    value, _, previous = restore_request
    manifest = _archive_manifest(value)
    capture = manifest.projects[0]
    repo = capture.recovery.repositories[0]
    machine = capture.recovery.machines[0]
    monkeypatch.setattr(
        layout,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(
            service_account=pwd.getpwuid(os.geteuid()).pw_name,
            projects_root=Path(machine.resolved_central_root),
        ),
    )
    granted = False
    material = SimpleNamespace(
        label="fresh restore key",
        public_key="ssh-ed25519 synthetic-test-public",
        public_key_fingerprint="SHA256:" + "B" * 43,
    )

    def probe(*args, **kwargs):
        return SimpleNamespace(
            ready=granted,
            commit=repo.git_commit if granted else None,
            diagnostic="Grant this fresh public key.",
            status="ready" if granted else "github_grant_needed",
        )

    credentials = SimpleNamespace(
        preflight_recovery_key=lambda *a, **k: None,
        prepare_recovery_key=lambda *a, **k: material,
        probe_write=probe,
    )
    monkeypatch.setattr(git_credentials, "GitCredentialManager", lambda *a: credentials)
    monkeypatch.setattr(
        git_credentials,
        "restore_deploy_key_operator_step",
        lambda *a, **k: SimpleNamespace(
            actions=(ExternalAction(instruction="Grant this fresh key."),), fields=()
        ),
    )

    def checkout(*args, **kwargs):
        return SimpleNamespace(
            repository_path=repo.resolved_path,
            central_root=machine.resolved_central_root,
            commit=repo.git_commit,
            checkout_disposition="request_created",
        )

    monkeypatch.setattr(
        project_checkout,
        "ProjectCheckoutManager",
        lambda *a: SimpleNamespace(prepare_recovery=checkout),
    )
    store = AppStore(Path(value["data_dir"]) / "rcp.sqlite3")
    members = tuple(
        restore.RestoreMemberRosterEntry(
            member_id=m.member_id, display_name=m.display_name, active_token_ids=m.active_token_ids
        )
        for m in store.active_team_member_authority()
    )
    request = RestorePrepareRequest(**value)
    first = restore._recover_repositories(request, manifest, None, members)
    assert first["status"] == "continue"
    assert first["progress"][0]["state"] == "key_started"

    def resumed(result):
        return request.model_copy(
            update={
                "progress": tuple(
                    restore.RestoreRepositoryRecovery.model_validate_json(
                        __import__("json").dumps(p)
                    )
                    for p in result["progress"]
                )
            }
        )

    second = restore._recover_repositories(resumed(first), manifest, None, members)
    assert second["status"] == "continue" and second["progress"][0]["state"] == "key_ready"
    pause = restore._recover_repositories(resumed(second), manifest, None, members)
    assert pause["status"] == "operator_action_needed"
    granted = True
    completed = restore._recover_repositories(resumed(pause), manifest, None, members)
    assert completed["status"] == "continue"
    assert completed["progress"][0]["state"] == "checkout_ready"
    assert restore._recover_repositories(resumed(completed), manifest, None, members) is None


@pytest.mark.parametrize(
    "damage", ["extra_member", "symlink", "changed_bytes", "unknown_manifest_field"]
)
def test_tampered_archive_structure_never_changes_live_state(restore_request, tmp_path, damage):
    import io
    import json

    value, state, _ = restore_request
    database = Path(value["data_dir"]) / "rcp.sqlite3"
    original = database.read_bytes()
    with tarfile.open(value["plaintext_path"], "r:") as archive:
        members = [(member, archive.extractfile(member).read()) for member in archive.getmembers()]
    altered = tmp_path / "altered.tar"
    with tarfile.open(altered, "w:") as archive:
        for index, (member, payload) in enumerate(members):
            if damage == "unknown_manifest_field" and index == 0:
                manifest = json.loads(payload)
                manifest["unknown_authority"] = True
                payload = (
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode()
                member.size = len(payload)
            if damage == "changed_bytes" and index == 1:
                payload = bytes([payload[0] ^ 1]) + payload[1:]
            if damage == "symlink" and index == 1:
                member.type = tarfile.SYMTYPE
                member.linkname = str(database)
                member.size = 0
                payload = b""
            archive.addfile(member, io.BytesIO(payload))
        if damage == "extra_member":
            extra = tarfile.TarInfo("unowned")
            extra.mode = 0o400
            extra.size = 4
            archive.addfile(extra, io.BytesIO(b"data"))
    altered.chmod(0o600)
    value.update(
        plaintext_path=str(altered),
        plaintext_sha256=hashlib.sha256(altered.read_bytes()).hexdigest(),
    )
    with pytest.raises(RestoreRefused):
        prepare_restore(RestorePrepareRequest(**value))
    assert database.read_bytes() == original
    assert Path(state["research"]).is_dir()


@pytest.mark.parametrize("omit_inventory", [False, True])
def test_restore_relocates_every_artifact_version(restore_request, tmp_path, omit_inventory):
    import json

    from rcp.storage import AppStore, Artifact

    value, _, previous = restore_request
    proof = ApplicationProof.model_validate_json(Path(previous["proof_path"]).read_bytes())
    root = Path(proof.capture_root)
    database = root / "rcp.sqlite3"
    database.chmod(0o600)
    store = AppStore(database)
    artifact = store.create_artifact(
        Artifact(
            artifact_id="restore-versions",
            project_id=store.projects()[0].project_id,
            supplier="turn",
            supplier_id=str(uuid.uuid4()),
            source_name="page.html",
            media_type="text/html",
            created_at=store.now(),
        ),
        data=b"original",
    )
    edited = store.publish_artifact_version(
        artifact.artifact_id,
        base_version=artifact.current_version,
        operation_id=str(uuid.uuid4()),
        data=b"edited",
    )
    manifest = _archive_manifest(value)
    inventory = tuple(store.artifact_inventory())
    store.close()
    snapshot = manifest.sqlite_snapshot.model_copy(
        update={
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "size_bytes": database.stat().st_size,
        }
    )
    manifest = manifest.model_copy(
        update={
            "sqlite_snapshot": snapshot,
            "artifact_inventory": inventory,
            "total_bytes": manifest.total_bytes
            - manifest.sqlite_snapshot.size_bytes
            + snapshot.size_bytes
            + sum(i.size_bytes for i in inventory),
        }
    )
    if omit_inventory:
        old_payload = manifest.model_dump(mode="json")
        old_payload.pop("artifact_inventory")
        old_payload["total_bytes"] -= sum(item.size_bytes for item in inventory)
        manifest = BackupArchiveManifest.model_validate_json(json.dumps(old_payload))
    _write_archive(value, manifest, root)
    if omit_inventory:
        with pytest.raises(RestoreRefused):
            prepare_restore(RestorePrepareRequest(**value))
        return
    review = prepare_restore(RestorePrepareRequest(**value))
    result = prepare_restore(_confirm(value, review, tmp_path / "artifact-restore-ready"))
    assert result["status"] == "prepared"
    candidate = AppStore(Path(result["roots"][0]["payload"]) / "rcp.sqlite3")
    assert (
        candidate.read_artifact_bytes(artifact.artifact_id, artifact.current_version) == b"original"
    )
    assert candidate.read_artifact_bytes(artifact.artifact_id, edited.version_id) == b"edited"


def test_restore_legacy_migrated_artifact_can_be_archived_again(
    restore_request, tmp_path, monkeypatch
):
    import gzip
    import json
    import sqlite3

    import rcp.server_ops.restore as restore_module
    from rcp.server_ops.backup_integrity import database_schema_sha256

    value, _, previous = restore_request
    proof = ApplicationProof.model_validate_json(Path(previous["proof_path"]).read_bytes())
    root = Path(proof.capture_root)
    database = root / "rcp.sqlite3"
    database.chmod(0o600)
    legacy = tmp_path / "legacy.sqlite3"
    legacy.write_bytes(
        gzip.decompress(
            Path(
                "tests/fixtures/server_upgrade/pre-artifacts-v15-ff090e1/data/rcp.sqlite3.gz"
            ).read_bytes()
        )
    )
    with sqlite3.connect(legacy) as connection:
        view_schema = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'result_views'"
        ).fetchone()[0]
    with sqlite3.connect(database) as connection:
        project_id = connection.execute("SELECT project_id FROM projects LIMIT 1").fetchone()[0]
        for table in ("artifacts", "artifact_versions", "artifact_operations", "artifact_imports"):
            connection.execute(f"DROP TABLE {table}")
        for table in (
            "digest_events",
            "digest_marks",
            "digest_heads",
            "consolidation_schedules",
            "consolidation_runs",
            "consolidation_apply_receipts",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("ALTER TABLE episode_reports DROP COLUMN artifact_id")
        connection.execute("ALTER TABLE episode_reports DROP COLUMN artifact_version_id")
        connection.execute("ALTER TABLE episode_reports ADD COLUMN html TEXT NOT NULL DEFAULT ''")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version >= 30")
        connection.execute(view_schema)
        insert_legacy_view(connection, project_id)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        schema = database_schema_sha256(connection)
    monkeypatch.setattr(
        restore_module,
        "SUPPORTED_RESTORE_DATABASE_SCHEMAS",
        {*restore_module.SUPPORTED_RESTORE_DATABASE_SCHEMAS, schema},
    )
    with tarfile.open(value["plaintext_path"]) as archive:
        raw = json.loads(archive.extractfile("manifest.json").read())
    raw.pop("artifact_inventory", None)
    old_size = raw["sqlite_snapshot"]["size_bytes"]
    raw["sqlite_snapshot"].update(
        sha256=hashlib.sha256(database.read_bytes()).hexdigest(), size_bytes=database.stat().st_size
    )
    raw["database_schema_sha256"] = schema
    raw["total_bytes"] += database.stat().st_size - old_size
    manifest = BackupArchiveManifest.model_validate_json(json.dumps(raw))
    _write_archive(value, manifest, root)
    review = prepare_restore(RestorePrepareRequest(**value))
    result = prepare_restore(_confirm(value, review, tmp_path / "legacy-ready"))
    restored = ApplicationProof.model_validate_json(Path(result["proof_path"]).read_bytes())
    publication = BackupProjectFileCapturePublication(
        receipt=restored.project_receipt,
        receipt_path=Path(restored.capture_root) / "project-files.json",
        receipt_sha256=restored.project_receipt_sha256,
    )
    installed = SimpleNamespace(
        installation_id=str(uuid.uuid4()), backup=SimpleNamespace(age_recipient="age1" + "q" * 58)
    )
    archived = build_archive_manifest(
        installed=installed, sqlite_receipt=restored.sqlite_receipt, project_publication=publication
    )
    output = tmp_path / "restored.tar"
    with output.open("wb") as stream:
        _write_deterministic_archive(stream, archived, Path(restored.capture_root))
    assert restored.sqlite_receipt.artifact_inventory
    with tarfile.open(output) as archive:
        for entry in restored.sqlite_receipt.artifact_inventory:
            assert (
                archive.extractfile(f"artifacts/{entry.artifact_id}/{entry.file_id}").read()
                == b"page"
            )
