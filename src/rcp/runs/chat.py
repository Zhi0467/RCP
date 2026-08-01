from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from rcp.agents import ChatContext
from rcp.artifacts import (
    ARTIFACT_MEDIA_TYPES,
    AgentArtifactDescriptor,
    descriptor_for,
    list_local_regular_files,
    read_local_regular_file,
    validate_artifact_bytes,
)
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.limits import (
    CHAT_ARTIFACT_MAX_COUNT,
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    CHAT_ARTIFACT_MAX_TOTAL_BYTES,
)
from rcp.runs.shared import (
    _remove_local_tree,
    _safe_stage_name,
    _session_bundle_relative_path,
)
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.storage import AgentTaskKind, AgentTaskReceiptRecord, AgentTaskRecord, AppStore
from rcp.transport import RemoteRunStage, StateUnavailable


def _clear_stale_patch(workspace: Path, remote_stage: RemoteRunStage | None) -> None:
    """Drop a previous turn's `patch.json` from a conversation's scratch folder.

    Fails the turn if it cannot: a scratch folder is reused across a conversation,
    so a survivor would be read as this turn's patch and applied under this turn's
    authorization.
    """
    if remote_stage is not None:
        remote_stage.remove_workspace_file("patch.json")
        return
    (workspace / "patch.json").unlink(missing_ok=True)


def _prepare_local_artifact_directory(
    stage: Path,
    scope_id: str,
    *,
    reuse: bool,
) -> Path:
    """Create an empty exact output boundary, or require it for Resume."""
    if _safe_stage_name(scope_id) != scope_id:
        raise ValueError("artifact scope contains unsupported characters")
    turns = stage / "turns"
    if os.path.lexists(turns) and (turns.is_symlink() or not turns.is_dir()):
        raise ValueError("artifact parent is unsafe")
    turns.mkdir(mode=0o700, exist_ok=True)
    scope = turns / scope_id
    target = scope / "artifacts"
    if reuse:
        if scope.is_symlink() or not scope.is_dir() or target.is_symlink() or not target.is_dir():
            raise ValueError(
                "The saved artifact directory is unavailable; retry this chat turn instead."
            )
        return target
    _remove_local_tree(scope, turns)
    target.mkdir(parents=True, mode=0o700)
    return target


def _discover_chat_artifacts(
    execution: AgentTaskExecution | None,
    scope_id: str,
    directory: Path,
    remote_stage: RemoteRunStage | None,
) -> list[AgentArtifactDescriptor]:
    """Discover bounded attachments without making their validity part of chat success."""
    ignored: dict[str, int] = {}

    def ignore(reason: str) -> None:
        ignored[reason] = ignored.get(reason, 0) + 1

    try:
        candidates = (
            remote_stage.list_artifact_files(scope_id)
            if remote_stage is not None
            else list_local_regular_files(directory)
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        _record_artifact_discovery_receipt(
            execution,
            attached=0,
            candidates=0,
            ignored={"discovery_unavailable": 1},
            detail=str(exc),
        )
        return []

    attached: list[AgentArtifactDescriptor] = []
    total_bytes = 0
    allowed_candidates = 0
    for name, advertised_size in sorted(candidates):
        if Path(name).suffix.casefold() not in ARTIFACT_MEDIA_TYPES:
            ignore("unsupported_type")
            continue
        if allowed_candidates >= CHAT_ARTIFACT_MAX_COUNT:
            ignore("count_limit")
            continue
        allowed_candidates += 1
        if advertised_size < 0 or advertised_size > CHAT_ARTIFACT_MAX_FILE_BYTES:
            ignore("file_size_limit")
            continue
        if total_bytes + advertised_size > CHAT_ARTIFACT_MAX_TOTAL_BYTES:
            ignore("total_size_limit")
            continue
        try:
            data = (
                remote_stage.read_artifact_bytes(
                    scope_id, name, max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES
                )
                if remote_stage is not None
                else read_local_regular_file(
                    directory, name, max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES
                )
            )
            if total_bytes + len(data) > CHAT_ARTIFACT_MAX_TOTAL_BYTES:
                ignore("total_size_limit")
                continue
            media_type = validate_artifact_bytes(name, data)
            descriptor = descriptor_for(scope_id, name)
            if descriptor.media_type != media_type:
                raise ValueError("artifact media type mismatch")
        except (FileNotFoundError, OSError, StateUnavailable, ValueError):
            ignore("invalid_or_unavailable")
            continue
        attached.append(descriptor)
        total_bytes += len(data)
    _record_artifact_discovery_receipt(
        execution,
        attached=len(attached),
        candidates=len(candidates),
        ignored=ignored,
    )
    return attached


def _record_artifact_discovery_receipt(
    execution: AgentTaskExecution | None,
    *,
    attached: int,
    candidates: int,
    ignored: dict[str, int],
    detail: str | None = None,
) -> None:
    if execution is None:
        return
    payload: dict[str, object] = {
        "candidate_count": candidates,
        "attached_count": attached,
        "ignored": ignored,
    }
    if detail:
        payload["detail"] = " ".join(detail.split())[:400]
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "artifact_discovery",
        payload,
        tier="diagnostic",
    )


def _read_chat_patch(workspace: Path, remote_stage: RemoteRunStage | None) -> str | None:
    """Read `patch.json` if the agent wrote one. Absence is the normal case.

    Unlike an ingest run, chat does not hunt the scratch folder for a stray JSON
    file — with no patch expected, that search would misread scratch work as a
    graph change. A file that exists but cannot be read raises: a written patch
    that silently reads as "no patch" is the one outcome nobody can see.
    """
    if remote_stage is not None:
        if "patch.json" not in remote_stage.list_workspace_files():
            return None
        return remote_stage.read_text(remote_stage.workspace / "patch.json")
    path = workspace / "patch.json"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _record_chat_context_receipt(
    execution: AgentTaskExecution | None,
    context: ChatContext,
    *,
    surface: AgentSurface,
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "chat_context_assembled",
        {
            "surface": surface,
            "repository_count": len(context.repositories),
            "relation_count": len(context.relations),
            "conversation_count": len(context.conversations),
            "conversations_truncated": context.conversations_truncated,
            "graph_revision": context.graph_revision,
            "node_id": context.node["id"] if context.node else None,
        },
    )


def _first_chat_base_revision(execution: AgentTaskExecution | None, fallback: int) -> int:
    """The graph revision the reasoning being resumed was actually written against.

    A resume is a *new* task whose parent holds the original attempt, so the
    revision has to be followed up the chain — reading only this attempt's
    receipts would find the revision it just re-assembled and wave the stale
    patch through. The walk stops at the first ancestor that was not itself a
    resume, because a retry starts fresh reasoning at its own revision.
    """
    if execution is None:
        return fallback
    store = execution.store
    operation_id: str | None = execution.operation_id
    seen: set[str] = set()
    first = True
    lineage_project: str | None = None
    lineage_kind: AgentTaskKind | None = None
    expected_attempt: int | None = None
    while operation_id is not None:
        if operation_id in seen:
            raise _resume_lineage_error("the task ancestry contains a cycle")
        seen.add(operation_id)
        record = store.agent_task(operation_id)
        if record is None:
            raise _resume_lineage_error(f"task {operation_id!r} is missing")
        if first:
            lineage_project = record.project_id
            lineage_kind = record.kind
        elif record.project_id != lineage_project or record.kind != lineage_kind:
            raise _resume_lineage_error(
                f"task {operation_id!r} crosses a project or task-kind boundary"
            )
        if expected_attempt is not None and record.attempt != expected_attempt:
            raise _resume_lineage_error(f"task {operation_id!r} has inconsistent attempt ancestry")
        receipts = store.agent_task_receipts(operation_id)
        resumed = _attempt_was_resumed(receipts, record)
        if first and not resumed:
            raise _resume_lineage_error("the current task is not marked as a Resume")
        if not resumed:
            return _assembled_graph_revision(receipts, operation_id)
        assert record.parent_operation_id is not None
        expected_attempt = record.attempt - 1
        if expected_attempt < 1:
            raise _resume_lineage_error(
                f"task {record.operation_id!r} has an invalid attempt number"
            )
        operation_id = record.parent_operation_id
        first = False
    raise _resume_lineage_error("the task ancestry ended without an original attempt")


def _logical_chat_turn_operation_id(store: AppStore, operation_id: str) -> str:
    """Resume shares its original turn directory; Retry begins a fresh one."""
    seen: set[str] = set()
    current_id = operation_id
    project_id: str | None = None
    kind: AgentTaskKind | None = None
    while current_id not in seen:
        seen.add(current_id)
        record = store.agent_task(current_id)
        if record is None:
            raise ValueError("chat task provenance is missing")
        if project_id is None:
            project_id = record.project_id
            kind = record.kind
        elif record.project_id != project_id or record.kind != kind:
            raise ValueError("chat task provenance crosses a task boundary")
        resumed = _attempt_was_resumed(store.agent_task_receipts(current_id), record)
        if not resumed:
            return current_id
        if record.parent_operation_id is None:
            raise ValueError("resumed chat task has no parent")
        current_id = record.parent_operation_id
    raise ValueError("chat task provenance contains a cycle")


def _resume_lineage_error(detail: str) -> ValueError:
    return ValueError(
        "Cannot safely resume this chat because "
        f"{detail}. Retry the turn from the beginning instead."
    )


def _attempt_was_resumed(receipts: list[AgentTaskReceiptRecord], record: AgentTaskRecord) -> bool:
    created = [receipt for receipt in receipts if receipt.category == "operation_created"]
    if len(created) != 1:
        raise _resume_lineage_error(
            f"task {record.operation_id!r} has no unique operation-created receipt"
        )
    payload = created[0].payload
    resumed = payload.get("resumed")
    has_parent = payload.get("has_parent")
    attempt = payload.get("attempt")
    if (
        not isinstance(resumed, bool)
        or not isinstance(has_parent, bool)
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt != record.attempt
        or payload.get("kind") != record.kind
        or record.kind not in {"node_chat", "project_chat"}
    ):
        raise _resume_lineage_error(
            f"task {record.operation_id!r} has invalid operation-created provenance"
        )
    actual_has_parent = record.parent_operation_id is not None
    if has_parent != actual_has_parent or (resumed and not actual_has_parent):
        raise _resume_lineage_error(
            f"task {record.operation_id!r} has inconsistent parent provenance"
        )
    return resumed


def _assembled_graph_revision(receipts: list[AgentTaskReceiptRecord], operation_id: str) -> int:
    assembled = [receipt for receipt in receipts if receipt.category == "chat_context_assembled"]
    if not assembled:
        raise _resume_lineage_error(
            f"the original attempt {operation_id!r} has no assembled chat context"
        )
    revision = assembled[0].payload.get("graph_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise _resume_lineage_error(
            f"the original attempt {operation_id!r} has an invalid graph revision"
        )
    return revision


def _chat_stage_name(
    service: ProjectService,
    request: RunRequest,
    execution: AgentTaskExecution | None,
) -> str:
    """Name a reusable chat workspace inside one stable project boundary."""
    if not request.chat_id:
        raise ValueError("Chat requires a chat_id")
    if execution is not None:
        task = execution.store.agent_task(execution.operation_id)
        if task is None or not task.project_id:
            raise ValueError(
                "Cannot identify this chat's project workspace; retry the turn from the beginning."
            )
        project_identity = f"task-project\0{task.project_id}"
    else:
        # Direct streams have no catalog task record. The canonical workspace
        # location is the stable project identity available at this boundary.
        project_identity = f"canonical-workspace\0{service.history.workspace.location}"
    project_key = hashlib.sha256(project_identity.encode()).hexdigest()[:16]
    return _safe_stage_name(f"chat-{project_key}-{request.chat_id}")


def _validated_remote_chat_resume_stage(
    execution: AgentTaskExecution | None,
    execution_host: str,
    stage_name: str,
) -> str:
    if execution is None or not execution.stage_root:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage is missing. "
            "Retry the turn from the beginning."
        )
    if (execution.stage_host or "") != execution_host:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage host does not match "
            "the execution machine. Retry the turn from the beginning."
        )
    expected = str(PurePosixPath("/tmp") / f"rcp-run.{stage_name}")
    if execution.stage_root != expected:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage belongs to a different "
            "project or conversation. Retry the turn from the beginning."
        )
    return execution.stage_root


def _validated_local_chat_resume_stage(
    execution: AgentTaskExecution | None,
    expected: Path,
) -> Path:
    if execution is None or not execution.stage_root:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage is missing. "
            "Retry the turn from the beginning."
        )
    if execution.stage_host:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage host does not match "
            "the execution machine. Retry the turn from the beginning."
        )
    stored = Path(execution.stage_root)
    if stored.absolute() != expected.absolute() or stored.is_symlink() or not stored.is_dir():
        raise ValueError(
            "Cannot safely resume this chat because its saved stage belongs to a different "
            "project or conversation, or is unavailable. Retry the turn from the beginning."
        )
    return stored


def _chat_native_checkpoint_available(
    execution: AgentTaskExecution | None,
    native_session_id: str | None,
) -> bool:
    if execution is None:
        return bool(native_session_id)
    task = execution.store.agent_task(execution.operation_id)
    return task is not None and bool(task.native_session_id)


def _chat_read_dirs(
    context: ChatContext,
    remote_stage: RemoteRunStage | None,
    service: ProjectService,
    execution_machine: str,
    conversation_projection: Path | PurePosixPath | None,
) -> list[Path]:
    """Provider-generic read roots outside the chat scratch folder."""
    read_dirs = [
        Path(item.path) for item in context.repositories if item.machine == execution_machine
    ]
    if conversation_projection is None:
        raise StateUnavailable(
            "The saved conversation projection is unavailable; retry this chat turn."
        )
    if remote_stage is not None:
        assert remote_stage.root is not None
        read_dirs.append(Path(str(remote_stage.root / "inputs")))
        state_repository = service.manifest.repository_map[service.manifest.state.repository]
        if state_repository.machine == execution_machine:
            state_root = Path(state_repository.path) / ".research"
            if str(state_root) not in {str(item) for item in read_dirs}:
                read_dirs.append(state_root)
        read_dirs.append(Path(str(conversation_projection)))
        return read_dirs
    read_dirs = [item for item in read_dirs if item.exists()]
    read_dirs.append(service.manifest.research_dir)
    projection = Path(conversation_projection)
    if not projection.is_dir():
        raise StateUnavailable(
            "The saved conversation projection is unavailable; retry this chat turn."
        )
    read_dirs.append(projection)
    return read_dirs


def _work_write_dirs(
    context: ChatContext,
    service: ProjectService,
    execution_machine: str,
    *,
    remote: bool,
) -> list[Path]:
    """Exact on-machine repository roots authorized by this Work turn."""

    pointers = [
        item for item in context.repositories if item.machine == execution_machine and not item.host
    ]
    state_repository = service.manifest.repository_map[service.manifest.state.repository]
    if state_repository.machine == execution_machine:
        canonical_root = PurePosixPath(posixpath.normpath(state_repository.path))
        canonical_research = canonical_root / ".research"
        for pointer in pointers:
            candidate = PurePosixPath(posixpath.normpath(pointer.path))
            if _overlaps_canonical_state(candidate, canonical_root, canonical_research):
                raise StateUnavailable(
                    f"Work repository root {pointer.path!r} overlaps canonical RCP state; "
                    "select the exact state repository root or a non-overlapping repository."
                )
        if not remote:
            resolved_root = Path(state_repository.path).resolve()
            resolved_research = service.manifest.research_dir.resolve()
            for pointer in pointers:
                resolved_candidate = Path(pointer.path).resolve()
                if _overlaps_canonical_state(
                    resolved_candidate,
                    resolved_root,
                    resolved_research,
                ):
                    raise StateUnavailable(
                        f"Work repository root {pointer.path!r} resolves across canonical RCP "
                        "state; select the exact state repository root or a non-overlapping "
                        "repository."
                    )
    roots = [Path(item.path) for item in pointers]
    if remote:
        return list(dict.fromkeys(roots))
    missing = [str(path) for path in roots if not path.is_dir()]
    if missing:
        raise StateUnavailable(
            f"Work repository roots are unavailable on the execution machine: {missing}"
        )
    return list(dict.fromkeys(roots))


def _overlaps_canonical_state(
    candidate: Path | PurePosixPath,
    canonical_root: Path | PurePosixPath,
    canonical_research: Path | PurePosixPath,
) -> bool:
    inside_research = candidate == canonical_research or canonical_research in candidate.parents
    ancestor_of_state = candidate != canonical_root and candidate in canonical_root.parents
    return inside_research or ancestor_of_state


def _project_chat_conversations(
    context: ChatContext,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> tuple[ChatContext, Path | PurePosixPath]:
    """Copy only authorized on-machine conversations into the chat stage."""
    on_machine = [pointer for pointer in context.conversations if not pointer.host]
    unavailable = context.conversations_unreachable + len(context.conversations) - len(on_machine)
    entries = [
        (pointer.path, _session_bundle_relative_path(pointer).as_posix()) for pointer in on_machine
    ]
    if remote_stage is not None:
        projected_paths = remote_stage.replace_conversation_inputs(entries)
        projection = remote_stage.require_conversation_inputs()
    else:
        if local_stage is None:
            raise RuntimeError("local chat stage is unavailable")
        projection = _replace_local_conversation_inputs(local_stage, entries)
        projected_paths = [str(projection / relative) for _source, relative in entries]
    conversations = [
        pointer.model_copy(update={"path": path})
        for pointer, path in zip(on_machine, projected_paths, strict=True)
    ]
    return context.model_copy(
        update={
            "conversations": conversations,
            "conversations_unreachable": unavailable,
        }
    ), projection


def _replace_local_conversation_inputs(stage: Path, sources: list[tuple[str, str]]) -> Path:
    """Replace ``inputs/conversations`` with real copies, failing before launch."""
    parent = stage / "inputs"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / "conversations"
    staged = Path(tempfile.mkdtemp(prefix=".conversations-", dir=parent))
    try:
        for source_text, relative in sources:
            source = Path(source_text)
            if not source.is_file():
                raise StateUnavailable(f"Conversation input is unavailable: {source}")
            output = staged / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output, follow_symlinks=True)
            output.chmod(0o400)
        for directory in sorted(
            (item for item in staged.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(0o500)
        staged.chmod(0o500)
        _remove_local_tree(target, parent)
        os.replace(staged, target)
    except Exception:
        if os.path.lexists(staged):
            _remove_local_tree(staged, parent)
        raise
    return target


def _rebind_chat_conversations(
    context: ChatContext,
    projection: Path | PurePosixPath,
    *,
    verify_local: bool,
) -> ChatContext:
    available = [pointer for pointer in context.conversations if not pointer.host]
    rebound = [
        pointer.model_copy(
            update={"path": str(projection / _session_bundle_relative_path(pointer))}
        )
        for pointer in available
    ]
    if verify_local and any(not Path(pointer.path).is_file() for pointer in rebound):
        raise StateUnavailable(
            "The saved grouped conversation inputs are incomplete; retry this chat turn."
        )
    return context.model_copy(
        update={
            "conversations": rebound,
            "conversations_unreachable": (
                context.conversations_unreachable + len(context.conversations) - len(available)
            ),
        }
    )


def _chat_conversation_roots(context: ChatContext) -> dict[str, str]:
    roots: dict[str, str] = {}
    for pointer in context.conversations:
        path = PurePosixPath(pointer.path)
        root = str(path.parents[2])
        previous = roots.setdefault(pointer.provider, root)
        if previous != root:
            raise ValueError(f"provider {pointer.provider!r} has more than one visible root")
    return roots


def _saved_chat_conversation_projection(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> Path | PurePosixPath:
    """Recover, but never refresh, the exact projection used by a resumed turn."""
    if remote_stage is not None:
        return remote_stage.require_conversation_inputs()
    if local_stage is None:
        raise RuntimeError("local chat stage is unavailable")
    projection = local_stage / "inputs" / "conversations"
    if not projection.is_dir():
        raise StateUnavailable(
            "The saved conversation projection is unavailable; retry this chat turn instead."
        )
    return projection


def _cleanup_chat_conversation_projection(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    execution: AgentTaskExecution | None,
) -> None:
    """Best-effort terminal cleanup of only ``inputs/conversations``."""
    try:
        if remote_stage is not None:
            remote_stage.remove_conversation_inputs()
            return
        if local_stage is None:
            return
        inputs = local_stage / "inputs"
        _remove_local_tree(inputs / "conversations", inputs)
    except (OSError, StateUnavailable, ValueError) as exc:
        if execution is not None:
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Conversation projection cleanup could not reclaim its copies: {exc}",
                level="warning",
            )


def _chat_path(service: ProjectService, request: RunRequest) -> Path:
    assert request.chat_id is not None
    return service.chat_path(
        request.chat_id,
        chat_scope=request.chat_scope,
        node_id=request.node_id,
    )


def _known_chat_session(service: ProjectService, request: RunRequest) -> bool:
    if not request.session_id:
        return True
    path = _chat_path(service, request)
    if not path.is_file():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if (
                record.get("nativeSessionId") == request.session_id
                and record.get("provider") == request.provider
                and record.get("nodeId") == request.node_id
                and record.get("chatScope", "node") == request.chat_scope
                and record.get("executionMachine") == request.run_on
                and record.get("model") == (request.model or "provider-default")
                and record.get("reasoning") == request.reasoning
            ):
                return True
    except (OSError, json.JSONDecodeError):
        return False
    return False


def _append_chat_exchange(
    service: ProjectService,
    request: RunRequest,
    answer: str,
    native_session_id: str | None,
    applied_revision: int | None,
    *,
    graph_update: GraphUpdateResult | None = None,
    execution: AgentTaskExecution | None = None,
) -> None:
    assert request.message is not None
    assert request.chat_id is not None
    with service.history.workspace.transaction():
        path = _chat_path(service, request)
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat()
        common = {
            "sessionId": request.chat_id,
            "nativeSessionId": native_session_id,
            "nodeId": request.node_id,
            "chatScope": request.chat_scope,
            "provider": request.provider,
            "model": request.model or "provider-default",
            "reasoning": request.reasoning,
            "executionMachine": request.run_on,
            "cwd": str(service.manifest.research_dir.parent),
            "timestamp": timestamp,
            "operationId": execution.operation_id if execution is not None else None,
            "mode": request.mode,
        }
        records = [
            {
                **common,
                "uuid": str(uuid.uuid4()),
                "type": "user",
                "role": "user",
                "text": request.message,
            },
            {
                **common,
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "role": "assistant",
                "text": answer,
                "appliedRevision": applied_revision,
                "graphUpdate": (
                    graph_update.model_dump(mode="json") if graph_update is not None else None
                ),
            },
        ]
        lock_path = service.history.workspace.root / ".chat.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    for record in records:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        service.history.workspace.publish([path.relative_to(service.history.workspace.root)])
    # The transcript is itself an indexed app_chat source.
    service.invalidate_source_index()


def _append_chat_graph_receipt(
    service: ProjectService,
    request: RunRequest,
    native_session_id: str | None,
    graph_update: GraphUpdateResult,
    execution: AgentTaskExecution,
) -> None:
    """Append only a durable receipt for a manual patch repair continuation."""

    assert request.chat_id is not None
    with service.history.workspace.transaction():
        path = _chat_path(service, request)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "sessionId": request.chat_id,
            "nativeSessionId": native_session_id,
            "nodeId": request.node_id,
            "chatScope": request.chat_scope,
            "provider": request.provider,
            "model": request.model or "provider-default",
            "reasoning": request.reasoning,
            "executionMachine": request.run_on,
            "cwd": str(service.manifest.research_dir.parent),
            "timestamp": datetime.now(UTC).isoformat(),
            "operationId": execution.operation_id,
            "mode": "work",
            "uuid": str(uuid.uuid4()),
            "type": "assistant",
            "role": "assistant",
            "text": "",
            "appliedRevision": graph_update.applied_revision,
            "graphUpdate": graph_update.model_dump(mode="json"),
        }
        lock_path = service.history.workspace.root / ".chat.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        service.history.workspace.publish([path.relative_to(service.history.workspace.root)])
    service.invalidate_source_index()
