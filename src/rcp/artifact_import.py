"""Resumable import of pre-storage artifact bytes, without changing source files."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rcp.artifacts import (
    AgentArtifactDescriptor,
    _open_local_directory,
    artifact_id,
    descriptor_for,
    read_local_regular_file,
)
from rcp.limits import (
    ARTIFACT_IMPORT_BATCH_SIZE,
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    RUN_STAGE_RETENTION_DAYS,
)
from rcp.runs.chat import _local_chat_artifact_directory, _logical_chat_turn_operation_id
from rcp.storage import AgentTaskRecord, AppStore, Artifact, ArtifactRevisionCandidateRecord
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.state import StateUnavailable, StateWorkspace


def _due(store: AppStore, identity: str) -> bool:
    status = store.artifact_import_status(identity)
    return status is None or (
        status["state"] == "unavailable" and status["next_attempt_at"] <= store.now()
    )


def _sources(
    store: AppStore, project_id: str
) -> Iterator[
    tuple[AgentTaskRecord, AgentArtifactDescriptor, ArtifactRevisionCandidateRecord | None]
]:
    for candidate in store.legacy_artifact_candidates(project_id):
        descriptor = descriptor_for(
            candidate.artifact_scope_id,
            candidate.source_name,
            media_type=candidate.media_type,
            size_bytes=candidate.candidate_size_bytes,
        )
        if _due(store, descriptor.artifact_id):
            task = store.agent_task(candidate.revision_operation_id)
            if task is not None:
                yield task, descriptor, candidate
    for task in store.artifact_import_tasks(project_id, limit=ARTIFACT_IMPORT_BATCH_SIZE):
        for raw in task.result.get("artifacts", []):
            try:
                descriptor = AgentArtifactDescriptor.model_validate(raw)
            except ValueError:
                if isinstance(raw, dict) and isinstance(raw.get("artifact_id"), str):
                    store.record_artifact_import_failure(
                        project_id,
                        raw["artifact_id"],
                        "The legacy artifact descriptor is invalid.",
                        permanent=True,
                    )
                continue
            if _due(store, descriptor.artifact_id):
                yield task, descriptor, None


def _read_source(
    store: AppStore,
    task: AgentTaskRecord,
    descriptor: AgentArtifactDescriptor,
    candidate: ArtifactRevisionCandidateRecord | None,
    workspace: Callable[[], StateWorkspace],
    protected: frozenset[str],
) -> tuple[str, bytes, str | None]:
    if candidate is not None:
        scope = candidate.artifact_scope_id
    elif artifact_id(task.operation_id, descriptor.name) == descriptor.artifact_id:
        scope = task.operation_id
    else:
        scope = _logical_chat_turn_operation_id(store, task.operation_id)
    if artifact_id(scope, descriptor.name) != descriptor.artifact_id:
        raise ValueError("artifact identity does not match its supplying turn")
    if descriptor.kept_filename is not None:
        return scope, workspace().read_kept_artifact(descriptor.kept_filename), None
    root = candidate.stage_root if candidate else task.stage_root
    host = candidate.stage_host if candidate else task.stage_host
    if not root:
        raise FileNotFoundError("The original run stage is no longer retained.")
    remote = RemoteRunStage(host).attach_artifact_source(root) if host else None
    if remote is not None:
        touched = remote.stage_last_touch()
    else:
        descriptor_fd = _open_local_directory(Path(root))
        try:
            touched = os.fstat(descriptor_fd).st_mtime
        finally:
            os.close(descriptor_fd)
    expires_at = datetime.fromtimestamp(touched, UTC) + timedelta(days=RUN_STAGE_RETENTION_DAYS)
    if (
        not descriptor.is_kept()
        and expires_at <= datetime.fromisoformat(store.now())
        and descriptor.artifact_id not in protected
    ):
        raise FileNotFoundError("The original run stage's retention period expired.")
    if remote is not None:
        data = remote.read_artifact_bytes(
            scope, descriptor.name, max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES
        )
    else:
        # Candidate provenance owns its exact saved root, independently of later task changes.
        source_task = task.model_copy(update={"stage_root": root, "stage_host": host})
        directory = _local_chat_artifact_directory(store, source_task, scope)
        data = read_local_regular_file(
            directory, descriptor.name, max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES
        )
    if candidate is not None and (
        len(data) != candidate.candidate_size_bytes
        or hashlib.sha256(data).hexdigest() != candidate.candidate_sha256
    ):
        raise ValueError("candidate bytes no longer match their recorded digest")
    return scope, data, None if descriptor.is_kept() else expires_at.isoformat()


def import_project_artifacts(
    store: AppStore, project_id: str, *, workspace: Callable[[], StateWorkspace]
) -> float | None:
    """One bounded pass. Return the next delay, or None when this project is done."""
    protected = store.legacy_artifact_import_ids() | store.protected_edit_artifact_ids()
    for processed, (task, descriptor, candidate) in enumerate(_sources(store, project_id)):
        if processed >= ARTIFACT_IMPORT_BATCH_SIZE:
            return 0
        with store.artifact_lock(descriptor.artifact_id):
            try:
                existing = store.artifact(descriptor.artifact_id)
                if existing is None:
                    scope, data, expires_at = _read_source(
                        store, task, descriptor, candidate, workspace, protected
                    )
                    store.create_artifact(
                        Artifact(
                            artifact_id=descriptor.artifact_id,
                            project_id=project_id,
                            supplier="turn",
                            supplier_id=scope,
                            source_name=descriptor.name,
                            media_type=descriptor.media_type,
                            created_at=candidate.created_at
                            if candidate
                            else task.finished_at or task.created_at,
                            expires_at=expires_at,
                            kept_at=(descriptor.kept_at or task.created_at)
                            if descriptor.is_kept()
                            else None,
                            origin_operation_id=task.operation_id,
                            episode_id=task.episode_id,
                            chat_id=task.request.get("chat_id"),
                        ),
                        data=data,
                    )
                elif existing.project_id != project_id:
                    raise ValueError("artifact identity belongs to another project")
                store.complete_artifact_import(
                    project_id,
                    descriptor,
                    candidate_operation_id=task.operation_id if candidate else None,
                )
            except (FileNotFoundError, ValueError) as exc:
                store.record_artifact_import_failure(
                    project_id,
                    descriptor.artifact_id,
                    f"Artifact import unavailable: {exc}",
                    permanent=True,
                )
            except (OSError, StateUnavailable) as exc:
                store.record_artifact_import_failure(
                    project_id,
                    descriptor.artifact_id,
                    f"Artifact source temporarily unavailable: {exc}",
                    permanent=False,
                )
    # Invalid descriptors also consume task rows, without consuming a byte-read slot.
    if store.artifact_import_tasks(project_id, limit=1):
        return 0
    return store.artifact_import_retry_delay(project_id)
