from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.server_ops.backup import _write_deterministic_archive, build_archive_manifest
from rcp.server_ops.backup_project_files import BackupProjectFileCapturePublication
from rcp.server_ops.deployment import ApplicationProof, prepare
from rcp.server_ops.restore import RestorePrepareRequest, RestoreRefused, prepare_restore
from tests.test_application_deployment import captured, socket_root  # noqa: F401


@pytest.fixture
def restore_request(captured, tmp_path):  # noqa: F811 - imported shared fixture
    request, state, _ = captured
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
    assert (data / "rcp.sqlite3").read_bytes() == original
    assert {
        p.relative_to(research): p.read_bytes() for p in research.rglob("*") if p.is_file()
    } == before
    assert {r["live"] for r in result["roots"]} >= {str(data), str(research)}
    assert not (Path(result["roots"][0]["payload"]) / "run-stage").exists()
    assert Path(result["proof_path"]).is_file()


def test_restore_rejects_changed_archive_before_any_candidate_publication(restore_request):
    value, _, _ = restore_request
    Path(value["plaintext_path"]).write_bytes(b"changed")
    with pytest.raises(RestoreRefused, match="digest changed"):
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


def test_restore_preserves_uncaptured_project_as_visible_unavailable(restore_request, tmp_path):
    import tarfile

    from rcp.server_ops.backup_models import BackupArchiveManifest, BackupProjectCapture
    from rcp.storage import AppStore

    value, state, previous = restore_request
    proof = ApplicationProof.model_validate_json(Path(previous["proof_path"]).read_bytes())
    with tarfile.open(value["plaintext_path"]) as archive:
        manifest = BackupArchiveManifest.model_validate_json(
            archive.extractfile("manifest.json").read()
        )
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
    with Path(value["plaintext_path"]).open("wb") as stream:
        _write_deterministic_archive(stream, manifest, Path(proof.capture_root))
    value["plaintext_sha256"] = hashlib.sha256(
        Path(value["plaintext_path"]).read_bytes()
    ).hexdigest()
    review = prepare_restore(RestorePrepareRequest(**value))
    result = prepare_restore(_confirm(value, review, tmp_path / "partial-ready"))
    candidate = AppStore(Path(result["roots"][0]["payload"]) / "rcp.sqlite3")
    record = candidate.project(state["project_id"])
    assert record.reachable is False
    assert record.error.startswith("Not captured by the replacement archive:")
    proof = ApplicationProof.model_validate_json(Path(result["proof_path"]).read_bytes())
    assert proof.read_model.projects[0].status == "not_replay_verified"


def test_restore_refuses_wrong_recorded_transition(restore_request, tmp_path):
    import tarfile

    from rcp.server_ops.backup_models import BackupArchiveManifest

    value, _, previous = restore_request
    proof = ApplicationProof.model_validate_json(Path(previous["proof_path"]).read_bytes())
    with tarfile.open(value["plaintext_path"]) as archive:
        manifest = BackupArchiveManifest.model_validate_json(
            archive.extractfile("manifest.json").read()
        )
    project = manifest.projects[0]
    project = project.model_copy(
        update={"main_head": project.main_head.model_copy(update={"transition_id": "e" * 64})}
    )
    manifest = manifest.model_copy(update={"projects": (project,)})
    with Path(value["plaintext_path"]).open("wb") as stream:
        _write_deterministic_archive(stream, manifest, Path(proof.capture_root))
    value["plaintext_sha256"] = hashlib.sha256(
        Path(value["plaintext_path"]).read_bytes()
    ).hexdigest()
    review = prepare_restore(RestorePrepareRequest(**value))
    with pytest.raises(ValueError, match="captured head"):
        prepare_restore(_confirm(value, review, tmp_path / "wrong-head"))


def test_fresh_checkout_pauses_and_resumes_same_key_after_durable_progress(
    restore_request, monkeypatch
):
    import os
    import pwd
    import tarfile

    from rcp.server_ops import git_credentials, layout, project_checkout, restore
    from rcp.server_ops.backup_models import BackupArchiveManifest
    from rcp.server_ops.models import ExternalAction
    from rcp.storage import AppStore

    value, _, previous = restore_request
    with tarfile.open(value["plaintext_path"]) as archive:
        manifest = BackupArchiveManifest.model_validate_json(
            archive.extractfile("manifest.json").read()
        )
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
    calls = []
    granted = False
    material = SimpleNamespace(
        label="fresh restore key",
        public_key="ssh-ed25519 synthetic-test-public",
        public_key_fingerprint="SHA256:" + "B" * 43,
    )

    def probe(*args, **kwargs):
        calls.append("probe")
        return SimpleNamespace(
            ready=granted,
            commit=repo.git_commit if granted else None,
            diagnostic="Grant this fresh public key.",
            status="ready" if granted else "github_grant_needed",
        )

    credentials = SimpleNamespace(
        preflight_recovery_key=lambda *a, **k: calls.append("preflight"),
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
        calls.append("checkout")
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
    assert calls == ["preflight"]

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
    assert calls.count("preflight") == 1
    assert calls.count("probe") == 2
    assert calls.count("checkout") == 2


@pytest.mark.parametrize(
    "damage", ["extra_member", "symlink", "changed_bytes", "unknown_manifest_field"]
)
def test_tampered_archive_structure_never_changes_live_state(restore_request, tmp_path, damage):
    import io
    import json
    import tarfile

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
