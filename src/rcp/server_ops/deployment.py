"""Bounded application preparation and verification commands for the supervisor.

This worker runs as the application account. It knows application state owners;
it never selects a release, writes a deployment journal, or opens live admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from rcp.core.models import upgrade_graph_projection
from rcp.limits import PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES
from rcp.server_ops._local_primitives import canonical_json_line, fsync_file_tree
from rcp.server_ops.application_snapshot import (
    _copy_declared_file,
    _project_restore_location,
    _set_private_directory_modes,
    copy_project_roots,
    copy_proof_tree,
)
from rcp.server_ops.application_validation import (
    CandidateProjectVerification,
    CandidateRehearsalResult,
    StartupRecoveryReadModel,
    _canonical_sha256,
    _write_private_json,
    build_rehearsal_overlay,
    run_candidate_child,
    unavailable_card_projection_sha256,
)
from rcp.server_ops.backup_capture import (
    BackupSQLiteCaptureReceipt,
    read_backup_sqlite_capture_receipt,
    validate_backup_sqlite_snapshot,
)
from rcp.server_ops.backup_models import inspect_app_data_capture_plan
from rcp.server_ops.backup_project_files import (
    BackupProjectFileCaptureCoordinator,
    BackupProjectFileCaptureReceipt,
)
from rcp.server_ops.maintenance import MaintenanceRefused
from rcp.storage import AppStore

_MAX_REQUEST_BYTES = 4 * 1024 * 1024


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PreparedRoot(_Model):
    live: str
    payload: str


class PrepareRequest(_Model):
    version: Literal[1]
    data_dir: str
    output_dir: str
    sqlite_receipt_path: str
    sqlite_receipt_sha256: str

    @field_validator("data_dir", "output_dir", "sqlite_receipt_path")
    @classmethod
    def path(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("sqlite_receipt_sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        return _digest(value)


class ValidateRequest(_Model):
    version: Literal[1]
    proof_path: str
    proof_sha256: str
    output_dir: str

    @field_validator("proof_path", "output_dir")
    @classmethod
    def path(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("proof_sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        return _digest(value)


class ApplicationProof(_Model):
    version: Literal[1] = 1
    boundary_sha256: str
    capture_root: str
    sqlite_receipt: BackupSQLiteCaptureReceipt
    sqlite_receipt_sha256: str
    project_receipt: BackupProjectFileCaptureReceipt
    project_receipt_sha256: str
    read_model: CandidateRehearsalResult


def _absolute(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or str(path) != value or ".." in path.parts:
        raise ValueError("worker paths must be normalized absolute non-root paths")
    return value


def _digest(value: str) -> str:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("worker digest must be lowercase SHA-256")
    return value


def _private_ancestors(path: Path) -> None:
    # Root-owned ancestors are permitted, but no symlink or writable foreign
    # ancestor may redirect application-owned request/payload paths.
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if not stat.S_ISDIR(info.st_mode) or candidate.is_symlink():
            raise MaintenanceRefused(
                "Application worker path traverses a non-directory or symlink."
            )
        if info.st_uid not in {0, os.geteuid()}:
            raise MaintenanceRefused("Application worker path traverses a foreign owner.")
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise MaintenanceRefused(
                "Application worker path traverses an unsafe writable directory."
            )


def _new_output(path: Path) -> None:
    missing = []
    current = path.parent
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    _private_ancestors(current)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    path.mkdir(mode=0o700)


def _read(
    path: Path, expected_sha256: str | None = None, *, max_bytes: int = _MAX_REQUEST_BYTES
) -> bytes:
    _private_ancestors(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or before.st_size > max_bytes
        ):
            raise MaintenanceRefused("Application proof has unsafe type, ownership, mode, or size.")
        chunks = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)

        def signature(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_mode,
                info.st_uid,
                info.st_gid,
                info.st_nlink,
            )

        if signature(before) != signature(after) or len(payload) != before.st_size:
            raise MaintenanceRefused("Application proof changed while reading.")
    finally:
        os.close(fd)
    if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != _digest(
        expected_sha256
    ):
        raise MaintenanceRefused("Application proof digest changed.")
    return payload


def _read_proof(path: Path, digest: str) -> ApplicationProof:
    proof = ApplicationProof.model_validate_json(_read(path, digest))
    _digest(proof.boundary_sha256)
    _absolute(proof.capture_root)
    if proof.read_model.status != "verified":
        raise MaintenanceRefused("Application proof was not verified.")
    return proof


def _publish_proof(path: Path, proof: ApplicationProof) -> str:
    _write_private_json(path, proof)
    return hashlib.sha256(_read(path)).hexdigest()


def _check_copy(
    output: Path,
    sqlite_receipt: BackupSQLiteCaptureReceipt,
    sqlite_digest: str,
    projects: BackupProjectFileCaptureReceipt,
    project_digest: str,
    capture: Path,
) -> CandidateRehearsalResult:
    output.mkdir(mode=0o700)
    overlay = build_rehearsal_overlay(
        output,
        sqlite_receipt=sqlite_receipt.model_copy(
            update={"snapshot_path": str(capture / "rcp.sqlite3")}
        ),
        sqlite_receipt_sha256=sqlite_digest,
        project_receipt=projects,
        project_receipt_sha256=project_digest,
        capture_root=capture,
    )
    overlay_path = output / "overlay.json"
    result_path = output / "read-model.json"
    _write_private_json(overlay_path, overlay)
    if run_candidate_child(overlay_path, result_path) != 0:
        result = CandidateRehearsalResult.model_validate_json(_read(result_path))
        raise MaintenanceRefused(result.diagnostic or "Application copied-state validation failed.")
    return CandidateRehearsalResult.model_validate_json(_read(result_path))


def prepare(request: PrepareRequest, *, offline: bool = False) -> dict[str, object]:
    from rcp.__main__ import instance_lock

    data_dir, output = Path(request.data_dir), Path(request.output_dir)
    _private_ancestors(data_dir)
    if output == data_dir or output.is_relative_to(data_dir) or data_dir.is_relative_to(output):
        raise MaintenanceRefused("Prepared storage overlaps application state.")
    if os.path.lexists(output):
        raise MaintenanceRefused("Prepared output already exists.")
    with instance_lock(data_dir, timeout=0.0):
        receipt_path = Path(request.sqlite_receipt_path)
        sqlite = read_backup_sqlite_capture_receipt(
            receipt_path, expected_sha256=request.sqlite_receipt_sha256
        )
        validate_backup_sqlite_snapshot(sqlite)
        if Path(sqlite.app_data_plan.data_dir) != data_dir:
            raise MaintenanceRefused("Application capture belongs to a different data directory.")
        coordinator = BackupProjectFileCaptureCoordinator(data_dir)
        capture_files = coordinator.capture_offline if offline else coordinator.capture
        publication = capture_files(receipt_path, expected_sha256=request.sqlite_receipt_sha256)
        projects = publication.receipt
        # Every local root must be captured; only explicitly remote state is excluded.
        for project in projects.projects:
            if project.status == "uncaptured":
                inventory = next(
                    item for item in sqlite.projects if item.project_id == project.project_id
                )
                with sqlite3.connect(
                    f"file:{sqlite.snapshot_path}?mode=ro", uri=True
                ) as connection:
                    row = connection.execute(
                        "SELECT state_remote FROM projects WHERE project_id=?",
                        (inventory.project_id,),
                    ).fetchone()
                if row is None or not bool(row[0]):
                    raise MaintenanceRefused("A local project root could not be captured.")
        _new_output(output)
        (output / "payload").mkdir(mode=0o700)
        capture = output / "capture"
        copy_proof_tree(receipt_path.parent, capture)
        app = output / "payload" / "app-data"
        app.mkdir(mode=0o700)
        # The supervisor owns the complete stopped copy. This payload contains
        # only the database and typed project material needed for application proof.
        _copy_declared_file(
            Path(sqlite.snapshot_path),
            app / "rcp.sqlite3",
            relative_path="rcp.sqlite3",
            expected_sha256=sqlite.sqlite_snapshot.sha256,
            expected_size=sqlite.sqlite_snapshot.size_bytes,
            restore_mode=0o600,
        )
        (app / "rcp.sqlite3").chmod(0o600)
        project_roots = copy_project_roots(output, projects, capture_root=capture)
        roots = [PreparedRoot(live=str(data_dir), payload=str(app))]
        for root in project_roots:
            payload = output / root.archive_path
            for item in root.files:
                payload.joinpath(*PurePosixPath(item.relative_path).parts).chmod(item.mode)
            roots.append(PreparedRoot(live=root.live_path, payload=str(payload)))
        _set_private_directory_modes(output)
        boundary = _canonical_sha256(
            {
                "sqlite_receipt_sha256": request.sqlite_receipt_sha256,
                "project_receipt_sha256": publication.receipt_sha256,
                "roots": [item.model_dump() for item in roots],
            }
        )
        baseline = _check_copy(
            output / "baseline",
            sqlite,
            request.sqlite_receipt_sha256,
            projects,
            publication.receipt_sha256,
            capture,
        )
        proof = ApplicationProof(
            boundary_sha256=boundary,
            capture_root=str(capture),
            sqlite_receipt=sqlite,
            sqlite_receipt_sha256=request.sqlite_receipt_sha256,
            project_receipt=projects,
            project_receipt_sha256=publication.receipt_sha256,
            read_model=baseline,
        )
        proof_path = output / "application-proof.json"
        proof_digest = _publish_proof(proof_path, proof)
        fsync_file_tree(output)
        return {
            "version": 1,
            "boundary_sha256": boundary,
            "roots": [item.model_dump() for item in roots],
            "proof_path": str(proof_path),
            "proof_sha256": proof_digest,
        }


def inventory(request: PrepareRequest) -> dict[str, object]:
    """Reuse backup's captured registration discovery before old preparation runs."""
    data = Path(request.data_dir)
    receipt = read_backup_sqlite_capture_receipt(
        Path(request.sqlite_receipt_path), expected_sha256=request.sqlite_receipt_sha256
    )
    if Path(receipt.app_data_plan.data_dir) != data:
        raise MaintenanceRefused("Inventory capture belongs to different application data.")
    roots = [{"live": str(data), "project_id": None}]
    for project in receipt.projects:
        if project.status != "capturable" or project.recovery is None:
            raise MaintenanceRefused("Inventory capture has an unresolved project.")
        _, live = _project_restore_location(project)
        # A remote project's state lives on another machine: an update never
        # replaces it, so only local roots are checkpointed.
        if live is not None:
            roots.append({"live": str(live), "project_id": project.project_id})
    for root in roots:
        _private_ancestors(Path(root["live"]))
    return {"version": 1, "roots": roots}


def validate(request: ValidateRequest) -> dict[str, object]:
    proof = _read_proof(Path(request.proof_path), request.proof_sha256)
    output = Path(request.output_dir)
    _new_output(output)
    observed = _check_copy(
        output / "candidate",
        proof.sqlite_receipt,
        proof.sqlite_receipt_sha256,
        proof.project_receipt,
        proof.project_receipt_sha256,
        Path(proof.capture_root),
    )
    expected = _upgrade_previous_projection(Path(request.proof_path), proof, observed)
    if observed != expected:
        raise MaintenanceRefused(
            "Candidate changed the captured graph or startup recovery read model."
        )
    result = proof.model_copy(update={"read_model": observed})
    proof_path = output / "application-proof.json"
    digest = _publish_proof(proof_path, result)
    fsync_file_tree(output)
    return {
        "version": 1,
        "status": "verified",
        "boundary_sha256": proof.boundary_sha256,
        "proof_path": str(proof_path),
        "proof_sha256": digest,
        "verification_sha256": _read_model_digest(observed),
    }


def _upgrade_previous_projection(
    proof_path: Path, proof: ApplicationProof, observed: CandidateRehearsalResult
) -> CandidateRehearsalResult:
    """Decode the shipped graph digest across known projection upgrades.

    Version-1 preparation retains its replayed graphs beside the proof. Bind each
    old graph to that proof's digest before applying only the listed upgrades: the
    retired coverage report is removed, and fields added with empty defaults are
    filled where absent. All remaining graph content and the rest of the read
    model still compare exactly.
    """
    candidates = {item.project_id: item for item in observed.projects}
    captures = {item.project_id: item for item in proof.project_receipt.projects}
    projects = []
    for expected in proof.read_model.projects:
        candidate = candidates.get(expected.project_id)
        if (
            expected.status == "verified"
            and candidate is not None
            and candidate.projection_sha256 != expected.projection_sha256
        ):
            capture = captures[expected.project_id]
            if capture.recovery is None:
                raise MaintenanceRefused("The previous graph has no captured repository identity.")
            graph_path = (
                proof_path.parent
                / "baseline/overlay/projects"
                / expected.project_id
                / "repositories"
                / capture.recovery.configuration.state_repository
                / ".research/graph.json"
            )
            graph = json.loads(_read(graph_path, max_bytes=PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES))
            if _canonical_sha256(graph) != expected.projection_sha256:
                raise MaintenanceRefused("The previous graph projection digest changed.")
            if isinstance(graph, dict):
                upgrade_graph_projection(graph)
                expected = expected.model_copy(
                    update={"projection_sha256": _canonical_sha256(graph)}
                )
        projects.append(expected)
    return proof.read_model.model_copy(update={"projects": tuple(projects)})


def _read_model_digest(model: CandidateRehearsalResult) -> str:
    return _canonical_sha256(
        {
            "startup_recovery": model.startup_recovery.model_dump(mode="json"),
            "projects": [item.model_dump(mode="json") for item in model.projects],
            "reads": list(model.reads),
        }
    )


def verify_live_application(
    proof_path: Path, *, proof_sha256: str, background, catalog, store
) -> str:
    proof = _read_proof(proof_path, proof_sha256)
    final = proof.read_model
    captures = {project.project_id: project for project in proof.project_receipt.projects}
    startup = StartupRecoveryReadModel.model_validate(background.plan_startup_recovery().as_dict())
    if startup != final.startup_recovery:
        raise MaintenanceRefused("The switched release changed the startup recovery read model.")
    cards = {str(card["id"]): card for card in catalog.cards()}
    if set(cards) != {project.project_id for project in final.projects}:
        raise MaintenanceRefused(
            "The switched release omitted or substituted a registered project."
        )
    projects: list[CandidateProjectVerification] = []
    for expected in final.projects:
        card = cards[expected.project_id]
        if expected.status == "not_replay_verified":
            observed = CandidateProjectVerification(
                project_id=expected.project_id,
                status="not_replay_verified",
                revision=None,
                projection_sha256=unavailable_card_projection_sha256(
                    card,
                    expected.projection_sha256,
                    team_space=store.space_kind == "team",
                ),
            )
        else:
            status, snapshot = catalog.cached_snapshot_status(expected.project_id)
            graph = snapshot.get("graph") if status == "valid" and snapshot is not None else None
            # A cache left behind by a failed refresh is readable but older than
            # canonical history; rebuild it rather than refuse the release.
            if isinstance(graph, dict) and graph.get("revision") != expected.revision:
                graph = None
            # Remote means what the checkpoint used: the captured classification,
            # never a row the candidate's own migration could have changed.
            capture = captures[expected.project_id]
            remote = _project_restore_location(capture)[1] is None
            record = store.project(expected.project_id)
            if record is None or bool(record.state_remote) != remote:
                raise MaintenanceRefused(
                    f"The switched release changed where project {expected.project_id} "
                    "keeps its state."
                )
            if not isinstance(graph, dict) and remote:
                # Two stages: before the release commits, nothing may write to a
                # remote project, which no local rollback can undo. Validation already
                # replayed its captured history on copies; its served projection is
                # rebuilt on first open once the release is chosen.
                observed = expected
            else:
                if not isinstance(graph, dict):
                    try:
                        _service, rebuilt = catalog.open_snapshot(expected.project_id)
                        graph = rebuilt["graph"]
                    except (FileNotFoundError, KeyError, OSError, RuntimeError, ValueError) as exc:
                        raise MaintenanceRefused(
                            "The switched release could not reconstruct project projection "
                            f"{expected.project_id}."
                        ) from exc
                if not isinstance(graph, dict):
                    raise MaintenanceRefused(
                        "The switched release has no valid project projection for "
                        f"{expected.project_id}."
                    )
                revision = graph.get("revision")
                observed = CandidateProjectVerification(
                    project_id=expected.project_id,
                    status="verified",
                    revision=revision if isinstance(revision, int) else None,
                    projection_sha256=_canonical_sha256(graph),
                )
        if observed != expected:
            raise MaintenanceRefused(
                f"The switched release changed project projection {expected.project_id}."
            )
        # These are the same storage-backed reads as the two operational API routes.
        store.agent_tasks(expected.project_id)
        store.watchers(expected.project_id)
        projects.append(observed)
    read_model = {
        "startup_recovery": startup.model_dump(mode="json"),
        "projects": [item.model_dump(mode="json") for item in projects],
        "reads": list(final.reads),
    }
    return _canonical_sha256(read_model)


class InspectRequest(_Model):
    version: Literal[1]
    data_dir: str

    @field_validator("data_dir")
    @classmethod
    def path(cls, value: str) -> str:
        return _absolute(value)


class OfflinePrepareRequest(InspectRequest):
    output_dir: str
    source_commit: str

    @field_validator("output_dir")
    @classmethod
    def output(cls, value: str) -> str:
        return _absolute(value)

    @field_validator("source_commit")
    @classmethod
    def commit(cls, value: str) -> str:
        if len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("offline source identity requires a full commit")
        return value


def inspect(request: InspectRequest) -> dict[str, object]:
    data = Path(request.data_dir)
    if not os.path.lexists(data):
        _private_ancestors(data.parent)
        return {"version": 1, "status": "uninitialized"}
    _private_ancestors(data)
    database = data / "rcp.sqlite3"
    if not os.path.lexists(database):
        return {"version": 1, "status": "uninitialized"}
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise MaintenanceRefused("Application database has unsafe metadata.")
    with sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True) as connection:
        schema = connection.execute(
            "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    if not schema:
        return {"version": 1, "status": "uninitialized"}
    store = AppStore.open_read_only_snapshot(database)
    if store.space_kind != "team" or not store.space_name:
        raise MaintenanceRefused("Existing database is not an initialized team.")
    return {"version": 1, "status": "initialized_team", "space_id": store.space_id}


def _copy_offline_database(data: Path, database: Path) -> None:
    """Read stopped SQLite bytes without opening or creating files beside the live DB."""
    import shutil
    import tempfile

    source = data / "rcp.sqlite3"
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise MaintenanceRefused("Offline database has unsafe metadata.")
    with tempfile.TemporaryDirectory(dir=database.parent) as directory:
        detached = Path(directory) / "rcp.sqlite3"
        shutil.copyfile(source, detached)
        wal = data / "rcp.sqlite3-wal"
        if wal.exists():
            shutil.copyfile(wal, detached.with_name("rcp.sqlite3-wal"))
        with sqlite3.connect(detached) as origin, sqlite3.connect(database) as destination:
            origin.backup(destination)
    database.chmod(0o600)


def offline_inventory(request: OfflinePrepareRequest) -> dict[str, object]:
    """Discover stopped legacy roots using only a migrated, disposable database."""
    from datetime import UTC, datetime

    from rcp.server_ops.backup_capture import inspect_snapshot_project_inventory

    data, output = Path(request.data_dir), Path(request.output_dir)
    _private_ancestors(data)
    if output == data or output.is_relative_to(data) or data.is_relative_to(output):
        raise MaintenanceRefused("Offline inventory overlaps live data.")
    _new_output(output)
    database = output / "rcp.sqlite3"
    _copy_offline_database(data, database)
    store = AppStore(database)
    if store.space_kind != "team" or not store.space_name:
        raise MaintenanceRefused("Offline snapshot is not an initialized team.")
    roots = [{"live": str(data)}]
    for record in sorted(store.projects(), key=lambda item: item.project_id):
        project = inspect_snapshot_project_inventory(
            store, record, data_dir=data, captured_at=datetime.now(UTC)
        )
        if project.status != "capturable" or project.recovery is None:
            raise MaintenanceRefused("Inventory capture has an unresolved project.")
        _, live = _project_restore_location(project)
        if live is not None:
            roots.append({"live": str(live)})
    return {"version": 1, "roots": roots}


def offline_prepare(request: OfflinePrepareRequest) -> dict[str, object]:
    """Inventory a stopped legacy installation, migrating only a private SQLite copy."""
    import shutil
    import uuid
    from datetime import UTC, datetime

    from rcp.__main__ import instance_lock
    from rcp.server_ops.backup_capture import (
        inspect_snapshot_project_inventory,
        write_immutable_backup_receipt,
    )
    from rcp.server_ops.backup_integrity import database_schema_sha256
    from rcp.server_ops.backup_models import BackupFileEntry, BackupImportedProviderSourceInventory
    from rcp.sources.imported import ImportedProviderSourceStore

    data, output = Path(request.data_dir), Path(request.output_dir)
    _private_ancestors(data)
    if output == data or output.is_relative_to(data) or data.is_relative_to(output):
        raise MaintenanceRefused("Offline preparation overlaps live data.")
    _new_output(output)
    capture_id = str(uuid.uuid4())
    capture = output / f"backup-{capture_id}"
    capture.mkdir(mode=0o700)
    database = capture / "rcp.sqlite3"
    with instance_lock(data, timeout=0):
        _copy_offline_database(data, database)
        original = output / "original.sqlite3"
        shutil.copyfile(database, original)
        original.chmod(0o600)
        store = AppStore(database)
        if store.space_kind != "team" or not store.space_name:
            raise MaintenanceRefused("Offline snapshot is not an initialized team.")
        captured_at = datetime.now(UTC)
        projects = tuple(
            inspect_snapshot_project_inventory(
                store, record, data_dir=data, captured_at=captured_at
            )
            for record in sorted(store.projects(), key=lambda item: item.project_id)
        )
        plan = inspect_app_data_capture_plan(data)
        imported = tuple(
            BackupImportedProviderSourceInventory.model_validate(
                ImportedProviderSourceStore(data, item.project_id).inventory().model_dump()
            )
            for item in projects
        )
        with database.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        with sqlite3.connect(database) as connection:
            schema_digest = database_schema_sha256(connection)
        receipt = BackupSQLiteCaptureReceipt(
            capture_id=capture_id,
            captured_at=captured_at,
            rcp_source_commit=request.source_commit,
            space_id=store.space_id,
            space_name=store.space_name,
            snapshot_path=str(database),
            database_schema_sha256=schema_digest,
            sqlite_snapshot=BackupFileEntry(
                archive_path="database/rcp.sqlite3",
                source_relative_path="rcp.sqlite3",
                group="sqlite_snapshot",
                sha256=digest,
                size_bytes=database.stat().st_size,
            ),
            app_data_plan=plan,
            projects=projects,
            imported_source_inventories=imported,
            status="partial"
            if not plan.complete or any(p.status == "uncaptured" for p in projects)
            else "complete",
        )
        receipt_path = capture / "sqlite-capture.json"
        receipt_digest = write_immutable_backup_receipt(receipt_path, receipt)
    result = prepare(
        PrepareRequest(
            version=1,
            data_dir=str(data),
            output_dir=str(output / "prepared"),
            sqlite_receipt_path=str(receipt_path),
            sqlite_receipt_sha256=receipt_digest,
        ),
        offline=True,
    )
    migrated = output / "migrated-data"
    copy_proof_tree(Path(result["roots"][0]["payload"]), migrated)
    # Rollback must restore the pre-migration database; the proof/capture keeps the
    # migrated copy for the candidate's bounded offline verification.
    shutil.copyfile(original, Path(result["roots"][0]["payload"]) / "rcp.sqlite3")
    result["migrated_data_root"] = str(migrated)
    return result


class OfflineProtectRequest(ValidateRequest):
    recipient: str
    installation_id: str


def offline_protect(request: OfflineProtectRequest) -> dict[str, object]:
    from rcp.server_ops.backup import build_archive_manifest, protect_backup_archive, require_age_1x
    from rcp.server_ops.backup_project_files import BackupProjectFileCapturePublication
    from rcp.server_ops.config import InstalledServerConfig, ServerBackupConfig, ServerPathsConfig

    proof = _read_proof(Path(request.proof_path), request.proof_sha256)
    if proof.sqlite_receipt.status != "complete" or proof.project_receipt.status != "complete":
        raise MaintenanceRefused("Adoption requires a complete protected application capture.")
    output = Path(request.output_dir)
    _new_output(output)
    capture = output / f"backup-{proof.sqlite_receipt.capture_id}"
    copy_proof_tree(Path(proof.capture_root), capture)
    installed = InstalledServerConfig(
        installation_id=request.installation_id,
        paths=ServerPathsConfig.from_layout(),
        backup=ServerBackupConfig(destination=str(output), age_recipient=request.recipient),
    )
    publication = BackupProjectFileCapturePublication(
        receipt=proof.project_receipt,
        receipt_path=capture / "project-files.json",
        receipt_sha256=proof.project_receipt_sha256,
    )
    manifest = build_archive_manifest(
        installed=installed, sqlite_receipt=proof.sqlite_receipt, project_publication=publication
    )
    protected = protect_backup_archive(
        installed=installed, manifest=manifest, capture_root=capture, age_version=require_age_1x()
    )
    return {
        "version": 1,
        "status": "protected",
        "receipt_path": str(protected.receipt_path),
        "receipt_sha256": protected.receipt_sha256,
        "archive_path": str(protected.archive_path),
        "archive_sha256": protected.receipt.archive_sha256,
        "uncaptured_projects": protected.receipt.uncaptured_project_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "inventory",
            "prepare",
            "validate",
            "capabilities",
            "inspect",
            "offline-inventory",
            "offline-prepare",
            "restore-prepare",
            "offline-protect",
        ),
    )
    parser.add_argument(
        "request", nargs="?", help="private JSON request path, or - for bounded stdin"
    )
    args = parser.parse_args(argv)
    if args.operation == "capabilities":
        if args.request is not None:
            parser.error("capabilities takes no request")
        print(
            json.dumps(
                {
                    "version": 1,
                    "maintenance_protocol": 10,
                    "commands": [
                        "inventory",
                        "prepare",
                        "validate",
                        "inspect",
                        "offline-inventory",
                        "offline-prepare",
                        "restore-prepare",
                        "offline-protect",
                    ],
                }
            )
        )
        return 0
    if args.request is None:
        parser.error("application worker operation requires a JSON request")
    try:
        payload = (
            sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
            if args.request == "-"
            else _read(Path(_absolute(args.request)))
        )
        if len(payload) > _MAX_REQUEST_BYTES:
            raise MaintenanceRefused("Application request exceeds its bound.")
        if args.operation == "inventory":
            result = inventory(PrepareRequest.model_validate_json(payload))
        elif args.operation == "prepare":
            result = prepare(PrepareRequest.model_validate_json(payload))
        elif args.operation == "restore-prepare":
            from rcp.server_ops.restore import RestorePrepareRequest, prepare_restore

            result = prepare_restore(RestorePrepareRequest.model_validate_json(payload))
        elif args.operation == "offline-protect":
            result = offline_protect(OfflineProtectRequest.model_validate_json(payload))
        elif args.operation == "inspect":
            result = inspect(InspectRequest.model_validate_json(payload))
        elif args.operation == "offline-inventory":
            result = offline_inventory(OfflinePrepareRequest.model_validate_json(payload))
        elif args.operation == "offline-prepare":
            result = offline_prepare(OfflinePrepareRequest.model_validate_json(payload))
        else:
            result = validate(ValidateRequest.model_validate_json(payload))
        sys.stdout.buffer.write(canonical_json_line(result))
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(
            json.dumps({"version": 1, "status": "refused", "diagnostic": str(exc)}), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
