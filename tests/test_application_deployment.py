from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from rcp_supervisor.checkpoint import create_stopped_snapshot, restore_checkpoint, verify_checkpoint

import rcp.storage.models as storage_models
from rcp.api import create_app
from rcp.config import load_manifest
from rcp.core.models import AuthorizedHuman, GraphBranchMetadata
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history import HistoryManager
from rcp.server_ops.application_snapshot import ApplicationSnapshotRefused
from rcp.server_ops.application_validation import _canonical_sha256
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.backup_project_files import BackupProjectFileCaptureCoordinator
from rcp.server_ops.control import ServerControlPeer, ServerControlRequest
from rcp.server_ops.deployment import (
    ApplicationProof,
    PrepareRequest,
    RollbackCopyRequest,
    ValidateRequest,
    _publish_proof,
    inventory,
    prepare,
    rollback_copy,
    validate,
)
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


def _tree_state(root: Path) -> dict:
    entries = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for path in [Path(current), *(Path(current) / name for name in directories + files)]:
            info = path.lstat()
            contents = (
                os.readlink(path)
                if stat.S_ISLNK(info.st_mode)
                else hashlib.sha256(path.read_bytes()).hexdigest()
                if stat.S_ISREG(info.st_mode)
                else None
            )
            entries[str(path.relative_to(root))] = (
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_size if stat.S_ISREG(info.st_mode) else None,
                contents,
            )
    return entries


@pytest.mark.parametrize("failure", ["prepare", "verification"])
def test_real_project_payload_restores_schema_graph_stage_and_attachment(
    captured, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    request, state, metadata = captured
    data, research = Path(request.data_dir), Path(state["research"])
    # An agent run left links in its stage: one inside the stage, one to the host.
    stage = Path(state["stage"])
    (stage / "current").symlink_to("retained.txt")
    (stage / "python").symlink_to("/usr/bin/python3")
    (stage / "pytest-0").mkdir(mode=0o700)
    (stage / "pytest-current").symlink_to("pytest-0")  # pytest's directory link
    for relative, content in {
        "providers/claude/test-account/setup-token": "synthetic-token-do-not-publish",
        "jobs/completed/receipt.json": '{"status":"completed"}',
        f"run-stage/{stage.name}/unrecognized.future-file": "unknown retained bytes",
    }.items():
        path = data / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o600)
    (data / "transfer-inbox").mkdir(mode=0o700, exist_ok=True)
    (research / "cursors.json").write_text('{"source":"retained-watermark"}')
    for name in ("facts", "paper"):
        (research / name).mkdir(mode=0o700, exist_ok=True)
        assert not list((research / name).iterdir())
    history = HistoryManager(load_manifest(research / "manifest.toml"))
    head = history.head_ref()
    branch_id = str(uuid.uuid4())
    history.create_auto_research_branch(
        GraphBranchMetadata(
            branch_id=branch_id,
            episode_id=branch_id,
            project_id=state["project_id"],
            base_head=head,
            head=GraphHeadRef(
                target=GraphTargetRef(kind="branch", branch_id=branch_id),
                revision=head.revision,
                transition_id=head.transition_id,
            ),
            authorized_by=AuthorizedHuman(
                space_id=state["space_id"], user_id=state["member_id"], display_name="Rollback test"
            ),
        )
    )
    for name in ("patches", "merges"):
        assert not list((research / "branches" / branch_id / name).iterdir())
    capture = BackupCaptureCoordinator(
        AppStore(data / "rcp.sqlite3"), data, metadata
    ).capture_sqlite()
    assert capture.receipt.status == "complete"
    request = request.model_copy(
        update={
            "sqlite_receipt_path": str(capture.receipt_path),
            "sqlite_receipt_sha256": capture.receipt_sha256,
        }
    )
    before = {root: _tree_state(root) for root in (data, research)}
    # Candidate root discovery must work before any old-code prepare and without
    # initializing even a read-only AppStore against the stopped installation.
    with monkeypatch.context() as guard:
        guard.setattr(AppStore, "__init__", lambda *args, **kwargs: pytest.fail("live AppStore"))
        discovered = inventory(
            request.model_copy(update={"output_dir": str(tmp_path / "inventory")})
        )
    assert [item["live"] for item in discovered["roots"]] == [str(data), str(research)]
    assert {root: _tree_state(root) for root in before} == before
    checkpoint = create_stopped_snapshot(
        tmp_path / "checkpoint",
        tuple(before),
        boundary_sha256="d" * 64,
    )
    prepared = None
    if failure == "prepare":

        def failed_prepare(*args, **kwargs):
            (stage / "retained.txt").write_text("partially settled preparation")
            raise MaintenanceRefused("Injected old prepare failure")

        with monkeypatch.context() as fault:
            fault.setattr(
                "rcp.server_ops.deployment._settle_accepting_artifact_replacements", failed_prepare
            )
            with pytest.raises(MaintenanceRefused):
                prepare(request)
    else:
        prepared = prepare(request)
        assert {root["live"] for root in prepared["roots"]} == set(map(str, before))
        data_payload = Path(prepared["roots"][0]["payload"])
        assert not (data_payload / "providers").exists()
        assert not (data_payload / "jobs").exists()
        # Corrupting the old selected payload cannot affect the rollback source.
        shutil.rmtree(data_payload)
    with sqlite3.connect(data / "rcp.sqlite3") as connection:
        connection.execute("CREATE TABLE candidate_only (value TEXT)")
    (Path(state["research"]) / "candidate-only").write_text("discarded candidate state")
    (Path(state["stage"]) / "retained.txt").write_text("candidate altered")
    (stage / "current").unlink()
    (stage / "python").unlink()
    (stage / "python").symlink_to("/usr/bin/python3.99")
    restore_checkpoint(checkpoint)
    verify_checkpoint(checkpoint)
    assert {root: _tree_state(root) for root in before} == before
    assert not (Path(state["research"]) / "candidate-only").exists()
    assert (Path(state["stage"]) / "retained.txt").read_text() != "candidate altered"
    assert os.readlink(stage / "current") == "retained.txt"
    assert os.readlink(stage / "python") == "/usr/bin/python3"
    assert os.readlink(stage / "pytest-current") == "pytest-0"
    if prepared is not None:
        restored_proof = rollback_copy(
            RollbackCopyRequest(
                version=1,
                proof_path=prepared["proof_path"],
                proof_sha256=prepared["proof_sha256"],
                data_dir=str(data),
                output_dir=str(tmp_path / "rollback-copy"),
            )
        )
        checked = validate(
            ValidateRequest(
                version=1,
                proof_path=restored_proof["proof_path"],
                proof_sha256=restored_proof["proof_sha256"],
                output_dir=str(tmp_path / "validated"),
            )
        )
        assert checked["status"] == "verified"
        assert {root: _tree_state(root) for root in before} == before
    restored = AppStore(data / "rcp.sqlite3")
    assert restored.authenticate_team_member_token(state["token"]).user_id == state["member_id"]
    with sqlite3.connect(data / "rcp.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='candidate_only'"
            ).fetchone()
            is None
        )
    fresh = BackupCaptureCoordinator(restored, data, metadata).capture_sqlite()
    backup = BackupProjectFileCaptureCoordinator(data).capture(
        fresh.receipt_path, expected_sha256=fresh.receipt_sha256
    )
    assert backup.receipt.status == "complete"
    assert sum(project.status == "uncaptured" for project in backup.receipt.projects) == 0


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


@pytest.mark.parametrize("registry", ["legacy", "missing_project", "changed_location"])
def test_stopped_inventory_reads_only_copied_legacy_registry(
    captured, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: str
) -> None:
    request, state, _metadata = captured
    data = Path(request.data_dir)
    # The inventory must not need any of the current AppStore tables, schema
    # ledger, or retirement column merely to discover the replacement roots.
    database = data / "rcp.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT project_id, home_space_id, locator, state_location, state_remote FROM projects"
        ).fetchone()
    database.unlink()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE projects (project_id TEXT, home_space_id TEXT, "
            "locator TEXT, state_location TEXT, state_remote INTEGER)"
        )
        if registry != "missing_project":
            connection.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?)", row)
        if registry == "changed_location":
            connection.execute("UPDATE projects SET state_location = ?", (str(tmp_path / "other"),))
    before = {root: _tree_state(root) for root in (data, Path(state["research"]))}
    monkeypatch.setattr(AppStore, "__init__", lambda *args, **kwargs: pytest.fail("AppStore init"))
    monkeypatch.setattr(
        AppStore,
        "open_read_only_snapshot",
        lambda *args, **kwargs: pytest.fail("AppStore snapshot"),
    )
    if registry == "legacy":
        discovered = inventory(request)
        assert {item["live"] for item in discovered["roots"]} == set(map(str, before))
        assert discovered["external_references"] == []
        assert [item["project_id"] for item in discovered["projects"]] == [state["project_id"]]
    else:
        with pytest.raises(ApplicationSnapshotRefused):
            inventory(request)
    assert {root: _tree_state(root) for root in before} == before


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


@pytest.mark.parametrize("failure", [None, "changed_graph", "changed_startup", "tampered_graph"])
def test_upgrade_accepts_only_authenticated_known_projection_changes(
    captured, tmp_path: Path, failure: str | None
) -> None:
    request, _state, _metadata = captured
    prepared = prepare(request)
    proof_path = Path(prepared["proof_path"])
    proof = ApplicationProof.model_validate_json(proof_path.read_bytes())
    capture = proof.project_receipt.projects[0]
    graph_path = (
        proof_path.parent
        / "baseline/overlay/projects"
        / capture.project_id
        / "repositories"
        / capture.recovery.configuration.state_repository
        / ".research/graph.json"
    )
    graph = json.loads(graph_path.read_text())
    # Reproduce the previous release's projection and authenticated proof. This
    # changes only disposable worker output, never the captured canonical input.
    graph["coverage"] = {
        "repositories_seen": ["research"],
        "repositories_never_seen": [],
        "sessions_read": ["old-session"],
        "sessions_skipped": [],
        "earliest_timestamp": None,
        # A graph past the 4 MiB request bound is still an ordinary graph.
        "note": "Historical reading report." + " " * (4 * 1024 * 1024),
    }
    # It also omitted fields later added with empty defaults.
    for edge in graph["edges"].values():
        edge.pop("expectation")
    experiments = [node for node in graph["nodes"].values() if node["type"] == "experiment"]
    for node in experiments:
        node.pop("proxies")
        node.pop("limitations")
    assert graph["edges"] and experiments
    if failure == "changed_graph":
        graph["nodes"] = {}
    graph_path.write_text(json.dumps(graph))
    projects = tuple(
        project.model_copy(update={"projection_sha256": _canonical_sha256(graph)})
        if project.project_id == capture.project_id
        else project
        for project in proof.read_model.projects
    )
    read_model = proof.read_model.model_copy(update={"projects": projects})
    if failure == "changed_startup":
        startup = read_model.startup_recovery.model_copy(
            update={
                "active_operation_ids": (*read_model.startup_recovery.active_operation_ids, "other")
            }
        )
        read_model = read_model.model_copy(update={"startup_recovery": startup})
    proof = proof.model_copy(update={"read_model": read_model})
    proof_path.unlink()
    digest = _publish_proof(proof_path, proof)
    if failure == "tampered_graph":
        graph["coverage"]["note"] = "Changed after the proof was signed."
        graph_path.write_text(json.dumps(graph))
    validation = ValidateRequest(
        version=1,
        proof_path=str(proof_path),
        proof_sha256=digest,
        output_dir=str(tmp_path / "validated"),
    )
    if failure:
        with pytest.raises(MaintenanceRefused, match="read model|projection digest changed"):
            validate(validation)
        assert not (tmp_path / "validated/application-proof.json").exists()
    else:
        checked = validate(validation)
        assert checked["status"] == "verified"
        current = ApplicationProof.model_validate_json(Path(checked["proof_path"]).read_bytes())
        graph.pop("coverage")
        for edge in graph["edges"].values():
            edge["expectation"] = None
        for node in experiments:
            node.update(proxies=[], limitations=[])
        assert current.read_model.projects[0].projection_sha256 == _canonical_sha256(graph)


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
            "inventory",
            "rollback-copy",
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
