from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    PromptFactory,
    parse_agent_patch_json,
    prepare_agent_patch,
    validate_agent_patch_shape,
    validate_work_patch,
)
from rcp.agents.command_mailbox import StagedCommandMailbox
from rcp.agents.experiment_loop_prompt import (
    experiment_loop_continuation_contract,
    experiment_loop_patch_correction_contract,
    experiment_loop_task_contract,
    experiment_loop_wake_message,
    experiment_loop_watcher_correction_contract,
    experiment_watcher_maintenance_correction_contract,
)
from rcp.agents.prompts import (
    CHAT_MASTER_CONTEXT_VERSION,
    invoked_package_pointers,
    invoked_provider_skill_section,
)
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.core.authority import AgentProfile
from rcp.core.models import ExperimentDecisionPin, Patch
from rcp.history import PatchRejected, ReplayHalted
from rcp.limits import PATCH_SELF_CHECK_TIMEOUT_SECONDS, RUN_STAGE_RETENTION_DAYS
from rcp.runs.chat import (
    _append_chat_exchange,
    _append_chat_graph_receipt,
    _chat_context_delta,
    _chat_read_dirs,
    _chat_stage_name,
    _clear_stale_turn_handoffs,
    _commit_chat_prompt_state,
    _discover_chat_artifacts,
    _logical_chat_turn_operation_id,
    _prepare_chat_prompt_state,
    _prepare_local_artifact_directory,
    _read_chat_patch,
    _read_watch_request,
    _record_applied_graph_revision,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _stage_chat_patch_inputs,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
    _work_write_dirs,
)
from rcp.runs.experiment_loop import (
    StagedExperimentWatcherResource,
    commit_experiment_episode_binding,
    experiment_episode_context_values,
    experiment_exit_problem,
    experiment_graph_result_summary,
    experiment_watcher_output_name,
    patch_explicitly_exits,
    persist_experiment_watchers_idempotently,
    prepare_experiment_episode_context_candidate,
    read_experiment_watcher_outputs,
    root_experiment_loop_operation_id,
    stage_chat_experiment_watcher_resources,
    stage_experiment_loop_context,
    validate_experiment_completion,
)
from rcp.runs.patch_validator import (
    PatchValidationBudget,
    PatchValidationResult,
    serve_patch_validation_mailbox,
    stage_patch_validation_mailbox,
)
from rcp.runs.result_views import (
    ResultViewSnapshot,
    clear_result_view_rollback_snapshot,
    discover_result_view,
    persist_result_view_rollback_snapshot,
    prepare_result_view_slot,
    read_result_view_rollback_snapshot,
    require_result_view_changed,
    restore_result_view,
    touch_conversation_stage,
    touch_saved_conversation_stages,
)
from rcp.runs.shared import (
    _existing_patch_digest,
    _parent_task_contract_path,
    _pinned_to_profile,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _record_patch_applied_receipt,
    _record_patch_receipt,
    _sse,
    _stage_context_paths,
    _stage_json_task_input,
    _stage_task_contract,
    _stage_task_input,
    _stream_agent_events,
    _swept_stage_root,
    _task_token,
)
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.storage import ResultViewRecord, WatcherContinuation
from rcp.transport import RemoteRunStage, RunLockCancelled, StateUnavailable
from rcp.watchers import (
    WatcherBinding,
    WatcherInitialCheckError,
    arm_watchers,
    parse_experiment_watch_json,
    parse_watch_json,
    validate_graph_conditions,
    validate_watch_specs,
)

_MAX_CORRECTION_ROUNDS = 2


@dataclass(frozen=True)
class _WorkPatchFailure:
    message: str
    correctable: bool
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PreparedWorkPatch:
    patch: Patch
    change_summary: tuple[str, ...]
    proposal_ids: tuple[str, ...]


@dataclass(frozen=True)
class _CorrectionPatchRead:
    text: str | None
    problem: Literal["unreadable", "missing", "unchanged"] | None = None
    detail: str | None = None


@dataclass
class _WorkValidatorMailboxLifecycle:
    staged: StagedCommandMailbox
    execution: AgentTaskExecution | None
    stop: asyncio.Event
    task: asyncio.Task[None]
    closed: bool = False

    async def close(self, *, primary_error: BaseException | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        await _close_work_validator_mailbox(
            self.staged,
            stop=self.stop,
            task=self.task,
            execution=self.execution,
            primary_error=primary_error,
        )


@dataclass(frozen=True)
class _PreparedResultView:
    action: Literal["create", "revise"]
    view_id: str
    prompt_path: str
    origin_operation_id: str | None = None
    record: ResultViewRecord | None = None
    before: ResultViewSnapshot | None = None


@dataclass(frozen=True)
class _ResultViewRollbackReceipt:
    task_operation_id: str
    task_parent_operation_id: str
    project_id: str
    task_kind: str
    view_id: str
    experiment_id: str
    chat_id: str
    provider: str
    model: str
    reasoning: str
    run_on: str
    native_session_id: str
    stage_host: str
    stage_root: str
    source_name: str
    size_bytes: int
    content_sha256: str


_RESULT_VIEW_ROLLBACK_SNAPSHOT_RECEIPT = "result_view_rollback_snapshot"


def _result_view_expiry(now: datetime) -> str:
    return (now + timedelta(days=RUN_STAGE_RETENTION_DAYS)).isoformat()


def _result_view_task(execution: AgentTaskExecution | None):
    if execution is None:
        raise ValueError("A result view requires a durable RCP Work task.")
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.kind != "node_chat" or not task.project_id:
        raise ValueError("A result view requires a durable node conversation task.")
    return task


def _preflight_result_view_revision(
    request: RunRequest,
    execution: AgentTaskExecution | None,
) -> ResultViewRecord | None:
    """Require the durable saved session and stage before any stage is opened or checkpointed."""

    result_view = request.result_view
    if result_view is None or result_view.action != "revise":
        return None
    task = _result_view_task(execution)
    assert execution is not None
    if not execution.stage_root:
        raise ValueError(
            "The result view revision has no inherited conversation stage; it cannot be redrawn "
            "from a fresh session."
        )
    record = execution.store.result_view(result_view.view_id)
    if record is None:
        raise ValueError("The result view is missing or expired and cannot be revised.")
    if record.kept_filename is not None:
        raise ValueError("A kept result view is immutable and cannot be revised.")
    expected_binding = {
        "project": (record.project_id, task.project_id),
        "Experiment": (record.experiment_id, request.node_id or ""),
        "conversation": (record.chat_id, request.chat_id or ""),
        "provider": (record.provider, request.provider or ""),
        "model": (record.model, request.model or ""),
        "reasoning": (record.reasoning, request.reasoning or ""),
        "execution machine": (record.run_on, request.run_on or ""),
        "native session": (record.native_session_id, request.session_id or ""),
        "stage host": (record.stage_host, execution.stage_host or ""),
        "stage root": (record.stage_root, execution.stage_root),
    }
    mismatched = [label for label, (saved, current) in expected_binding.items() if saved != current]
    if mismatched:
        raise ValueError(
            "The result view cannot be revised because its saved "
            + ", ".join(mismatched)
            + " binding does not match this turn."
        )
    return record


def _persist_result_view_rollback(
    execution: AgentTaskExecution | None,
    prepared: _PreparedResultView | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> None:
    """Checkpoint trusted prior bytes and bindings immediately before provider launch."""

    if prepared is None or prepared.action != "revise":
        return
    if execution is None or prepared.record is None or prepared.before is None:
        raise ValueError("The result view revision lost its durable rollback binding.")
    task = _result_view_task(execution)
    persist_result_view_rollback_snapshot(
        local_stage,
        remote_stage,
        prepared.view_id,
        prepared.before,
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        _RESULT_VIEW_ROLLBACK_SNAPSHOT_RECEIPT,
        {
            "version": 1,
            "task_operation_id": task.operation_id,
            "task_parent_operation_id": task.parent_operation_id or "",
            "project_id": task.project_id,
            "task_kind": task.kind,
            "view_id": prepared.record.view_id,
            "experiment_id": prepared.record.experiment_id,
            "chat_id": prepared.record.chat_id,
            "provider": prepared.record.provider,
            "model": prepared.record.model,
            "reasoning": prepared.record.reasoning,
            "run_on": prepared.record.run_on,
            "native_session_id": prepared.record.native_session_id,
            "stage_host": prepared.record.stage_host,
            "stage_root": prepared.record.stage_root,
            "source_name": prepared.before.name,
            "size_bytes": prepared.before.size,
            "content_sha256": prepared.before.sha256,
        },
    )


def _result_view_rollback_receipt(
    execution: AgentTaskExecution,
    record: ResultViewRecord,
) -> _ResultViewRollbackReceipt:
    """Find the nearest exact same-stage snapshot receipt in this task lineage."""

    current = execution.store.agent_task(execution.operation_id)
    seen: set[str] = set()
    while current is not None:
        if current.operation_id in seen:
            raise ValueError("The result view rollback lineage contains a cycle.")
        seen.add(current.operation_id)
        if (
            current.project_id != record.project_id
            or current.kind != "node_chat"
            or (current.stage_host or "") != record.stage_host
            or current.stage_root != record.stage_root
        ):
            raise ValueError("The result view rollback lineage crossed its saved task or stage.")
        try:
            current_request = RunRequest.model_validate(current.request)
        except ValueError as exc:
            raise ValueError("The result view rollback lineage has an invalid request.") from exc
        if (
            current_request.result_view is None
            or current_request.result_view.action != "revise"
            or current_request.result_view.view_id != record.view_id
            or current_request.node_id != record.experiment_id
            or current_request.chat_id != record.chat_id
            or current_request.session_id != record.native_session_id
            or current.native_session_id != record.native_session_id
        ):
            raise ValueError("The result view rollback lineage changed its saved binding.")
        candidates = [
            receipt
            for receipt in execution.store.agent_task_receipts(current.operation_id)
            if receipt.category == _RESULT_VIEW_ROLLBACK_SNAPSHOT_RECEIPT
        ]
        if candidates:
            receipt = _parse_result_view_rollback_receipt(candidates[-1].payload)
            expected = _ResultViewRollbackReceipt(
                task_operation_id=current.operation_id,
                task_parent_operation_id=current.parent_operation_id or "",
                project_id=record.project_id,
                task_kind=current.kind,
                view_id=record.view_id,
                experiment_id=record.experiment_id,
                chat_id=record.chat_id,
                provider=record.provider,
                model=record.model,
                reasoning=record.reasoning,
                run_on=record.run_on,
                native_session_id=record.native_session_id,
                stage_host=record.stage_host,
                stage_root=record.stage_root,
                source_name=record.source_name,
                size_bytes=record.size_bytes,
                content_sha256=record.content_sha256,
            )
            if receipt != expected:
                raise ValueError("The result view rollback receipt does not match its saved view.")
            return receipt
        if current.parent_operation_id is None:
            break
        parent = execution.store.agent_task(current.parent_operation_id)
        if parent is None:
            raise ValueError("The result view rollback lineage lost its parent task.")
        current = parent
    raise ValueError("The interrupted result view revision has no durable rollback snapshot.")


def _parse_result_view_rollback_receipt(
    payload: dict[str, object],
) -> _ResultViewRollbackReceipt:
    expected_keys = {
        "version",
        "task_operation_id",
        "task_parent_operation_id",
        "project_id",
        "task_kind",
        "view_id",
        "experiment_id",
        "chat_id",
        "provider",
        "model",
        "reasoning",
        "run_on",
        "native_session_id",
        "stage_host",
        "stage_root",
        "source_name",
        "size_bytes",
        "content_sha256",
    }
    if set(payload) != expected_keys or payload.get("version") != 1:
        raise ValueError("The result view rollback receipt is invalid.")
    string_keys = expected_keys - {"version", "size_bytes"}
    if any(not isinstance(payload.get(key), str) for key in string_keys):
        raise ValueError("The result view rollback receipt is invalid.")
    size = payload.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError("The result view rollback receipt is invalid.")
    return _ResultViewRollbackReceipt(
        task_operation_id=str(payload["task_operation_id"]),
        task_parent_operation_id=str(payload["task_parent_operation_id"]),
        project_id=str(payload["project_id"]),
        task_kind=str(payload["task_kind"]),
        view_id=str(payload["view_id"]),
        experiment_id=str(payload["experiment_id"]),
        chat_id=str(payload["chat_id"]),
        provider=str(payload["provider"]),
        model=str(payload["model"]),
        reasoning=str(payload["reasoning"]),
        run_on=str(payload["run_on"]),
        native_session_id=str(payload["native_session_id"]),
        stage_host=str(payload["stage_host"]),
        stage_root=str(payload["stage_root"]),
        source_name=str(payload["source_name"]),
        size_bytes=size,
        content_sha256=str(payload["content_sha256"]),
    )


def _recover_result_view_rollback(
    execution: AgentTaskExecution,
    record: ResultViewRecord,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    public_problem: str,
) -> ResultViewSnapshot:
    """Restore a hard-interrupted revision from its exact trusted ancestor receipt."""

    try:
        receipt = _result_view_rollback_receipt(execution, record)
        snapshot = read_result_view_rollback_snapshot(
            local_stage,
            remote_stage,
            receipt.view_id,
            expected_name=receipt.source_name,
            expected_size=receipt.size_bytes,
            expected_sha256=receipt.content_sha256,
        )
        restored = restore_result_view(
            local_stage,
            remote_stage,
            receipt.view_id,
            snapshot,
        )
        verified = discover_result_view(
            local_stage,
            remote_stage,
            record.view_id,
            expected_name=record.source_name,
        )
        if verified.sha256 != record.content_sha256 or verified.size != record.size_bytes:
            raise ValueError("the restored public bytes do not match the saved view")
        if not clear_result_view_rollback_snapshot(
            local_stage,
            remote_stage,
            receipt.view_id,
            snapshot,
        ):
            raise ValueError("the verified rollback snapshot disappeared before cleanup")
    except Exception as exc:
        raise ValueError(
            "The result view no longer matches its saved revision, and its durable rollback "
            f"could not be recovered ({public_problem}): {exc}"
        ) from exc
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "result_view_rollback_recovered",
        {
            "ancestor_operation_id": receipt.task_operation_id,
            "view_id": receipt.view_id,
            "content_sha256": receipt.content_sha256,
            "restored": restored,
        },
    )
    return verified


def _roll_result_view_retention(
    request: RunRequest,
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> None:
    """Keep one reused Work conversation and its unkept cards on the same rolling clock."""

    if request.trigger != "human" or request.patch_kind != "work":
        return
    current_binding = touch_conversation_stage(local_stage, remote_stage)
    if execution is None or not request.chat_id:
        return
    task = execution.store.agent_task(execution.operation_id)
    if task is None or not task.project_id:
        return
    try:
        now = datetime.fromisoformat(execution.store.now()).astimezone(UTC)
        views = execution.store.list_result_views(
            task.project_id,
            chat_id=request.chat_id,
            as_of=now,
        )
        touch_saved_conversation_stages(
            ((view.stage_host, view.stage_root) for view in views if view.kept_filename is None),
            current_binding=current_binding,
        )
        execution.store.refresh_result_view_expiry(
            task.project_id,
            request.chat_id,
            expires_at=_result_view_expiry(now),
            as_of=now,
        )
    except Exception as exc:
        with suppress(Exception):
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Result-view retention could not be refreshed: {exc}",
                level="warning",
            )


def _result_view_action_was_settled_by_ancestor(
    request: RunRequest,
    execution: AgentTaskExecution,
    record: ResultViewRecord,
) -> bool:
    """Recognize only this recovery lineage's exact already-committed view action."""

    if execution.continuation not in {"resume", "retry", "handoff"}:
        return False
    result_view = request.result_view
    if result_view is None:
        return False
    current = _result_view_task(execution)
    ancestor_id = current.parent_operation_id
    seen = {current.operation_id}
    while ancestor_id is not None:
        if ancestor_id in seen:
            raise ValueError("The result view recovery lineage contains a cycle.")
        seen.add(ancestor_id)
        ancestor = execution.store.agent_task(ancestor_id)
        if ancestor is None:
            raise ValueError("The result view recovery lineage lost its parent task.")
        if ancestor.project_id != current.project_id or ancestor.kind != current.kind:
            raise ValueError("The result view recovery lineage crossed a task boundary.")
        if ancestor.operation_id == record.latest_operation_id:
            try:
                ancestor_request = RunRequest.model_validate(ancestor.request)
            except ValueError as exc:
                raise ValueError(
                    "The result view recovery lineage has an invalid request."
                ) from exc
            ancestor_view = ancestor_request.result_view
            same_view = bool(
                ancestor_view is not None
                and ancestor_view.action == result_view.action
                and (result_view.action == "create" or ancestor_view.view_id == record.view_id)
            )
            ancestor_matches = bool(
                same_view
                and ancestor.project_id == record.project_id
                and ancestor.kind == "node_chat"
                and ancestor_request.trigger == "human"
                and ancestor_request.patch_kind == "work"
                and ancestor_request.mode == "work"
                and ancestor_request.chat_scope == "node"
                and ancestor_request.node_id == record.experiment_id == request.node_id
                and ancestor_request.chat_id == record.chat_id == request.chat_id
                and ancestor_request.provider in {None, record.provider}
                and ancestor_request.model in {None, "", record.model}
                and ancestor_request.reasoning in {None, record.reasoning}
                and ancestor_request.run_on in {None, record.run_on}
                and ancestor.native_session_id == record.native_session_id
                and (ancestor.stage_host or "") == record.stage_host
                and ancestor.stage_root == record.stage_root
            )
            if not ancestor_matches:
                return False
            if execution.continuation == "handoff":
                return result_view.action == "create"
            return bool(
                record.provider == request.provider
                and record.model == request.model
                and record.reasoning == request.reasoning
                and record.run_on == request.run_on
                and request.session_id == record.native_session_id
                and (execution.stage_host or "") == record.stage_host
                and execution.stage_root == record.stage_root
            )
        ancestor_id = ancestor.parent_operation_id
    return False


def _prepare_result_view_create_slot(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    view_id: str,
    *,
    recovering: bool,
) -> Path | PurePosixPath:
    """Reuse a recovery slot, creating it only when its exact path is genuinely absent."""

    if not recovering:
        return prepare_result_view_slot(local_stage, remote_stage, view_id, reuse=False)
    try:
        return prepare_result_view_slot(local_stage, remote_stage, view_id, reuse=True)
    except FileNotFoundError:
        return prepare_result_view_slot(local_stage, remote_stage, view_id, reuse=False)


def _prepare_result_view_turn(
    request: RunRequest,
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    focused_node: dict[str, object] | None,
    logical_operation_id: str,
    revision_record: ResultViewRecord | None,
) -> _PreparedResultView | None:
    result_view = request.result_view
    if result_view is None:
        return None
    if (
        request.trigger != "human"
        or request.patch_kind != "work"
        or request.mode != "work"
        or request.chat_scope != "node"
        or execution is None
        or (
            execution.continuation not in {"fresh", "resume"}
            and not (
                execution.continuation == "retry" and result_view.action in {"create", "revise"}
            )
            and not (execution.continuation == "handoff" and result_view.action == "create")
        )
    ):
        raise ValueError("A result view is available only on an ordinary human node Work turn.")
    if (
        focused_node is None
        or focused_node.get("type") != "experiment"
        or focused_node.get("id") != request.node_id
    ):
        raise ValueError("A result view must be scoped to an existing Experiment.")
    if not request.chat_id or not request.node_id:
        raise ValueError("A result view requires its exact Experiment conversation.")

    task = _result_view_task(execution)
    if result_view.action == "create":
        origin_operation_id = logical_operation_id
        if execution.continuation in {"resume", "retry", "handoff"}:
            current = execution.store.agent_task(execution.operation_id)
            seen: set[str] = set()
            while current is not None and current.parent_operation_id is not None:
                if current.operation_id in seen:
                    raise ValueError("The result view create lineage contains a cycle.")
                seen.add(current.operation_id)
                parent = execution.store.agent_task(current.parent_operation_id)
                if (
                    parent is None
                    or parent.project_id != current.project_id
                    or parent.kind != current.kind
                ):
                    raise ValueError("The result view create recovery lost its task lineage.")
                parent_request = RunRequest.model_validate(parent.request)
                if (
                    parent_request.result_view is None
                    or parent_request.result_view.action != "create"
                    or parent_request.chat_id != request.chat_id
                    or parent_request.node_id != request.node_id
                ):
                    raise ValueError("The result view create recovery crossed a task boundary.")
                origin_operation_id = parent.operation_id
                current = parent
        view_id = hashlib.sha256(f"result-view\0{origin_operation_id}".encode()).hexdigest()[:24]
        existing = execution.store.result_view_for_diagnostics(view_id)
        if existing is not None:
            if _result_view_action_was_settled_by_ancestor(request, execution, existing):
                return None
            expected_binding = {
                "project": (existing.project_id, task.project_id),
                "Experiment": (existing.experiment_id, request.node_id),
                "conversation": (existing.chat_id, request.chat_id),
                "origin": (existing.origin_operation_id, origin_operation_id),
                "provider": (existing.provider, request.provider or ""),
                "model": (existing.model, request.model or ""),
                "reasoning": (existing.reasoning, request.reasoning or ""),
                "execution machine": (existing.run_on, request.run_on or ""),
                "native session": (existing.native_session_id, request.session_id or ""),
                "stage host": (existing.stage_host, execution.stage_host or ""),
                "stage root": (existing.stage_root, execution.stage_root or ""),
            }
            mismatched = [
                label for label, (saved, current) in expected_binding.items() if saved != current
            ]
            if mismatched:
                raise ValueError(
                    "The created result view has a mismatched "
                    + ", ".join(mismatched)
                    + " binding."
                )
            raise ValueError("This result view was already created and cannot be created again.")
        slot = _prepare_result_view_create_slot(
            local_stage,
            remote_stage,
            view_id,
            recovering=execution.continuation in {"resume", "retry", "handoff"},
        )
        return _PreparedResultView(
            action="create",
            view_id=view_id,
            prompt_path=str(slot),
            origin_operation_id=origin_operation_id,
        )

    record = revision_record
    if record is None or record.view_id != result_view.view_id:
        raise ValueError("The result view revision lost its durable preflight binding.")
    if _result_view_action_was_settled_by_ancestor(request, execution, record):
        return None
    try:
        slot = prepare_result_view_slot(local_stage, remote_stage, record.view_id, reuse=True)
        before = discover_result_view(
            local_stage,
            remote_stage,
            record.view_id,
            expected_name=record.source_name,
        )
        if before.sha256 != record.content_sha256 or before.size != record.size_bytes:
            raise ValueError("The result view bytes no longer match their saved revision.")
    except (OSError, StateUnavailable, ValueError) as exc:
        if execution.continuation not in {"resume", "retry"}:
            raise
        before = _recover_result_view_rollback(
            execution,
            record,
            local_stage,
            remote_stage,
            str(exc),
        )
        slot = prepare_result_view_slot(local_stage, remote_stage, record.view_id, reuse=True)
    return _PreparedResultView(
        action="revise",
        view_id=record.view_id,
        prompt_path=str(slot / record.source_name),
        record=record,
        before=before,
    )


def _record_result_view_rejection(
    execution: AgentTaskExecution,
    prepared: _PreparedResultView,
    problem: str,
    *,
    restored: bool | None = None,
    restore_problem: str | None = None,
    snapshot_cleared: bool | None = None,
    snapshot_clear_problem: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "action": prepared.action,
        "view_id": prepared.view_id,
        "problem": problem[:1600],
    }
    if restored is not None:
        payload["restored"] = restored
    if restore_problem is not None:
        payload["restore_problem"] = restore_problem[:800]
    if snapshot_cleared is not None:
        payload["snapshot_cleared"] = snapshot_cleared
    if snapshot_clear_problem is not None:
        payload["snapshot_clear_problem"] = snapshot_clear_problem[:800]
    with suppress(Exception):
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "result_view_rejected",
            payload,
            tier="diagnostic",
        )
    detail = f"Result view was not updated: {problem}"
    if restore_problem:
        detail += f" Its previous bytes also could not be restored: {restore_problem}"
    if snapshot_clear_problem:
        detail += f" Its rollback snapshot was retained for stage cleanup: {snapshot_clear_problem}"
    with suppress(Exception):
        execution.store.record_agent_task_event(
            execution.operation_id,
            detail,
            level="warning",
        )


def _restore_rejected_result_view(
    execution: AgentTaskExecution,
    prepared: _PreparedResultView | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    problem: str,
) -> None:
    if prepared is None or prepared.action != "revise" or prepared.before is None:
        return
    restored: bool | None = None
    restore_problem: str | None = None
    snapshot_cleared: bool | None = None
    snapshot_clear_problem: str | None = None
    try:
        restored = restore_result_view(
            local_stage,
            remote_stage,
            prepared.view_id,
            prepared.before,
        )
    except Exception as exc:
        restore_problem = str(exc)
    else:
        try:
            verified = discover_result_view(
                local_stage,
                remote_stage,
                prepared.view_id,
                expected_name=prepared.before.name,
            )
            if verified.sha256 != prepared.before.sha256 or verified.size != prepared.before.size:
                raise ValueError("the restored public bytes do not match the saved view")
        except Exception as exc:
            restore_problem = str(exc)
        else:
            try:
                snapshot_cleared = clear_result_view_rollback_snapshot(
                    local_stage,
                    remote_stage,
                    prepared.view_id,
                    prepared.before,
                )
                if not snapshot_cleared:
                    raise ValueError("the rollback snapshot was already absent")
            except Exception as exc:
                snapshot_clear_problem = str(exc)
    _record_result_view_rejection(
        execution,
        prepared,
        problem,
        restored=restored,
        restore_problem=restore_problem,
        snapshot_cleared=snapshot_cleared,
        snapshot_clear_problem=snapshot_clear_problem,
    )


def _finalize_result_view_turn(
    request: RunRequest,
    execution: AgentTaskExecution | None,
    prepared: _PreparedResultView | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    native_session_id: str | None,
) -> bool:
    """Validate and bind a view without coupling its outcome to answer or graph delivery."""

    if prepared is None:
        return True
    assert execution is not None
    try:
        snapshot = discover_result_view(
            local_stage,
            remote_stage,
            prepared.view_id,
            expected_name=(prepared.record.source_name if prepared.record is not None else None),
        )
        now = datetime.fromisoformat(execution.store.now()).astimezone(UTC)
        expires_at = _result_view_expiry(now)
        if prepared.action == "create":
            if not native_session_id:
                raise ValueError("the provider returned no native session for later revision")
            task = _result_view_task(execution)
            assert request.node_id is not None
            assert request.chat_id is not None
            record = execution.store.create_result_view(
                ResultViewRecord(
                    view_id=prepared.view_id,
                    project_id=task.project_id,
                    experiment_id=request.node_id,
                    chat_id=request.chat_id,
                    origin_operation_id=prepared.origin_operation_id or task.operation_id,
                    latest_operation_id=execution.operation_id,
                    provider=request.provider or "",
                    model=request.model or "",
                    reasoning=request.reasoning or "",
                    run_on=request.run_on or "",
                    native_session_id=native_session_id,
                    stage_host=execution.stage_host or "",
                    stage_root=execution.stage_root or "",
                    source_name=snapshot.name,
                    content_sha256=snapshot.sha256,
                    size_bytes=snapshot.size,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                    expires_at=expires_at,
                )
            )
            category = "result_view_created"
        else:
            assert prepared.record is not None
            assert prepared.before is not None
            if native_session_id != prepared.record.native_session_id:
                raise ValueError(
                    "the provider did not resume the result view's exact native session"
                )
            require_result_view_changed(prepared.before, snapshot)
            record = execution.store.revise_result_view(
                prepared.view_id,
                expected_content_sha256=prepared.before.sha256,
                latest_operation_id=execution.operation_id,
                content_sha256=snapshot.sha256,
                size_bytes=snapshot.size,
                updated_at=now.isoformat(),
                expires_at=expires_at,
            )
            category = "result_view_revised"
    except Exception as exc:
        if prepared.action == "revise":
            _restore_rejected_result_view(
                execution,
                prepared,
                local_stage,
                remote_stage,
                str(exc),
            )
        else:
            _record_result_view_rejection(execution, prepared, str(exc))
        return True

    if prepared.action == "revise" and prepared.before is not None:
        try:
            cleared = clear_result_view_rollback_snapshot(
                local_stage,
                remote_stage,
                prepared.view_id,
                prepared.before,
            )
            if not cleared:
                raise ValueError("the rollback snapshot was already absent")
        except Exception as exc:
            with suppress(Exception):
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"The accepted result view's rollback snapshot was retained: {exc}",
                    level="warning",
                )

    payload = {
        "view_id": record.view_id,
        "experiment_id": record.experiment_id,
        "chat_id": record.chat_id,
        "source_name": record.source_name,
        "content_sha256": record.content_sha256,
        "size_bytes": record.size_bytes,
        "updated_at": record.updated_at,
        "expires_at": record.expires_at,
        "native_session_id": record.native_session_id,
        "stage_host": record.stage_host,
        "stage_root": record.stage_root,
    }
    with suppress(Exception):
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            category,
            payload,
        )
    with suppress(Exception):
        execution.store.record_agent_task_event(
            execution.operation_id,
            "Result view created." if prepared.action == "create" else "Result view revised.",
        )
    return True


def _read_correction_patch(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    *,
    pre_launch_digest: str | None,
) -> _CorrectionPatchRead:
    """Classify one correction round's patch output without applying policy."""

    try:
        corrected = _read_chat_patch(workspace, remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        return _CorrectionPatchRead(text=None, problem="unreadable", detail=str(exc))
    if corrected is None:
        return _CorrectionPatchRead(text=None, problem="missing")
    if (
        pre_launch_digest is not None
        and hashlib.sha256(corrected.encode("utf-8")).hexdigest() == pre_launch_digest
    ):
        return _CorrectionPatchRead(text=None, problem="unchanged")
    return _CorrectionPatchRead(text=corrected)


def _work_patch_source_operation_id(
    execution: AgentTaskExecution | None,
    patch_kind: Literal["work", "experiment_loop"],
) -> str | None:
    if execution is None:
        return None
    return execution.operation_id


def _prepare_work_chat_prompt(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    artifact_path: str,
    master_context: str,
    stable_values: dict[str, object],
    skill_pointers: list[dict[str, object]],
    attachment_pointers: list[dict[str, object]],
    result_view: _PreparedResultView | None,
) -> tuple[str, str]:
    """Prepare the provisional session baseline behind one Work-local seam."""

    if request.message is None:
        raise ValueError("An ordinary Work turn requires a human message.")
    bootstrap_path, context_delta, retained_master_path = _prepare_chat_prompt_state(
        execution,
        request,
        local_stage=local_stage,
        remote_stage=remote_stage,
        master_context=master_context,
        contract_key=f"chat-master-v{CHAT_MASTER_CONTEXT_VERSION}",
        values=stable_values,
    )
    prompt = PromptFactory.work_turn_prompt(
        artifact_path=artifact_path,
        human_message=request.message,
        master_context_path=bootstrap_path,
        context_delta=context_delta,
        invoked_skill_pointers=invoked_package_pointers(
            skill_pointers,
            workflow_ids=request.invoked_workflow_ids,
            skill_ids=request.invoked_skill_ids,
        ),
        invoked_provider_skills=request.resolved_provider_skills,
        attachments=attachment_pointers,
        result_view_action=result_view.action if result_view is not None else None,
        result_view_path=result_view.prompt_path if result_view is not None else None,
    )
    return prompt, retained_master_path


def _retry_deliverable_is_unchanged(
    execution: AgentTaskExecution | None,
    *,
    filename: str,
    predecessor_digest: str | None,
    current_text: str | None,
) -> bool:
    """Record whether a reused Retry stage still contains its predecessor's output."""

    if execution is None or execution.continuation != "retry":
        return False
    current_digest = (
        hashlib.sha256(current_text.encode("utf-8")).hexdigest()
        if current_text is not None
        else None
    )
    unchanged = predecessor_digest is not None and current_digest == predecessor_digest
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "retry_deliverable_comparison",
        {
            "filename": filename,
            "predecessor_sha256": predecessor_digest,
            "retry_sha256": current_digest,
            "unchanged": unchanged,
            "consumed": current_text is not None and not unchanged,
        },
        tier="diagnostic",
    )
    return unchanged


def _experiment_maintenance_binding(
    execution: AgentTaskExecution,
    staged: StagedExperimentWatcherResource,
) -> WatcherBinding:
    """Bind maintenance authority from the durable actor and staged node resource."""

    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("Experiment watcher maintenance actor is no longer available.")
    if task.kind not in {"node_chat", "project_chat"}:
        raise ValueError("Experiment watcher maintenance requires a durable chat actor.")
    chat_id = task.request.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("Experiment watcher maintenance actor has no durable conversation.")
    resource = staged.resource
    return WatcherBinding(
        project_id=task.project_id,
        origin_operation_id=execution.operation_id,
        origin_task_kind=task.kind,
        chat_id=chat_id,
        node_id=resource.control_node_id,
        execution_host=resource.execution_host,
        continuation=resource.continuation,
    )


async def _process_experiment_watcher_maintenance(
    *,
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    execution: AgentTaskExecution | None,
    staged_resources: list[StagedExperimentWatcherResource],
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    local_stage: Path | None,
    base_contract_path: str,
    token: str,
    native_session_id: str | None,
    read_dirs: list[Path | PurePosixPath],
    write_dirs: list[Path | PurePosixPath],
    execution_host: str,
    provider_binary: str | None,
    retry_output_digests: dict[str, str],
) -> tuple[list[str], str | None, bool]:
    """Admit, validate, and atomically persist each physical Experiment watcher file."""

    if execution is None:
        return [], native_session_id, False
    frames: list[str] = []
    staged_by_name = {
        experiment_watcher_output_name(item.resource.control_node_id): item
        for item in staged_resources
    }
    try:
        outputs = read_experiment_watcher_outputs(workspace, remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"Experiment watcher maintenance output could not be inspected: {exc}",
            level="warning",
        )
        return frames, native_session_id, False

    for name, initial_text in sorted(outputs.items()):
        # A Retry reuses the conversation's folder without clearing it, so a
        # previous attempt's maintenance file is still sitting there. Applying it
        # would commit that attempt's handoff under this attempt's authorization
        # (invariant 10c), so an unchanged survivor counts as nothing written.
        if _retry_deliverable_is_unchanged(
            execution,
            filename=name,
            predecessor_digest=retry_output_digests.get(name),
            current_text=initial_text,
        ):
            continue
        staged = staged_by_name.get(name)
        if staged is None:
            problem = (
                "Experiment watcher maintenance permission denied: the physical output path was "
                "not staged for this actor's resolved resource scope."
            )
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "experiment_watcher_maintenance_rejected",
                {"path": str(workspace / name), "problem": problem},
                tier="diagnostic",
            )
            execution.store.record_agent_task_event(
                execution.operation_id,
                problem,
                level="warning",
            )
            continue

        text = initial_text
        correction_round = 0
        target_digest = hashlib.sha256(staged.resource.control_node_id.encode("utf-8")).hexdigest()[
            :16
        ]

        def reject_maintenance(
            problem: str,
            target: StagedExperimentWatcherResource,
            rounds: int,
        ) -> None:
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "experiment_watcher_maintenance_rejected",
                {
                    "control_node_id": target.resource.control_node_id,
                    "episode_id": target.resource.episode_id,
                    "path": target.watch_path,
                    "problem": problem[:1600],
                    "correction_rounds": rounds,
                },
                tier="diagnostic",
            )
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Experiment watcher maintenance was not applied: {problem}",
                level="warning",
            )

        while True:
            binding = _experiment_maintenance_binding(execution, staged)
            try:
                execution.store.admit_experiment_watcher_maintenance(binding)
            except ValueError as exc:
                problem = str(exc)
                correctable = False
            else:
                try:
                    handoff = parse_experiment_watch_json(text)
                    graph_state = (
                        await asyncio.to_thread(service.history.state)
                        if handoff.graph_conditions
                        else None
                    )
                    graph_armed_revision = graph_state.revision if graph_state is not None else None
                    if graph_state is not None:
                        await asyncio.to_thread(
                            validate_graph_conditions,
                            handoff.graph_conditions,
                            graph_state,
                        )
                    check_results = (
                        await asyncio.to_thread(
                            validate_watch_specs,
                            handoff.observers,
                            staged.resource.execution_host,
                        )
                        if handoff.observers
                        else []
                    )
                except (WatcherInitialCheckError, ValueError) as exc:
                    problem = str(exc)
                    correctable = True
                except (OSError, ReplayHalted, StateUnavailable) as exc:
                    problem = str(exc)
                    correctable = False
                else:
                    try:
                        fresh_graph_state = (
                            await asyncio.to_thread(service.history.state)
                            if handoff.graph_conditions
                            else None
                        )
                        if handoff.graph_conditions:
                            # Mark the settlement before the insert attempt. If
                            # cancellation lands after SQLite commits but before
                            # this await resumes, ordered reconciliation must
                            # still catch canonical movement after validation.
                            execution.armed_graph_watchers = True
                        armed = await asyncio.to_thread(
                            persist_experiment_watchers_idempotently,
                            execution,
                            handoff.observers,
                            check_results,
                            binding,
                            handoff.stops,
                            graph_conditions=handoff.graph_conditions,
                            graph_state=fresh_graph_state,
                            armed_revision=graph_armed_revision,
                            expected_watcher_snapshot_token=(
                                staged.resource.watcher_snapshot_token
                            ),
                        )
                    except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
                        problem = str(exc)
                        correctable = False
                    else:
                        execution.store.record_agent_task_receipt(
                            execution.operation_id,
                            "experiment_watchers_maintained",
                            {
                                "control_node_id": staged.resource.control_node_id,
                                "episode_id": staged.resource.episode_id,
                                "watcher_ids": [item.watcher_id for item in armed],
                                "stopped_watcher_ids": [
                                    item.stop_watcher_id for item in handoff.stops
                                ],
                                "correction_rounds": correction_round,
                            },
                        )
                        break

            if (
                not correctable
                or correction_round >= _MAX_CORRECTION_ROUNDS
                or not native_session_id
            ):
                reject_maintenance(problem, staged, correction_round)
                break

            correction_round += 1
            diagnostics_path = _stage_json_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-experiment-watch-correction-{target_digest}-{correction_round}.json",
                {
                    "control_node_id": staged.resource.control_node_id,
                    "problem": problem,
                },
            )
            correction_contract = experiment_watcher_maintenance_correction_contract(
                original_contract_path=base_contract_path,
                diagnostics_path=diagnostics_path,
                watch_path=staged.watch_path,
            )
            correction_path, correction_prompt = _stage_task_contract(
                local_stage,
                remote_stage,
                f"task-{token}-experiment-watch-correction-{target_digest}-{correction_round}.md",
                correction_contract,
                execution=execution,
                role=f"experiment_watch_correction_{target_digest}_{correction_round}",
            )
            before_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            _record_agent_launch_receipt(
                execution,
                request,
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(execution_host),
                resumed=True,
                continuation="watch_correction",
                extra={
                    "surface": binding.origin_task_kind,
                    "mode": "work",
                    "capability": "work_auto",
                    "network_access": True,
                    "launch_kind": "experiment_watch_correction",
                    "correction_round": correction_round,
                    "control_node_id": staged.resource.control_node_id,
                    "write_directory_count": len(write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
            correction_outcome = _ProviderOutcome(session_id=native_session_id)
            correction_error: str | None = None
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    correction_prompt,
                    workspace=workspace,
                    session_id=native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="work_auto",
                    outcome=correction_outcome,
                    binary=provider_binary,
                )
            ) as stream:
                async for frame in stream:
                    event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                    if event.event == "error":
                        correction_error = event.text or "Watcher maintenance correction failed."
                    elif event.event not in {"answer", "done"}:
                        frames.append(frame)
            native_session_id = correction_outcome.session_id or native_session_id
            if correction_outcome.paused:
                return frames, native_session_id, True
            if correction_error or not correction_outcome.completed:
                problem = correction_error or (
                    f"{request.provider} produced no watcher maintenance correction result."
                )
                reject_maintenance(problem, staged, correction_round)
                break
            try:
                corrected_outputs = read_experiment_watcher_outputs(workspace, remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                problem = f"The corrected watcher maintenance output could not be read: {exc}"
                reject_maintenance(problem, staged, correction_round)
                break
            corrected = corrected_outputs.get(name)
            if corrected is None:
                problem = "The correction completed without rewriting the Experiment watcher file."
                reject_maintenance(problem, staged, correction_round)
                break
            if hashlib.sha256(corrected.encode("utf-8")).hexdigest() == before_digest:
                problem = (
                    f"{problem} The correction left the Experiment watcher file byte-identical."
                )
                reject_maintenance(problem, staged, correction_round)
                break
            text = corrected

    return frames, native_session_id, False


async def stream_work_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    """Run one operational conversation turn with optional graph reflection."""

    if execution is not None and execution.continuation == "graph_repair":
        async with aclosing(
            _stream_work_graph_repair(
                service,
                launcher,
                request,
                data_dir,
                execution=execution,
            )
        ) as stream:
            async for frame in stream:
                yield frame
        return

    continuation = execution.continuation if execution is not None else "fresh"
    reusing_checkpoint = bool(execution is not None and execution.reuses_native_checkpoint)
    resuming = continuation == "resume"
    retrying = continuation == "retry"
    # An Experiment-loop watcher wake resumes the episode's native session, but it
    # is a new turn at the next invocation -- never task Resume, never a retry, and
    # never a rebuilt master contract.
    waking = continuation == "watcher_wake"
    retry_attempt = continuation in {"retry", "handoff"}
    uses_master_protocol = (
        request.trigger == "human" and request.patch_kind == "work" and not retry_attempt
    )
    surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        request = _pinned_to_profile(request, profile)
        revision_record = _preflight_result_view_revision(request, execution)
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    local_stage: Path | None = None
    execution_machine = service.manifest.machine_map[profile.run_on]
    execution_host = execution_machine.host
    provider_binary = execution_machine.provider_paths.get(profile.provider)
    remote_stage: RemoteRunStage | None = None
    artifact_scope_id: str | None = None
    artifact_directory: Path | PurePosixPath | None = None
    prepared_result_view: _PreparedResultView | None = None
    patch_inputs = None
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None
    outcome = _ProviderOutcome(session_id=request.session_id)
    validator_budget = PatchValidationBudget()
    try:
        context = service.assemble_chat(request)
        _record_chat_context_receipt(execution, context, surface=surface)
        stage_name = _chat_stage_name(service, request, execution)
        saved_stage = execution is not None and execution.stage_root is not None
        if execution_host:
            if saved_stage:
                stage_root = _validated_remote_chat_resume_stage(
                    execution, execution_host, stage_name
                )
                remote_stage = RemoteRunStage(execution_host).attach(stage_root)
            else:
                remote_stage = RemoteRunStage(execution_host).open(stage_name, reuse=True)
            assert remote_stage.root is not None
            if execution is not None:
                execution.checkpoint_stage(execution_host, str(remote_stage.root))
            context = context.model_copy(
                update=_stage_context_paths(context, service, remote_stage, execution_machine.alias)
            )
            workspace = Path(str(remote_stage.workspace))
        else:
            stage_root = _swept_stage_root(data_dir)
            expected_stage = stage_root / stage_name
            if saved_stage:
                local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            else:
                local_stage = expected_stage
                local_stage.mkdir(parents=True, exist_ok=True)
            if execution is not None:
                execution.checkpoint_stage("", str(local_stage))
            workspace = local_stage
        _roll_result_view_retention(request, execution, local_stage, remote_stage)
        token = _task_token(execution)
        patch_inputs = _stage_chat_patch_inputs(
            local_stage,
            remote_stage,
            workspace=workspace,
            stage_name=stage_name,
            task_id=execution.operation_id if execution is not None else token,
            turn_id=f"{token}:work",
        )
        validator_lifecycle = _start_work_validator_mailbox(
            service,
            patch_inputs.validator_staged,
            execution=execution,
            budget=validator_budget,
            run_truth_scope=context.run_truth_scope,
            patch_kind=request.patch_kind,
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
        )
        patch_path = patch_inputs.patch_path
        watch_path = patch_inputs.watch_path
        schema_path = patch_inputs.schema_path
        validator_command = patch_inputs.validator_command
        validator_mailbox_id = patch_inputs.validator_mailbox_id
        if not reusing_checkpoint or waking:
            _clear_stale_turn_handoffs(workspace, remote_stage)
        artifact_scope_id = (
            _logical_chat_turn_operation_id(execution.store, execution.operation_id)
            if execution is not None and resuming
            else execution.operation_id
            if execution is not None
            else str(uuid.uuid4())
        )
        prepared_result_view = _prepare_result_view_turn(
            request,
            execution,
            local_stage,
            remote_stage,
            focused_node=context.node,
            logical_operation_id=artifact_scope_id,
            revision_record=revision_record,
        )
        if remote_stage is not None:
            artifact_directory = remote_stage.prepare_artifact_directory(
                artifact_scope_id, reuse=resuming
            )
        else:
            assert local_stage is not None
            artifact_directory = _prepare_local_artifact_directory(
                local_stage, artifact_scope_id, reuse=resuming
            )
        read_dirs = _chat_read_dirs(
            context,
            remote_stage,
            service,
            execution_machine.alias,
        )
        write_dirs = _work_write_dirs(
            context,
            service,
            execution_machine.alias,
            remote=remote_stage is not None,
        )
        experiment_resources = (
            await stage_chat_experiment_watcher_resources(
                request,
                execution,
                local_stage,
                remote_stage,
                workspace=workspace,
                token=token,
                clear_stale=not reusing_checkpoint or waking,
            )
            if request.patch_kind == "work"
            else []
        )
        experiment_resource_pointers = [item.prompt_value() for item in experiment_resources]
        skill_selection = service.resolve_skill_selection(request)
        skill_pointers = stage_skill_selection(
            skill_selection,
            local_stage=local_stage,
            remote_stage=remote_stage,
            label=skill_bundle_label(skill_selection),
            reuse_existing=True,
        )
        if bool(request.attachment_batch_id) != bool(request.attachments):
            raise ValueError("The chat task has incomplete attachment batch metadata.")
        attachment_pointers = (
            ChatAttachmentStore(data_dir / "chat-attachments").stage(
                request.attachment_batch_id,
                request.attachments,
                local_stage=local_stage,
                remote_stage=remote_stage,
            )
            if request.attachment_batch_id
            else []
        )
        read_dirs.extend(
            path
            for path in dict.fromkeys(
                Path(str(item["path"])).parent for item in attachment_pointers
            )
            if path not in read_dirs
        )
        repositories = [
            {"alias": item.alias, "host": item.host, "path": item.path}
            for item in context.repositories
        ]
        episode_context_baseline: dict[str, object] | None = None
        wake_episode = None
        context_replacement: dict[str, object] | None = None
        loop_control_path: str | None = None
        watcher_state_path: str | None = None
        provider_switch_recovery = False
        if request.patch_kind == "experiment_loop":
            control_node = context.node
            if (
                control_node is None
                or control_node.get("id") != request.control_node_id
                or control_node.get("type") != "experiment"
            ):
                raise ValueError("Experiment-loop work no longer resolves to its Experiment.")
            loop_control_path, watcher_state_path = await stage_experiment_loop_context(
                service,
                request,
                execution,
                local_stage,
                remote_stage,
                token=token,
                continuation=continuation,
            )
            assert execution is not None
            episode = (
                execution.store.experiment_episode(request.control_episode_id)
                if request.control_episode_id
                else None
            )
            provider_switch_recovery = bool(
                continuation == "handoff" and episode is not None and episode.session_bound
            )
            ontology = service.history.state().ontology.model_dump(mode="json")
            episode_context_baseline = prepare_experiment_episode_context_candidate(
                execution,
                experiment_episode_context_values(
                    ontology_extensions=context.ontology_extensions,
                    ontology=ontology,
                    repositories=repositories,
                    skill_pointers=skill_pointers,
                ),
            )
            if waking:
                if not request.control_episode_id or request.control_invocation is None:
                    raise ValueError("Experiment-loop wake is missing its episode invocation.")
                wake_episode = execution.store.experiment_episode(request.control_episode_id)
                if wake_episode is None or not wake_episode.session_bound:
                    raise ValueError(
                        "Experiment-loop wake has no committed episode session to continue."
                    )
                if (
                    wake_episode.native_session_id != request.session_id
                    or wake_episode.stage_host != execution.stage_host
                    or wake_episode.stage_root != execution.stage_root
                ):
                    raise ValueError(
                        "Experiment-loop wake does not match its committed native session and "
                        "exact stage."
                    )
                if wake_episode.last_turn_invocation != request.control_invocation - 1:
                    raise ValueError(
                        "Experiment-loop wake does not immediately follow the episode's last "
                        "successful turn."
                    )
                if not wake_episode.last_graph_result:
                    raise ValueError(
                        "Experiment-loop wake cannot confirm the preceding graph handoff."
                    )
                context_replacement = _chat_context_delta(
                    wake_episode.context_baseline,
                    episode_context_baseline,
                )
        if reusing_checkpoint and not request.session_id:
            raise ValueError(
                "The continued Work turn has no native agent session; retry it from a clean "
                "attempt instead."
            )
        if resuming:
            assert execution is not None
            original_contract_path = _parent_task_contract_path(
                execution, local_stage, remote_stage
            )
            base_contract_path = original_contract_path
            if request.patch_kind == "experiment_loop":
                if not loop_control_path:
                    raise ValueError("Experiment-loop Resume is missing fresh loop control.")
                contract = experiment_loop_continuation_contract(
                    original_contract_path=original_contract_path,
                    mode="resume",
                    loop_control_path=loop_control_path,
                    patch_path=patch_path,
                    watch_path=watch_path,
                    output_schema_path=schema_path,
                    validator_command=validator_command,
                    invoked_skill_pointers=invoked_package_pointers(
                        skill_pointers,
                        workflow_ids=request.invoked_workflow_ids,
                        skill_ids=request.invoked_skill_ids,
                    ),
                )
                contract += invoked_provider_skill_section(request.resolved_provider_skills)
            else:
                contract = PromptFactory.continuation_task_contract(
                    original_contract_path=original_contract_path,
                    mode="resume",
                    patch_path=patch_path,
                    validator_command=validator_command,
                    invoked_skill_pointers=invoked_package_pointers(
                        skill_pointers,
                        workflow_ids=request.invoked_workflow_ids,
                        skill_ids=request.invoked_skill_ids,
                    ),
                    invoked_provider_skills=request.resolved_provider_skills,
                    result_view_action=(
                        prepared_result_view.action if prepared_result_view is not None else None
                    ),
                    result_view_path=(
                        prepared_result_view.prompt_path
                        if prepared_result_view is not None
                        else None
                    ),
                )
            contract_path, prompt = _stage_task_contract(
                local_stage,
                remote_stage,
                f"task-{token}-resume.md",
                contract,
                execution=execution,
                role="work_resume",
            )
        elif waking:
            if (
                wake_episode is None
                or not request.control_node_id
                or request.control_invocation is None
                or request.control_invocation_ceiling is None
                or not loop_control_path
                or not watcher_state_path
            ):
                raise ValueError("Experiment-loop wake inputs are incomplete after staging.")
            contract = experiment_loop_wake_message(
                focused_experiment_id=request.control_node_id,
                invocation=request.control_invocation,
                invocation_ceiling=request.control_invocation_ceiling,
                previous_graph_result=wake_episode.last_graph_result or "",
                previous_watcher_ids=wake_episode.last_watcher_ids,
                delivered_watcher_ids=request.watcher_ids,
                loop_control_path=loop_control_path,
                watcher_state_path=watcher_state_path,
                graph_path=context.graph_path,
                research_path=context.research_md_path,
                patch_path=patch_path,
                watch_path=watch_path,
                output_schema_path=schema_path,
                validator_command=validator_command,
                execution_host=execution_host,
                context_replacement=context_replacement,
                invoked_skill_pointers=invoked_package_pointers(
                    skill_pointers,
                    workflow_ids=request.invoked_workflow_ids,
                    skill_ids=request.invoked_skill_ids,
                ),
            )
            contract_path, prompt = _stage_task_contract(
                local_stage,
                remote_stage,
                f"task-{token}-watcher-wake.md",
                contract,
                execution=execution,
                role="experiment_loop_wake",
            )
            base_contract_path = contract_path
        else:
            assert request.message is not None
            focused_node_id = str(context.node["id"]) if context.node else None
            retry_diagnostics_path = (
                _stage_json_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-retry-diagnostics.json",
                    {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                )
                if execution is not None
                and not uses_master_protocol
                and (execution.retry_feedback or retry_attempt)
                else None
            )
            # A retry that still holds its native session already has the contract in the
            # conversation; it gets a follow-up naming what changed, not a rebuilt contract.
            resumed_retry = retrying and reusing_checkpoint
            loop_retry = request.patch_kind == "experiment_loop" and retrying
            explicit_contract = not uses_master_protocol and not resumed_retry and not loop_retry
            current_contract_path = None
            current_prompt = None
            if explicit_contract:
                human_request_path = _stage_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-human-request.txt",
                    request.message,
                )
                if request.patch_kind == "experiment_loop":
                    if (
                        not request.control_node_id
                        or not loop_control_path
                        or not watcher_state_path
                    ):
                        raise ValueError(
                            "Experiment-loop contract inputs are incomplete after staging."
                        )
                    contract = experiment_loop_task_contract(
                        project_name=context.project_name,
                        ontology_path=f"{context.graph_path}#ontology",
                        ontology_extensions=context.ontology_extensions,
                        graph_path=context.graph_path,
                        research_path=context.research_md_path,
                        focused_experiment_id=request.control_node_id,
                        repositories=repositories,
                        introduction_path=context.introduction_path,
                        human_request_path=human_request_path,
                        loop_control_path=loop_control_path,
                        watcher_state_path=watcher_state_path,
                        patch_path=patch_path,
                        watch_path=watch_path,
                        artifact_path=str(artifact_directory),
                        output_schema_path=schema_path,
                        validator_command=validator_command,
                        execution_host=execution_host,
                        recovery_diagnostics_path=(
                            retry_diagnostics_path if provider_switch_recovery else None
                        ),
                        skill_pointers=skill_pointers,
                        invoked_skill_pointers=invoked_package_pointers(
                            skill_pointers,
                            workflow_ids=request.invoked_workflow_ids,
                            skill_ids=request.invoked_skill_ids,
                        ),
                    )
                    contract += invoked_provider_skill_section(request.resolved_provider_skills)
                else:
                    contract = PromptFactory.work_task_contract(
                        project_name=context.project_name,
                        ontology_path=f"{context.graph_path}#ontology",
                        ontology_extensions=context.ontology_extensions,
                        graph_path=context.graph_path,
                        research_path=context.research_md_path,
                        focused_node_id=focused_node_id,
                        repositories=repositories,
                        introduction_path=context.introduction_path,
                        human_request_path=human_request_path,
                        patch_path=patch_path,
                        artifact_path=str(artifact_directory),
                        output_schema_path=schema_path,
                        retry_diagnostics_path=retry_diagnostics_path,
                        watch_path=watch_path,
                        execution_host=execution_host,
                        experiment_watcher_resources=experiment_resource_pointers,
                        validator_command=validator_command,
                        skill_pointers=skill_pointers,
                        invoked_skill_pointers=invoked_package_pointers(
                            skill_pointers,
                            workflow_ids=request.invoked_workflow_ids,
                            skill_ids=request.invoked_skill_ids,
                        ),
                        invoked_provider_skills=request.resolved_provider_skills,
                        attachments=attachment_pointers,
                    )
                current_contract_path, current_prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-{'base' if retry_attempt else 'initial'}.md",
                    contract,
                    execution=execution,
                    role="work_retry_base" if retry_attempt else "work",
                )
                base_contract_path = current_contract_path
            elif uses_master_protocol:
                master_context = PromptFactory.chat_master_context(
                    project_name=context.project_name,
                    ontology_path=f"{context.graph_path}#ontology",
                    ontology_extensions=context.ontology_extensions,
                    graph_path=context.graph_path,
                    research_path=context.research_md_path,
                    graph_revision=context.graph_revision,
                    focused_node_id=focused_node_id,
                    focused_node=context.node,
                    focused_relations=[item.model_dump(mode="json") for item in context.relations],
                    repositories=repositories,
                    introduction_path=context.introduction_path,
                    patch_path=patch_path,
                    workspace_path=str(workspace),
                    output_schema_path=schema_path,
                    validator_command=validator_command,
                    watch_path=watch_path,
                    execution_host=execution_host,
                    experiment_watcher_resources=experiment_resource_pointers,
                    skill_pointers=skill_pointers,
                )
                stable_prompt_values: dict[str, object] = {
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
                        "focused_node_id": focused_node_id,
                        "introduction_path": context.introduction_path,
                        "experiment_watcher_resources": experiment_resource_pointers,
                    },
                    "repositories": repositories,
                    "skills": {"pointers": skill_pointers},
                    "patch": {
                        "path": patch_path,
                        "watch_path": watch_path,
                        "schema_path": schema_path,
                        "validator_command": validator_command,
                        "validator_mailbox_id": validator_mailbox_id,
                    },
                    "workspace": {"path": str(workspace)},
                }
                prompt, retained_master_path = _prepare_work_chat_prompt(
                    execution,
                    request,
                    local_stage=local_stage,
                    remote_stage=remote_stage,
                    artifact_path=str(artifact_directory),
                    master_context=master_context,
                    stable_values=stable_prompt_values,
                    skill_pointers=skill_pointers,
                    attachment_pointers=attachment_pointers,
                    result_view=prepared_result_view,
                )
                contract_path = retained_master_path
                base_contract_path = retained_master_path

            result_view_handoff = bool(
                continuation == "handoff" and prepared_result_view is not None
            )
            if retrying or result_view_handoff:
                assert execution is not None
                if result_view_handoff:
                    if current_contract_path is None:
                        raise ValueError(
                            "The result view create handoff lost its current Work contract."
                        )
                    original_contract_path = current_contract_path
                    continuation_contract_path = None
                else:
                    original_contract_path = _parent_task_contract_path(
                        execution, local_stage, remote_stage
                    )
                    continuation_contract_path = current_contract_path
                if resumed_retry:
                    base_contract_path = original_contract_path
                if request.patch_kind == "experiment_loop":
                    if not loop_control_path or not retry_diagnostics_path:
                        raise ValueError(
                            "Experiment-loop Retry is missing fresh control or diagnostics."
                        )
                    retry_contract = experiment_loop_continuation_contract(
                        original_contract_path=original_contract_path,
                        mode="retry",
                        loop_control_path=loop_control_path,
                        patch_path=patch_path,
                        watch_path=watch_path,
                        output_schema_path=schema_path,
                        validator_command=validator_command,
                        diagnostics_path=retry_diagnostics_path,
                        invoked_skill_pointers=invoked_package_pointers(
                            skill_pointers,
                            workflow_ids=request.invoked_workflow_ids,
                            skill_ids=request.invoked_skill_ids,
                        ),
                    )
                    retry_contract += invoked_provider_skill_section(
                        request.resolved_provider_skills
                    )
                else:
                    retry_contract = PromptFactory.continuation_task_contract(
                        original_contract_path=original_contract_path,
                        current_contract_path=continuation_contract_path,
                        diagnostics_path=retry_diagnostics_path,
                        patch_path=patch_path,
                        watch_path=watch_path,
                        mode="retry",
                        validator_command=validator_command,
                        output_schema_path=schema_path if resumed_retry else None,
                        skill_pointers=skill_pointers if resumed_retry else None,
                        invoked_skill_pointers=invoked_package_pointers(
                            skill_pointers,
                            workflow_ids=request.invoked_workflow_ids,
                            skill_ids=request.invoked_skill_ids,
                        ),
                        invoked_provider_skills=request.resolved_provider_skills,
                        result_view_action=(
                            prepared_result_view.action
                            if prepared_result_view is not None
                            else None
                        ),
                        result_view_path=(
                            prepared_result_view.prompt_path
                            if prepared_result_view is not None
                            else None
                        ),
                    )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-retry.md",
                    retry_contract,
                    execution=execution,
                    role="work_retry",
                )
            elif explicit_contract:
                contract_path, prompt = current_contract_path, current_prompt

        retry_patch_digest: str | None = None
        retry_watch_digest: str | None = None
        retry_experiment_watch_digests: dict[str, str] = {}
        if retrying:
            assert execution is not None
            predecessor_patch = _read_chat_patch(workspace, remote_stage)
            predecessor_watch = _read_watch_request(workspace, remote_stage)
            retry_experiment_watch_digests = {
                name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for name, text in read_experiment_watcher_outputs(workspace, remote_stage).items()
            }
            retry_patch_digest = (
                hashlib.sha256(predecessor_patch.encode("utf-8")).hexdigest()
                if predecessor_patch is not None
                else None
            )
            retry_watch_digest = (
                hashlib.sha256(predecessor_watch.encode("utf-8")).hexdigest()
                if predecessor_watch is not None
                else None
            )
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "retry_deliverable_baseline",
                {
                    "patch_sha256": retry_patch_digest,
                    "watch_sha256": retry_watch_digest,
                },
                tier="diagnostic",
            )
        _persist_result_view_rollback(
            execution,
            prepared_result_view,
            local_stage,
            remote_stage,
        )
    except BaseException as exc:
        if validator_lifecycle is not None:
            await validator_lifecycle.close(primary_error=exc)
        elif patch_inputs is not None and not patch_inputs.validator_staged.credential.expired:
            await _close_work_validator_mailbox(
                patch_inputs.validator_staged,
                stop=None,
                task=None,
                execution=execution,
                primary_error=exc,
            )
        if isinstance(exc, (OSError, ReplayHalted, StateUnavailable, ValueError)):
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        raise
    else:
        assert validator_lifecycle is not None
        try:
            _record_agent_launch_receipt(
                execution,
                request,
                prompt=prompt,
                contract_path=contract_path,
                remote=bool(execution_host),
                resumed=reusing_checkpoint,
                continuation=continuation,
                extra={
                    "surface": surface,
                    "mode": "work",
                    "capability": "work_auto",
                    "network_access": True,
                    "launch_kind": (
                        "retry"
                        if retry_attempt
                        else "resume"
                        if resuming
                        else "watcher_wake"
                        if waking
                        else "initial"
                    ),
                    "write_directory_count": len(write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
        except BaseException as exc:
            await validator_lifecycle.close(primary_error=exc)
            raise
        result_view_settled = False
        result_view_rollback_problem = "the Work turn ended before the revision could be accepted"
        try:
            try:
                async with aclosing(
                    _stream_work_agent_events(
                        service,
                        launcher,
                        request,
                        prompt,
                        workspace=workspace,
                        session_id=request.session_id,
                        read_dirs=read_dirs,
                        write_dirs=write_dirs,
                        execution_host=execution_host,
                        execution=execution,
                        remote_stage=remote_stage,
                        capability="work_auto",
                        outcome=outcome,
                        binary=provider_binary,
                        validator_staged=patch_inputs.validator_staged,
                        validator_lifecycle=validator_lifecycle,
                        validator_budget=validator_budget,
                        run_truth_scope=context.run_truth_scope,
                        patch_kind=request.patch_kind,
                        control_node_id=request.control_node_id,
                        control_decision_bundle=request.control_decision_bundle,
                    )
                ) as stream:
                    async for frame in stream:
                        yield frame
            except Exception:
                outcome.failed = True
                result_view_rollback_problem = (
                    "the provider launch failed before the revision could be accepted"
                )
                raise

            answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip()).strip()
            if not outcome.completed:
                if outcome.failed or outcome.paused:
                    result_view_rollback_problem = "the provider did not complete the revision"
                    return
                outcome.failed = True
                result_view_rollback_problem = "the provider produced no result"
                yield _sse(
                    AgentEvent(event="error", text=f"{request.provider} produced no result.")
                )
                return
            if not answer:
                result_view_rollback_problem = "the provider finished without an answer"
                yield _sse(
                    AgentEvent(
                        event="error", text=f"{request.provider} finished without answering."
                    )
                )
                return
            if waking and (
                wake_episode is None or outcome.session_id != wake_episode.native_session_id
            ):
                yield _sse(
                    AgentEvent(
                        event="error",
                        text=(
                            "The automatic Experiment wake did not continue its committed native "
                            "provider session. The watcher handoff was not accepted."
                        ),
                    )
                )
                return

            if uses_master_protocol:
                _commit_chat_prompt_state(execution, request, outcome.session_id)

            assert artifact_scope_id is not None
            assert artifact_directory is not None
            try:
                artifacts = _discover_chat_artifacts(
                    execution,
                    artifact_scope_id,
                    Path(str(artifact_directory)),
                    remote_stage,
                )
            except Exception as exc:
                with suppress(Exception):
                    _record_artifact_discovery_receipt(
                        execution,
                        attached=0,
                        candidates=0,
                        ignored={"unexpected_error": 1},
                        detail=str(exc),
                    )
                artifacts = []
            result_view_settled = _finalize_result_view_turn(
                request,
                execution,
                prepared_result_view,
                local_stage,
                remote_stage,
                native_session_id=outcome.session_id,
            )
        finally:
            if not result_view_settled and execution is not None:
                _restore_rejected_result_view(
                    execution,
                    prepared_result_view,
                    local_stage,
                    remote_stage,
                    result_view_rollback_problem,
                )
        yield _sse(AgentEvent(event="answer", text=answer))
        for artifact in artifacts:
            yield _sse(AgentEvent(event="artifact", artifact=artifact))

        graph_update: GraphUpdateResult
        correction_rounds = 0
        native_session_id = outcome.session_id
        try:
            patch_text = _read_chat_patch(workspace, remote_stage)
        except (OSError, StateUnavailable, ValueError) as exc:
            patch_text = None
            failure = _WorkPatchFailure(
                f"The agent wrote a patch file that could not be read: {exc}",
                correctable=False,
            )
        else:
            failure = None
        if _retry_deliverable_is_unchanged(
            execution,
            filename="patch.json",
            predecessor_digest=retry_patch_digest,
            current_text=patch_text,
        ):
            patch_text = None
        deferred_loop_patch = request.patch_kind == "experiment_loop" and patch_text is not None
        if deferred_loop_patch:
            assert patch_text is not None
            if execution is not None:
                execution.store.record_agent_task_patch_output(execution.operation_id, patch_text)
                execution.store.record_agent_task_receipt(
                    execution.operation_id,
                    "patch_retained",
                    {
                        "byte_length": len(patch_text.encode("utf-8")),
                        "file_name": "patch.json",
                    },
                    tier="diagnostic",
                )
            patch_text = None
        if request.patch_kind == "experiment_loop":
            # Loop graph admission is a joint Patch/watch handoff. Nothing in
            # the generic pre-handoff path may validate-correct-and-apply it.
            failure = None
        if patch_text is None and failure is None:
            graph_update = GraphUpdateResult(status="none")
        else:
            while True:
                if patch_text is not None:
                    try:
                        result, failure = _apply_work_patch(
                            service,
                            execution,
                            patch_text,
                            run_truth_scope=context.run_truth_scope,
                            patch_kind=request.patch_kind,
                            control_node_id=request.control_node_id,
                            control_decision_bundle=request.control_decision_bundle,
                        )
                    except RunLockCancelled:
                        yield _sse(
                            AgentEvent(
                                event="paused",
                                text=(
                                    "Paused while waiting for canonical state. The operational "
                                    "answer and retained patch are preserved."
                                ),
                            )
                        )
                        return
                    if result is not None:
                        graph_update = result.model_copy(
                            update={"correction_rounds": correction_rounds}
                        )
                        break
                assert failure is not None
                if (
                    not failure.correctable
                    or correction_rounds >= _MAX_CORRECTION_ROUNDS
                    or not native_session_id
                ):
                    repairable = _work_graph_repairable(
                        execution,
                        native_session_id,
                        failure,
                    )
                    graph_update = GraphUpdateResult(
                        status="rejected",
                        change_summary=list(failure.change_summary),
                        proposal_ids=list(failure.proposal_ids),
                        validation_messages=_bounded_graph_messages(failure.message),
                        correction_rounds=correction_rounds,
                        repairable=repairable,
                    )
                    _record_work_graph_rejection(execution, graph_update)
                    break

                correction_rounds += 1
                if execution is not None:
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_correction_requested",
                        {"round": correction_rounds, "problem": failure.message[:400]},
                        tier="diagnostic",
                    )
                    execution.store.update_agent_task_message(
                        execution.operation_id,
                        "Correcting graph update.",
                        phase="correcting",
                        event=True,
                    )
                diagnostics_path = _stage_json_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-work-correction-{correction_rounds}.json",
                    {"kind": "work", "problem": failure.message},
                )
                correction_validator = stage_patch_validation_mailbox(
                    local_stage=local_stage,
                    remote_stage=remote_stage,
                    task_id=execution.operation_id if execution is not None else token,
                    turn_id=f"{token}:work-patch-correction:{correction_rounds}",
                    timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
                )
                correction_validator_lifecycle = _start_work_validator_mailbox(
                    service,
                    correction_validator,
                    execution=execution,
                    budget=validator_budget,
                    run_truth_scope=context.run_truth_scope,
                    patch_kind=request.patch_kind,
                    control_node_id=request.control_node_id,
                    control_decision_bundle=request.control_decision_bundle,
                )
                try:
                    correction_validator_command = correction_validator.client_command(
                        "validate",
                        patch_path,
                    )
                    correction_contract = PromptFactory.continuation_task_contract(
                        original_contract_path=base_contract_path,
                        mode="work_patch_correction",
                        patch_path=patch_path,
                        diagnostics_path=diagnostics_path,
                        validator_command=correction_validator_command,
                    )
                    correction_path, correction_prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-work-correction-{correction_rounds}.md",
                        correction_contract,
                        execution=execution,
                        role=f"work_patch_correction_{correction_rounds}",
                    )
                    pre_launch_digest = _existing_patch_digest(workspace, remote_stage)
                    _record_agent_launch_receipt(
                        execution,
                        request,
                        prompt=correction_prompt,
                        contract_path=correction_path,
                        remote=bool(execution_host),
                        resumed=True,
                        continuation="graph_correction",
                        extra={
                            "surface": surface,
                            "mode": "work",
                            "capability": "work_auto",
                            "network_access": True,
                            "launch_kind": "graph_correction",
                            "correction_round": correction_rounds,
                            "write_directory_count": len(write_dirs),
                            "canonical_state_boundary": "prompt_only",
                        },
                    )
                    correction_outcome = _ProviderOutcome(session_id=native_session_id)
                    correction_error: str | None = None
                except BaseException as exc:
                    await correction_validator_lifecycle.close(primary_error=exc)
                    raise
                async with aclosing(
                    _stream_work_agent_events(
                        service,
                        launcher,
                        request,
                        correction_prompt,
                        workspace=workspace,
                        session_id=native_session_id,
                        read_dirs=read_dirs,
                        write_dirs=write_dirs,
                        execution_host=execution_host,
                        execution=execution,
                        remote_stage=remote_stage,
                        capability="work_auto",
                        outcome=correction_outcome,
                        binary=provider_binary,
                        validator_staged=correction_validator,
                        validator_lifecycle=correction_validator_lifecycle,
                        validator_budget=validator_budget,
                        run_truth_scope=context.run_truth_scope,
                        patch_kind=request.patch_kind,
                        control_node_id=request.control_node_id,
                        control_decision_bundle=request.control_decision_bundle,
                    )
                ) as stream:
                    async for frame in stream:
                        event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                        if event.event == "error":
                            correction_error = event.text or "Patch correction failed."
                            continue
                        yield frame
                native_session_id = correction_outcome.session_id or native_session_id
                if correction_outcome.paused:
                    return
                if correction_error or not correction_outcome.completed:
                    detail = (
                        correction_error or f"{request.provider} produced no correction result."
                    )
                    failure = _WorkPatchFailure(
                        detail,
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    patch_text = None
                    correction_rounds = _MAX_CORRECTION_ROUNDS
                    continue
                corrected = _read_correction_patch(
                    workspace,
                    remote_stage,
                    pre_launch_digest=pre_launch_digest,
                )
                if corrected.problem == "unreadable":
                    failure = _WorkPatchFailure(
                        f"The corrected patch could not be read: {corrected.detail}",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    patch_text = None
                    continue
                if corrected.problem == "missing":
                    failure = _WorkPatchFailure(
                        "The correction completed without writing patch.json.",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    patch_text = None
                    continue
                if corrected.problem == "unchanged":
                    failure = _WorkPatchFailure(
                        f"{failure.message} The correction left patch.json byte-identical; "
                        "rewrite it with the required changes.",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    # Revalidating it would only reproduce the original
                    # diagnostic and drop the one detail this round adds: that
                    # the agent never rewrote the file.
                    patch_text = None
                    continue
                assert corrected.text is not None
                patch_text = corrected.text

        watch_correction_rounds = 0
        max_watch_corrections = 1 if request.patch_kind == "experiment_loop" else 2
        loop_watch_empty = False
        pending_loop_handoff = None
        accepted_loop_watcher_ids: list[str] = []
        try:
            watch_text = _read_watch_request(workspace, remote_stage)
        except ValueError as exc:
            watch_text = None
            watch_problem = f"The watcher request could not be read: {exc}"
            watch_correctable = True
        except (OSError, StateUnavailable) as exc:
            watch_text = None
            watch_problem = f"The watcher request could not be read: {exc}"
            watch_correctable = False
        else:
            watch_problem = None
            watch_correctable = True
        if _retry_deliverable_is_unchanged(
            execution,
            filename="watch.json",
            predecessor_digest=retry_watch_digest,
            current_text=watch_text,
        ):
            watch_text = None
        if request.patch_kind == "experiment_loop" and watch_text is None and watch_problem is None:
            watch_problem = (
                "Experiment-loop work must write watch.json as an object with external and graph "
                "lists; leave both lists empty only after confirming nothing remains to watch."
            )

        while watch_text is not None or watch_problem is not None:
            if watch_text is not None:
                try:
                    if execution is None:
                        raise ValueError("Watcher arming requires a durable originating operation.")
                    origin_task = execution.store.agent_task(execution.operation_id)
                    if origin_task is None:
                        raise ValueError("The originating Work operation is no longer available.")
                    experiment_handoff = None
                    ordinary_handoff = None
                    if request.patch_kind == "experiment_loop":
                        experiment_handoff = parse_experiment_watch_json(watch_text)
                    else:
                        ordinary_handoff = parse_watch_json(watch_text)
                    if request.patch_kind == "experiment_loop" and experiment_handoff.is_empty:
                        exit_patch_text = _read_chat_patch(workspace, remote_stage)
                        if not request.control_node_id or not patch_explicitly_exits(
                            exit_patch_text, request.control_node_id
                        ):
                            completion_problem = (
                                experiment_exit_problem(exit_patch_text, request.control_node_id)
                                if request.control_node_id
                                else None
                            )
                            raise ValueError(
                                completion_problem
                                or "An Experiment-loop watch.json with both lists empty requires "
                                "patch.json to explicitly record success, a Proposal, or a "
                                "same-Patch Blocker."
                            )
                    specs = (
                        experiment_handoff.observers
                        if experiment_handoff is not None
                        else ordinary_handoff.external
                    )
                    graph_conditions = (
                        experiment_handoff.graph_conditions
                        if experiment_handoff is not None
                        else ordinary_handoff.graph
                    )
                    stop_requests = (
                        experiment_handoff.stops if experiment_handoff is not None else []
                    )
                    binding = WatcherBinding(
                        project_id=origin_task.project_id,
                        origin_operation_id=(
                            root_experiment_loop_operation_id(execution)
                            if request.patch_kind == "experiment_loop"
                            else execution.operation_id
                        ),
                        origin_task_kind=surface,
                        chat_id=request.chat_id or "",
                        node_id=request.node_id,
                        execution_host=execution_host,
                        continuation=WatcherContinuation(
                            provider=request.provider or "",
                            model=request.model,
                            reasoning=request.reasoning,
                            run_on=request.run_on or "",
                            run_truth_scope=context.run_truth_scope,
                            patch_kind=request.patch_kind,
                            control_node_id=request.control_node_id,
                            control_revision=request.control_revision,
                            control_episode_id=request.control_episode_id,
                            control_invocation=request.control_invocation,
                            control_invocation_ceiling=request.control_invocation_ceiling,
                            control_decision_bundle=[
                                item.model_dump(mode="json")
                                for item in request.control_decision_bundle
                            ],
                            control_completion_criteria=request.control_completion_criteria,
                            workflow_ids=skill_selection.workflow_ids,
                            skill_ids=skill_selection.skill_ids,
                            invoked_workflow_ids=request.invoked_workflow_ids,
                            invoked_skill_ids=request.invoked_skill_ids,
                            resolved_skill_packages=skill_selection.resolved_skill_packages,
                        ),
                    )
                    if stop_requests:
                        execution.store.validate_experiment_agent_watcher_stops(
                            binding,
                            stop_requests,
                        )
                    if request.patch_kind == "experiment_loop":
                        graph_armed_revision = None
                        if graph_conditions:
                            graph_state = await asyncio.to_thread(service.history.state)
                            await asyncio.to_thread(
                                validate_graph_conditions,
                                graph_conditions,
                                graph_state,
                            )
                            graph_armed_revision = graph_state.revision
                        check_results = (
                            await asyncio.to_thread(
                                validate_watch_specs,
                                specs,
                                execution_host,
                            )
                            if specs
                            else []
                        )
                        pending_loop_handoff = (
                            specs,
                            check_results,
                            graph_conditions,
                            graph_armed_revision,
                            binding,
                            stop_requests,
                        )
                        armed = []
                    else:
                        graph_state = (
                            await asyncio.to_thread(service.history.state)
                            if graph_conditions
                            else None
                        )
                        if graph_conditions:
                            execution.armed_graph_watchers = True
                        armed = await asyncio.to_thread(
                            arm_watchers,
                            execution.store,
                            specs,
                            binding,
                            graph_conditions=graph_conditions,
                            state=graph_state,
                        )
                except WatcherInitialCheckError as exc:
                    watch_problem = str(exc)
                    watch_correctable = True
                except ValueError as exc:
                    watch_problem = str(exc)
                    watch_correctable = True
                except (OSError, ReplayHalted, StateUnavailable) as exc:
                    watch_problem = str(exc)
                    watch_correctable = False
                else:
                    loop_watch_empty = (
                        request.patch_kind == "experiment_loop"
                        and not specs
                        and not graph_conditions
                        and (
                            not stop_requests
                            or not execution.store.experiment_handoff_has_live_watcher_after_stops(
                                binding,
                                [item.stop_watcher_id for item in stop_requests],
                            )
                        )
                    )
                    if request.patch_kind != "experiment_loop":
                        execution.store.record_agent_task_receipt(
                            execution.operation_id,
                            "watchers_armed",
                            {
                                "watcher_ids": [item.watcher_id for item in armed],
                                "count": len(armed),
                                "correction_rounds": watch_correction_rounds,
                            },
                        )
                    watch_problem = None
                    break

            if watch_problem is None:
                break
            if (
                not watch_correctable
                or watch_correction_rounds >= max_watch_corrections
                or not native_session_id
            ):
                if execution is not None:
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "watcher_handoff_rejected",
                        {
                            "problem": watch_problem[:1600],
                            "correction_rounds": watch_correction_rounds,
                        },
                        tier="diagnostic",
                    )
                    execution.store.record_agent_task_event(
                        execution.operation_id,
                        f"Watcher handoff was not armed: {watch_problem}",
                        level="warning",
                    )
                if request.patch_kind == "experiment_loop":
                    yield _sse(
                        AgentEvent(
                            event="error",
                            text=f"Experiment-loop watcher handoff failed: {watch_problem}",
                        )
                    )
                    return
                break

            watch_correction_rounds += 1
            assert execution is not None
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "watcher_correction_requested",
                {"round": watch_correction_rounds, "problem": watch_problem[:400]},
                tier="diagnostic",
            )
            execution.store.update_agent_task_message(
                execution.operation_id,
                "Correcting watcher handoff.",
                phase="correcting",
                event=True,
            )
            diagnostics_path = _stage_json_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-watch-correction-{watch_correction_rounds}.json",
                {"problem": watch_problem},
            )
            watch_validator: StagedCommandMailbox | None = None
            watch_validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None
            watch_validator_command = validator_command
            if request.patch_kind == "experiment_loop":
                watch_validator = stage_patch_validation_mailbox(
                    local_stage=local_stage,
                    remote_stage=remote_stage,
                    task_id=execution.operation_id,
                    turn_id=f"{token}:watch-correction:{watch_correction_rounds}",
                    timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
                )
                watch_validator_lifecycle = _start_work_validator_mailbox(
                    service,
                    watch_validator,
                    execution=execution,
                    budget=validator_budget,
                    run_truth_scope=context.run_truth_scope,
                    patch_kind=request.patch_kind,
                    control_node_id=request.control_node_id,
                    control_decision_bundle=request.control_decision_bundle,
                )
            try:
                if watch_validator is not None:
                    watch_validator_command = watch_validator.client_command(
                        "validate",
                        patch_path,
                    )
                correction_contract = (
                    experiment_loop_watcher_correction_contract(
                        original_contract_path=base_contract_path,
                        diagnostics_path=diagnostics_path,
                        watch_path=watch_path,
                        patch_path=patch_path,
                        output_schema_path=schema_path,
                        validator_command=watch_validator_command,
                    )
                    if request.patch_kind == "experiment_loop"
                    else PromptFactory.continuation_task_contract(
                        original_contract_path=base_contract_path,
                        mode="watch_correction",
                        diagnostics_path=diagnostics_path,
                        watch_path=watch_path,
                    )
                )
                correction_path, correction_prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-watch-correction-{watch_correction_rounds}.md",
                    correction_contract,
                    execution=execution,
                    role=f"watch_correction_{watch_correction_rounds}",
                )
                _record_agent_launch_receipt(
                    execution,
                    request,
                    prompt=correction_prompt,
                    contract_path=correction_path,
                    remote=bool(execution_host),
                    resumed=True,
                    continuation="watch_correction",
                    extra={
                        "surface": surface,
                        "mode": "work",
                        "capability": "work_auto",
                        "network_access": True,
                        "launch_kind": "watch_correction",
                        "correction_round": watch_correction_rounds,
                        "write_directory_count": len(write_dirs),
                        "canonical_state_boundary": "prompt_only",
                    },
                )
                correction_outcome = _ProviderOutcome(session_id=native_session_id)
                correction_error: str | None = None
                if request.patch_kind == "experiment_loop":
                    assert watch_validator is not None
                    assert watch_validator_lifecycle is not None
                correction_stream = (
                    _stream_work_agent_events(
                        service,
                        launcher,
                        request,
                        correction_prompt,
                        workspace=workspace,
                        session_id=native_session_id,
                        read_dirs=read_dirs,
                        write_dirs=write_dirs,
                        execution_host=execution_host,
                        execution=execution,
                        remote_stage=remote_stage,
                        capability="work_auto",
                        outcome=correction_outcome,
                        binary=provider_binary,
                        validator_staged=watch_validator,
                        validator_lifecycle=watch_validator_lifecycle,
                        validator_budget=validator_budget,
                        run_truth_scope=context.run_truth_scope,
                        patch_kind=request.patch_kind,
                        control_node_id=request.control_node_id,
                        control_decision_bundle=request.control_decision_bundle,
                    )
                    if request.patch_kind == "experiment_loop"
                    else _stream_agent_events(
                        launcher,
                        request,
                        correction_prompt,
                        workspace=workspace,
                        session_id=native_session_id,
                        read_dirs=read_dirs,
                        write_dirs=write_dirs,
                        execution_host=execution_host,
                        execution=execution,
                        remote_stage=remote_stage,
                        capability="work_auto",
                        outcome=correction_outcome,
                        binary=provider_binary,
                    )
                )
            except BaseException as exc:
                if watch_validator_lifecycle is not None:
                    await watch_validator_lifecycle.close(primary_error=exc)
                raise
            async with aclosing(correction_stream) as stream:
                async for frame in stream:
                    event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                    if event.event == "error":
                        correction_error = event.text or "Watcher correction failed."
                    elif event.event not in {"answer", "done"}:
                        yield frame
            native_session_id = correction_outcome.session_id or native_session_id
            if correction_outcome.paused:
                return
            if correction_error or not correction_outcome.completed:
                watch_problem = correction_error or (
                    f"{request.provider} produced no watcher correction result."
                )
                watch_text = None
                watch_correction_rounds = max_watch_corrections
                continue
            try:
                corrected = _read_watch_request(workspace, remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                watch_problem = f"The corrected watcher request could not be read: {exc}"
                watch_text = None
                continue
            if corrected is None:
                watch_problem = "The correction completed without writing watch.json."
                watch_text = None
                continue
            # Correction validates the resulting Patch/watch handoff, not an
            # output delta. A handoff with both lists empty may already be
            # correct while the agent repairs only patch.json to record a
            # terminal outcome.
            watch_text = corrected
            watch_problem = None

        if request.patch_kind == "work":
            (
                maintenance_frames,
                native_session_id,
                maintenance_paused,
            ) = await _process_experiment_watcher_maintenance(
                service=service,
                launcher=launcher,
                request=request,
                execution=execution,
                staged_resources=experiment_resources,
                workspace=workspace,
                remote_stage=remote_stage,
                local_stage=local_stage,
                base_contract_path=base_contract_path,
                token=token,
                native_session_id=native_session_id,
                read_dirs=read_dirs,
                write_dirs=write_dirs,
                execution_host=execution_host,
                provider_binary=provider_binary,
                retry_output_digests=retry_experiment_watch_digests,
            )
            for frame in maintenance_frames:
                yield frame
            if maintenance_paused:
                return

        if request.patch_kind == "experiment_loop":
            try:
                final_patch_text = _read_chat_patch(workspace, remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                yield _sse(
                    AgentEvent(
                        event="error",
                        text=f"The final Experiment-loop patch could not be read: {exc}",
                    )
                )
                return
            if final_patch_text is None:
                graph_update = GraphUpdateResult(status="none")
            else:
                loop_patch_correction_rounds = 0
                while True:
                    # A correction round that produced nothing new leaves this
                    # None so its own diagnostic reaches the agent. Re-applying
                    # the unchanged bytes would overwrite that diagnostic with
                    # the original one and blank the retained patch output.
                    if final_patch_text is not None:
                        if loop_watch_empty and (
                            not request.control_node_id
                            or not patch_explicitly_exits(final_patch_text, request.control_node_id)
                        ):
                            final_failure = _WorkPatchFailure(
                                "A watch.json with both lists empty requires this Patch to retain "
                                "an explicit success, Proposal, or same-Patch Blocker.",
                                correctable=True,
                            )
                            final_result = None
                        else:
                            try:
                                final_result, final_failure = _apply_work_patch(
                                    service,
                                    execution,
                                    final_patch_text,
                                    run_truth_scope=context.run_truth_scope,
                                    patch_kind=request.patch_kind,
                                    control_node_id=request.control_node_id,
                                    control_decision_bundle=request.control_decision_bundle,
                                )
                            except RunLockCancelled:
                                yield _sse(
                                    AgentEvent(
                                        event="paused",
                                        text=(
                                            "Paused while waiting for canonical state. The "
                                            "operational answer and retained patch are preserved."
                                        ),
                                    )
                                )
                                return
                        if final_result is not None:
                            graph_update = final_result.model_copy(
                                update={"correction_rounds": loop_patch_correction_rounds}
                            )
                            break
                    assert final_failure is not None
                    if (
                        not final_failure.correctable
                        or loop_patch_correction_rounds >= _MAX_CORRECTION_ROUNDS
                        or not native_session_id
                    ):
                        if loop_watch_empty:
                            yield _sse(
                                AgentEvent(
                                    event="error",
                                    text=(
                                        "Experiment-loop Patch could not be validated after its "
                                        f"watcher handoff: {final_failure.message}"
                                    ),
                                )
                            )
                            return
                        repairable = _work_graph_repairable(
                            execution,
                            native_session_id,
                            final_failure,
                        )
                        graph_update = GraphUpdateResult(
                            status="rejected",
                            change_summary=list(final_failure.change_summary),
                            proposal_ids=list(final_failure.proposal_ids),
                            validation_messages=_bounded_graph_messages(final_failure.message),
                            correction_rounds=loop_patch_correction_rounds,
                            repairable=repairable,
                        )
                        _record_work_graph_rejection(execution, graph_update)
                        break

                    loop_patch_correction_rounds += 1
                    assert execution is not None
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_correction_requested",
                        {
                            "round": loop_patch_correction_rounds,
                            "problem": final_failure.message[:400],
                        },
                        tier="diagnostic",
                    )
                    diagnostics_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-loop-patch-correction-{loop_patch_correction_rounds}.json",
                        {"kind": "experiment_loop", "problem": final_failure.message},
                    )
                    loop_validator = stage_patch_validation_mailbox(
                        local_stage=local_stage,
                        remote_stage=remote_stage,
                        task_id=execution.operation_id,
                        turn_id=(f"{token}:loop-patch-correction:{loop_patch_correction_rounds}"),
                        timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
                    )
                    loop_validator_lifecycle = _start_work_validator_mailbox(
                        service,
                        loop_validator,
                        execution=execution,
                        budget=validator_budget,
                        run_truth_scope=context.run_truth_scope,
                        patch_kind=request.patch_kind,
                        control_node_id=request.control_node_id,
                        control_decision_bundle=request.control_decision_bundle,
                    )
                    try:
                        loop_validator_command = loop_validator.client_command(
                            "validate",
                            patch_path,
                        )
                        correction_contract = experiment_loop_patch_correction_contract(
                            original_contract_path=base_contract_path,
                            diagnostics_path=diagnostics_path,
                            patch_path=patch_path,
                            watch_path=watch_path,
                            validator_command=loop_validator_command,
                        )
                        correction_path, correction_prompt = _stage_task_contract(
                            local_stage,
                            remote_stage,
                            f"task-{token}-loop-patch-correction-{loop_patch_correction_rounds}.md",
                            correction_contract,
                            execution=execution,
                            role=f"experiment_loop_patch_correction_{loop_patch_correction_rounds}",
                        )
                        pre_launch_digest = _existing_patch_digest(workspace, remote_stage)
                        _record_agent_launch_receipt(
                            execution,
                            request,
                            prompt=correction_prompt,
                            contract_path=correction_path,
                            remote=bool(execution_host),
                            resumed=True,
                            continuation="graph_correction",
                            extra={
                                "surface": surface,
                                "mode": "work",
                                "capability": "work_auto",
                                "network_access": True,
                                "launch_kind": "graph_correction",
                                "correction_round": loop_patch_correction_rounds,
                                "write_directory_count": len(write_dirs),
                                "canonical_state_boundary": "prompt_only",
                            },
                        )
                        correction_outcome = _ProviderOutcome(session_id=native_session_id)
                    except BaseException as exc:
                        await loop_validator_lifecycle.close(primary_error=exc)
                        raise
                    async with aclosing(
                        _stream_work_agent_events(
                            service,
                            launcher,
                            request,
                            correction_prompt,
                            workspace=workspace,
                            session_id=native_session_id,
                            read_dirs=read_dirs,
                            write_dirs=write_dirs,
                            execution_host=execution_host,
                            execution=execution,
                            remote_stage=remote_stage,
                            capability="work_auto",
                            outcome=correction_outcome,
                            binary=provider_binary,
                            validator_staged=loop_validator,
                            validator_lifecycle=loop_validator_lifecycle,
                            validator_budget=validator_budget,
                            run_truth_scope=context.run_truth_scope,
                            patch_kind=request.patch_kind,
                            control_node_id=request.control_node_id,
                            control_decision_bundle=request.control_decision_bundle,
                        )
                    ) as stream:
                        async for frame in stream:
                            yield frame
                    native_session_id = correction_outcome.session_id or native_session_id
                    if correction_outcome.paused:
                        return
                    if not correction_outcome.completed:
                        final_failure = _WorkPatchFailure(
                            f"{request.provider} produced no Patch correction result.",
                            correctable=True,
                        )
                        final_patch_text = None
                        loop_patch_correction_rounds = _MAX_CORRECTION_ROUNDS
                        continue
                    corrected = _read_correction_patch(
                        workspace,
                        remote_stage,
                        pre_launch_digest=pre_launch_digest,
                    )
                    if corrected.problem == "unreadable":
                        final_failure = _WorkPatchFailure(
                            f"The corrected loop Patch could not be read: {corrected.detail}",
                            correctable=True,
                        )
                        final_patch_text = None
                        continue
                    if corrected.problem in {"missing", "unchanged"}:
                        final_failure = _WorkPatchFailure(
                            "The loop Patch correction did not rewrite patch.json.",
                            correctable=True,
                        )
                        final_patch_text = None
                        continue
                    assert corrected.text is not None
                    final_patch_text = corrected.text

            if pending_loop_handoff is not None:
                assert execution is not None
                (
                    specs,
                    check_results,
                    graph_conditions,
                    graph_armed_revision,
                    binding,
                    stop_requests,
                ) = pending_loop_handoff
                try:
                    graph_state = (
                        await asyncio.to_thread(service.history.state) if graph_conditions else None
                    )
                    if graph_conditions:
                        execution.armed_graph_watchers = True
                    armed = await asyncio.to_thread(
                        persist_experiment_watchers_idempotently,
                        execution,
                        specs,
                        check_results,
                        binding,
                        stop_requests,
                        graph_conditions=graph_conditions,
                        graph_state=graph_state,
                        armed_revision=graph_armed_revision,
                    )
                except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
                    yield _sse(
                        AgentEvent(
                            event="error",
                            text=f"Experiment-loop watcher persistence failed: {exc}",
                        )
                    )
                    return
                execution.store.record_agent_task_receipt(
                    root_experiment_loop_operation_id(execution),
                    "watchers_armed",
                    {
                        "watcher_ids": [item.watcher_id for item in armed],
                        "stopped_watcher_ids": [item.stop_watcher_id for item in stop_requests],
                        "count": len(armed),
                        "correction_rounds": watch_correction_rounds,
                    },
                )
                accepted_loop_watcher_ids = [item.watcher_id for item in armed]
            if (
                execution is not None
                and request.control_node_id
                and graph_update.status == "applied"
                and patch_explicitly_exits(final_patch_text, request.control_node_id)
            ):
                execution.store.record_agent_task_receipt(
                    root_experiment_loop_operation_id(execution),
                    "experiment_loop_exit",
                    {
                        "control_node_id": request.control_node_id,
                        "episode_id": request.control_episode_id,
                        "invocation": request.control_invocation,
                        "applied_revision": graph_update.applied_revision,
                    },
                )

            if execution is None or episode_context_baseline is None:
                raise ValueError("Experiment-loop handoff lost its durable episode context.")
            commit_experiment_episode_binding(
                execution,
                request,
                native_session_id=native_session_id,
                execution_host=execution_host,
                stage_host=execution.stage_host,
                stage_root=execution.stage_root,
                graph_result=experiment_graph_result_summary(graph_update),
                watcher_ids=accepted_loop_watcher_ids,
                context_baseline=episode_context_baseline,
            )

        if uses_master_protocol:
            try:
                _record_applied_graph_revision(
                    execution,
                    request,
                    outcome.session_id,
                    graph_update.applied_revision,
                )
            except ValueError as exc:
                if execution is not None:
                    execution.store.record_agent_task_event(
                        execution.operation_id,
                        "This turn's own revision could not be absorbed into the session "
                        f"baseline; the next turn may re-announce it: {exc}",
                        level="warning",
                    )
        try:
            _append_chat_exchange(
                service,
                request,
                answer,
                outcome.session_id,
                graph_update.applied_revision,
                graph_update=graph_update,
                execution=execution,
            )
        except (OSError, StateUnavailable, ValueError) as exc:
            if execution is not None:
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"The reply was delivered but could not be written to the chat transcript: {exc}",
                    level="warning",
                )
        payload: dict[str, object] = {
            "graph_update": graph_update.model_dump(mode="json"),
        }
        if graph_update.applied_revision is not None:
            payload["applied_revision"] = graph_update.applied_revision
        yield _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":"))))
        yield _sse(AgentEvent(event="done"))


def _rejected_graph_update_for_repair(execution: AgentTaskExecution) -> GraphUpdateResult:
    """Find the rejected Work result behind a graph-repair recovery chain."""

    record = execution.store.agent_task(execution.operation_id)
    seen: set[str] = set()
    while record is not None and record.parent_operation_id is not None:
        parent_id = record.parent_operation_id
        if parent_id in seen:
            break
        seen.add(parent_id)
        record = execution.store.agent_task(parent_id)
        raw_graph_update = record.result.get("graph_update") if record and record.result else None
        if isinstance(raw_graph_update, dict):
            try:
                graph_update = GraphUpdateResult.model_validate(raw_graph_update)
            except ValueError:
                pass
            else:
                if graph_update.status == "rejected":
                    return graph_update
    raise ValueError("The graph repair has no rejected Work ancestor.")


async def _stream_work_graph_repair(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    *,
    execution: AgentTaskExecution,
) -> AsyncIterator[str]:
    """Repair only a retained Work patch; never repeat the operational turn."""

    surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
    patch_inputs = None
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None
    validator_budget = PatchValidationBudget()
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        request = _pinned_to_profile(request, profile)
        execution_machine = service.manifest.machine_map[profile.run_on]
        execution_host = execution_machine.host
        provider_binary = execution_machine.provider_paths.get(profile.provider)
        context = service.assemble_chat(request)
        stage_name = _chat_stage_name(service, request, execution)
        local_stage: Path | None = None
        remote_stage: RemoteRunStage | None = None
        if execution_host:
            stage_root = _validated_remote_chat_resume_stage(execution, execution_host, stage_name)
            remote_stage = RemoteRunStage(execution_host).attach(stage_root)
            context = context.model_copy(
                update=_stage_context_paths(
                    context,
                    service,
                    remote_stage,
                    execution_machine.alias,
                )
            )
            workspace = Path(str(remote_stage.workspace))
        else:
            expected_stage = _swept_stage_root(data_dir) / stage_name
            local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            workspace = local_stage
        token = _task_token(execution)
        patch_inputs = _stage_chat_patch_inputs(
            local_stage,
            remote_stage,
            workspace=workspace,
            stage_name=stage_name,
            task_id=execution.operation_id,
            turn_id=f"{token}:work-graph-repair",
        )
        validator_lifecycle = _start_work_validator_mailbox(
            service,
            patch_inputs.validator_staged,
            execution=execution,
            budget=validator_budget,
            run_truth_scope=context.run_truth_scope,
            patch_kind=request.patch_kind,
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
        )
        patch_path = patch_inputs.patch_path
        read_dirs = _chat_read_dirs(
            context,
            remote_stage,
            service,
            execution_machine.alias,
        )
        write_dirs = _work_write_dirs(
            context,
            service,
            execution_machine.alias,
            remote=remote_stage is not None,
        )
        previous = _rejected_graph_update_for_repair(execution)
        original_contract_path = _parent_task_contract_path(execution, local_stage, remote_stage)
        validator_command = patch_inputs.validator_command
        diagnostics_path = _stage_json_task_input(
            local_stage,
            remote_stage,
            f"task-{token}-manual-graph-repair.json",
            {
                "kind": "work",
                "problems": previous.validation_messages,
                "prior_correction_rounds": previous.correction_rounds,
            },
        )
        contract = PromptFactory.continuation_task_contract(
            original_contract_path=original_contract_path,
            mode="work_patch_correction",
            patch_path=patch_path,
            diagnostics_path=diagnostics_path,
            validator_command=validator_command,
        )
        contract_path, prompt = _stage_task_contract(
            local_stage,
            remote_stage,
            f"task-{token}-manual-graph-repair.md",
            contract,
            execution=execution,
            role="work_patch_repair",
        )
        pre_launch_digest = _existing_patch_digest(workspace, remote_stage)
    except BaseException as exc:
        if validator_lifecycle is not None:
            await validator_lifecycle.close(primary_error=exc)
        elif patch_inputs is not None and not patch_inputs.validator_staged.credential.expired:
            await _close_work_validator_mailbox(
                patch_inputs.validator_staged,
                stop=None,
                task=None,
                execution=execution,
                primary_error=exc,
            )
        if isinstance(exc, (OSError, ReplayHalted, StateUnavailable, ValueError)):
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        raise

    assert validator_lifecycle is not None
    try:
        _record_agent_launch_receipt(
            execution,
            request,
            prompt=prompt,
            contract_path=contract_path,
            remote=bool(execution_host),
            resumed=True,
            continuation="graph_repair",
            extra={
                "surface": surface,
                "mode": "work",
                "capability": "work_auto",
                "network_access": True,
                "launch_kind": "graph_repair",
                "write_directory_count": len(write_dirs),
                "canonical_state_boundary": "prompt_only",
            },
        )
    except BaseException as exc:
        await validator_lifecycle.close(primary_error=exc)
        raise
    outcome = _ProviderOutcome(session_id=request.session_id)
    async with aclosing(
        _stream_work_agent_events(
            service,
            launcher,
            request,
            prompt,
            workspace=workspace,
            session_id=request.session_id,
            read_dirs=read_dirs,
            write_dirs=write_dirs,
            execution_host=execution_host,
            execution=execution,
            remote_stage=remote_stage,
            capability="work_auto",
            outcome=outcome,
            binary=provider_binary,
            validator_staged=patch_inputs.validator_staged,
            validator_lifecycle=validator_lifecycle,
            validator_budget=validator_budget,
            run_truth_scope=context.run_truth_scope,
            patch_kind=request.patch_kind,
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if not outcome.completed:
        if outcome.failed or outcome.paused:
            return
        yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
        return
    try:
        patch_text = _read_chat_patch(workspace, remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=f"The repaired patch could not be read: {exc}"))
        return
    if patch_text is None:
        yield _sse(AgentEvent(event="error", text="The repair did not write patch.json."))
        return
    if (
        pre_launch_digest is not None
        and hashlib.sha256(patch_text.encode("utf-8")).hexdigest() == pre_launch_digest
    ):
        yield _sse(
            AgentEvent(
                event="error",
                text="The repair left patch.json byte-identical to the rejected patch.",
            )
        )
        return
    try:
        graph_update, failure = _apply_work_patch(
            service,
            execution,
            patch_text,
            run_truth_scope=context.run_truth_scope,
            patch_kind=request.patch_kind,
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
        )
    except RunLockCancelled:
        yield _sse(
            AgentEvent(
                event="paused",
                text="Paused while waiting for canonical state. The retained patch is preserved.",
            )
        )
        return
    if graph_update is None:
        assert failure is not None
        graph_update = GraphUpdateResult(
            status="rejected",
            change_summary=list(failure.change_summary),
            proposal_ids=list(failure.proposal_ids),
            validation_messages=_bounded_graph_messages(failure.message),
            correction_rounds=1,
        )
        _record_work_graph_rejection(execution, graph_update)
    elif (
        request.patch_kind == "experiment_loop"
        and request.control_node_id
        and patch_explicitly_exits(patch_text, request.control_node_id)
    ):
        execution.store.record_agent_task_receipt(
            root_experiment_loop_operation_id(execution),
            "experiment_loop_exit",
            {
                "control_node_id": request.control_node_id,
                "episode_id": request.control_episode_id,
                "invocation": request.control_invocation,
                "applied_revision": graph_update.applied_revision,
            },
        )
    if request.patch_kind == "experiment_loop":
        if not request.control_episode_id:
            yield _sse(
                AgentEvent(event="error", text="The graph repair lost its Experiment episode.")
            )
            return
        episode = execution.store.experiment_episode(request.control_episode_id)
        if episode is None or not episode.session_bound:
            yield _sse(
                AgentEvent(
                    event="error",
                    text="The graph repair has no bound Experiment episode to update.",
                )
            )
            return
        try:
            commit_experiment_episode_binding(
                execution,
                request,
                native_session_id=outcome.session_id,
                execution_host=execution_host,
                stage_host=episode.stage_host,
                stage_root=episode.stage_root,
                graph_result=experiment_graph_result_summary(graph_update),
                watcher_ids=episode.last_watcher_ids,
                context_baseline=episode.context_baseline,
            )
        except ValueError as exc:
            yield _sse(
                AgentEvent(
                    event="error",
                    text=f"The graph repair could not update its Experiment handoff: {exc}",
                )
            )
            return
    try:
        _append_chat_graph_receipt(
            service,
            request,
            outcome.session_id,
            graph_update,
            execution,
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"The graph repair completed but its chat receipt could not be written: {exc}",
            level="warning",
        )
    payload: dict[str, object] = {
        "graph_update": graph_update.model_dump(mode="json"),
    }
    if graph_update.applied_revision is not None:
        payload["applied_revision"] = graph_update.applied_revision
    yield _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":"))))
    yield _sse(AgentEvent(event="done"))


def _start_work_validator_mailbox(
    service: ProjectService,
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution | None,
    budget: PatchValidationBudget,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin],
) -> _WorkValidatorMailboxLifecycle:
    stop = asyncio.Event()
    try:
        task = asyncio.create_task(
            serve_patch_validation_mailbox(
                staged=staged,
                execution=execution,
                validate=lambda text: _validate_work_patch_live(
                    service,
                    text,
                    run_truth_scope=run_truth_scope,
                    patch_kind=patch_kind,
                    control_node_id=control_node_id,
                    control_decision_bundle=control_decision_bundle,
                    source_operation_id=_work_patch_source_operation_id(execution, patch_kind),
                ),
                stop=stop,
                budget=budget,
            )
        )
    except BaseException:
        with suppress(BaseException):
            staged.cleanup()
        raise
    return _WorkValidatorMailboxLifecycle(
        staged=staged,
        execution=execution,
        stop=stop,
        task=task,
    )


async def _wait_for_work_validator_task(
    task: asyncio.Task[None],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    """Wait without allowing caller cancellation to abandon an owned mailbox task."""

    caller_cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if caller_cancelled is None:
                caller_cancelled = exc
        except BaseException:
            break
    try:
        task.result()
    except BaseException as exc:
        return exc, caller_cancelled
    return None, caller_cancelled


async def _close_work_validator_mailbox(
    staged: StagedCommandMailbox,
    *,
    stop: asyncio.Event | None,
    task: asyncio.Task[None] | None,
    execution: AgentTaskExecution | None,
    primary_error: BaseException | None = None,
) -> None:
    if stop is not None:
        stop.set()

    serve_error: BaseException | None = None
    caller_cancelled: asyncio.CancelledError | None = None
    if task is not None:
        serve_error, caller_cancelled = await _wait_for_work_validator_task(task)

    cleanup_task = asyncio.create_task(asyncio.to_thread(staged.cleanup))
    cleanup_error, cleanup_cancelled = await _wait_for_work_validator_task(cleanup_task)
    if caller_cancelled is None:
        caller_cancelled = cleanup_cancelled

    def warning(message: str) -> None:
        if execution is None:
            return
        with suppress(Exception):
            execution.store.record_agent_task_event(
                execution.operation_id,
                message,
                level="warning",
            )

    expected_errors = (OSError, StateUnavailable, ValueError)
    if primary_error is not None:
        if serve_error is not None and not isinstance(serve_error, asyncio.CancelledError):
            warning(f"Patch validator became unavailable: {serve_error}")
        if cleanup_error is not None and not isinstance(cleanup_error, asyncio.CancelledError):
            warning(f"Patch validator cleanup failed: {cleanup_error}")
        return

    if caller_cancelled is not None:
        if serve_error is not None and not isinstance(serve_error, asyncio.CancelledError):
            warning(f"Patch validator became unavailable: {serve_error}")
        if cleanup_error is not None and not isinstance(cleanup_error, asyncio.CancelledError):
            warning(f"Patch validator cleanup failed: {cleanup_error}")
        raise caller_cancelled

    if serve_error is not None:
        if isinstance(serve_error, expected_errors):
            warning(f"Patch validator became unavailable: {serve_error}")
        else:
            if cleanup_error is not None:
                warning(f"Patch validator cleanup failed: {cleanup_error}")
            raise serve_error
    if cleanup_error is not None:
        if isinstance(cleanup_error, expected_errors):
            warning(f"Patch validator cleanup failed: {cleanup_error}")
        else:
            raise cleanup_error


async def _stream_work_agent_events(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    prompt: str,
    *,
    workspace: Path,
    session_id: str | None,
    read_dirs: list[Path],
    write_dirs: list[Path],
    execution_host: str,
    execution: AgentTaskExecution | None,
    remote_stage: RemoteRunStage | None,
    capability: Literal["work_auto"],
    outcome: _ProviderOutcome,
    binary: str | None,
    validator_staged: StagedCommandMailbox,
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None,
    validator_budget: PatchValidationBudget,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin],
) -> AsyncIterator[str]:
    lifecycle = validator_lifecycle or _start_work_validator_mailbox(
        service,
        validator_staged,
        execution=execution,
        budget=validator_budget,
        run_truth_scope=run_truth_scope,
        patch_kind=patch_kind,
        control_node_id=control_node_id,
        control_decision_bundle=control_decision_bundle,
    )
    primary_error: BaseException | None = None
    try:
        async with aclosing(
            _stream_agent_events(
                launcher,
                request,
                prompt,
                workspace=workspace,
                session_id=session_id,
                read_dirs=read_dirs,
                write_dirs=write_dirs,
                execution_host=execution_host,
                execution=execution,
                remote_stage=remote_stage,
                capability=capability,
                outcome=outcome,
                binary=binary,
            )
        ) as stream:
            async for frame in stream:
                yield frame
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        await lifecycle.close(primary_error=primary_error)


def _prepare_work_patch_candidate(
    service: ProjectService,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin] | None,
    source_operation_id: str | None = None,
    profile: AgentProfile = "ordinary",
) -> _PreparedWorkPatch:
    if profile == "ordinary":
        draft, _ = service.parse_patch_output([patch_text])
    else:
        draft = parse_agent_patch_json(patch_text, profile=profile)
    validate_agent_patch_shape(draft, profile=profile)
    patch = prepare_agent_patch(
        draft,
        kind=patch_kind,
        run_truth_scope=run_truth_scope,
        source_operation_id=source_operation_id,
        profile=profile,
    )
    if patch_kind == "experiment_loop":
        patch = patch.model_copy(
            update={
                "experiment_control_node_id": control_node_id,
                "experiment_decision_bundle": list(control_decision_bundle or ()),
            }
        )
        if not control_node_id:
            raise ValueError("Experiment-loop Patch validation requires its focused Experiment.")
        validate_experiment_completion(patch, control_node_id)
    validate_work_patch(patch)
    return _PreparedWorkPatch(
        patch=patch,
        change_summary=tuple(draft.change_summary),
        proposal_ids=tuple(_work_patch_proposal_ids(patch)),
    )


def _validate_work_patch_live(
    service: ProjectService,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin] | None,
    source_operation_id: str | None = None,
    profile: AgentProfile = "ordinary",
) -> PatchValidationResult:
    try:
        candidate = _prepare_work_patch_candidate(
            service,
            patch_text,
            run_truth_scope=run_truth_scope,
            patch_kind=patch_kind,
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
            source_operation_id=source_operation_id,
            profile=profile,
        )
        prepared, report, state = service.history.validate_candidate(candidate.patch)
    except (ReplayHalted, StateUnavailable, OSError) as exc:
        return PatchValidationResult(status="unavailable", messages=[str(exc)])
    except ValueError as exc:
        return PatchValidationResult(status="invalid", messages=[str(exc)])
    rejects = [item.message for item in report.messages if item.level == "reject"]
    if rejects:
        return PatchValidationResult(
            status="invalid",
            messages=_bounded_graph_messages(*rejects),
            live_revision=state.revision,
            candidate_revision=prepared.revision,
        )
    return PatchValidationResult(
        status="valid",
        messages=_bounded_graph_messages(*(item.message for item in report.flags)),
        live_revision=state.revision,
        candidate_revision=prepared.revision,
    )


def _record_work_lock_wait(
    execution: AgentTaskExecution,
    message: str,
    location: str,
) -> None:
    detail = f"{message} Location: {location}"
    execution.store.update_agent_task_message(
        execution.operation_id,
        detail,
        phase="waiting",
        event=True,
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "canonical_state_lock_wait",
        {"location": location},
        tier="diagnostic",
    )


def _record_work_lock_lost(
    execution: AgentTaskExecution,
    message: str,
    location: str,
) -> None:
    detail = (
        f"{message} RCP will report the observed outcome of the retained Work patch without "
        f"repeating operational work. Location: {location}"
    )
    execution.store.update_agent_task_message(
        execution.operation_id,
        detail,
        phase="applying",
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        detail,
        level="warning",
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "canonical_state_lock_lost",
        {"location": location},
        tier="diagnostic",
    )


def _apply_work_patch(
    service: ProjectService,
    execution: AgentTaskExecution | None,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"] = "work",
    control_node_id: str | None = None,
    control_decision_bundle: list[ExperimentDecisionPin] | None = None,
    profile: AgentProfile = "ordinary",
) -> tuple[GraphUpdateResult | None, _WorkPatchFailure | None]:
    """Validate and atomically apply one Work patch candidate."""

    if execution is not None:
        execution.store.record_agent_task_patch_output(execution.operation_id, patch_text)
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "patch_retained",
            {"byte_length": len(patch_text.encode("utf-8")), "file_name": "patch.json"},
            tier="diagnostic",
        )
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    source_operation_id = _work_patch_source_operation_id(execution, patch_kind)
    canonical_patch: Patch | None = None
    try:
        candidate = _prepare_work_patch_candidate(
            service,
            patch_text,
            run_truth_scope=run_truth_scope,
            patch_kind=patch_kind,
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
            source_operation_id=source_operation_id,
            profile=profile,
        )
        patch = candidate.patch
        change_summary = candidate.change_summary
        proposal_ids = candidate.proposal_ids
        _record_patch_receipt(
            execution,
            patch,
            byte_length=len(patch_text.encode("utf-8")),
        )
        if not patch.ops and patch_kind != "experiment_loop":
            return GraphUpdateResult(status="none"), None
        workspace = service.history.workspace
        with workspace.run_lock(
            on_wait=(lambda message: _record_work_lock_wait(execution, message, workspace.location))
            if execution is not None
            else None,
            on_lost=(lambda message: _record_work_lock_lost(execution, message, workspace.location))
            if execution is not None
            else None,
            cancelled=(execution.control.pause_requested.is_set if execution is not None else None),
        ) as lease:
            lease.assert_owned()
            if patch_kind == "experiment_loop" and source_operation_id:
                matches = [
                    item
                    for item in service.history.load_patches()
                    if item.source_operation_id == source_operation_id
                    and item.admission == "accepted"
                ]
                if len(matches) > 1:
                    raise ValueError(
                        "Experiment-loop invocation has multiple canonical Patch commits."
                    )
                if matches:
                    canonical_patch = matches[0]
                    if (
                        canonical_patch.kind != "experiment_loop"
                        or canonical_patch.experiment_control_node_id != control_node_id
                    ):
                        raise ValueError(
                            "Experiment-loop invocation source is bound to a different canonical "
                            "Patch."
                        )
                    result = service.history.current_materialization()
                    appended = canonical_patch
                elif not patch.ops:
                    return GraphUpdateResult(status="none"), None
                else:
                    appended, result = service.history.append(
                        patch,
                        discard_on_reject=True,
                    )
            else:
                appended, result = service.history.append(
                    patch,
                    discard_on_reject=True,
                )
    except PatchRejected as exc:
        messages = [item.message for item in exc.report.messages if item.level == "reject"]
        detail = "; ".join(messages) or str(exc) or "The graph rejected the Work patch."
        if execution is not None:
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "patch_rejected",
                {"messages": [item.model_dump(mode="json") for item in exc.report.messages[:16]]},
                tier="diagnostic",
            )
        return None, _WorkPatchFailure(
            detail,
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except (ReplayHalted, StateUnavailable) as exc:
        return None, _WorkPatchFailure(
            str(exc),
            correctable=False,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except ValueError as exc:
        return None, _WorkPatchFailure(
            str(exc),
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )

    if canonical_patch is not None:
        change_summary = tuple(canonical_patch.change_summary)
        proposal_ids = tuple(_work_patch_proposal_ids(canonical_patch))
    report = result.reports[appended.revision]
    _record_patch_applied_receipt(execution, result.state)
    return (
        GraphUpdateResult(
            status="applied",
            applied_revision=appended.revision,
            change_summary=list(change_summary),
            proposal_ids=list(proposal_ids),
            validation_messages=_bounded_graph_messages(*(item.message for item in report.flags)),
        ),
        None,
    )


def _work_patch_proposal_ids(patch: Patch) -> list[str]:
    proposal_ids: list[str] = []
    for operation in patch.ops:
        if operation.get("op") != "create_proposals":
            continue
        proposals = operation.get("proposals")
        if not isinstance(proposals, list):
            continue
        for proposal in proposals:
            if isinstance(proposal, dict) and isinstance(proposal.get("id"), str):
                proposal_ids.append(proposal["id"])
    return list(dict.fromkeys(proposal_ids))


def _bounded_graph_messages(*messages: str) -> list[str]:
    bounded: list[str] = []
    for raw in messages:
        detail = " ".join(raw.split())[:1600]
        if detail and detail not in bounded:
            bounded.append(detail)
        if len(bounded) == 8:
            break
    return bounded


def _work_graph_repairable(
    execution: AgentTaskExecution | None,
    native_session_id: str | None,
    failure: _WorkPatchFailure,
) -> bool:
    return bool(
        failure.correctable and native_session_id and execution is not None and execution.stage_root
    )


def _record_work_graph_rejection(
    execution: AgentTaskExecution | None,
    graph_update: GraphUpdateResult,
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "work_graph_update_rejected",
        graph_update.model_dump(mode="json"),
    )
    detail = (
        graph_update.validation_messages[0]
        if graph_update.validation_messages
        else "The graph update was rejected."
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        f"Operational work completed, but the graph update was rejected: {detail}",
        level="warning",
    )
