from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from rcp.agents import ChatContext, agent_output_schema
from rcp.agents.command_mailbox import StagedCommandMailbox
from rcp.agents.continuation_prompt import (
    LaunchPhase,
    MasterRef,
    PromptNode,
    changed_since_master,
    classify,
    master_key,
)
from rcp.agents.prompts import CHAT_MASTER_CONTEXT_VERSION, chat_master_contract_key
from rcp.agents.write_scope import (
    ProjectWriteScope,
    WritableRepositoryRoot,
    resolve_project_write_scope,
)
from rcp.artifact_comments import (
    CROPPABLE_MEDIA_TYPES,
    crop_region,
    croppable_frame,
    supports_comments,
)
from rcp.artifacts import (
    AgentArtifactDescriptor,
    classify_artifact_bytes,
    descriptor_for,
    list_local_regular_files,
    read_local_regular_file,
)
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.conversation_worktrees import validate_worktree_binding
from rcp.core.models import ConversationWorktreeBinding
from rcp.limits import (
    CHAT_ARTIFACT_MAX_COUNT,
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    CHAT_ARTIFACT_MAX_TOTAL_BYTES,
    PATCH_SELF_CHECK_TIMEOUT_SECONDS,
    RUN_STAGE_RETENTION_DAYS,
)
from rcp.providers import AgentCapability
from rcp.rcp_home import rcp_temp_dir
from rcp.runs.patch_validator import stage_patch_validation_mailbox
from rcp.runs.session_master import (
    continuation_session_master,
    read_legacy_session_master,
    record_session_master,
    recorded_master_values,
    session_master_label,
    stage_session_master,
)
from rcp.runs.shared import (
    _remove_local_tree,
    _safe_stage_name,
    _stage_or_reuse_task_input,
    _stage_task_contract,
    _task_token,
    _touch_local_stage,
)
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.storage import (
    AgentTaskKind,
    AgentTaskReceiptRecord,
    AgentTaskRecord,
    AppStore,
    Artifact,
    ArtifactVersionConflict,
)
from rcp.storage.artifact_models import ArtifactOperationConflict
from rcp.storage.artifacts import ArtifactByteLimitError
from rcp.storage.question_models import QuestionRecord
from rcp.transport import (
    RemoteRunStage,
    RunStageMailbox,
    StateUnavailable,
    clear_turn_handoff_files,
)
from rcp.transport.remote_stage_root import stage_artifact
from rcp.transport.run_stage import remote_stage_name

if TYPE_CHECKING:
    from rcp.runs.auto_research import AutoResearchRunRequest

_CHAT_PROMPT_STATE_ROLE = "chat_prompt_state"


class _ChatMasterSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    master_context_version: int
    master_context_path: str
    contract_key: str
    values: dict[str, object]
    # Where the exact master bytes are recorded; absent on snapshots from before the record.
    master_operation_id: str | None = None
    master_sha256: str | None = None


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
    validator_staged: StagedCommandMailbox
    command_client: str

    def prompt_values(self) -> dict[str, str]:
        """The Patch values a session is told once and sent again only when they change."""

        return {
            "path": self.patch_path,
            "watch_path": self.watch_path,
            "schema_path": self.schema_path,
            "command_client": self.command_client,
        }


@dataclass(frozen=True)
class StagedArtifactContext:
    pointer: dict[str, object]
    protected_write_paths: tuple[str, ...] = ()


def _stage_chat_patch_inputs(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    workspace: Path,
    stage_name: str,
    task_id: str,
    turn_id: str,
    broker: bool = False,
    episode_id: str | None = None,
) -> _ChatPatchInputs:
    """Stage stable schema plus one turn-scoped unified validator credential."""

    schema = json.dumps(agent_output_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    schema_digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()[:16]
    schema_path = _stage_or_reuse_task_input(
        local_stage,
        remote_stage,
        f"chat-patch-schema-{schema_digest}.json",
        schema,
    )
    patch_path = str(workspace / "patch.json")
    validator_staged = stage_patch_validation_mailbox(
        local_stage=workspace if remote_stage is None else None,
        remote_stage=remote_stage,
        local_input_stage=local_stage if remote_stage is None else None,
        task_id=task_id,
        turn_id=turn_id,
        timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
        authority="broker" if broker else "validate_only",
        episode_id=episode_id,
    )
    validator_command = validator_staged.client_command("validate", patch_path)
    return _ChatPatchInputs(
        patch_path=patch_path,
        watch_path=str(workspace / "watch.json"),
        schema_path=schema_path,
        validator_command=validator_command,
        validator_mailbox_id=validator_staged.credential.mailbox_id,
        validator_staged=validator_staged,
        command_client=validator_staged.client_command(),
    )


def chat_prompt_values(
    context: ChatContext,
    request: RunRequest,
    *,
    repositories: list[dict[str, object]],
    compute_profiles: list[dict[str, str]],
    skill_pointers: list[dict[str, object]],
    experiment_watcher_resources: list[dict[str, object]],
    workspace: str,
    patch: Mapping[str, object] | None = None,
    work: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Every value a conversation master states that can differ between launches.

    Discuss, Work, and their recoveries and corrections build their values here, so a
    continuation's changed values are always taken against the same shape. ``patch``
    holds the Patch outputs and command client, and ``work`` what only a Work turn
    resolves: its write roots and its launch facts. A Discuss launch keeps both as its
    session last had them.
    """

    values: dict[str, object] = {
        "project": {"name": context.project_name},
        "settings": {
            "provider": request.provider,
            "model": request.model,
            "reasoning": request.reasoning,
            "run_on": request.run_on,
        },
        "current": {
            "ontology_path": f"{context.graph_path}#ontology",
            "graph_revision": context.graph_revision,
            "graph_path": context.graph_path,
            "research_path": context.research_md_path,
            "focused_node_id": str(context.node["id"]) if context.node else None,
            "introduction_path": context.introduction_path,
            "experiment_watcher_resources": experiment_watcher_resources,
        },
        "repositories": repositories,
        "compute": {"active": compute_profiles},
        "skills": [
            {
                "id": item.get("id"),
                "kind": item.get("kind", "skill"),
                "version": item.get("version"),
                "path": item.get("path"),
            }
            for item in skill_pointers
        ],
        "workspace": {"path": workspace},
    }
    if patch is not None:
        values["patch"] = dict(patch)
    if work is not None:
        values["work"] = dict(work)
    return values


def retained_work_values(values: Mapping[str, object] | None) -> dict[str, object]:
    """The Work-only entries a Discuss launch keeps unchanged from its session's values."""

    return {key: values[key] for key in ("patch", "work") if values and key in values}


def _stage_chat_turn_contract(
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    prompt: str,
) -> str:
    """Record a chat turn's inline prompt as its original contract.

    A later Resume or Retry finds it here, not in the bounded launch receipt.
    """

    contract_path, _ = _stage_task_contract(
        local_stage,
        remote_stage,
        f"task-{_task_token(execution)}-prompt.md",
        prompt,
        execution=execution,
        role="chat_turn",
    )
    return contract_path


def _with_graph_revision(
    master_values: dict[str, object], baseline: dict[str, object]
) -> dict[str, object]:
    """The master's values, with the graph revision the last committed turn ended on."""

    current = baseline.get("current")
    if not isinstance(current, dict) or "graph_revision" not in current:
        return master_values
    master_current = master_values.get("current")
    return {
        **master_values,
        "current": {
            **(master_current if isinstance(master_current, dict) else {}),
            "graph_revision": current["graph_revision"],
        },
    }


def _prepare_chat_prompt_state(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    master_context: str,
    contract_key: str,
    values: dict[str, object],
) -> tuple[PromptNode, MasterRef, dict[str, object] | None]:
    """Persist a candidate baseline and return the turn's node, master, and compact delta."""

    previous, expected_snapshot_sha256 = _committed_chat_prompt_state(execution, request)
    node = classify(
        LaunchPhase(
            session_id=request.session_id,
            phase="wake"
            if execution is not None and execution.continuation == "watcher_wake"
            else "turn",
        )
    )
    master_operation_id: str | None = None
    master_sha256: str | None = None
    master_source = "rendered"
    if previous is not None and previous.contract_key == contract_key:
        if previous.master_operation_id is not None and previous.master_sha256 is not None:
            master_operation_id = previous.master_operation_id
            master_sha256 = previous.master_sha256
            master_source = "restored"
        elif execution is not None:
            legacy = read_legacy_session_master(
                local_stage=local_stage,
                remote_stage=remote_stage,
                path=previous.master_context_path,
            )
            if legacy is not None:
                master_operation_id = execution.operation_id
                master_sha256 = record_session_master(
                    execution.store, execution.operation_id, legacy
                )
                master_source = "legacy_capture"
    must_bootstrap = master_operation_id is None
    if not must_bootstrap:
        assert execution is not None and previous is not None and master_sha256 is not None
        master_context_path = stage_session_master(
            execution.store,
            local_stage=local_stage,
            remote_stage=remote_stage,
            operation_id=master_operation_id,
            sha256=master_sha256,
            path=previous.master_context_path,
        )
    else:
        master_context_path = _stage_or_reuse_task_input(
            local_stage,
            remote_stage,
            session_master_label(f"chat-master-v{CHAT_MASTER_CONTEXT_VERSION}", master_context),
            master_context,
        )
        if execution is not None:
            master_operation_id = execution.operation_id
            master_sha256 = record_session_master(
                execution.store, execution.operation_id, master_context, values=values
            )

    # The delta is a complete overlay on the master, never on the previous turn: a value a
    # later turn leaves out is the master's, so an agent that compacted and reread the
    # master reads the same current state as one that kept every turn.
    # The graph revision is the one exception: it signals a graph change someone else made,
    # so it is compared with the last committed turn, which absorbed this chat's own Apply.
    delta = None
    if not must_bootstrap:
        assert execution is not None and master_operation_id is not None
        reference = recorded_master_values(execution.store, master_operation_id)
        if reference is not None and previous is not None:
            reference = _with_graph_revision(reference, previous.values)
        delta = changed_since_master(
            MasterRef(path=master_context_path, bootstrap=False, values=reference), values
        )
    replaces = previous is not None and must_bootstrap
    if replaces:
        delta = {
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
        master_operation_id=master_operation_id,
        master_sha256=master_sha256,
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
                "node": node,
                "bootstrapped": must_bootstrap,
                "master_context_version": CHAT_MASTER_CONTEXT_VERSION,
                "master_context_path": master_context_path,
                "master_source": master_source,
                "changed_fields": sorted(delta or {}),
            },
            tier="diagnostic",
        )
    master = MasterRef(
        path=master_context_path,
        bootstrap=must_bootstrap,
        replaces=replaces,
        values=previous.values if previous is not None else None,
    )
    return node, master, delta


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


def chat_continuation_master(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    *,
    session_id: str,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    policy_version: str,
    ontology_extensions: bool,
    render: Callable[[], str],
    values: dict[str, object] | None = None,
    force_bootstrap: bool = False,
) -> MasterRef:
    """The master a Discuss or Work continuation points to in this native session.

    A session with a committed chat baseline keeps its chat master, restored from its
    record. Any other session continues from the master recorded for it under the owner's
    key, or bootstraps a freshly rendered owner contract. A forced bootstrap reopens the
    same master rather than implying that its content changed. A launch with no task
    record has nowhere to find a master, so it always bootstraps one.
    """

    if execution is None:
        content = render()
        path = _stage_or_reuse_task_input(
            local_stage, remote_stage, session_master_label(policy_version, content), content
        )
        return MasterRef(path=path, bootstrap=True, values=values)
    previous, _ = _committed_chat_prompt_state(
        execution, request.model_copy(update={"session_id": session_id})
    )
    if (
        previous is not None
        and previous.contract_key
        == chat_master_contract_key(ontology_extensions=ontology_extensions)
        and previous.master_operation_id is not None
        and previous.master_sha256 is not None
    ):
        path = stage_session_master(
            execution.store,
            local_stage=local_stage,
            remote_stage=remote_stage,
            operation_id=previous.master_operation_id,
            sha256=previous.master_sha256,
            path=previous.master_context_path,
        )
        return MasterRef(
            path=path,
            bootstrap=force_bootstrap,
            values=recorded_master_values(execution.store, previous.master_operation_id),
        )
    master = continuation_session_master(
        execution,
        local_stage=local_stage,
        remote_stage=remote_stage,
        native_session_id=session_id,
        label_prefix=policy_version,
        key=master_key(policy_version, ontology_extensions=ontology_extensions),
        render=render,
        values=values,
    )
    if force_bootstrap and not master.bootstrap:
        return replace(master, bootstrap=True)
    return master


def _retained_chat_patch_values(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    *,
    ontology_extensions: bool,
) -> dict[str, object] | None:
    """Reuse an inactive Discuss Patch contract and Work values without issuing a credential."""

    previous, _ = _committed_chat_prompt_state(execution, request)
    if previous is None or previous.contract_key != chat_master_contract_key(
        ontology_extensions=ontology_extensions
    ):
        return None
    value = previous.values.get("patch")
    if not isinstance(value, dict):
        return None
    names = ("path", "watch_path", "schema_path", "command_client")
    if not all(isinstance(value.get(name), str) and value[name] for name in names):
        return None
    return retained_work_values(previous.values)


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
    committed = execution.store.validate_chat_session_context_binding(
        request.provider,
        request.run_on,
        native_session_id,
        project_id=task.project_id,
        kind=kind,
        chat_id=request.chat_id,
        node_id=request.node_id,
    )
    if committed is not None and committed.committed_operation_id == execution.operation_id:
        return
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


def _clear_stale_turn_handoffs(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> None:
    """Drop patch, watcher, and message output from a prior reusable-stage turn.

    Fails the turn if it cannot: a scratch folder is reused across a conversation,
    so a survivor could be consumed under this turn's authorization or attribution.
    """

    mailbox = RunStageMailbox.for_stage(
        local_stage=workspace if remote_stage is None else None,
        remote_stage=remote_stage,
    )
    clear_turn_handoff_files(mailbox)


def _prepare_local_chat_workspace(
    stage: Path,
    *,
    execution: AgentTaskExecution | None,
    saved_stage: bool,
) -> Path:
    """Create a split workspace or reopen the layout recorded for a saved stage."""

    task: AgentTaskRecord | None = None
    if execution is not None:
        task = execution.store.agent_task(execution.operation_id)
        if task is None:
            raise ValueError("The local conversation stage has no durable task binding.")
    if saved_stage:
        if task is None or task.stage_root != str(stage) or task.stage_host:
            raise ValueError("The saved local conversation stage binding is unavailable.")
        workspace = _local_chat_workspace_for_task(execution.store, task)
        if workspace == stage:
            _touch_local_stage(stage)
            return stage
        require_existing = True
    else:
        workspace = stage / "workspace"
        require_existing = False
        if task is not None:
            chat_id = task.request.get("chat_id")
            if (
                task.kind not in {"node_chat", "project_chat"}
                or not isinstance(chat_id, str)
                or not chat_id
            ):
                raise ValueError("The local conversation stage has no durable chat binding.")
            layout = execution.store.chat_stage_layout(
                project_id=task.project_id,
                kind=task.kind,
                chat_id=chat_id,
                stage_host="",
                stage_root=str(stage),
            )
            if layout is None and execution.store.chat_stage_has_prior_binding(
                operation_id=task.operation_id,
                project_id=task.project_id,
                kind=task.kind,
                chat_id=chat_id,
                stage_host="",
                stage_root=str(stage),
            ):
                _touch_local_stage(stage)
                return stage
    if os.path.lexists(workspace):
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("The saved provider workspace is unsafe.")
    elif require_existing:
        raise ValueError(
            "The saved provider workspace is unavailable; retry this chat turn instead."
        )
    else:
        workspace.mkdir(mode=0o700)
    if not saved_stage and execution is not None and not require_existing:
        execution.store.record_chat_stage_layout(
            execution.operation_id,
            stage_root=str(stage),
            workspace_root=str(workspace),
        )
    _touch_local_stage(stage)
    return workspace


def _local_chat_workspace_for_task(store: AppStore, task: AgentTaskRecord) -> Path:
    """Resolve a saved local provider cwd only from its durable layout provenance."""

    if not task.stage_root:
        raise ValueError("The conversation's source stage is unavailable.")
    if task.stage_host:
        raise ValueError("A remote conversation has no local workspace path.")
    chat_id = task.request.get("chat_id")
    if (
        task.kind not in {"node_chat", "project_chat"}
        or not isinstance(chat_id, str)
        or not chat_id
    ):
        raise ValueError("The conversation stage has no durable chat binding.")
    layout = store.chat_stage_layout(
        project_id=task.project_id,
        kind=task.kind,
        chat_id=chat_id,
        stage_host="",
        stage_root=task.stage_root,
    )
    stage = Path(task.stage_root)
    return stage / "workspace" if layout == "split-v1" else stage


def _local_chat_artifact_directory(
    store: AppStore,
    task: AgentTaskRecord,
    scope_id: str,
) -> Path:
    """Reconstruct one local output boundary from durable stage-layout provenance."""

    if not task.stage_root:
        raise ValueError("The artifact's source stage is unavailable.")
    if task.request.get("mode") not in {"discuss", "work"}:
        raise ValueError("The artifact's source task has no recognized chat mode.")
    return _local_chat_workspace_for_task(store, task) / "turns" / scope_id / "artifacts"


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


def prepare_artifact_edit_directory(
    request: RunRequest,
    execution: AgentTaskExecution,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> Path | PurePosixPath:
    """Create an unstarted edit, preserve any existing output, and fail closed after staging."""
    edit = request.artifact_edit
    if edit is None:
        raise ValueError("An artifact edit requires its admitted snapshot.")
    staged = any(
        execution.store.agent_task_has_receipt(operation_id, "artifact_edit_staged")
        for operation_id in {execution.operation_id, edit.operation_id}
    )
    if remote_stage is not None:
        return remote_stage.prepare_artifact_edit_directory(edit.staged_scope_id, staged=staged)
    scope = workspace / "turns" / edit.staged_scope_id
    return _prepare_local_artifact_directory(
        workspace, edit.staged_scope_id, reuse=staged or os.path.lexists(scope)
    )


def _discover_chat_artifacts(
    execution: AgentTaskExecution | None,
    scope_id: str,
    directory: Path,
    remote_stage: RemoteRunStage | None,
    *,
    service: ProjectService | None = None,
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

    task = execution.store.agent_task(execution.operation_id) if execution is not None else None
    edit = task.request.get("artifact_edit") if task is not None else None
    edited_name = edit.get("source_name") if isinstance(edit, dict) else None

    attached: list[AgentArtifactDescriptor] = []
    total_bytes = 0
    allowed_candidates = 0
    for name, advertised_size in sorted(candidates):
        if name == edited_name:
            continue
        if advertised_size < 0 or advertised_size > CHAT_ARTIFACT_MAX_FILE_BYTES:
            ignore("file_size_limit")
            continue
        if total_bytes + advertised_size > CHAT_ARTIFACT_MAX_TOTAL_BYTES:
            ignore("total_size_limit")
            continue
        if advertised_size == 0:
            ignore("empty")
            continue
        if allowed_candidates >= CHAT_ARTIFACT_MAX_COUNT:
            ignore("count_limit")
            continue
        allowed_candidates += 1
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
            if not data:
                ignore("empty")
                continue
            media_type = classify_artifact_bytes(name, data)
            descriptor = descriptor_for(scope_id, name, media_type=media_type, size_bytes=len(data))
            if execution is not None:
                if task is None:
                    raise ValueError("The artifact supplier task is unavailable.")
                now = execution.store.now()
                stored_artifact = execution.store.create_artifact(
                    Artifact(
                        artifact_id=descriptor.artifact_id,
                        project_id=task.project_id,
                        supplier="turn",
                        supplier_id=scope_id,
                        source_name=name,
                        media_type=media_type,
                        created_at=now,
                        expires_at=(
                            datetime.fromisoformat(now) + timedelta(days=RUN_STAGE_RETENTION_DAYS)
                        ).isoformat(),
                        origin_operation_id=task.operation_id,
                        episode_id=edit.get("episode_id")
                        if isinstance(edit, dict)
                        else task.episode_id,
                        chat_id=task.request.get("chat_id"),
                    ),
                    data=data,
                )
                if service is not None and media_type == "text/html":
                    from rcp.live_artifact_runtime import resolve_artifact_live_version

                    resolve_artifact_live_version(
                        execution.store,
                        service,
                        stored_artifact.artifact_id,
                        stored_artifact.current_version,
                    )
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


def stage_artifact_context(
    service: ProjectService,
    request: RunRequest,
    execution: AgentTaskExecution | None,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    artifact_path: str,
) -> StagedArtifactContext | None:
    """Stage the artifact's current bytes and bounded human selection context."""

    context = request.artifact_context
    if context is None:
        return None
    if execution is None or (local_stage is None) == (remote_stage is None):
        raise ValueError("Artifact context requires one durable chat stage.")
    current = execution.store.agent_task(execution.operation_id)
    origin = execution.store.agent_task(context.operation_id)
    if current is None or origin is None or origin.project_id != current.project_id:
        raise ValueError("The artifact context origin is unavailable.")
    edit = request.artifact_edit
    protected_write_paths = (
        (str(execution.store.path.parent / "artifacts"),) if not current.stage_host else ()
    )
    if edit is not None:
        for operation_id in dict.fromkeys((execution.operation_id, edit.operation_id)):
            saved = execution.store.agent_task_contract(operation_id, "artifact_edit_pointer")
            if saved is not None and not execution.store.agent_task_has_receipt(
                operation_id, "artifact_edit_staged"
            ):
                pointer = json.loads(saved)
                if pointer.get("path") != str(Path(artifact_path) / edit.source_name):
                    raise ValueError("The saved artifact edit path changed.")
                execution.store.record_agent_task_receipt(
                    operation_id,
                    "artifact_edit_staged",
                    {
                        "artifact_id": edit.artifact_id,
                        "base_version": edit.base_version,
                        "path": pointer["path"],
                        "operation_id": edit.operation_id,
                        "sha256": pointer["sha256"],
                    },
                    tier="summary",
                )
            for receipt in execution.store.agent_task_receipts(operation_id):
                if receipt.category == "artifact_edit_staged":
                    pointer = json.loads(saved) if saved else receipt.payload.get("pointer")
                    if not isinstance(pointer, dict):
                        raise ValueError("The saved artifact edit pointer is unavailable.")
                    if pointer.get("path") != str(Path(artifact_path) / edit.source_name):
                        raise ValueError("The saved artifact edit path changed.")
                    if remote_stage is not None:
                        remote_stage.read_artifact_bytes(
                            edit.staged_scope_id,
                            edit.source_name,
                            max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
                        )
                    else:
                        read_local_regular_file(
                            Path(artifact_path),
                            edit.source_name,
                            max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
                        )
                    return StagedArtifactContext(
                        pointer=pointer, protected_write_paths=protected_write_paths
                    )
    artifact = execution.store.artifact(edit.artifact_id if edit else context.artifact_id)
    if artifact is None or artifact.project_id != current.project_id:
        raise ValueError("The artifact context is unavailable.")
    data = execution.store.read_artifact_bytes(
        artifact.artifact_id, edit.base_version if edit else artifact.current_version
    )
    descriptor = AgentArtifactDescriptor(
        artifact_id=artifact.artifact_id,
        name=artifact.source_name,
        media_type=artifact.media_type,
        size_bytes=len(data),
    )
    if not supports_comments(descriptor.media_type):
        raise ValueError("The artifact type does not support comments.")
    if classify_artifact_bytes(descriptor.name, data) != descriptor.media_type:
        raise ValueError("The current artifact no longer matches its declared type.")
    base_sha256 = hashlib.sha256(data).hexdigest()
    protected_write_paths = (
        (str(execution.store.path.parent / "artifacts"),) if not current.stage_host else ()
    )

    # Each box the current viewer drew on a raster image is cropped from the exact bytes
    # staged beside it, so a recovery restages the same crops.
    boxes = {
        index: selection.rect
        for index, selection in enumerate(context.selections, 1)
        if selection.kind == "box" and selection.elements is not None
    }
    frame, animated = (
        croppable_frame(data)
        if boxes and descriptor.media_type in CROPPABLE_MEDIA_TYPES
        else (None, False)
    )
    crops = sorted(boxes) if frame is not None else []

    def write_crops(folder: Path) -> None:
        assert frame is not None
        for index in crops:
            rect = boxes[index]
            path = folder / f"{index}.png"
            path.write_bytes(
                crop_region(frame, x=rect.x, y=rect.y, width=rect.width, height=rect.height)
            )
            path.chmod(0o400)

    label = f"artifact-context-v2-{execution.operation_id}-{descriptor.artifact_id}"
    if remote_stage is not None:
        with tempfile.TemporaryDirectory(
            prefix="rcp-artifact-context-", dir=rcp_temp_dir()
        ) as temporary:
            root = Path(temporary)
            _write_artifact_context(root, descriptor.name, data, write_crops if crops else None)
            staged_root = Path(remote_stage.put_directory(root, label, reuse=True))
    else:
        assert local_stage is not None
        staged_root = local_stage / "inputs" / label
        if staged_root.exists():
            existing = read_local_regular_file(
                staged_root,
                descriptor.name,
                max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
            )
            if existing != data:
                raise ValueError("The saved artifact context changed during this turn.")
        else:
            staged_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f".{label}-", dir=staged_root.parent))
            try:
                _write_artifact_context(
                    temporary, descriptor.name, data, write_crops if crops else None
                )
                os.replace(temporary, staged_root)
            finally:
                if temporary.exists():
                    _make_tree_writable(temporary)
                    shutil.rmtree(temporary)

    pointer = {
        "path": str(staged_root / descriptor.name),
        "name": descriptor.name,
        "media_type": descriptor.media_type,
        "size": len(data),
        "sha256": base_sha256,
        "source_operation_id": origin.operation_id,
        "source_artifact_id": descriptor.artifact_id,
        "selections": [
            {
                **item.model_dump(mode="json"),
                **(
                    {
                        "crop_path": str(staged_root / _SELECTION_CROPS / f"{index}.png"),
                        "first_frame_only": animated,
                    }
                    if index in crops
                    else {}
                ),
            }
            for index, item in enumerate(context.selections, 1)
        ],
    }
    if edit is not None:
        staged_path = Path(artifact_path) / descriptor.name
        if remote_stage is not None:
            remote_stage.stage_artifact_bytes(edit.staged_scope_id, descriptor.name, data)
        else:
            assert local_stage is not None
            stage_artifact(
                str(Path(artifact_path).parents[2]), edit.staged_scope_id, descriptor.name, data
            )
        pointer["path"] = str(staged_path)
        if not execution.store.agent_task_has_receipt(
            execution.operation_id, "artifact_edit_staged"
        ):
            content = json.dumps(pointer)
            execution.store.record_agent_task_contract(
                edit.operation_id,
                "artifact_edit_pointer",
                content,
                hashlib.sha256(content.encode()).hexdigest(),
            )
            execution.store.record_agent_task_receipt(
                edit.operation_id,
                "artifact_edit_staged",
                {
                    "artifact_id": edit.artifact_id,
                    "base_version": edit.base_version,
                    "path": str(staged_path),
                    "operation_id": edit.operation_id,
                    "sha256": base_sha256,
                },
                tier="summary",
            )
    return StagedArtifactContext(
        pointer=pointer,
        protected_write_paths=protected_write_paths,
    )


_SELECTION_CROPS = "selections"


def _write_artifact_context(
    root: Path, name: str, data: bytes, write_crops: Callable[[Path], None] | None
) -> None:
    """Write the artifact copy and its selection crops as one read-only tree."""

    (root / name).write_bytes(data)
    (root / name).chmod(0o400)
    if write_crops is not None:
        folder = root / _SELECTION_CROPS
        folder.mkdir()
        write_crops(folder)
        folder.chmod(0o500)
    root.chmod(0o500)


def _make_tree_writable(root: Path) -> None:
    for folder in (root, root / _SELECTION_CROPS):
        if folder.is_dir():
            folder.chmod(0o700)


def finalize_artifact_edit(
    request: RunRequest,
    execution: AgentTaskExecution | None,
    *,
    artifact_scope_id: str,
    artifact_directory: Path,
    remote_stage: RemoteRunStage | None,
    artifacts: list[AgentArtifactDescriptor],
    service: ProjectService,
) -> list[AgentArtifactDescriptor]:
    """Publish the admitted file, or retain a raced edit as an ordinary turn artifact."""
    from rcp.live_artifact_runtime import resolve_artifact_live_version

    edit = request.artifact_edit
    if edit is None or execution is None:
        return artifacts
    published = next(
        (
            receipt
            for receipt in execution.store.agent_task_receipts(edit.operation_id)
            if receipt.category == "artifact_edit_published"
        ),
        None,
    )
    if published is not None:
        descriptor = published.payload.get("descriptor")
        return (
            [*artifacts, AgentArtifactDescriptor.model_validate(descriptor)]
            if descriptor
            else artifacts
        )
    try:
        if artifact_scope_id != edit.staged_scope_id:
            raise ValueError("The artifact edit staging scope changed.")
        staged = next(
            (
                receipt
                for operation_id in dict.fromkeys((execution.operation_id, edit.operation_id))
                for receipt in execution.store.agent_task_receipts(operation_id)
                if receipt.category == "artifact_edit_staged"
            ),
            None,
        )
        if staged is None or staged.payload.get("path") != str(
            artifact_directory / edit.source_name
        ):
            raise ValueError("The artifact edit staged path could not be verified.")
        data = (
            remote_stage.read_artifact_bytes(
                edit.staged_scope_id, edit.source_name, max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES
            )
            if remote_stage is not None
            else read_local_regular_file(
                artifact_directory, edit.source_name, max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES
            )
        )
        media_type = classify_artifact_bytes(edit.source_name, data)
        if media_type != edit.media_type:
            raise ValueError("The artifact edit changed its file type.")
    except (FileNotFoundError, ValueError) as exc:
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "artifact_edit_publish_failed",
            {"error": str(exc)},
            tier="summary",
        )
        return artifacts

    base_sha256 = staged.payload.get("sha256") or staged.payload.get("pointer", {}).get("sha256")
    if hashlib.sha256(data).hexdigest() == base_sha256:
        return artifacts
    try:
        version = execution.store.publish_artifact_version(
            edit.artifact_id,
            base_version=edit.base_version,
            operation_id=edit.operation_id,
            data=data,
        )
    except ArtifactByteLimitError as exc:
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "artifact_edit_publish_failed",
            {"error": str(exc)},
            tier="summary",
        )
        return artifacts
    except ArtifactOperationConflict:
        raise
    except ArtifactVersionConflict:
        task = execution.store.agent_task(execution.operation_id)
        if task is None:
            raise ValueError("The artifact edit task is unavailable.") from None
        descriptor = descriptor_for(
            artifact_scope_id, edit.source_name, media_type=media_type, size_bytes=len(data)
        )
        now = execution.store.now()
        execution.store.create_artifact(
            Artifact(
                artifact_id=descriptor.artifact_id,
                project_id=task.project_id,
                supplier="turn",
                supplier_id=artifact_scope_id,
                source_name=edit.source_name,
                media_type=media_type,
                created_at=now,
                expires_at=(
                    datetime.fromisoformat(now) + timedelta(days=RUN_STAGE_RETENTION_DAYS)
                ).isoformat(),
                origin_operation_id=task.operation_id,
                episode_id=edit.episode_id,
                chat_id=task.request.get("chat_id"),
            ),
            data=data,
        )
        forked = execution.store.artifact(descriptor.artifact_id)
        resolve_artifact_live_version(
            execution.store, service, forked.artifact_id, forked.current_version
        )
        execution.store.record_agent_task_receipt(
            edit.operation_id,
            "artifact_edit_published",
            {
                "artifact_id": forked.artifact_id,
                "descriptor": descriptor.model_dump(mode="json"),
            },
            tier="summary",
        )
        return [*artifacts, descriptor]
    resolve_artifact_live_version(execution.store, service, edit.artifact_id, version.version_id)
    execution.store.record_agent_task_receipt(
        edit.operation_id,
        "artifact_edit_published",
        {"artifact_id": edit.artifact_id, "version_id": version.version_id},
        tier="summary",
    )
    return artifacts


def artifact_omissions(receipt: AgentTaskReceiptRecord) -> dict[str, int | bool]:
    """Project only omission counts, never diagnostic paths or errors."""
    ignored = receipt.payload.get("ignored")
    if not isinstance(ignored, dict):
        ignored = {}
    result: dict[str, int | bool] = {}
    for reason in (
        "count_limit",
        "file_size_limit",
        "total_size_limit",
        "empty",
        "invalid_or_unavailable",
    ):
        count = ignored.get(reason)
        if type(count) is int and count >= 0:
            result[reason] = count
    result["discovery_failed"] = any(
        type(ignored.get(reason)) is int and ignored[reason] > 0
        for reason in ("discovery_unavailable", "unexpected_error")
    )
    return result


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
    latest = execution.store.agent_task_artifact_discoveries([execution.operation_id]).get(
        execution.operation_id
    )
    if latest is not None and latest.payload == payload:
        return
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "artifact_discovery",
        payload,
        tier="summary",
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
        if execution.stage_root:
            if execution.stage_host:
                stage_name = remote_stage_name(execution.stage_root) or ""
            else:
                stage_name = Path(execution.stage_root).name
            if not stage_name or _safe_stage_name(stage_name) != stage_name:
                raise ValueError(
                    "Cannot safely resume this chat because its saved stage is invalid. "
                    "Retry the turn from the beginning."
                )
            return stage_name
        task = execution.store.agent_task(execution.operation_id)
        if task is None or not task.project_id:
            raise ValueError(
                "Cannot identify this chat's project workspace; retry the turn from the beginning."
            )
        if request.artifact_edit is not None and request.artifact_edit.fresh_session:
            if task.attempt > 1 or execution.operation_id != request.artifact_edit.operation_id:
                raise ValueError(
                    "Cannot resume the artifact edit because its saved stage is missing."
                )
            return _safe_stage_name(f"artifact-edit-{request.artifact_edit.operation_id}")
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
    # The remote home is checked on the host when the stage is attached. A saved
    # legacy `/tmp/rcp-run.<name>` stage still resumes; a later release removes it.
    if remote_stage_name(execution.stage_root) != stage_name:
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
    local_stage: Path | None,
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
    if local_stage is None:
        raise ValueError("The local chat input stage is unavailable.")
    read_dirs.append(local_stage / "inputs")
    read_dirs.append(service.manifest.research_dir)
    return read_dirs


def _machine_writable_paths(
    service: ProjectService, execution_machine: str, store: AppStore | None
) -> list[str]:
    """The space-level writable paths granted on this execution machine."""

    if store is None:
        return []
    machine = service.manifest.machine_map[execution_machine]
    card = store.space_machine_for(machine.host)
    return list(card.writable_paths) if card is not None else []


def _project_write_scope(
    context: ChatContext,
    service: ProjectService,
    execution_machine: str,
    *,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    data_dir: Path,
    execution: AgentTaskExecution | None,
    capability: AgentCapability,
    episode_request: RunRequest | AutoResearchRunRequest | None = None,
    stage_only: bool = False,
    local_stage: Path | None = None,
    additional_protected_write_paths: list[str] | None = None,
) -> ProjectWriteScope:
    """Resolve one durable provider-neutral scope from project-owned authority."""

    task = execution.store.agent_task(execution.operation_id) if execution is not None else None
    if execution is not None and task is None:
        raise ValueError("project write scope has no durable agent task binding")
    if stage_only:
        admitted_aliases: list[str] = []
    elif task is not None:
        if task.dispatch_authority is None:
            raise ValueError("Work-like task has no durable dispatch authority")
        if task.dispatch_authority.task_contract != capability:
            raise ValueError("Work-like capability conflicts with its durable dispatch authority")
        admitted_aliases = list(task.dispatch_authority.scope.run_truth_scope)
        if sorted(set(context.run_truth_scope)) != admitted_aliases:
            raise ValueError("Work-like context conflicts with its durable repository scope")
    else:
        admitted_aliases = sorted(set(context.run_truth_scope))
    project_id = (
        task.project_id
        if task is not None
        else service.history.project_id
        or "manifest-" + hashlib.sha256(str(service.manifest.path).encode("utf-8")).hexdigest()
    )
    stage_root = (
        str(remote_stage.root)
        if remote_stage is not None and remote_stage.root is not None
        else str(local_stage or workspace)
    )
    binding = None
    include_shared = False
    if task is not None and execution is not None and not stage_only:
        if task.episode_id is not None:
            from rcp.runs.episodes.isolation import ensure_episode_isolation

            if episode_request is None:
                raise ValueError("episode write scope requires its owner's launch request")
            isolation = ensure_episode_isolation(
                service, execution.store, task.episode_id, episode_request
            )
            binding = isolation.worktree
            if binding is not None:
                context.repositories = [
                    pointer.model_copy(update={"path": binding.worktree_path})
                    if pointer.alias == binding.repository_alias
                    else pointer
                    for pointer in context.repositories
                ]
        else:
            chat_id = task.request.get("chat_id")
            if isinstance(chat_id, str):
                binding = execution.store.conversation_worktree(project_id, chat_id)
        if isinstance(binding, ConversationWorktreeBinding):
            if task.kind not in {"node_chat", "project_chat"} or task.episode_id is not None:
                raise ValueError("Episodes and workers cannot use conversation worktrees.")
            request = RunRequest.model_validate(task.request)
            validate_worktree_binding(service, request, binding, execution.store)
            include_shared = request.worktree_integration in {"starting_branch", "default_branch"}
            if include_shared and not request.worktree_integration_target:
                raise ValueError("The integration turn has no admitted target branch.")
    scope = resolve_project_write_scope(
        manifest=service.manifest,
        project_id=project_id,
        execution_machine=execution_machine,
        capability=capability,
        stage_root=stage_root,
        workspace_root=str(workspace),
        admitted_aliases=admitted_aliases,
        repository_pointers=context.repositories,
        remote_stage=remote_stage,
        app_data_dir=data_dir,
        repository_inventory=service.repository_ownership_inventory(project_id=project_id),
        conversation_worktree=binding,
        include_shared_checkout=include_shared,
        machine_writable_paths=_machine_writable_paths(
            service, execution_machine, execution.store if execution is not None else None
        ),
        additional_protected_write_paths=[
            *(
                [str(local_stage / "inputs")]
                if remote_stage is None and local_stage is not None and workspace == local_stage
                else []
            ),
            *(additional_protected_write_paths or []),
        ],
    )
    if isinstance(binding, ConversationWorktreeBinding) and execution is not None:
        # Human-authored local integration is the only related-turn root change.
        # Derive both admissible fingerprints from this exact validated binding;
        # a retry of an already-bound operation still requires its original scope.
        repositories = [item for item in scope.repositories if item.path != binding.shared_path]
        protected = [
            path
            for path in scope.protected_write_paths
            if path != str(PurePosixPath(binding.shared_path) / ".research")
        ]
        # Canonical state is protected in both variants, including when stored in
        # the shared checkout. Keep every ordinary deny in the derived variants.
        state_repository = service.manifest.repository_map[service.manifest.state.repository]
        if state_repository.alias == binding.repository_alias:
            protected.append(str(PurePosixPath(binding.shared_path) / ".research"))
        common = scope.model_dump(
            exclude={
                "schema_generation",
                "fingerprint",
                "repositories",
                "protected_write_paths",
            }
        )
        variants = []
        for shared in (False, True):
            roots = list(repositories)
            denied = list(protected)
            if shared:
                roots.append(
                    WritableRepositoryRoot(
                        alias=binding.repository_alias,
                        machine=binding.machine,
                        path=binding.shared_path,
                    )
                )
                denied.append(str(PurePosixPath(binding.shared_path) / ".research"))
            variants.append(
                ProjectWriteScope.create(
                    **common,
                    repositories=roots,
                    protected_write_paths=denied,
                ).fingerprint
            )
        execution.compatible_related_write_scope_fingerprints = frozenset(variants)
    if execution is not None:
        question = execution.store.question_for_followup(execution.operation_id)
        if question is not None and scope.fingerprint != question.origin.write_scope_fingerprint:
            raise ValueError("question_origin_write_scope_changed")
    return scope


def _chat_path(service: ProjectService, request: RunRequest) -> Path:
    assert request.chat_id is not None
    return service.chat_path(
        request.chat_id,
        chat_scope=request.chat_scope,
        node_id=request.node_id,
    )


def question_answer_text(question: QuestionRecord) -> str:
    return question.answer or "\n".join(question.chosen_choices)


def _question_answer_message_id(question: QuestionRecord) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:question:{question.question_id}:answer:{question.answer_revision}",
        )
    )


def project_chat_question_answer(
    service: ProjectService, store: AppStore, question: QuestionRecord
) -> None:
    """Retry canonical projection from SQLite; the stable id makes replay harmless."""
    current = store.get_question(question.question_id)
    if current is not None and current.answer_projected_revision >= question.answer_revision:
        return
    task = store.agent_task(question.origin.operation_id)
    if task is None:
        raise ValueError("question_origin_missing")
    request = RunRequest.model_validate(task.request)
    with service.history.workspace.transaction():
        _append_chat_records(
            service,
            _chat_path(service, request),
            [
                {
                    "uuid": _question_answer_message_id(question),
                    "sessionId": request.chat_id,
                    "nativeSessionId": question.origin.native_session_id,
                    "nodeId": request.node_id,
                    "chatScope": request.chat_scope,
                    "provider": question.origin.provider,
                    "model": request.model or "provider-default",
                    "reasoning": request.reasoning,
                    "executionMachine": request.run_on,
                    "cwd": str(service.manifest.research_dir.parent),
                    "timestamp": question.resolved_at,
                    "operationId": question.followup_operation_id,
                    "mode": "work",
                    "trigger": "human",
                    "type": "user",
                    "role": "user",
                    "text": question_answer_text(question),
                    "questionId": question.question_id,
                    "answerRevision": question.answer_revision,
                    "attachments": [],
                }
            ],
        )
    store.mark_question_answer_projected(question.question_id, question.answer_revision)
    service.invalidate_source_index()


def reconcile_chat_question_answers(
    store: AppStore,
    background_tasks,
    project_service: Callable[[str], ProjectService],
    *,
    project_id: str | None = None,
) -> dict[str, str]:
    """Answer/API, settlement and startup entry; never invoked by provider output.

    The question itself is the durable projection outbox. Pending revisions repair
    crashes between SQLite admission and canonical history publication.
    Deferred or unusable origins remain unclaimed with a returned reason.
    """
    statuses = {}
    for question in store.questions_needing_chat_reconciliation(project_id=project_id):
        origin_task = store.agent_task(question.origin.operation_id)
        if origin_task is not None and origin_task.kind not in {"node_chat", "project_chat"}:
            continue
        try:
            from rcp.runs.questions import reconcile_question_receipt

            reconcile_question_receipt(store, question.question_id)
            question = store.get_question(question.question_id)
            assert question is not None
            if question.answer_projected_revision < question.answer_revision:
                service = project_service(question.origin.project_id).for_graph_target(
                    question.origin.graph_target
                )
                project_chat_question_answer(service, store, question)
            if question.origin.owner_kind != "chat":
                # Experiment's owner admits its paid invocation after projection.
                statuses[question.question_id] = "projected"
                continue
            if question.client_receipt_revision is not None:
                statuses[question.question_id] = "received"
                continue
            task = (
                store.agent_task(question.followup_operation_id)
                if question.followup_operation_id
                else store.admit_chat_question_followup(question.question_id)
            )
            if task is not None and task.status == "queued":
                background_tasks.launch_admitted(task.operation_id)
            current = store.agent_task(task.operation_id) if task is not None else None
            if current is not None and current.status in {"failed", "interrupted", "paused"}:
                statuses[question.question_id] = (
                    current.error or f"question_followup_{current.status}"
                )
            else:
                statuses[question.question_id] = "admitted" if task is not None else "ineligible"
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            statuses[question.question_id] = str(exc)
    return statuses


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
            "activeComputeIds": request.active_compute_ids,
        }
        records = []
        if request.trigger != "watcher":
            question = (
                execution.store.question_for_followup(execution.operation_id)
                # Experiment roots may claim answers alongside their human prompt.
                if execution is not None and request.trigger == "human"
                else None
            )
            records.append(
                {
                    **common,
                    "uuid": (
                        _question_answer_message_id(question)
                        if question is not None
                        else str(uuid.uuid4())
                    ),
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
        _append_chat_records(service, path, records, reserve_prompt=True)


def _append_chat_graph_receipt(
    service: ProjectService,
    request: RunRequest,
    native_session_id: str | None,
    graph_update: GraphUpdateResult,
    operation_id: str,
) -> None:
    """Append only a durable receipt for a patch repair or an Apply again."""

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
            "operationId": operation_id,
            "mode": "work",
            "trigger": request.trigger,
            "activeComputeIds": request.active_compute_ids,
            "uuid": str(uuid.uuid4()),
            "type": "assistant",
            "role": "assistant",
            "text": "",
            "appliedRevision": graph_update.applied_revision,
            "graphUpdate": graph_update.model_dump(mode="json"),
        }
        _append_chat_records(service, path, [record])
    service.invalidate_source_index()


def _append_chat_records(
    service: ProjectService,
    path: Path,
    records: list[dict[str, object]],
    *,
    reserve_prompt: bool = False,
) -> None:
    """Append under the chat lock; callers own the StateWorkspace transaction."""
    records = [
        {**record, "graphTarget": service.history.graph_target.model_dump(mode="json")}
        for record in records
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = service.history.workspace.root / ".chat.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                existing_ids = {
                    json.loads(line).get("uuid") for line in path.read_text().split("\n") if line
                }
                # Steering appends receipt snapshots under the original message
                # UUID; the transcript reader folds them and validates identity.
                records = [
                    item
                    for item in records
                    if item.get("steering") is not None or item.get("uuid") not in existing_ids
                ]
            if reserve_prompt and path.exists():
                # A live steer may already have recorded this attempt's original
                # human prompt, or finalization may be resuming after appending
                # the answer. Inspect only identity, never use transcript as input.
                existing = [json.loads(line) for line in path.read_text().split("\n") if line]
                recorded = {
                    (item.get("operationId"), item.get("role"))
                    for item in existing
                    if item.get("role") in {"user", "assistant"}
                    and item.get("steering") is None
                    and item.get("questionId") is None
                }
                records = [
                    item
                    for item in records
                    if not item.get("operationId")
                    or (item.get("operationId"), item.get("role")) not in recorded
                ]
            if not records:
                return
            with path.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    service.history.workspace.publish([path.relative_to(service.history.workspace.root)])
