from __future__ import annotations

import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from rcp_supervisor.checkpoint import SnapshotRoot, create_checkpoint, restore_checkpoint

import rcp.storage.models as storage_models
from rcp.api import create_app
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.control import ServerControlPeer, ServerControlRequest
from rcp.server_ops.deployment import PrepareRequest, ValidateRequest, prepare, validate
from rcp.server_ops.maintenance import MaintenanceIdentity, MaintenanceRefused
from rcp.server_runtime import ServerMetadata
from rcp.storage import AppStore
from tests.supervisor_reboot_build import MIGRATION_TABLE, add_forward_migration
from tests.supervisor_reboot_data import prepare_data


@pytest.fixture
def socket_root():
    with tempfile.TemporaryDirectory(prefix="rcp-maint-", dir="/tmp") as directory:
        os.chown(directory, os.geteuid(), os.getegid())
        yield Path(directory)


@pytest.fixture
def captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_root: Path):
    account = pwd.getpwuid(os.geteuid()).pw_name
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(service_account=account, projects_root=tmp_path / "projects"),
    )
    state = prepare_data(tmp_path / "data", tmp_path / "projects", account=account)
    data = tmp_path / "data"
    (data / "run-stage").chmod(0o700)
    metadata = ServerMetadata.create(
        data,
        host="127.0.0.1",
        port=8421,
        owner_kind="cli",
        control_socket=socket_root / "control.sock",
        running_commit="a" * 40,
        web_build_id="sha256:" + "b" * 64,
    )
    capture = BackupCaptureCoordinator(
        AppStore(data / "rcp.sqlite3"), data, metadata
    ).capture_sqlite()
    assert capture.receipt.status == "complete"
    request = PrepareRequest(
        version=1,
        data_dir=str(data),
        output_dir=str(tmp_path / "prepared"),
        sqlite_receipt_path=str(capture.receipt_path),
        sqlite_receipt_sha256=capture.receipt_sha256,
    )
    return request, state, metadata


def test_real_project_payload_restores_schema_graph_stage_and_attachment(
    captured, tmp_path: Path
) -> None:
    request, state, _metadata = captured
    # The canonical graph file names remain owned by the app; compare its whole
    # prepared .research tree and the retained payload after exact replacement.
    prepared = prepare(request)
    assert {root["live"] for root in prepared["roots"]} == {request.data_dir, state["research"]}
    data_payload = Path(prepared["roots"][0]["payload"])
    assert (data_payload / "run-stage" / Path(state["stage"]).name / "retained.txt").read_text()
    assert any((data_payload / "chat-attachments").rglob("notes.txt")) or any(
        (data_payload / "chat-attachments").rglob("files/*")
    )
    assert not (data_payload / "rcp.lock").exists()
    checkpoint = create_checkpoint(
        tmp_path / "checkpoint",
        tuple(
            SnapshotRoot(Path(root["live"]), Path(root["payload"])) for root in prepared["roots"]
        ),
        boundary_sha256=prepared["boundary_sha256"],
    )
    data = Path(request.data_dir)
    with sqlite3.connect(data / "rcp.sqlite3") as connection:
        connection.execute("CREATE TABLE candidate_only (value TEXT)")
    (Path(state["research"]) / "candidate-only").write_text("discarded candidate state")
    (Path(state["stage"]) / "retained.txt").write_text("candidate altered")
    restore_checkpoint(checkpoint)
    assert not (Path(state["research"]) / "candidate-only").exists()
    assert (Path(state["stage"]) / "retained.txt").read_text() != "candidate altered"
    restored = AppStore(data / "rcp.sqlite3")
    assert restored.authenticate_team_member_token(state["token"]).user_id == state["member_id"]
    with sqlite3.connect(data / "rcp.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='candidate_only'"
            ).fetchone()
            is None
        )
    checked = validate(
        ValidateRequest(
            version=1,
            proof_path=prepared["proof_path"],
            proof_sha256=prepared["proof_sha256"],
            output_dir=str(tmp_path / "validated"),
        )
    )
    assert checked["status"] == "verified"


def test_candidate_worker_crosses_real_forward_migration_without_touching_live(
    captured, tmp_path: Path
) -> None:
    request, state, _metadata = captured
    prepared = prepare(request)
    workspace = Path(__file__).resolve().parents[1]
    candidate = tmp_path / "candidate-code"
    shutil.copytree(
        workspace / "src" / "rcp",
        candidate / "rcp",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    head = add_forward_migration(candidate / "rcp" / "storage" / "base.py")
    worker_request = {
        "version": 1,
        "proof_path": prepared["proof_path"],
        "proof_sha256": prepared["proof_sha256"],
        "output_dir": str(tmp_path / "candidate-check"),
    }
    outcome = subprocess.run(
        [sys.executable, "-m", "rcp.server_ops.deployment", "validate", "-"],
        input=json.dumps(worker_request),
        env={**os.environ, "PYTHONPATH": str(candidate)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert outcome.returncode == 0, outcome.stderr
    result = json.loads(outcome.stdout)
    assert result["status"] == "verified"
    migrated = tmp_path / "candidate-check" / "candidate" / "overlay" / "data" / "rcp.sqlite3"
    with sqlite3.connect(migrated) as connection:
        assert (
            connection.execute(
                "SELECT MAX(migration_version) FROM storage_schema_migrations"
            ).fetchone()[0]
            == head
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (MIGRATION_TABLE,)
        ).fetchone()
    assert (
        AppStore(Path(request.data_dir) / "rcp.sqlite3").storage_schema_ledger_head()
        == state["ledger_head"]
    )


def test_changed_proof_and_existing_output_fail_closed(captured, tmp_path: Path) -> None:
    request, _state, _metadata = captured
    prepared = prepare(request)
    proof = Path(prepared["proof_path"])
    proof.write_bytes(proof.read_bytes() + b" ")
    with pytest.raises(MaintenanceRefused, match="digest changed"):
        validate(
            ValidateRequest(
                version=1,
                proof_path=str(proof),
                proof_sha256=prepared["proof_sha256"],
                output_dir=str(tmp_path / "should-not-exist"),
            )
        )
    assert not (tmp_path / "should-not-exist").exists()


def test_explicit_probe_stays_fenced_until_matching_app_proof(captured, tmp_path: Path) -> None:
    request, _state, metadata = captured
    prepared = prepare(request)
    boundary = MaintenanceIdentity(str(uuid.uuid4()), "c" * 64)
    app = create_app(
        data_dir=Path(request.data_dir), instance_metadata=metadata, maintenance_identity=boundary
    )
    control = app.state.server_control
    assert control is not None

    def command(operation: str, **kwargs):
        return control.handler(
            ServerControlRequest(
                request_id=str(uuid.uuid4()),
                instance_id=metadata.instance_id,
                operation=operation,
                selector_id=boundary.maintenance_id,
                boundary_sha256=boundary.boundary_sha256,
                **kwargs,
            ),
            ServerControlPeer(pid=os.getpid(), uid=0, gid=0),
        )

    # Running the lifespan opens its disposable local socket but leaves every
    # ordinary runtime owner asleep until the verified fence release.
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 503
        assert command("maintenance_status").quiescent
        assert not app.state.startup_effect_runtime_started
        with pytest.raises(RuntimeError, match="verification"):
            command("maintenance_release")
        verified = command(
            "maintenance_verify",
            proof_path=prepared["proof_path"],
            proof_sha256=prepared["proof_sha256"],
        )
        assert len(verified.verification_sha256) == 64
        assert client.get("/api/health").status_code == 503
        released = command("maintenance_release")
        assert not released.closed
        assert client.get("/api/health").status_code == 200
        assert app.state.startup_effect_runtime_started


def test_capabilities_never_opens_data(tmp_path: Path) -> None:
    data = tmp_path / "must-not-exist"
    completed = subprocess.run(
        [sys.executable, "-m", "rcp.server_ops.deployment", "capabilities"],
        env={**os.environ, "RCP_DATA_DIR": str(data)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "version": 1,
        "maintenance_protocol": 10,
        "commands": [
            "prepare",
            "validate",
            "inspect",
            "offline-prepare",
            "restore-prepare",
            "offline-protect",
        ],
    }
    assert not data.exists()


@pytest.mark.parametrize(
    "mutation", ["unknown_data", "missing_local_project", "symlink_bootstrap", "existing_output"]
)
def test_prepare_refuses_incomplete_or_unsafe_local_boundary(
    captured, tmp_path: Path, mutation: str
) -> None:
    request, state, _metadata = captured
    data = Path(request.data_dir)
    if mutation == "unknown_data":
        (data / "unknown-owner").write_text("preserve")
    elif mutation == "missing_local_project":
        research = Path(state["research"])
        research.rename(research.with_name("held-research"))
    elif mutation == "symlink_bootstrap":
        (data / "bootstrap-manifests").symlink_to(tmp_path, target_is_directory=True)
    else:
        output = Path(request.output_dir)
        output.mkdir()
        (output / "sentinel").write_text("preserve")
    with pytest.raises((RuntimeError, ValueError, OSError)):
        prepare(request)
    assert not (Path(request.output_dir) / "application-proof.json").exists()
    if mutation == "existing_output":
        assert (Path(request.output_dir) / "sentinel").read_text() == "preserve"


def test_trusted_deployed_commit_is_bound_to_wheel_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rcp.server_runtime import ServerMetadataError, capture_installed_release_identity

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("packaged app")
    monkeypatch.setattr("rcp.__version__", "0.3.4+build.123.gabcdef0")
    monkeypatch.setattr("rcp.web_assets.web_dist_path", lambda: bundle)
    monkeypatch.setenv("RCP_DEPLOYED_COMMIT", "abcdef0" + "1" * 33)
    identity = capture_installed_release_identity()
    assert identity.commit == "abcdef0" + "1" * 33
    assert identity.web_build_id.startswith("sha256:")
    monkeypatch.setenv("RCP_DEPLOYED_COMMIT", "fedcba0" + "1" * 33)
    with pytest.raises(ServerMetadataError, match="does not match this wheel"):
        capture_installed_release_identity()


def test_running_service_closes_drains_captures_and_releases_maintenance(
    captured, monkeypatch
) -> None:
    request, _state, metadata = captured
    app = create_app(data_dir=Path(request.data_dir), instance_metadata=metadata)
    control = app.state.server_control
    boundary = MaintenanceIdentity(str(uuid.uuid4()), "c" * 64)

    def command(operation: str, identity=boundary):
        return control.handler(
            ServerControlRequest(
                request_id=str(uuid.uuid4()),
                instance_id=metadata.instance_id,
                operation=operation,
                selector_id=identity.maintenance_id,
                boundary_sha256=identity.boundary_sha256,
            ),
            ServerControlPeer(pid=os.getpid(), uid=0, gid=0),
        )

    with TestClient(app) as client:
        initial = command("maintenance_status")
        assert not initial.closed and initial.maintenance_id is None
        assert initial.boundary_sha256 is None
        gate = app.state.runtime_admission_gate
        close = gate.close_and_wait

        def close_then_timeout(**kwargs):
            close(**kwargs)
            raise MaintenanceRefused("Timed out after closing HTTP admission.")

        with monkeypatch.context() as patch:
            patch.setattr(gate, "close_and_wait", close_then_timeout)
            with pytest.raises(RuntimeError, match="Timed out"):
                command("maintenance_enter")
        partial = command("maintenance_status")
        assert partial.closed and not partial.quiescent
        assert not app.state.background_admission_gate.closed
        assert client.get("/api/health").status_code == 503
        assert not command("maintenance_release").closed
        assert client.get("/api/health").status_code == 200
        entered = command("maintenance_enter")
        assert entered.closed and entered.quiescent
        assert entered.capture.status == "complete"
        assert Path(entered.capture.receipt_path).is_file()
        assert command("maintenance_enter").capture == entered.capture
        assert client.get("/api/health").status_code == 503
        with pytest.raises(RuntimeError, match="another maintenance boundary"):
            command("maintenance_release", MaintenanceIdentity(str(uuid.uuid4()), "c" * 64))
        assert not command("maintenance_release").closed
        assert client.get("/api/health").status_code == 200
        next_boundary = MaintenanceIdentity(str(uuid.uuid4()), "d" * 64)
        assert (
            command("maintenance_enter", next_boundary).maintenance_id
            == next_boundary.maintenance_id
        )
        command("maintenance_release", next_boundary)


def test_offline_preparation_keeps_live_database_unchanged(captured, tmp_path):
    from rcp.server_ops.deployment import (
        InspectRequest,
        OfflinePrepareRequest,
        inspect,
        offline_prepare,
    )

    request, state, _ = captured
    data = Path(request.data_dir)
    original = (data / "rcp.sqlite3").read_bytes()
    assert inspect(InspectRequest(version=1, data_dir=str(data)))["status"] == "initialized_team"
    result = offline_prepare(
        OfflinePrepareRequest(
            version=1,
            data_dir=str(data),
            output_dir=str(tmp_path / "offline"),
            source_commit="a" * 40,
        )
    )
    assert (data / "rcp.sqlite3").read_bytes() == original
    assert Path(result["migrated_data_root"]).is_dir()
    assert {root["live"] for root in result["roots"]} == {str(data), state["research"]}


def test_offline_protect_uses_complete_typed_capture_and_existing_archive_publisher(
    captured, tmp_path, monkeypatch
):
    from rcp.server_ops.backup import read_backup_archive_receipt
    from rcp.server_ops.deployment import (
        OfflinePrepareRequest,
        OfflineProtectRequest,
        offline_prepare,
        offline_protect,
    )
    from tests.test_backup_encryption import AGE_RECIPIENT, _fake_age

    request, _, _ = captured
    prepared = offline_prepare(
        OfflinePrepareRequest(
            version=1,
            data_dir=request.data_dir,
            output_dir=str(tmp_path / "offline"),
            source_commit="a" * 40,
        )
    )
    fake = _fake_age(tmp_path)
    (tmp_path / "age").symlink_to(fake)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    installation = str(uuid.uuid4())
    output = tmp_path / "protected"
    result = offline_protect(
        OfflineProtectRequest(
            version=1,
            proof_path=prepared["proof_path"],
            proof_sha256=prepared["proof_sha256"],
            output_dir=str(output),
            recipient=AGE_RECIPIENT,
            installation_id=installation,
        )
    )
    assert result["status"] == "protected" and result["uncaptured_projects"] == 0
    receipt = read_backup_archive_receipt(
        Path(result["receipt_path"]),
        expected_destination=output,
        expected_installation_id=installation,
        expected_uid=os.geteuid(),
        verify_digest=True,
    )
    assert receipt.capture_status == "complete"
    assert receipt.protected_project_count == 1


@pytest.mark.parametrize("empty_sqlite", [False, True])
def test_inspect_uninitialized_does_not_create_schema(tmp_path, empty_sqlite):
    from rcp.server_ops.deployment import InspectRequest, inspect

    data = tmp_path / "fresh-data"
    data.mkdir(mode=0o700)
    if empty_sqlite:
        (data / "rcp.sqlite3").write_bytes(b"")
        (data / "rcp.sqlite3").chmod(0o600)
    before = {p.name: p.read_bytes() for p in data.iterdir()}
    assert inspect(InspectRequest(version=1, data_dir=str(data))) == {
        "version": 1,
        "status": "uninitialized",
    }
    assert {p.name: p.read_bytes() for p in data.iterdir()} == before
    (data / "unowned").write_text("do not change")
    with pytest.raises(MaintenanceRefused):
        inspect(InspectRequest(version=1, data_dir=str(data)))
