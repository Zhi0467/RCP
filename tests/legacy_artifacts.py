"""Archived candidate rows from releases before versioned artifact editing."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from rcp.artifacts import descriptor_for
from rcp.runs.chat import _local_chat_artifact_directory
from rcp.storage import AgentTaskRecord, AppStore, ArtifactRevisionCandidateRecord


def insert_legacy_candidate(
    store: AppStore, candidate: ArtifactRevisionCandidateRecord
) -> ArtifactRevisionCandidateRecord:
    values = candidate.model_dump(mode="json")
    actor = values.pop("decided_by")
    assert actor is None
    values["decided_by_json"] = None
    columns = tuple(values)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO artifact_revision_candidates ("
            + ", ".join(columns)
            + ") VALUES ("
            + ", ".join("?" for _ in columns)
            + ")",
            tuple(values[column] for column in columns),
        )
    return candidate


def insert_legacy_view(connection, project_id="project"):
    """A pre-migration view whose bytes exist only in SQLite, never its stage."""
    now = AppStore.now()
    connection.execute(
        "INSERT INTO result_views VALUES ("
        "?, ?, 'experiment', 'chat', 'origin', 'latest', 'codex', 'model', '', 'local', "
        "'session', '', '/missing/stage', 'page.html', ?, 4, 'page', ?, ?, ?, NULL, NULL)",
        ("a" * 24, project_id, "0" * 64, now, now, now),
    )


def create_legacy_artifact(
    store,
    tmp_path,
    *,
    kept=False,
    split=False,
    parent=None,
    result=True,
    project_id="project",
    data=b"page",
):
    operation_id = str(uuid.uuid4())
    scope = parent.operation_id if parent else operation_id
    descriptor = descriptor_for(scope, "plot.html", media_type="text/html", size_bytes=len(data))
    if kept:
        descriptor = descriptor.model_copy(
            update={"kept_filename": "saved.html", "kept_at": store.now()}
        )
    root = str(tmp_path / operation_id)
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="project_chat",
            status="succeeded",
            request={"chat_id": operation_id, "mode": "discuss"},
            result={"artifacts": [descriptor.model_dump(mode="json")]} if result else {},
            created_at=store.now(),
            updated_at=store.now(),
            finished_at=store.now(),
            status_message="",
            stage_root=root,
            parent_operation_id=parent.operation_id if parent else None,
            attempt=2 if parent else 1,
        )
    )
    store.record_agent_task_receipt(
        operation_id,
        "operation_created",
        {
            "kind": task.kind,
            "attempt": task.attempt,
            "has_parent": parent is not None,
            "resumed": parent is not None,
        },
    )
    if split:
        store.record_chat_stage_layout(
            operation_id, stage_root=root, workspace_root=str(Path(root) / "workspace")
        )
    directory = _local_chat_artifact_directory(store, task, scope)
    directory.mkdir(parents=True)
    (directory / descriptor.name).write_bytes(data)
    touched = datetime.fromisoformat(store.now()) - timedelta(days=2)
    os.utime(root, (touched.timestamp(), touched.timestamp()))
    return task, descriptor, directory
