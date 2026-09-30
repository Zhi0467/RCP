"""Resolve an artifact edit from durable origin facts, never the open view."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from rcp.artifact_comments import supports_comments
from rcp.runs.session_master import SESSION_MASTER_ROLE, session_master_label
from rcp.service import ArtifactEditAdmission, ProjectService, RunRequest
from rcp.storage import AgentTaskAdmissionConflict, AgentTaskRecord, AppStore, Artifact
from rcp.transport import RemoteRunStage


class ArtifactFreshSessionRequired(AgentTaskAdmissionConflict):
    """The origin can only be edited with an explicitly requested new session."""


@dataclass(frozen=True)
class ArtifactEditAvailability:
    can_comment: bool
    comment_unavailable_reason: str | None = None
    fresh_session_required: bool = False


def artifact_edit_availability(
    store: AppStore, service: ProjectService, artifact: Artifact
) -> ArtifactEditAvailability:
    """Read durable availability hints; POST proves stage, bytes, and master integrity."""
    fresh = False
    try:
        if not supports_comments(artifact.media_type):
            raise ValueError("The artifact type does not support editing.")
        origin, _ = artifact_reply_origin(store, artifact)
        if origin.graph_target != service.history.graph_target:
            raise ValueError("The artifact belongs to another graph target.")
        master = store.latest_session_master(artifact.project_id, origin.native_session_id or "")
        fresh = origin.history_only or not (
            origin.native_session_id and origin.stage_root and master
        )
        if not fresh and not origin.stage_host:
            stage = Path(origin.stage_root or "")
            fresh = not (stage.is_absolute() and stage.is_dir() and not stage.is_symlink())
        if master and not fresh:
            fresh = store.agent_task_contract(master[0], SESSION_MASTER_ROLE) is None
        if master and not fresh:
            try:
                _launch_kind_for_master_owner(store.agent_task(master[0]))
            except ArtifactFreshSessionRequired:
                fresh = True
        now = store.now()
        reason = store.session_launch_unavailable_reason(
            AgentTaskRecord(
                operation_id=str(uuid.uuid4()),
                project_id=artifact.project_id,
                kind="artifact_edit",
                status="queued",
                status_message="Checking artifact edit availability.",
                created_at=now,
                updated_at=now,
                request={"provider": origin.request.get("provider")},
                native_session_id=None if fresh else origin.native_session_id,
                stage_host=None if fresh else origin.stage_host,
                stage_root=None if fresh else origin.stage_root,
            )
        )
    except (KeyError, OSError, ValueError) as exc:
        return ArtifactEditAvailability(False, str(exc), fresh)
    return ArtifactEditAvailability(reason is None, reason, fresh)


def _launch_kind_for_master_owner(owner: AgentTaskRecord | None) -> str:
    """The task that recorded a session's master is that master's owner.

    Its durable kind and patch kind say which contract the session holds; the
    master's prose is never parsed for authority.
    """

    if owner is not None and owner.kind == "auto_research":
        return "revoking"
    if owner is not None and owner.kind in {"node_chat", "project_chat"}:
        if owner.request.get("patch_kind") == "experiment_loop":
            return "revoking"
        return "discuss"
    raise ArtifactFreshSessionRequired(
        "The artifact's session holds no master that can admit an edit. "
        "Edit in a new session explicitly."
    )


def artifact_reply_origin(
    store: AppStore, artifact: Artifact
) -> tuple[AgentTaskRecord, str | None]:
    origin = store.agent_task(artifact.origin_operation_id or "")
    if origin is None or origin.project_id != artifact.project_id:
        raise AgentTaskAdmissionConflict("The artifact's origin task is unavailable.")
    episode = store.episode(artifact.episode_id or origin.episode_id or "")
    if origin.kind == "episode_report":
        parent = store.agent_task(origin.parent_operation_id or "")
        if parent is not None:
            origin = parent
    if origin.request.get("chat_id"):
        return origin, None
    if episode is not None and episode.mode == "auto_research":
        root = store.agent_task(episode.root_operation_id or "")
        if root is None:
            raise AgentTaskAdmissionConflict("The episode's orchestrator thread is unavailable.")
        return origin, episode.episode_id
    if episode is not None and episode.mode == "experiment_loop":
        for task in reversed(store.episode_tasks(episode.episode_id)):
            if task.request.get("chat_id") and task.request.get("patch_kind") == "experiment_loop":
                return task, None
    raise AgentTaskAdmissionConflict("The artifact's reply thread is unavailable.")


def admit_artifact_edit(
    store: AppStore, service: ProjectService, project_id: str, request: RunRequest
) -> RunRequest:
    context = request.artifact_context
    assert context is not None
    if context.fresh_session:
        exact = request.model_copy(
            update={"artifact_context": context.model_copy(update={"fresh_session": False})}
        )
        try:
            return admit_artifact_edit(store, service, project_id, exact)
        except ArtifactFreshSessionRequired:
            pass
    artifact = store.artifact(context.artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise ValueError("The artifact is unavailable.")
    if (
        artifact.expires_at is not None
        and artifact.expires_at <= store.now()
        and (artifact.artifact_id not in store.protected_edit_artifact_ids())
    ):
        raise AgentTaskAdmissionConflict("The artifact has expired.")
    if not supports_comments(artifact.media_type):
        raise ValueError("The artifact type does not support editing.")
    origin, reply_episode_id = artifact_reply_origin(store, artifact)
    if origin.graph_target != service.history.graph_target:
        raise ValueError("The artifact belongs to another graph target.")
    if context.operation_id not in {artifact.origin_operation_id, origin.operation_id}:
        raise ValueError("The artifact does not belong to this origin task.")
    if context.source == "episode_report" and context.episode_id != artifact.episode_id:
        raise ValueError("The report belongs to another episode.")
    if context.selections and artifact.media_type not in {
        "text/html",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
    }:
        raise ValueError("This artifact supports a comment without selections.")
    fresh = context.fresh_session
    available = not origin.history_only and bool(origin.native_session_id and origin.stage_root)
    if available:
        if origin.stage_host:
            available = (
                RemoteRunStage(origin.stage_host).directory_exists(origin.stage_root or "") is True
            )
        else:
            stage = Path(origin.stage_root or "")
            available = stage.is_absolute() and stage.is_dir() and not stage.is_symlink()
    if not available and not fresh:
        raise ArtifactFreshSessionRequired(
            "The artifact's native session or stage is unavailable. Edit in a new session explicitly."
        )
    found = (
        None if fresh else store.latest_session_master(project_id, origin.native_session_id or "")
    )
    content = None if found is None else store.agent_task_contract(found[0], SESSION_MASTER_ROLE)
    if not fresh and (found is None or content is None):
        raise ArtifactFreshSessionRequired(
            "The artifact's recorded master is unavailable. Edit in a new session explicitly."
        )
    launch_kind = "revoking" if reply_episode_id else "discuss"
    if found is not None and content is not None:
        if hashlib.sha256(content.encode()).hexdigest() != found[1]:
            raise AgentTaskAdmissionConflict("The artifact's recorded master is corrupt.")
        launch_kind = _launch_kind_for_master_owner(store.agent_task(found[0]))
    operation_id = str(uuid.uuid4())
    with store.artifact_lock(artifact.artifact_id):
        artifact = store.artifact(artifact.artifact_id)
        assert artifact is not None
        # Prove the admitted version is readable before creating a task.
        store.read_artifact_bytes(artifact.artifact_id)
        edit = ArtifactEditAdmission(
            artifact_id=artifact.artifact_id,
            base_version=artifact.current_version,
            source_name=artifact.source_name,
            media_type=artifact.media_type,
            operation_id=operation_id,
            staged_scope_id=operation_id,
            origin_operation_id=origin.operation_id,
            launch_kind=launch_kind,
            master_operation_id=found[0] if found else None,
            master_sha256=found[1] if found else None,
            master_path=session_master_label("artifact-master", content) if content else None,
            stage_host=origin.stage_host if not fresh else None,
            stage_root=origin.stage_root if not fresh else None,
            reply_episode_id=reply_episode_id,
            episode_id=artifact.episode_id or origin.episode_id,
            fresh_session=fresh,
        )
    updates = {
        "artifact_edit": edit,
        "mode": "discuss",
        "patch_kind": "work",
        "trigger": "human",
        "control_episode_id": None,
        "control_node_id": None,
        "session_id": None if fresh else origin.native_session_id,
        "chat_id": origin.request.get("chat_id") if reply_episode_id is None else None,
        "chat_scope": origin.request.get("chat_scope", "node"),
        "node_id": origin.request.get("node_id") if reply_episode_id is None else None,
    }
    for key in ("provider", "model", "reasoning", "run_on"):
        updates[key] = origin.request.get(key)
    admitted = RunRequest.model_validate({**request.model_dump(mode="python"), **updates})
    if fresh or not admitted.provider or not admitted.reasoning or not admitted.run_on:
        if not fresh:
            raise AgentTaskAdmissionConflict("The artifact's execution profile is unavailable.")
        profile = service.resolve_agent_profile(
            "project_chat" if admitted.chat_scope == "project" else "node_chat",
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        admitted = admitted.model_copy(
            update={
                "provider": profile.provider,
                "model": profile.model,
                "reasoning": profile.reasoning,
                "run_on": profile.run_on,
            }
        )
    return admitted


def start_artifact_edit(
    tasks, project_id, request, *, authorized_by, parent=None, continuation="fresh"
):
    """Reserve an edit without going through operational episode admission."""
    edit = request.artifact_edit
    if edit is None or request.mode != "discuss":
        raise ValueError("An artifact edit requires its admitted Discuss binding.")
    tasks._require_startup_effects_open("artifact edit admission")
    origin = tasks.store.agent_task(edit.origin_operation_id)
    if origin is None:
        raise ValueError("The artifact origin is unavailable.")
    tasks.admit_provider_task(
        project_id,
        request,
        execution_host=edit.stage_host or "" if edit.stage_root else None,
        graph_target=origin.graph_target,
    )
    kind = (
        "artifact_edit"
        if edit.launch_kind == "revoking"
        else ("project_chat" if request.chat_scope == "project" else "node_chat")
    )
    now = tasks.store.now()
    record = AgentTaskRecord(
        operation_id=str(uuid.uuid4()) if parent else edit.operation_id,
        project_id=project_id,
        graph_target=origin.graph_target,
        kind=kind,
        status="queued",
        status_message="Editing artifact.",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        authorized_by=authorized_by,
        parent_operation_id=parent.operation_id if parent else None,
        attempt=parent.attempt + 1 if parent else 1,
        native_session_id=request.session_id,
        stage_host=parent.stage_host if parent else edit.stage_host,
        stage_root=parent.stage_root if parent else edit.stage_root,
        phase="queued",
        last_activity_at=now,
    )
    with tasks.store.artifact_lock(edit.artifact_id):
        if not tasks.store.agent_task_has_receipt(edit.operation_id, "artifact_edit_staged"):
            try:
                tasks.store.read_artifact_bytes(edit.artifact_id, edit.base_version)
            except (KeyError, OSError, ValueError) as exc:
                raise AgentTaskAdmissionConflict(
                    "The selected artifact version is no longer available. Send the comment again."
                ) from exc
        tasks.store.create_artifact_edit_task(record, continuation_cause=continuation)
    return tasks.launch_admitted(record.operation_id)


def validate_artifact_edit_launch(record, request, *, parent):
    """The file-only owner validates its immutable dispatch and recovery boundary."""
    edit = request.artifact_edit
    if edit is None or request.mode != "discuss" or record.dispatch_authority is not None:
        raise ValueError("An artifact edit has only scratch authority.")
    expected = (
        "artifact_edit"
        if edit.launch_kind == "revoking"
        else ("project_chat" if request.chat_scope == "project" else "node_chat")
    )
    if record.kind != expected or record.episode_id is not None:
        raise ValueError("The edit changed its admitted launch or episode binding.")
    if parent is not None and (
        parent.project_id != record.project_id
        or parent.kind != record.kind
        or parent.graph_target != record.graph_target
        or parent.request.get("artifact_edit") != edit.model_dump(mode="json")
    ):
        raise ValueError("The edit recovery changed its admitted binding.")
