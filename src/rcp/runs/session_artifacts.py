"""A session's turn artifacts: an earlier turn's file, changed in place, is its next version.

Each turn artifact has one tracked file, `turns/<supplier_id>/artifacts/<source_name>`
in the workspace of the stage its origin task ran on. RCP records the digest it last
knows is there (`Artifact.synced_sha256`). Turn start rewrites a tracked file the
agent has not changed to the version the human sees; turn end publishes a changed
one as the next version. Versions are artifact storage, never graph authority.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from rcp.artifacts import (
    AgentArtifactDescriptor,
    ArtifactMediaType,
    classify_artifact_bytes,
    descriptor_for,
    read_local_regular_file,
)
from rcp.background import AgentTaskExecution
from rcp.limits import CHAT_ARTIFACT_MAX_FILE_BYTES, RUN_STAGE_RETENTION_DAYS
from rcp.regular_file_reader import RegularFileTooLargeError
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskReceiptRecord, AppStore, Artifact, ArtifactVersionConflict
from rcp.storage.artifact_models import ArtifactOperationConflict
from rcp.storage.artifacts import ArtifactByteLimitError
from rcp.transport import RemoteRunStage
from rcp.transport.remote_stage_root import artifact_digests, sync_artifact

OUTCOME_CATEGORIES = {
    "published": "artifact_edit_published",
    "forked": "artifact_edit_published",
    "failed": "artifact_edit_publish_failed",
    "unchanged": "artifact_edit_unchanged",
}
_FAILURE_REASONS = {"missing", "type_changed", "too_large", "unverified"}
_SAFE_PART = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class _Tracked:
    artifact: Artifact
    # The artifact's own tracked file, whose synced digest RCP stores. A commented
    # artifact from another stage is copied to the same relative path here instead.
    durable: bool


def _parts(directory: Path | PurePosixPath) -> tuple[Path | PurePosixPath, str]:
    """This turn's workspace and folder, from its `turns/<scope>/artifacts` directory."""
    return directory.parents[2], directory.parent.name


def _tracked(execution: AgentTaskExecution, request: RunRequest, scope_id: str) -> list[_Tracked]:
    store = execution.store
    task = store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The turn's task is unavailable.")
    found = [
        _Tracked(artifact, durable=True)
        for artifact in (
            store.session_artifacts(
                task.project_id, execution.stage_host or "", execution.stage_root
            )
            if execution.stage_root
            else []
        )
        # This turn's own folder belongs to discovery.
        if artifact.supplier_id != scope_id
        and _SAFE_PART.fullmatch(artifact.supplier_id)
        and _SAFE_PART.fullmatch(artifact.source_name)
    ]
    edit = request.artifact_edit
    if edit is not None and all(item.artifact.artifact_id != edit.artifact_id for item in found):
        artifact = store.artifact(edit.artifact_id)
        if artifact is None or not _SAFE_PART.fullmatch(artifact.supplier_id):
            raise ValueError("The commented artifact is unavailable.")
        found.append(_Tracked(artifact, durable=False))
    return found


def _digests(
    workspace: Path | PurePosixPath, remote: RemoteRunStage | None, tracked: list[_Tracked]
) -> dict[tuple[str, str], tuple[str, int]]:
    entries = [(item.artifact.supplier_id, item.artifact.source_name) for item in tracked]
    if remote is not None:
        return remote.artifact_digests(entries)
    return {
        (scope, name): (digest, size)
        for scope, name, digest, size in artifact_digests(
            str(workspace), [list(entry) for entry in entries], CHAT_ARTIFACT_MAX_FILE_BYTES
        )
    }


def _write(
    workspace: Path | PurePosixPath,
    remote: RemoteRunStage | None,
    artifact: Artifact,
    expected: str,
    data: bytes,
) -> bool:
    if remote is not None:
        return remote.sync_artifact(artifact.supplier_id, artifact.source_name, expected, data)
    return sync_artifact(
        str(workspace),
        artifact.supplier_id,
        artifact.source_name,
        expected,
        data,
        CHAT_ARTIFACT_MAX_FILE_BYTES,
    )


def _version_sha(store: AppStore, artifact_id: str, version_id: str) -> str:
    for version in store.artifact_versions(artifact_id):
        if version.version_id == version_id:
            return version.sha256
    raise KeyError(version_id)


def _tracked_path(workspace: Path | PurePosixPath, artifact: Artifact) -> str:
    return str(workspace / "turns" / artifact.supplier_id / "artifacts" / artifact.source_name)


def sync_session_artifacts(
    execution: AgentTaskExecution,
    request: RunRequest,
    directory: Path | PurePosixPath,
    remote: RemoteRunStage | None,
) -> str | None:
    """At turn start, give every untouched tracked file the version the human sees.

    A comment's artifact gets the version the comment was drawn on, written back
    even when its file is gone. Returns that file's path for the turn's prompt.
    """
    workspace, scope_id = _parts(directory)
    store = execution.store
    edit = request.artifact_edit
    tracked = _tracked(execution, request, scope_id)
    if not tracked:
        return None
    try:
        digests = _digests(workspace, remote, tracked)
    except Exception:
        if edit is not None:
            raise
        store.record_agent_task_event(
            execution.operation_id,
            "Earlier artifacts of this session could not be checked before the turn.",
            level="warning",
        )
        return None
    path = None
    for item in tracked:
        commented = edit is not None and item.artifact.artifact_id == edit.artifact_id
        if commented:
            path = _tracked_path(workspace, item.artifact)
        found = digests.get((item.artifact.supplier_id, item.artifact.source_name))
        if not commented and found is None:
            continue
        with store.artifact_lock(item.artifact.artifact_id):
            artifact = store.artifact(item.artifact.artifact_id)
            if artifact is None:
                if commented:
                    raise ValueError("The commented artifact is unavailable.")
                continue
            # The admitted turn edits the version its comment was drawn on; a
            # retry edits the current one, which may be its own earlier publish.
            target = (
                edit.base_version
                if edit is not None
                and commented
                and (not item.durable or execution.operation_id == edit.operation_id)
                else artifact.current_version
            )
            try:
                target_sha = _version_sha(store, artifact.artifact_id, target)
                data = store.read_artifact_bytes(artifact.artifact_id, target)
            except KeyError as exc:
                if commented:
                    raise ValueError(
                        "The selected artifact version is no longer available. "
                        "Send the comment again."
                    ) from exc
                continue
            if not item.durable:
                # A copy here is written once; a retry keeps the agent's changes.
                if found is None:
                    _write(workspace, remote, artifact, "", data)
                continue
            synced = store.synced_artifact_digest(artifact)
            if found is not None and found[0] != synced:
                continue  # The agent changed or replaced it; turn end settles it.
            if found is not None and target_sha == synced:
                continue
            if _write(workspace, remote, artifact, found[0] if found else "", data):
                store.record_artifact_sync(artifact.artifact_id, target_sha)
    return path


def _record(
    execution: AgentTaskExecution, status: str, artifact_id: str, **payload: object
) -> None:
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        OUTCOME_CATEGORIES[status],
        {"status": status, "artifact_id": artifact_id, **payload},
        tier="summary",
    )


def publish_session_artifacts(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    directory: Path | PurePosixPath,
    remote: RemoteRunStage | None,
    *,
    artifacts: list[AgentArtifactDescriptor],
    service: ProjectService,
) -> list[AgentArtifactDescriptor]:
    """At turn end, publish each changed tracked file as its artifact's next version.

    Failures leave the change in place, so a later turn end still publishes it.
    """
    if execution is None:
        return artifacts
    try:
        return [*artifacts, *_publish(execution, request, directory, remote, service)]
    except Exception as exc:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"Changed artifacts of this session could not be published yet: {exc}",
            level="warning",
        )
        return artifacts


def _publish(
    execution: AgentTaskExecution,
    request: RunRequest,
    directory: Path | PurePosixPath,
    remote: RemoteRunStage | None,
    service: ProjectService,
) -> list[AgentArtifactDescriptor]:
    from rcp.live_artifact_runtime import resolve_artifact_live_version

    workspace, scope_id = _parts(directory)
    store = execution.store
    edit = request.artifact_edit
    tracked = _tracked(execution, request, scope_id)
    if not tracked:
        return []
    # Unbounded per category: a replay must see every artifact this turn settled.
    settled = {
        receipt.payload.get("artifact_id"): receipt
        for category in dict.fromkeys(OUTCOME_CATEGORIES.values())
        for receipt in store.agent_task_receipts_by_category(execution.operation_id, category)
    }
    digests = _digests(workspace, remote, tracked)
    listed: list[AgentArtifactDescriptor] = []
    for item in tracked:
        artifact = store.artifact(item.artifact.artifact_id)
        if artifact is None:
            continue
        commented = edit is not None and artifact.artifact_id == edit.artifact_id
        if artifact.artifact_id in settled:
            # A replayed turn end lists what it already published.
            descriptor = settled[artifact.artifact_id].payload.get("descriptor")
            if descriptor:
                listed.append(AgentArtifactDescriptor.model_validate(descriptor))
            continue
        if item.durable:
            synced = store.synced_artifact_digest(artifact)
        else:
            assert edit is not None
            synced = _version_sha(store, artifact.artifact_id, edit.base_version)
        found = digests.get((artifact.supplier_id, artifact.source_name))
        if found is None or found[0] == synced:
            if commented:
                _record(
                    execution,
                    "unchanged" if found else "failed",
                    artifact.artifact_id,
                    **({} if found else {"reason": "missing"}),
                )
            continue

        reason, data = None, b""
        if found[1] > CHAT_ARTIFACT_MAX_FILE_BYTES:
            reason = "too_large"
        else:
            try:
                data = (
                    remote.read_artifact_bytes(
                        artifact.supplier_id,
                        artifact.source_name,
                        max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
                    )
                    if remote is not None
                    else read_local_regular_file(
                        Path(str(workspace)) / "turns" / artifact.supplier_id / "artifacts",
                        artifact.source_name,
                        max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
                    )
                )
            except FileNotFoundError:
                reason = "missing"
            except RegularFileTooLargeError:
                reason = "too_large"
            except ValueError:
                reason = "unverified"
        media_type = classify_artifact_bytes(artifact.source_name, data) if not reason else None
        if media_type is not None and media_type != artifact.media_type:
            reason = "type_changed"
        if reason == "missing" and not commented:
            continue
        if reason is not None:
            _record(execution, "failed", artifact.artifact_id, reason=reason)
            _settle(store, item, found[0])
            continue
        assert media_type is not None
        digest = hashlib.sha256(data).hexdigest()
        try:
            version = store.publish_artifact_version(
                artifact.artifact_id,
                base_version=_base_version(store, artifact.artifact_id, synced),
                operation_id=execution.operation_id,
                data=data,
            )
        except ArtifactByteLimitError:
            _record(execution, "failed", artifact.artifact_id, reason="too_large")
            _settle(store, item, digest)
            continue
        except ArtifactOperationConflict:
            raise
        except ArtifactVersionConflict:
            fork = _fork(execution, service, artifact, scope_id, media_type, data)
            if fork is None:
                _record(execution, "failed", artifact.artifact_id, reason="unverified")
            else:
                _record(
                    execution,
                    "forked",
                    artifact.artifact_id,
                    separate_artifact_id=fork.artifact_id,
                    descriptor=fork.model_dump(mode="json"),
                )
                listed.append(fork)
            _settle(store, item, digest)
            continue
        resolve_artifact_live_version(store, service, artifact.artifact_id, version.version_id)
        descriptor = AgentArtifactDescriptor(
            artifact_id=artifact.artifact_id,
            name=artifact.source_name,
            media_type=media_type,
            size_bytes=len(data) or None,
        )
        _settle(store, item, digest)
        _record(
            execution,
            "published",
            artifact.artifact_id,
            version_id=version.version_id,
            descriptor=descriptor.model_dump(mode="json"),
        )
        listed.append(descriptor)
    return listed


def _settle(store: AppStore, item: _Tracked, digest: str) -> None:
    """Every outcome records what the tracked file holds, so the next turn start
    rewrites a rejected or forked file. An unreadable file has no digest to keep."""
    if item.durable and digest:
        store.record_artifact_sync(item.artifact.artifact_id, digest)


def _base_version(store: AppStore, artifact_id: str, synced: str) -> str:
    """The version the tracked file last held; the current one when it is gone."""
    artifact = store.artifact(artifact_id)
    assert artifact is not None
    matches = [v.version_id for v in store.artifact_versions(artifact_id) if v.sha256 == synced]
    if artifact.current_version in matches or not matches:
        return artifact.current_version
    return matches[-1]


def _fork(
    execution: AgentTaskExecution,
    service: ProjectService,
    artifact: Artifact,
    scope_id: str,
    media_type: ArtifactMediaType,
    data: bytes,
) -> AgentArtifactDescriptor | None:
    """The artifact moved since the turn's base: keep the bytes as a separate artifact."""
    from rcp.live_artifact_runtime import resolve_artifact_live_version

    store = execution.store
    task = store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The turn's task is unavailable.")
    descriptor = descriptor_for(
        scope_id, artifact.source_name, media_type=media_type, size_bytes=len(data) or None
    )
    now = store.now()
    chat_id = task.request.get("chat_id")
    try:
        forked = store.create_artifact(
            Artifact(
                artifact_id=descriptor.artifact_id,
                project_id=task.project_id,
                supplier="turn",
                supplier_id=scope_id,
                source_name=artifact.source_name,
                media_type=descriptor.media_type,
                created_at=now,
                expires_at=(
                    datetime.fromisoformat(now) + timedelta(days=RUN_STAGE_RETENTION_DAYS)
                ).isoformat(),
                origin_operation_id=task.operation_id,
                episode_id=artifact.episode_id or task.episode_id,
                chat_id=chat_id if isinstance(chat_id, str) else None,
                synced_sha256=hashlib.sha256(data).hexdigest(),
            ),
            data=data,
        )
    except ArtifactVersionConflict:
        return None  # This turn wrote a different file under the same name.
    resolve_artifact_live_version(store, service, forked.artifact_id, forked.current_version)
    return descriptor


def artifact_edit_outcomes(
    receipts: list[AgentTaskReceiptRecord], edited_artifact_id: str | None
) -> list[dict[str, object]]:
    """Project one stable outcome per artifact, latest receipt first to last."""
    outcomes: dict[str, dict[str, object]] = {}
    for receipt in receipts:
        # Receipts from before per-artifact outcomes name only their edit's artifact.
        artifact_id = (
            receipt.payload.get("artifact_id")
            if receipt.payload.get("status") in OUTCOME_CATEGORIES
            else edited_artifact_id
        )
        if isinstance(artifact_id, str):
            outcomes[artifact_id] = _outcome(receipt, artifact_id)
    return list(outcomes.values())


def _outcome(receipt: AgentTaskReceiptRecord, artifact_id: str) -> dict[str, object]:
    payload = receipt.payload
    status = payload.get("status")
    separate_id = payload.get("separate_artifact_id")
    if status not in OUTCOME_CATEGORIES:
        status = (
            "unchanged"
            if receipt.category == "artifact_edit_unchanged"
            else "failed"
            if receipt.category == "artifact_edit_publish_failed"
            else "forked"
            if payload.get("artifact_id") != artifact_id
            else "published"
        )
        if status == "forked":
            separate_id = payload.get("artifact_id")
    reason = payload.get("reason")
    return {
        "status": status,
        "artifact_id": artifact_id,
        "version_id": payload.get("version_id") if status == "published" else None,
        "separate_artifact_id": separate_id if status == "forked" else None,
        "reason": (reason if reason in _FAILURE_REASONS else "unverified")
        if status == "failed"
        else None,
    }
