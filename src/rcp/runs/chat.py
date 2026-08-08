from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.agents import ChatContext, agent_output_schema
from rcp.agents.prompts import CHAT_MASTER_CONTEXT_VERSION
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
    PATCH_SELF_CHECK_TIMEOUT_SECONDS,
)
from rcp.runs.patch_validator import VALIDATOR_CLIENT_SOURCE
from rcp.runs.shared import (
    _remove_local_tree,
    _safe_stage_name,
    _stage_or_reuse_task_input,
)
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.storage import AgentTaskKind, AgentTaskReceiptRecord, AgentTaskRecord, AppStore
from rcp.transport import RemoteRunStage, StateUnavailable

_CHAT_PROMPT_STATE_ROLE = "chat_prompt_state"


class _ChatMasterSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    master_context_version: int
    master_context_path: str
    contract_key: str
    values: dict[str, object]


class _ChatPromptCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: _ChatMasterSnapshot
    expected_snapshot_sha256: str | None = None


@dataclass(frozen=True)
class _ChatPatchInputs:
    patch_path: str
    watch_path: str
    schema_path: str
    validator_command: str
    validator_mailbox_id: str


def _stage_chat_patch_inputs(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    workspace: Path,
    stage_name: str,
) -> _ChatPatchInputs:
    """Stage stable patch tooling once for a persistent native chat session."""

    schema = json.dumps(agent_output_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    schema_digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()[:16]
    schema_path = _stage_or_reuse_task_input(
        local_stage,
        remote_stage,
        f"chat-patch-schema-{schema_digest}.json",
        schema,
    )
    client_digest = hashlib.sha256(VALIDATOR_CLIENT_SOURCE.encode("utf-8")).hexdigest()[:16]
    validator_client_path = _stage_or_reuse_task_input(
        local_stage,
        remote_stage,
        f"chat-validator-client-{client_digest}.py",
        VALIDATOR_CLIENT_SOURCE,
    )
    mailbox_id = hashlib.sha256(f"chat-validator:{stage_name}".encode()).hexdigest()[:32]
    patch_path = str(workspace / "patch.json")
    validator_command = shlex.join(
        [
            "python3",
            validator_client_path,
            patch_path,
            mailbox_id,
            str(PATCH_SELF_CHECK_TIMEOUT_SECONDS),
            str(workspace),
        ]
    )
    return _ChatPatchInputs(
        patch_path=patch_path,
        watch_path=str(workspace / "watch.json"),
        schema_path=schema_path,
        validator_command=validator_command,
        validator_mailbox_id=mailbox_id,
    )


def _prepare_chat_prompt_state(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    master_context: str,
    contract_key: str,
    values: dict[str, object],
) -> tuple[str | None, dict[str, object] | None, str]:
    """Persist a candidate baseline and return bootstrap path plus compact delta."""

    previous, expected_snapshot_sha256 = _committed_chat_prompt_state(execution, request)
    must_bootstrap = previous is None or previous.contract_key != contract_key
    if must_bootstrap:
        digest = hashlib.sha256(master_context.encode("utf-8")).hexdigest()
        label = f"chat-master-v{CHAT_MASTER_CONTEXT_VERSION}-{digest[:16]}.md"
        master_context_path = _stage_or_reuse_task_input(
            local_stage,
            remote_stage,
            label,
            master_context,
        )
    else:
        master_context_path = previous.master_context_path

    delta = None if previous is None else _chat_context_delta(previous.values, values)
    bootstrap_path: str | None = master_context_path if must_bootstrap else None
    if previous is not None and previous.contract_key != contract_key:
        delta = {
            **(delta or {}),
            "master_context": {
                "version": CHAT_MASTER_CONTEXT_VERSION,
                "path": master_context_path,
            },
        }

    snapshot = _ChatMasterSnapshot(
        master_context_version=CHAT_MASTER_CONTEXT_VERSION,
        master_context_path=master_context_path,
        contract_key=contract_key,
        values=values,
    )
    if execution is not None:
        candidate = _ChatPromptCandidate(
            snapshot=snapshot,
            expected_snapshot_sha256=expected_snapshot_sha256,
        )
        content = candidate.model_dump_json(indent=2)
        execution.store.record_agent_task_contract(
            execution.operation_id,
            _CHAT_PROMPT_STATE_ROLE,
            content,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "chat_master_context",
            {
                "bootstrapped": must_bootstrap,
                "master_context_version": CHAT_MASTER_CONTEXT_VERSION,
                "master_context_path": master_context_path,
                "changed_fields": sorted(delta or {}),
            },
            tier="diagnostic",
        )
    return bootstrap_path, delta, master_context_path


def _committed_chat_prompt_state(
    execution: AgentTaskExecution | None,
    request: RunRequest,
) -> tuple[_ChatMasterSnapshot | None, str | None]:
    if (
        execution is None
        or request.session_id is None
        or request.chat_id is None
        or request.chat_scope is None
    ):
        return None, None
    kind: Literal["node_chat", "project_chat"] = (
        "node_chat" if request.chat_scope == "node" else "project_chat"
    )
    current = execution.store.agent_task(execution.operation_id)
    if current is None:
        raise ValueError("The current chat task record is unavailable.")
    if request.provider is None or request.run_on is None:
        raise ValueError("The chat provider and execution machine must be pinned before launch.")
    baseline = execution.store.validate_chat_session_context_binding(
        request.provider,
        request.run_on,
        request.session_id,
        project_id=current.project_id,
        kind=kind,
        chat_id=request.chat_id,
        node_id=request.node_id,
    )
    if baseline is None:
        if not execution.store.has_chat_native_session_origin(
            current.project_id,
            kind,
            request.chat_id,
            request.node_id,
            request.provider,
            request.run_on,
            request.session_id,
        ):
            raise ValueError(
                "The supplied native session does not belong to this RCP chat, provider, and "
                "execution machine. Start a new chat session instead."
            )
        return None, None
    return (
        _ChatMasterSnapshot.model_validate_json(baseline.snapshot_json),
        baseline.snapshot_sha256,
    )


def _commit_chat_prompt_state(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    native_session_id: str | None,
) -> None:
    """Commit the candidate sent to a provider only after its ordinary turn succeeds."""

    if execution is None or native_session_id is None or request.chat_id is None:
        return
    logical_operation_id = (
        _logical_chat_turn_operation_id(execution.store, execution.operation_id)
        if execution.continuation == "resume"
        else execution.operation_id
    )
    content = execution.store.agent_task_contract(logical_operation_id, _CHAT_PROMPT_STATE_ROLE)
    if content is None:
        return
    candidate = _ChatPromptCandidate.model_validate_json(content)
    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The completed chat task record is unavailable.")
    kind: Literal["node_chat", "project_chat"] = (
        "node_chat" if request.chat_scope == "node" else "project_chat"
    )
    if request.provider is None or request.run_on is None:
        raise ValueError("The chat provider and execution machine are unavailable at commit.")
    snapshot_json = candidate.snapshot.model_dump_json()
    execution.store.commit_chat_session_context(
        provider=request.provider,
        execution_machine=request.run_on,
        native_session_id=native_session_id,
        project_id=task.project_id,
        kind=kind,
        chat_id=request.chat_id,
        node_id=request.node_id,
        protocol_version=candidate.snapshot.master_context_version,
        snapshot_json=snapshot_json,
        snapshot_sha256=hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
        committed_operation_id=execution.operation_id,
        expected_snapshot_sha256=candidate.expected_snapshot_sha256,
    )


def _record_applied_graph_revision(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    native_session_id: str | None,
    applied_revision: int | None,
) -> None:
    """Absorb this conversation's own accepted patch into its committed baseline.

    The baseline is committed before the patch is applied, so without this the next
    turn would announce the conversation's own revision back to it. The delta must
    mean the graph moved for some other reason — a human Sync between turns.
    """

    if (
        execution is None
        or native_session_id is None
        or applied_revision is None
        or request.chat_id is None
        or request.provider is None
        or request.run_on is None
    ):
        return
    # Read the row this turn just committed, not the one the turn started from:
    # a first turn has no request.session_id to look itself up by.
    record = execution.store.chat_session_context(
        request.provider,
        request.run_on,
        native_session_id,
    )
    if record is None:
        return
    baseline = _ChatMasterSnapshot.model_validate_json(record.snapshot_json)
    current = baseline.values.get("current")
    if not isinstance(current, dict) or current.get("graph_revision") == applied_revision:
        return
    snapshot = baseline.model_copy(
        update={
            "values": {
                **baseline.values,
                "current": {**current, "graph_revision": applied_revision},
            }
        }
    )
    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        return
    kind: Literal["node_chat", "project_chat"] = (
        "node_chat" if request.chat_scope == "node" else "project_chat"
    )
    snapshot_json = snapshot.model_dump_json()
    execution.store.commit_chat_session_context(
        provider=request.provider,
        execution_machine=request.run_on,
        native_session_id=native_session_id,
        project_id=task.project_id,
        kind=kind,
        chat_id=request.chat_id,
        node_id=request.node_id,
        protocol_version=snapshot.master_context_version,
        snapshot_json=snapshot_json,
        snapshot_sha256=hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
        committed_operation_id=execution.operation_id,
        expected_snapshot_sha256=record.snapshot_sha256,
    )


def _chat_context_delta(
    previous: dict[str, object],
    current: dict[str, object],
) -> dict[str, object] | None:
    changed = {
        key: value
        for key, value in current.items()
        if key not in previous or previous[key] != value
    }
    removed = sorted(key for key in previous if key not in current)
    if removed:
        changed["removed"] = removed
    return changed or None


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


def _clear_stale_watch(workspace: Path, remote_stage: RemoteRunStage | None) -> None:
    """Drop a prior turn's watcher request from the reusable conversation stage."""

    if remote_stage is not None:
        remote_stage.remove_workspace_file("watch.json")
        return
    (workspace / "watch.json").unlink(missing_ok=True)


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


def _read_watch_request(workspace: Path, remote_stage: RemoteRunStage | None) -> str | None:
    """Read the exact optional watcher deliverable without searching scratch output."""

    if remote_stage is not None:
        if "watch.json" not in remote_stage.list_workspace_files():
            return None
        return remote_stage.read_text(remote_stage.workspace / "watch.json")
    path = workspace / "watch.json"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _existing_watch_digest(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> str | None:
    try:
        text = _read_watch_request(workspace, remote_stage)
    except (OSError, StateUnavailable, ValueError):
        return None
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
            "graph_revision": context.graph_revision,
            "node_id": context.node["id"] if context.node else None,
        },
    )


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


def _chat_read_dirs(
    context: ChatContext,
    remote_stage: RemoteRunStage | None,
    service: ProjectService,
    execution_machine: str,
) -> list[Path]:
    """Provider-generic graph and exact repository roots outside chat scratch."""
    read_dirs = [
        Path(item.path) for item in context.repositories if item.machine == execution_machine
    ]
    if remote_stage is not None:
        assert remote_stage.root is not None
        read_dirs.append(Path(str(remote_stage.root / "inputs")))
        state_repository = service.manifest.repository_map[service.manifest.state.repository]
        if state_repository.machine == execution_machine:
            state_root = Path(state_repository.path) / ".research"
            if str(state_root) not in {str(item) for item in read_dirs}:
                read_dirs.append(state_root)
        return read_dirs
    read_dirs = [item for item in read_dirs if item.exists()]
    read_dirs.append(service.manifest.research_dir)
    return read_dirs


def _work_write_dirs(
    context: ChatContext,
    service: ProjectService,
    execution_machine: str,
    *,
    remote: bool,
) -> list[Path]:
    """On-machine repository pointers supplied to an unrestricted Work provider."""

    del service
    pointers = [item for item in context.repositories if item.machine == execution_machine]
    roots = [Path(item.path) for item in pointers]
    if remote:
        return list(dict.fromkeys(roots))
    missing = [str(path) for path in roots if not path.is_dir()]
    if missing:
        raise StateUnavailable(
            f"Work repository roots are unavailable on the execution machine: {missing}"
        )
    return list(dict.fromkeys(roots))


def _chat_path(service: ProjectService, request: RunRequest) -> Path:
    assert request.chat_id is not None
    return service.chat_path(
        request.chat_id,
        chat_scope=request.chat_scope,
        node_id=request.node_id,
    )


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
            "trigger": request.trigger,
        }
        records = []
        if request.trigger != "watcher":
            records.append(
                {
                    **common,
                    "uuid": str(uuid.uuid4()),
                    "type": "user",
                    "role": "user",
                    "text": request.message,
                    "attachments": [item.model_dump(mode="json") for item in request.attachments],
                }
            )
        records.append(
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
            }
        )
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
            "trigger": request.trigger,
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
