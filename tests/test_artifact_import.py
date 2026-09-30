from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rcp.artifact_import import import_project_artifacts
from rcp.limits import ARTIFACT_IMPORT_RETRY_SECONDS, RUN_STAGE_RETENTION_DAYS
from rcp.runs.chat import _local_chat_artifact_directory
from rcp.storage import AppStore, ArtifactRevisionCandidateRecord
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.state import StateUnavailable, StateWorkspace

from .legacy_artifacts import create_legacy_artifact as _legacy
from .legacy_artifacts import insert_legacy_candidate


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(store, "now", lambda: clock[0].isoformat())
    store.test_clock = clock
    return store


def _no_workspace():
    raise AssertionError("temporary import must not open a state workspace")


def _run(store, workspace=_no_workspace):
    return import_project_artifacts(store, "project", workspace=workspace)


@pytest.mark.parametrize("split", [False, True])
def test_local_stage_expiry_and_identity(store, tmp_path, split):
    task, descriptor, _ = _legacy(store, tmp_path, split=split)
    with store.connection() as connection:
        connection.execute(
            "DELETE FROM graph_run_receipts WHERE operation_id = ? AND category = 'operation_created'",
            (task.operation_id,),
        )
    touched = Path(task.stage_root).stat().st_mtime
    assert _run(store) is None
    artifact = store.artifact(descriptor.artifact_id)
    assert artifact.supplier_id == task.operation_id
    assert artifact.origin_operation_id == task.operation_id
    assert artifact.chat_id == task.request["chat_id"]
    assert (
        artifact.expires_at
        == (
            datetime.fromtimestamp(touched, UTC) + timedelta(days=RUN_STAGE_RETENTION_DAYS)
        ).isoformat()
    )
    assert Path(task.stage_root).stat().st_mtime == touched
    assert store.read_artifact_bytes(artifact.artifact_id) == b"page"


def test_resumed_turn_keeps_logical_supplier_scope(store, tmp_path):
    parent, _, _ = _legacy(store, tmp_path, result=False)
    task, descriptor, _ = _legacy(store, tmp_path, parent=parent)
    assert _run(store) is None
    artifact = store.artifact(descriptor.artifact_id)
    assert artifact.supplier_id == parent.operation_id
    assert artifact.origin_operation_id == task.operation_id


def test_kept_file_import_uses_workspace_and_leaves_repository_unchanged(store, tmp_path):
    task, descriptor, _ = _legacy(store, tmp_path, kept=True)
    _legacy(store, tmp_path, parent=task)
    repository = tmp_path / "repository"
    (repository / "artifacts").mkdir(parents=True)
    path = repository / "artifacts" / descriptor.kept_filename
    path.write_bytes(b"kept page")
    workspace = StateWorkspace(repository / ".research", str(repository))
    assert _run(store, lambda: workspace) is None
    artifact = store.artifact(descriptor.artifact_id)
    assert artifact.expires_at is None
    assert artifact.kept_at == descriptor.kept_at
    assert store.read_artifact_bytes(artifact.artifact_id) == b"kept page"
    assert path.read_bytes() == b"kept page"
    assert tuple(path.parent.iterdir()) == (path,)


def test_offline_remote_failure_survives_restart_and_backs_off_then_imports(
    store, tmp_path, monkeypatch
):
    task, descriptor, _ = _legacy(store, tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET stage_host = ?, stage_root = ? WHERE operation_id = ?",
            ("compute", "/tmp/rcp-run.test", task.operation_id),
        )
    touched = (store.test_clock[0] - timedelta(days=1)).timestamp()
    calls = []

    def stage_last_touch(self):
        calls.append(self.host)
        raise StateUnavailable("SSH host is offline")

    monkeypatch.setattr(RemoteRunStage, "stage_last_touch", stage_last_touch)
    assert _run(store) == ARTIFACT_IMPORT_RETRY_SECONDS
    status = store.artifact_import_status(descriptor.artifact_id)
    assert status["state"] == "unavailable"
    assert status["attempts"] == 1
    assert AppStore(store.path).artifact_import_status(descriptor.artifact_id) == status
    _run(store)
    assert len(calls) == 1
    store.test_clock[0] += timedelta(seconds=ARTIFACT_IMPORT_RETRY_SECONDS)
    assert _run(store) == ARTIFACT_IMPORT_RETRY_SECONDS * 2
    assert len(calls) == 2
    monkeypatch.setattr(RemoteRunStage, "stage_last_touch", lambda self: touched)
    monkeypatch.setattr(
        RemoteRunStage, "read_artifact_bytes", lambda self, scope, name, **kw: b"remote page"
    )
    store.test_clock[0] += timedelta(seconds=ARTIFACT_IMPORT_RETRY_SECONDS * 2)
    assert _run(store) is None
    assert store.read_artifact_bytes(descriptor.artifact_id) == b"remote page"
    assert store.artifact_import_status(descriptor.artifact_id)["state"] == "imported"


@pytest.mark.parametrize("failure", ["missing", "expired", "symlink"])
def test_permanent_sources_are_recorded_and_not_retried(store, tmp_path, failure):
    task, descriptor, directory = _legacy(store, tmp_path)
    path = directory / descriptor.name
    if failure == "missing":
        path.unlink()
    elif failure == "expired":
        old = (store.test_clock[0] - timedelta(days=RUN_STAGE_RETENTION_DAYS + 1)).timestamp()
        os.utime(task.stage_root, (old, old))
    else:
        path.unlink()
        path.symlink_to(tmp_path / "elsewhere")
    assert _run(store) is None
    status = store.artifact_import_status(descriptor.artifact_id)
    assert status["state"] == "missing"
    assert status["next_attempt_at"] is None
    assert _run(store) is None
    assert store.artifact_import_status(descriptor.artifact_id) == status


def _candidate(store, tmp_path):
    source, original, _ = _legacy(store, tmp_path)
    turn, descriptor, _ = _legacy(store, tmp_path, result=False)
    candidate = insert_legacy_candidate(
        store,
        ArtifactRevisionCandidateRecord(
            candidate_id="c" * 24,
            project_id="project",
            source_operation_id=source.operation_id,
            source_artifact_id=original.artifact_id,
            revision_operation_id=turn.operation_id,
            stage_host="",
            stage_root=turn.stage_root,
            artifact_scope_id=turn.operation_id,
            source_name=descriptor.name,
            media_type=descriptor.media_type,
            base_sha256=hashlib.sha256(b"source").hexdigest(),
            candidate_sha256=hashlib.sha256(b"page").hexdigest(),
            candidate_size_bytes=4,
            status="pending",
            created_at=store.now(),
            updated_at=store.now(),
        ),
    )
    return source, original, turn, descriptor, candidate


@pytest.mark.parametrize("status", ["pending", "accepting", "conflicted"])
def test_candidate_import_preserves_identity_retention_and_unblocks_transfer(
    store, tmp_path, status
):
    _, original, turn, descriptor, candidate = _candidate(store, tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE artifact_revision_candidates SET status = ?, diagnostic = ? "
            "WHERE candidate_id = ?",
            (
                status,
                "legacy" if status in {"conflicted", "abandoned"} else None,
                candidate.candidate_id,
            ),
        )
        store._require_finished_transfer_state(connection, "other-project")
        with pytest.raises(ValueError):
            store._require_finished_transfer_state(connection, "project")
    old = (store.test_clock[0] - timedelta(days=RUN_STAGE_RETENTION_DAYS + 1)).timestamp()
    os.utime(turn.stage_root, (old, old))
    assert store.legacy_artifact_import_ids() == {original.artifact_id, descriptor.artifact_id}
    assert _run(store) is None
    assert not store.legacy_artifact_import_ids()
    artifact = store.artifact(descriptor.artifact_id)
    assert artifact.origin_operation_id == turn.operation_id
    assert datetime.fromisoformat(artifact.expires_at) == store.test_clock[0] + timedelta(
        days=RUN_STAGE_RETENTION_DAYS
    )
    store.expire_artifacts()
    assert store.read_artifact_bytes(descriptor.artifact_id) == b"page"
    assert len(store.artifact_versions(original.artifact_id)) == 1
    assert store.legacy_artifact_candidates("project")[0].status == status
    with store.connection() as connection:
        store._require_finished_transfer_state(connection, "project")
    assert store.agent_task(turn.operation_id).result["artifacts"] == [
        descriptor.model_dump(mode="json")
    ]
    assert _run(store) is None
    assert len(store.agent_task(turn.operation_id).result["artifacts"]) == 1


def test_expiry_pruning_cannot_resurrect_legacy_descriptor(store, tmp_path):
    task, descriptor, _ = _legacy(store, tmp_path)
    _run(store)
    store.test_clock[0] += timedelta(days=RUN_STAGE_RETENTION_DAYS + 1)
    assert store.expire_artifacts() == 1
    now = store.test_clock[0].timestamp()
    os.utime(task.stage_root, (now, now))
    assert _run(store) is None
    assert store.artifact(descriptor.artifact_id) is None


@pytest.mark.parametrize("candidate", [False, True])
def test_retry_after_copy_does_not_reread_or_overwrite_existing_artifact(
    store, tmp_path, monkeypatch, candidate
):
    if candidate:
        _, _, task, descriptor, _ = _candidate(store, tmp_path)
        directory = _local_chat_artifact_directory(store, task, task.operation_id)
    else:
        task, descriptor, directory = _legacy(store, tmp_path)

    def interrupted_receipt(*args, **kwargs):
        raise OSError("interrupted import receipt write")

    monkeypatch.setattr(store, "complete_artifact_import", interrupted_receipt)
    assert _run(store) == ARTIFACT_IMPORT_RETRY_SECONDS
    artifact = store.artifact(descriptor.artifact_id)
    assert artifact is not None
    (directory / descriptor.name).write_bytes(b"changed source")
    store.test_clock[0] += timedelta(seconds=ARTIFACT_IMPORT_RETRY_SECONDS)
    restarted = AppStore(store.path)
    monkeypatch.setattr(restarted, "now", store.now)
    assert _run(restarted) is None
    assert store.artifact(descriptor.artifact_id) == artifact
    assert store.read_artifact_bytes(descriptor.artifact_id) == b"page"
    assert len(store.artifact_versions(descriptor.artifact_id)) == 1
    assert len(store.agent_task(task.operation_id).result["artifacts"]) == 1


def test_schema_30_upgrade_only_adds_import_state(store, tmp_path):
    _, descriptor, _ = _legacy(store, tmp_path)
    _run(store)
    original = store.artifact(descriptor.artifact_id)
    with store.connection() as connection:
        connection.execute("DROP INDEX graph_runs_artifact_edit_episode")
        connection.execute("DROP TABLE artifact_imports")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version >= 31")
    reopened = AppStore(store.path)
    assert reopened.artifact(descriptor.artifact_id) == original
    assert reopened.read_artifact_bytes(descriptor.artifact_id) == b"page"
    assert reopened.artifact_import_status(descriptor.artifact_id) is None
    assert _run(reopened) is None


def test_invalid_descriptor_batch_does_not_hide_later_valid_sources(store, tmp_path, monkeypatch):
    import json

    monkeypatch.setattr("rcp.artifact_import.ARTIFACT_IMPORT_BATCH_SIZE", 1)
    _, valid, _ = _legacy(store, tmp_path)
    _, second, _ = _legacy(store, tmp_path)
    store.test_clock[0] += timedelta(seconds=1)
    invalid_task, invalid, _ = _legacy(store, tmp_path)
    raw = invalid.model_dump(mode="json")
    raw["media_type"] = "invalid"
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET result_json = ? WHERE operation_id = ?",
            (json.dumps({"artifacts": ["unidentified", {}, raw]}), invalid_task.operation_id),
        )
    assert _run(store) == 0
    assert store.artifact_import_status(invalid.artifact_id)["state"] == "missing"
    assert _run(store) == 0
    assert len(store.artifacts("project")) == 1
    assert _run(store) is None
    assert {a.artifact_id for a in store.artifacts("project")} == {
        valid.artifact_id,
        second.artifact_id,
    }
    assert store.read_artifact_bytes(valid.artifact_id) == b"page"
