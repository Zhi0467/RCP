from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel

from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    PromptFactory,
    RunContext,
    agent_output_schema,
    bounded_session_metadata,
    normalize_agent_patch_bookkeeping,
    normalize_processed_cursors,
    validate_agent_patch_shape,
    validate_session_evidence,
)
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.core.models import CoverageBoundary, Patch
from rcp.history import PatchRejected, ReplayHalted
from rcp.providers import classify_terminal_error, profile_for
from rcp.runs.shared import (
    AgentOutputProblem,
    _collect_patch_text,
    _existing_patch_digest,
    _parent_task_contract_path,
    _pinned_to_profile,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _record_patch_applied_receipt,
    _record_patch_receipt,
    _record_provider_exit,
    _remove_local_tree,
    _safe_stage_name,
    _session_bundle_relative_path,
    _sse,
    _stage_context_paths,
    _stage_json_task_input,
    _stage_task_contract,
    _stage_task_input,
    _stream_agent_events,
    _swept_stage_root,
    _task_token,
)
from rcp.service import ProjectService, RunRequest
from rcp.sources import (
    ConversationIndex,
)
from rcp.storage import AgentTaskRecord
from rcp.transport import RemoteRunStage, StateUnavailable

logger = logging.getLogger(__name__)
_MAX_CORRECTION_ROUNDS = 2
_PREPARED_GRAPH_CONTEXT_FILE = "prepared-context.json"


def _require_agent_patch_identity(patch: Patch, run_kind: str) -> None:
    if patch.author != "agent" or patch.kind != run_kind:
        raise ValueError(
            f"The {run_kind} agent must return an agent-authored {run_kind} patch; "
            "human approval patches can only be created by the RCP review UI."
        )


class _PreparedGraphContext(BaseModel):
    version: Literal[1] = 1
    project_id: str
    kind: Literal["seed", "refresh"]
    graph_revision: int
    run_truth_scope: list[str]
    execution_host: str
    source_snapshot_digest: str
    original_contract_path: str | None = None
    context: RunContext
    previous_coverage: CoverageBoundary


@dataclass(frozen=True)
class _GraphRetryState:
    lineage: tuple[AgentTaskRecord, ...]
    prepared: _PreparedGraphContext | None
    prepared_parent: AgentTaskRecord | None
    progress_parent: AgentTaskRecord | None
    progress: dict[str, object]
    transcript_sources: tuple[str, ...] = ()
    prior_progress_text: str | None = None
    retained_patch_text: str | None = None
    base_contract_content: str | None = None
    context_reason: str | None = None
    progress_reason: str | None = None


def _source_snapshot_digest(
    index: ConversationIndex,
    run_truth_scope: list[str],
    *,
    exclude_native_session_id: str | None = None,
    exclude_provider: str | None = None,
    exclude_native_sessions: set[tuple[str, str]] | None = None,
) -> str:
    sessions = index.for_scope(run_truth_scope)
    excluded = set(exclude_native_sessions or set())
    if exclude_native_session_id and exclude_provider:
        excluded.add((exclude_provider, exclude_native_session_id))
    if excluded:
        changed = True
        while changed:
            changed = False
            for item in sessions:
                key = (item.provider, item.session_id)
                parent_key = (item.provider, item.parent_session_id or "")
                if key in excluded or parent_key not in excluded:
                    continue
                excluded.add(key)
                changed = True
    rows = [
        {
            "key": item.key,
            "provider": item.provider,
            "machine": item.source_machine,
            "last_uuid": item.last_uuid,
            "record_count": item.record_count,
            "first_timestamp": (
                item.first_timestamp.isoformat() if item.first_timestamp is not None else None
            ),
            "last_timestamp": (
                item.last_timestamp.isoformat() if item.last_timestamp is not None else None
            ),
        }
        for item in sorted(sessions, key=lambda value: value.key)
        if (item.provider, item.session_id) not in excluded
    ]
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_prepared_graph_context(parent: AgentTaskRecord) -> _PreparedGraphContext:
    if not parent.stage_root:
        raise ValueError("the prior attempt has no retained stage")
    if parent.stage_host:
        stage = RemoteRunStage(parent.stage_host).attach(parent.stage_root)
        assert stage.root is not None
        raw = stage.read_input_text(_PREPARED_GRAPH_CONTEXT_FILE)
    else:
        root = Path(parent.stage_root).resolve()
        path = (root / "inputs" / _PREPARED_GRAPH_CONTEXT_FILE).resolve()
        if path.parent != (root / "inputs").resolve() or not path.is_file():
            raise ValueError("the prior attempt has no prepared context metadata")
        raw = path.read_text(encoding="utf-8")
    return _PreparedGraphContext.model_validate_json(raw)


def _retry_lineage(execution: AgentTaskExecution | None) -> list[AgentTaskRecord]:
    if execution is None or execution.reuses_native_checkpoint:
        return []
    current = execution.store.agent_task(execution.operation_id)
    if current is None or current.parent_operation_id is None:
        return []
    lineage: list[AgentTaskRecord] = []
    seen = {current.operation_id}
    parent_id = current.parent_operation_id
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = execution.store.agent_task(parent_id)
        if parent is None or parent.project_id != current.project_id or parent.kind != current.kind:
            break
        lineage.append(parent)
        parent_id = parent.parent_operation_id
    return lineage


def _continuation_graph_context(
    service: ProjectService,
    execution: AgentTaskExecution,
    *,
    kind: str,
    request: RunRequest,
    execution_host: str,
) -> _PreparedGraphContext:
    """Load the immutable context owned by a native-session continuation.

    Resume and same-provider correction continue a provider process in its
    original stage. Reassembling here would silently give that process a
    different graph and different evidence than the contract it is continuing.
    """
    record = execution.store.agent_task(execution.operation_id)
    if record is None:
        raise ValueError("The saved continuation task is unavailable. Retry this task.")
    try:
        prepared = _read_prepared_graph_context(record)
    except (OSError, StateUnavailable, ValueError) as exc:
        reason = " ".join(str(exc).split())[:400]
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "continuation_context_unavailable",
            {"reason": reason, "retry_required": True},
            tier="diagnostic",
        )
        raise ValueError(
            f"The saved prepared context is unavailable ({exc}). Retry this task."
        ) from exc
    expected_scope = sorted(
        request.run_truth_scope or service.manifest.agent.default_run_truth_scope
    )
    current_revision = int(service.graph_snapshot()["revision"])
    problems: list[str] = []
    if prepared.project_id != record.project_id:
        problems.append("project identity changed")
    if prepared.kind != kind:
        problems.append("task kind changed")
    if sorted(prepared.run_truth_scope) != expected_scope:
        problems.append("run truth scope changed")
    if prepared.execution_host != execution_host or record.stage_host != (execution_host or None):
        problems.append("execution host changed")
    if prepared.graph_revision != current_revision:
        problems.append(
            f"graph revision moved from {prepared.graph_revision} to {current_revision}"
        )
    if problems:
        reason = "; ".join(problems)
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "continuation_context_unavailable",
            {"reason": reason, "retry_required": True},
            tier="diagnostic",
        )
        raise ValueError(
            f"The saved prepared context no longer matches ({reason}). Retry this task."
        )
    return prepared


def _native_session_paths(
    service: ProjectService,
    parent: AgentTaskRecord,
    *,
    execution_host: str,
) -> list[str]:
    if not parent.native_session_id or parent.stage_host != (execution_host or None):
        return []
    provider = str(parent.request.get("provider") or "")
    roots = profile_for(provider).session_roots(
        service.manifest.sources, remote=bool(execution_host)
    )
    if execution_host:
        return RemoteRunStage(execution_host).find_native_session_files(
            roots, parent.native_session_id
        )
    matches: list[str] = []
    for declared in roots:
        root = Path(declared).expanduser()
        if not root.is_dir():
            continue
        for candidate in root.rglob("*.jsonl"):
            if parent.native_session_id in candidate.stem and candidate.is_file():
                matches.append(str(candidate.resolve()))
                if len(matches) >= 8:
                    return sorted(set(matches))
    return sorted(set(matches))


def _legacy_base_contract(execution: AgentTaskExecution, record: AgentTaskRecord) -> str | None:
    stored = execution.store.agent_task_contract(record.operation_id, "base")
    if stored is not None:
        return stored
    paths = [
        receipt.payload.get("contract_path")
        for receipt in execution.store.agent_task_receipts(record.operation_id)
        if receipt.category == "agent_prompt"
        and receipt.payload.get("launch_kind") == "initial"
        and isinstance(receipt.payload.get("contract_path"), str)
    ]
    for value in paths:
        assert isinstance(value, str)
        try:
            if not record.stage_root:
                continue
            if record.stage_host:
                candidate = PurePosixPath(value)
                if candidate.parent != PurePosixPath(record.stage_root) / "inputs":
                    continue
                content = (
                    RemoteRunStage(record.stage_host)
                    .attach(record.stage_root)
                    .read_input_text(candidate.name)
                )
            else:
                candidate = Path(value).resolve()
                if candidate.parent != (Path(record.stage_root) / "inputs").resolve():
                    continue
                content = candidate.read_text(encoding="utf-8")
            execution.store.record_agent_task_contract(
                record.operation_id,
                "base",
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
            return content
        except (OSError, StateUnavailable, ValueError):
            continue
    return None


def _read_prior_progress(execution: AgentTaskExecution, parent: AgentTaskRecord) -> str | None:
    messages = (parent.result or {}).get("messages", [])
    if isinstance(messages, list) and messages:
        return "\n\n".join(str(item) for item in messages[:16])
    path = next(
        (
            receipt.payload.get("path")
            for receipt in reversed(execution.store.agent_task_receipts(parent.operation_id))
            if receipt.category == "provider_progress"
            and isinstance(receipt.payload.get("path"), str)
        ),
        None,
    )
    if not isinstance(path, str) or not parent.stage_root:
        return None
    try:
        if parent.stage_host:
            candidate = PurePosixPath(path)
            if candidate.parent != PurePosixPath(parent.stage_root) / "inputs":
                return None
            return (
                RemoteRunStage(parent.stage_host)
                .attach(parent.stage_root)
                .read_input_text(candidate.name)
            )
        candidate = Path(path).resolve()
        if candidate.parent != (Path(parent.stage_root) / "inputs").resolve():
            return None
        return candidate.read_text(encoding="utf-8")
    except (OSError, StateUnavailable, ValueError):
        return None


def _try_reuse_graph_context(
    service: ProjectService,
    execution: AgentTaskExecution | None,
    *,
    kind: str,
    request: RunRequest,
    execution_machine: str,
    execution_host: str,
) -> _GraphRetryState | None:
    lineage = _retry_lineage(execution)
    if not lineage or execution is None:
        return None
    expected_scope = sorted(
        request.run_truth_scope or service.manifest.agent.default_run_truth_scope
    )
    graph_revision = int(service.graph_snapshot()["revision"])
    index = service.index_snapshot(refresh=True, execution_machine=execution_machine)
    excluded_sessions = {
        (str(item.request.get("provider") or ""), item.native_session_id)
        for item in lineage
        if item.native_session_id
    }
    prepared = None
    prepared_parent = None
    context_errors: list[str] = []
    for candidate in lineage:
        try:
            value = _read_prepared_graph_context(candidate)
            if kind not in {"seed", "refresh"} or value.kind != kind:
                raise ValueError("task kind changed")
            if value.project_id != candidate.project_id:
                raise ValueError("project identity changed")
            if sorted(value.run_truth_scope) != expected_scope:
                raise ValueError("run truth scope changed")
            if value.execution_host != execution_host or candidate.stage_host != (
                execution_host or None
            ):
                raise ValueError("execution host changed")
            if value.graph_revision != graph_revision:
                raise ValueError("graph revision changed")
            if (
                _source_snapshot_digest(
                    index,
                    value.run_truth_scope,
                    exclude_native_sessions=excluded_sessions,
                )
                != value.source_snapshot_digest
            ):
                raise ValueError("source snapshot changed")
            prepared = value
            prepared_parent = candidate
            break
        except (OSError, StateUnavailable, ValueError) as exc:
            context_errors.append(f"attempt {candidate.attempt}: {exc}")

    progress_parent = None
    progress: dict[str, object] = {}
    transcript_sources: tuple[str, ...] = ()
    prior_progress_text = None
    retained_patch_text = None
    progress_errors: list[str] = []
    for candidate in lineage:
        try:
            transcript_sources = tuple(
                _native_session_paths(service, candidate, execution_host=execution_host)
            )
        except (OSError, StateUnavailable, ValueError) as exc:
            transcript_sources = ()
            progress_errors.append(f"attempt {candidate.attempt}: {exc}")
        prior_progress_text = _read_prior_progress(execution, candidate)
        retained_patch_text = execution.store.agent_task_patch_output(candidate.operation_id)
        if transcript_sources or prior_progress_text or retained_patch_text:
            progress_parent = candidate
            progress = {
                "prior_operation_id": candidate.operation_id,
                "prior_attempt": candidate.attempt,
                "prior_provider": candidate.request.get("provider"),
                "prior_error": candidate.error,
            }
            if candidate.native_session_id:
                progress["native_session_id"] = candidate.native_session_id
            break
        progress_errors.append(f"attempt {candidate.attempt}: no retained provider progress")

    base_contract_content = next(
        (
            content
            for candidate in reversed(lineage)
            if (content := _legacy_base_contract(execution, candidate)) is not None
        ),
        None,
    )
    return _GraphRetryState(
        lineage=tuple(lineage),
        prepared=prepared,
        prepared_parent=prepared_parent,
        progress_parent=progress_parent,
        progress=progress,
        transcript_sources=transcript_sources,
        prior_progress_text=prior_progress_text if progress_parent else None,
        retained_patch_text=retained_patch_text if progress_parent else None,
        base_contract_content=base_contract_content,
        context_reason="; ".join(context_errors)[:1200] if prepared is None else None,
        progress_reason="; ".join(progress_errors)[:1200] if progress_parent is None else None,
    )


def _record_context_reuse(
    execution: AgentTaskExecution | None,
    *,
    reused: bool,
    reason: str | None = None,
) -> None:
    if execution is None:
        return
    category = "context_reused" if reused else "context_reuse_unavailable"
    payload = {"reused": reused}
    if reason:
        payload["reason"] = " ".join(reason.split())[:400]
    execution.store.record_agent_task_receipt(
        execution.operation_id, category, payload, tier="diagnostic"
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        (
            "Reusing the prior attempt's prepared context."
            if reused
            else (
                "Prepared context could not be reused; rebuilding it. "
                f"Reason: {' '.join(reason.split())[:400]}"
                if reason
                else "Prepared context could not be reused; rebuilding it."
            )
        ),
        level="info" if reused else "warning",
    )


def _record_progress_handoff(
    execution: AgentTaskExecution | None,
    *,
    handed_off: bool,
    source: AgentTaskRecord | None = None,
    reason: str | None = None,
) -> None:
    if execution is None:
        return
    payload: dict[str, object] = {"handed_off": handed_off}
    if source is not None:
        payload.update(
            {
                "source_operation_id": source.operation_id,
                "source_attempt": source.attempt,
                "source_provider": source.request.get("provider"),
            }
        )
    if reason:
        payload["reason"] = " ".join(reason.split())[:400]
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "progress_handed_off" if handed_off else "progress_handoff_unavailable",
        payload,
        tier="diagnostic",
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        (
            f"Handing off provider progress from attempt {source.attempt}."
            if handed_off and source is not None
            else (
                "No prior provider progress was handed off. "
                f"Reason: {' '.join(reason.split())[:400]}"
                if reason
                else "No prior provider progress was handed off."
            )
        ),
        level="info" if handed_off else "warning",
    )


def _stage_prepared_graph_context(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    project_id: str,
    kind: str,
    graph_revision: int,
    execution_host: str,
    source_snapshot_digest: str,
    original_contract_path: str,
    context: RunContext,
    previous_coverage: CoverageBoundary,
) -> None:
    prepared = _PreparedGraphContext(
        project_id=project_id,
        kind=kind,
        graph_revision=graph_revision,
        run_truth_scope=context.run_truth_scope,
        execution_host=execution_host,
        source_snapshot_digest=source_snapshot_digest,
        original_contract_path=original_contract_path,
        context=context,
        previous_coverage=previous_coverage,
    )
    _stage_json_task_input(
        local_stage,
        remote_stage,
        _PREPARED_GRAPH_CONTEXT_FILE,
        prepared.model_dump(mode="json"),
    )


def _stage_authorized_session_keys(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    context: RunContext,
) -> str:
    return _stage_json_task_input(
        local_stage,
        remote_stage,
        "authorized-session-keys.json",
        [{"key": session.key, "path": session.path} for session in context.sessions],
    )


def _project_native_transcripts(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    sources: tuple[str, ...],
    label: str,
) -> list[str]:
    if not sources:
        return []
    if remote_stage is not None:
        return remote_stage.project_host_files(list(sources), label)
    if local_stage is None:
        raise RuntimeError("local run stage is unavailable")
    inputs = local_stage / "inputs"
    target = inputs / _safe_stage_name(label)
    if target.exists():
        raise ValueError("immutable native transcript projection already exists")
    staged = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=inputs))
    try:
        projected: list[str] = []
        for index, value in enumerate(sources):
            source = Path(value).resolve()
            if not source.is_file():
                raise ValueError(f"native transcript is unavailable: {source}")
            destination = staged / f"{index:02d}.jsonl"
            # This must be a snapshot, not a hard link. A provider may keep
            # appending to its native transcript, and chmod on a hard link
            # would also mutate the provider-owned source inode.
            shutil.copy2(source, destination)
            destination.chmod(0o400)
            projected.append(str(target / destination.name))
        staged.chmod(0o500)
        os.replace(staged, target)
        return projected
    finally:
        if staged.exists():
            _remove_local_tree(staged, inputs)


async def stream_graph_run(
    service: ProjectService,
    launcher: AgentLauncher,
    kind: str,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    continuation = execution.continuation if execution is not None else "fresh"
    reuses_native_checkpoint = bool(execution is not None and execution.reuses_native_checkpoint)
    if request.session_id and not reuses_native_checkpoint:
        yield _sse(
            AgentEvent(
                event="error",
                text=(
                    "Seed and refresh sessions can only be resumed from an RCP background "
                    "task checkpoint."
                ),
            )
        )
        return
    surface: AgentSurface = "seed" if kind == "seed" else "refresh"
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    request = _pinned_to_profile(request, profile)
    local_stage: Path | None = None
    execution_machine = service.manifest.machine_map[profile.run_on]
    execution_host = execution_machine.host
    provider_binary = execution_machine.provider_paths.get(profile.provider)
    remote_stage: RemoteRunStage | None = None
    run_lock = service.history.workspace.run_lock()
    run_lock_acquired = False
    cache_pin = None
    applied = False
    retry_state: _GraphRetryState | None = None
    source_snapshot_digest = ""
    graph_revision = 0
    try:
        try:
            run_lock.__enter__()
            run_lock_acquired = True
        except StateUnavailable as exc:
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        try:
            continuation_prepared = (
                _continuation_graph_context(
                    service,
                    execution,
                    kind=kind,
                    request=request,
                    execution_host=execution_host,
                )
                if execution is not None and reuses_native_checkpoint
                else None
            )
            retry_state = (
                None
                if continuation_prepared is not None
                else _try_reuse_graph_context(
                    service,
                    execution,
                    kind=kind,
                    request=request,
                    execution_machine=execution_machine.alias,
                    execution_host=execution_host,
                )
            )
            if continuation_prepared is not None:
                context = continuation_prepared.context
                source_snapshot_digest = continuation_prepared.source_snapshot_digest
                graph_revision = continuation_prepared.graph_revision
                _record_context_reuse(execution, reused=True)
            elif retry_state is not None and retry_state.prepared is not None:
                context = retry_state.prepared.context
                source_snapshot_digest = retry_state.prepared.source_snapshot_digest
                graph_revision = retry_state.prepared.graph_revision
                _record_context_reuse(execution, reused=True)
            else:
                if retry_state is not None:
                    _record_context_reuse(
                        execution, reused=False, reason=retry_state.context_reason
                    )
                cache_pin = service.indexer.pin_rebuildable_scope()
                pin_artifact = cache_pin.__enter__()
                context = service.assemble_run(
                    request,
                    surface,
                    pin_artifact=pin_artifact,
                )
                _record_context_receipt(execution, context, surface=surface)
                _report_source_errors(execution, context.source_errors)
                graph_revision = context.graph_revision
                execution_record = (
                    execution.store.agent_task(execution.operation_id)
                    if execution is not None
                    else None
                )
                if execution_record is not None:
                    source_snapshot_digest = _source_snapshot_digest(
                        service.index_snapshot(
                            execution_machine=execution_machine.alias,
                            pin_artifact=pin_artifact,
                        ),
                        context.run_truth_scope,
                        exclude_native_sessions=(
                            {
                                (str(item.request.get("provider") or ""), item.native_session_id)
                                for item in retry_state.lineage
                                if item.native_session_id
                            }
                            if retry_state is not None
                            else None
                        ),
                    )
            previous_coverage = (
                continuation_prepared.previous_coverage
                if continuation_prepared is not None
                else retry_state.prepared.previous_coverage
                if retry_state is not None and retry_state.prepared is not None
                else CoverageBoundary.model_validate_json(
                    Path(context.coverage_path).read_text(encoding="utf-8")
                )
            )
            # One scratch folder per operation, reused by every rung of the recovery
            # ladder so a resumed native session still points at the directory it was
            # originally given. It is never deleted on failure; _sweep_stale_stages
            # ages it out instead.
            if execution_host:
                if request.session_id and not reuses_native_checkpoint:
                    raise ValueError(
                        "Remote native-session resume needs persistent run staging; "
                        "start this chat on the local execution machine."
                    )
                if reuses_native_checkpoint and execution is not None and execution.stage_root:
                    remote_stage = RemoteRunStage(execution_host).attach(execution.stage_root)
                elif reuses_native_checkpoint:
                    raise ValueError(
                        "The interrupted remote operation has no staging checkpoint; retry it."
                    )
                else:
                    remote_stage = RemoteRunStage(execution_host).open(
                        execution.operation_id if execution is not None else None
                    )
                    if execution is not None:
                        assert remote_stage.root is not None
                        execution.checkpoint_stage(execution_host, str(remote_stage.root))
                    if retry_state is None or retry_state.prepared is None:
                        context = _stage_graph_context(
                            context,
                            service,
                            remote_stage,
                            execution_machine.alias,
                        )
                workspace = Path(str(remote_stage.workspace))
                patch_path = str(remote_stage.workspace / "patch.json")
            else:
                stage_root = _swept_stage_root(data_dir)
                if reuses_native_checkpoint and execution is not None and execution.stage_root:
                    local_stage = Path(execution.stage_root).resolve()
                    if local_stage.parent != stage_root.resolve() or not local_stage.is_dir():
                        raise ValueError(
                            "The interrupted local operation has no valid staging checkpoint; "
                            "retry it instead."
                        )
                    context = _rebind_graph_conversations(
                        context, local_stage / "inputs" / "conversations"
                    )
                elif reuses_native_checkpoint:
                    raise ValueError(
                        "The interrupted local operation has no staging checkpoint; retry it."
                    )
                else:
                    name = execution.operation_id if execution is not None else uuid.uuid4().hex
                    local_stage = stage_root / _safe_stage_name(name)
                    local_stage.mkdir(parents=True, exist_ok=True)
                    if execution is not None:
                        execution.checkpoint_stage("", str(local_stage))
                    if retry_state is None or retry_state.prepared is None:
                        context = _stage_local_graph_conversations(context, local_stage)
                workspace = local_stage
                patch_path = str(local_stage / "patch.json")

            read_dirs = _agent_read_dirs(context, remote_stage, service, execution_machine.alias)
            if (
                retry_state is not None
                and retry_state.prepared is not None
                and retry_state.prepared_parent is not None
                and retry_state.prepared_parent.stage_root
            ):
                parent_inputs = (
                    PurePosixPath(retry_state.prepared_parent.stage_root) / "inputs"
                    if execution_host
                    else Path(retry_state.prepared_parent.stage_root) / "inputs"
                )
                read_dirs.append(Path(str(parent_inputs)))
                for conversation_root in _conversation_roots(context).values():
                    read_dirs.append(Path(conversation_root))
            token = _task_token(execution)
            if reuses_native_checkpoint:
                if not request.session_id:
                    raise ValueError(
                        "The interrupted operation has no native agent session; retry it instead."
                    )
                assert execution is not None
                original_contract_path = _parent_task_contract_path(
                    execution, local_stage, remote_stage
                )
                if continuation == "correction":
                    diagnostics_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry-correction.json",
                        {
                            "kind": kind,
                            "prior_attempt_diagnostics": list(execution.retry_feedback),
                            "retained_patch_path": patch_path,
                        },
                    )
                    contract = PromptFactory.continuation_task_contract(
                        original_contract_path=original_contract_path,
                        mode="patch_correction",
                        patch_path=patch_path,
                        diagnostics_path=diagnostics_path,
                    )
                    contract_path, prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-correction.md",
                        contract,
                        execution=execution,
                        role="correction",
                    )
                else:
                    # A literal native Resume already owns its immutable contract
                    # and saved stage. Its only new instruction is to continue.
                    contract_path = original_contract_path
                    prompt = "Continue the interrupted task."
                base_contract_path = contract_path
            else:
                base_contract_content = (
                    retry_state.base_contract_content
                    if retry_state is not None and retry_state.prepared is not None
                    else None
                )
                if base_contract_content is None:
                    schema_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-patch-schema.json",
                        agent_output_schema(),
                    )
                    human_request_path = (
                        _stage_task_input(
                            local_stage,
                            remote_stage,
                            f"task-{token}-human-request.txt",
                            request.message,
                        )
                        if request.message
                        else None
                    )
                    retry_diagnostics_path = (
                        _stage_json_task_input(
                            local_stage,
                            remote_stage,
                            f"task-{token}-retry-diagnostics.json",
                            {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                        )
                        if execution is not None and execution.retry_feedback
                        else None
                    )
                    authorized_session_keys_path = _stage_authorized_session_keys(
                        local_stage,
                        remote_stage,
                        context,
                    )
                    base_contract_content = service.graph_task_contract(
                        kind,
                        project_name=context.project_name,
                        ontology_path=f"{context.graph_path}#ontology",
                        graph_path=context.graph_path,
                        research_path=context.research_md_path,
                        conversation_roots=_conversation_roots(context),
                        authorized_session_keys_path=authorized_session_keys_path,
                        cursor_path=str(
                            PurePosixPath(context.coverage_path).with_name("cursors.json")
                        ),
                        repositories=[
                            {"alias": item.alias, "host": item.host, "path": item.path}
                            for item in context.repositories
                        ],
                        patch_path=patch_path,
                        output_schema_path=schema_path,
                        human_request_path=human_request_path,
                        retry_diagnostics_path=retry_diagnostics_path,
                    )
                base_label = (
                    f"task-{token}-base.md"
                    if retry_state is not None
                    else f"task-{token}-initial.md"
                )
                base_contract_path, base_prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    base_label,
                    base_contract_content,
                    execution=execution,
                    role="base",
                )

                if retry_state is not None and retry_state.progress_parent is not None:
                    handoff = dict(retry_state.progress)
                    transcript_paths = _project_native_transcripts(
                        local_stage,
                        remote_stage,
                        retry_state.transcript_sources,
                        f"task-{token}-native-transcripts",
                    )
                    if transcript_paths:
                        handoff["native_transcript_paths"] = transcript_paths
                    if retry_state.prior_progress_text:
                        handoff["prior_progress_path"] = _stage_task_input(
                            local_stage,
                            remote_stage,
                            f"task-{token}-prior-progress.md",
                            retry_state.prior_progress_text,
                        )
                    if retry_state.retained_patch_text:
                        handoff["retained_patch_path"] = _stage_task_input(
                            local_stage,
                            remote_stage,
                            f"task-{token}-prior-patch.json",
                            retry_state.retained_patch_text,
                        )
                    handoff_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-handoff.json",
                        handoff,
                    )
                    contract = PromptFactory.retry_handoff_task_contract(
                        kind=kind,
                        handoff_path=handoff_path,
                        original_contract_path=base_contract_path,
                        patch_path=patch_path,
                    )
                    contract_path, prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry.md",
                        contract,
                        execution=execution,
                        role="retry",
                    )
                    _record_progress_handoff(
                        execution,
                        handed_off=True,
                        source=retry_state.progress_parent,
                    )
                else:
                    contract_path, prompt = base_contract_path, base_prompt
                    if retry_state is not None:
                        _record_progress_handoff(
                            execution,
                            handed_off=False,
                            reason=retry_state.progress_reason,
                        )
            if not reuses_native_checkpoint and execution is not None:
                execution_record = execution.store.agent_task(execution.operation_id)
                if execution_record is not None:
                    _stage_prepared_graph_context(
                        local_stage,
                        remote_stage,
                        project_id=execution_record.project_id,
                        kind=kind,
                        graph_revision=graph_revision,
                        execution_host=execution_host,
                        source_snapshot_digest=source_snapshot_digest,
                        original_contract_path=base_contract_path,
                        context=context,
                        previous_coverage=previous_coverage,
                    )
            base_contract_path = contract_path
        except (ReplayHalted, StateUnavailable, ValueError) as exc:
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return

        native_session_id = request.session_id
        session_id = request.session_id if reuses_native_checkpoint else None
        rounds = 0
        last_problem = (
            execution.retry_feedback[0]
            if execution is not None and continuation == "correction" and execution.retry_feedback
            else None
        )
        while True:
            # A correction reuses its predecessor's stage, so the patch it is
            # meant to replace is still lying there. Remember it rather than
            # deleting it: invariant 9 says a failed run keeps its patch text.
            # Only a reused stage can hold one, so a first launch skips the probe
            # and its remote round-trip.
            correcting = bool(rounds) or continuation == "correction"
            pre_launch_patch_digest = (
                _existing_patch_digest(workspace, remote_stage)
                if reuses_native_checkpoint or rounds
                else None
            )
            _record_agent_launch_receipt(
                execution,
                request,
                prompt=prompt,
                contract_path=contract_path,
                remote=bool(execution_host),
                resumed=reuses_native_checkpoint,
                continuation=("correction" if rounds else continuation),
                extra={
                    "surface": surface,
                    "capability": "scratch_patch",
                    "network_access": True,
                    "launch_kind": ("correction" if rounds else continuation),
                    "correction_round": rounds,
                },
            )
            # An ingest run's deliverable is the patch file; its prose only confirms
            # it was written, so the collected answers go unread. `done` is held back
            # until the patch is applied so the wire order stays applied_revision,
            # then done.
            outcome = _ProviderOutcome(session_id=native_session_id)
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    prompt,
                    workspace=workspace,
                    session_id=session_id,
                    read_dirs=read_dirs,
                    write_dirs=[],
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="scratch_patch",
                    outcome=outcome,
                    binary=provider_binary,
                )
            ) as stream:
                async for frame in stream:
                    if execution is not None:
                        streamed = AgentEvent.model_validate_json(
                            frame.removeprefix("data: ").strip()
                        )
                        execution_record = execution.store.agent_task(execution.operation_id)
                        if streamed.event == "error" and execution_record is not None:
                            if outcome.trace_messages:
                                progress_path = _stage_task_input(
                                    local_stage,
                                    remote_stage,
                                    f"task-{token}-provider-progress.md",
                                    "\n\n".join(outcome.trace_messages),
                                )
                                try:
                                    if remote_stage is not None:
                                        await asyncio.to_thread(remote_stage.finalize_inputs)
                                except (OSError, StateUnavailable, ValueError) as exc:
                                    execution.store.record_agent_task_event(
                                        execution.operation_id,
                                        f"Provider progress could not be retained: {exc}",
                                        level="warning",
                                    )
                                else:
                                    execution.store.record_agent_task_receipt(
                                        execution.operation_id,
                                        "provider_progress",
                                        {"path": progress_path},
                                        tier="diagnostic",
                                    )
                            execution.store.record_agent_task_receipt(
                                execution.operation_id,
                                "provider_terminal_error",
                                {
                                    "provider": request.provider,
                                    "classification": classify_terminal_error(streamed.text),
                                },
                                tier="diagnostic",
                            )
                            execution.store.record_agent_task_receipt(
                                execution.operation_id,
                                "patch_collection_skipped",
                                {
                                    "reason": "provider_terminal_error",
                                    "patch_availability_evaluated": False,
                                },
                                tier="diagnostic",
                            )
                    yield frame
            _record_provider_exit(
                execution,
                outcome,
                workspace=workspace,
                remote_stage=remote_stage,
            )
            native_session_id = outcome.session_id
            if not outcome.completed:
                if outcome.failed or outcome.paused:
                    return
                yield _sse(
                    AgentEvent(event="error", text=f"{request.provider} produced no result.")
                )
                return

            if execution is not None:
                execution.store.update_agent_task_message(
                    execution.operation_id,
                    "Validating and applying the graph update.",
                    phase="applying",
                    event=True,
                )
            stale_patch = False
            try:
                patch_text, output_name = _collect_patch_text(workspace, remote_stage)
                unchanged = (
                    pre_launch_patch_digest is not None
                    and hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
                    == pre_launch_patch_digest
                )
                if unchanged and execution is not None:
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_predates_launch",
                        {"correction_round": rounds, "accepted": not correcting},
                        tier="diagnostic",
                    )
                if unchanged and correcting:
                    # Applying it would report a correction that never happened.
                    # The substantive diagnostic still leads: why the patch is
                    # unacceptable is what the human and the agent both need,
                    # and "you did not rewrite it" only explains this launch.
                    stale_patch = True
                    raise AgentOutputProblem(
                        (f"{last_problem} " if last_problem else "")
                        + "The patch file is byte-identical to the one this launch "
                        "was asked to correct, so no corrected patch was written. "
                        "Rewrite patch.json with the changes the diagnostic requires."
                    )
            except AgentOutputProblem as exc:
                problem = str(exc)
                if not stale_patch:
                    last_problem = problem
            else:
                if execution is not None:
                    # Persisted before validation: a patch that fails validation is
                    # still the run's work product and must survive the failure.
                    execution.store.record_agent_task_patch_output(
                        execution.operation_id, patch_text
                    )
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_retained",
                        {
                            "byte_length": len(patch_text.encode("utf-8")),
                            "file_name": output_name,
                        },
                        tier="diagnostic",
                    )
                    if output_name != "patch.json":
                        execution.store.record_agent_task_event(
                            execution.operation_id,
                            f"Recovered the patch from {output_name}.",
                            level="warning",
                        )
                try:
                    patch, _ = service.parse_patch_output([patch_text])
                    _record_patch_receipt(
                        execution,
                        patch,
                        byte_length=len(patch_text.encode("utf-8")),
                    )
                    _require_agent_patch_identity(patch, kind)
                    patch = normalize_agent_patch_bookkeeping(patch)
                    patch = normalize_processed_cursors(context, patch, previous_coverage)
                    validate_agent_patch_shape(patch)
                    validate_session_evidence(context, patch, previous_coverage)
                except ValueError as exc:
                    problem = str(exc)
                    last_problem = problem
                else:
                    try:
                        _appended, result = service.history.append(
                            patch,
                            discard_on_reject=True,
                        )
                    except PatchRejected as exc:
                        problem = str(exc)
                        last_problem = problem
                        if execution is not None:
                            execution.store.record_agent_task_receipt(
                                execution.operation_id,
                                "patch_rejected",
                                {
                                    "round": rounds,
                                    "messages": [
                                        item.model_dump(mode="json") for item in exc.report.messages
                                    ],
                                },
                                tier="diagnostic",
                            )
                    except (ReplayHalted, StateUnavailable) as exc:
                        yield _sse(AgentEvent(event="error", text=str(exc)))
                        return
                    else:
                        _record_patch_applied_receipt(execution, result.state)
                        applied = True
                        yield _sse(
                            AgentEvent(
                                event="message",
                                text=json.dumps(
                                    {"applied_revision": result.state.revision},
                                    separators=(",", ":"),
                                ),
                            )
                        )
                        yield _sse(AgentEvent(event="done"))
                        return

            # Rungs 2 and 3: hand the concrete problem back to the agent that is still
            # holding the analysis, rather than discarding the run and asking a human.
            if rounds >= _MAX_CORRECTION_ROUNDS or not native_session_id:
                yield _sse(AgentEvent(event="error", text=problem))
                return
            rounds += 1
            if execution is not None:
                execution.store.record_agent_task_receipt(
                    execution.operation_id,
                    "patch_correction_requested",
                    {"round": rounds, "problem": problem[:400]},
                    tier="diagnostic",
                )
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"Asking the agent to correct its patch (round {rounds}).",
                    level="info",
                )
                execution.store.update_agent_task_message(
                    execution.operation_id,
                    "Asking the agent to correct its patch.",
                    phase="agent",
                    event=True,
                )
            diagnostics_path = _stage_json_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-correction-{rounds}.json",
                {"kind": kind, "problem": problem},
            )
            correction_contract = PromptFactory.continuation_task_contract(
                original_contract_path=base_contract_path,
                mode="patch_correction",
                patch_path=patch_path,
                diagnostics_path=diagnostics_path,
            )
            contract_path, prompt = _stage_task_contract(
                local_stage,
                remote_stage,
                f"task-{token}-correction-{rounds}.md",
                correction_contract,
            )
            session_id = native_session_id
    finally:
        if cache_pin is not None:
            cache_pin.__exit__(None, None, None)
        if applied:
            if local_stage is not None:
                with suppress(OSError, ValueError):
                    _remove_local_tree(local_stage, local_stage.parent)
            if remote_stage is not None:
                remote_stage.close()
            if execution is not None and (local_stage is not None or remote_stage is not None):
                execution.store.clear_agent_task_stage(execution.operation_id)
        if run_lock_acquired:
            run_lock.__exit__(None, None, None)


def _record_context_receipt(
    execution: AgentTaskExecution | None,
    context: RunContext,
    *,
    surface: AgentSurface,
) -> None:
    if execution is None:
        return
    slice_hashes = {session.slice_sha256 for session in context.sessions if session.slice_sha256}
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "context_assembled",
        {
            "surface": surface,
            "repository_count": len(context.repositories),
            "session_count": len(context.sessions),
            "session_record_count": sum(session.slice_record_count for session in context.sessions),
            "unique_slice_count": len(slice_hashes),
            "source_error_count": len(context.source_errors),
            "graph_revision": context.graph_revision,
        },
    )


def _report_source_errors(
    execution: AgentTaskExecution | None,
    source_errors: list[str],
) -> None:
    """Raise degraded sources as run warnings so a dropped session is never silent."""

    if execution is None:
        return
    for detail in source_errors[:16]:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"Conversation source unavailable and excluded from this run: {detail}",
            level="warning",
        )


def _agent_read_dirs(
    context: RunContext,
    remote_stage: RemoteRunStage | None,
    service: ProjectService,
    execution_machine: str,
) -> list[Path]:
    """Directories the agent may need to read from outside its scratch folder.

    Only Claude consumes these (as `--add-dir`); Codex reads are unrestricted in
    every sandbox mode. Repositories on another machine are deliberately absent —
    those are reached over ssh from the pointers in the prompt, never copied.
    """
    read_dirs = [
        Path(item.path) for item in context.repositories if item.machine == execution_machine
    ]
    if remote_stage is not None:
        # Derived from the manifest, not from the context: on a resumed run the
        # context still carries local paths because it is never re-staged.
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
    for root in _conversation_roots(context).values():
        candidate = Path(root)
        if candidate.is_dir():
            read_dirs.append(candidate)
    return read_dirs


def _stage_graph_context(
    context: RunContext,
    service: ProjectService,
    stage: RemoteRunStage,
    execution_machine: str,
) -> RunContext:
    """Give a remote agent paths it can actually open.

    RCP's materialized state is never copied when the canonical state repository
    already lives on the execution machine — the agent reads `.research/` there,
    which is the same bytes RCP validates against because the local tree is an
    rsync mirror of it and the run lock is held. Only conversation slices are
    staged, because they are RCP-derived artifacts that exist nowhere else.
    """
    updates = _stage_context_paths(context, service, stage, execution_machine)
    staged_sessions = []
    with tempfile.TemporaryDirectory(prefix="rcp-session-stage-") as bundle_root:
        bundle = Path(bundle_root)
        labels: list[Path] = []
        created: set[Path] = set()
        for session in context.sessions:
            path = Path(session.path)
            if not path.is_file():
                raise StateUnavailable(f"Conversation slice is unavailable: {session.path}")
            label = _session_bundle_relative_path(session)
            target = bundle / label
            if label not in created:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(path, target)
                except OSError:
                    shutil.copy2(path, target)
                created.add(label)
            labels.append(label)
        remote_bundle = PurePosixPath(stage.put_directory(bundle, "conversations"))
        staged_sessions = [
            session.model_copy(update={"path": str(remote_bundle / label)})
            for session, label in zip(context.sessions, labels, strict=True)
        ]
    staged_context = context.model_copy(update=updates)
    inline, omitted = bounded_session_metadata(staged_sessions)
    return staged_context.model_copy(
        update={
            "sessions": staged_sessions,
            "sessions_inline": inline,
            "sessions_omitted": omitted,
            "session_routing_index": None,
        }
    )


def _stage_local_graph_conversations(context: RunContext, stage: Path) -> RunContext:
    """Project normalized slices into one reversible directory tree per provider."""
    inputs = stage / "inputs"
    inputs.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_root = inputs / "conversations"
    if target_root.exists():
        raise ValueError("immutable graph conversation inputs already exist")
    staged_root = Path(tempfile.mkdtemp(prefix=".conversations-", dir=inputs))
    labels: list[Path] = []
    created: set[Path] = set()
    try:
        for session in context.sessions:
            source = Path(session.path)
            if not source.is_file():
                raise StateUnavailable(f"Conversation slice is unavailable: {session.path}")
            label = _session_bundle_relative_path(session)
            destination = staged_root / label
            if label not in created:
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)
                destination.chmod(0o400)
                created.add(label)
            labels.append(label)
        for directory in sorted(
            (item for item in staged_root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(0o500)
        staged_root.chmod(0o500)
        os.replace(staged_root, target_root)
    finally:
        if staged_root.exists():
            _remove_local_tree(staged_root, inputs)
    staged_sessions = [
        session.model_copy(update={"path": str(target_root / label)})
        for session, label in zip(context.sessions, labels, strict=True)
    ]
    inline, omitted = bounded_session_metadata(staged_sessions)
    return context.model_copy(
        update={
            "sessions": staged_sessions,
            "sessions_inline": inline,
            "sessions_omitted": omitted,
            "session_routing_index": None,
        }
    )


def _rebind_graph_conversations(context: RunContext, root: Path) -> RunContext:
    if not root.is_dir():
        raise StateUnavailable(
            "The saved grouped conversation inputs are unavailable; retry this operation."
        )
    staged_sessions = [
        session.model_copy(update={"path": str(root / _session_bundle_relative_path(session))})
        for session in context.sessions
    ]
    if any(not Path(session.path).is_file() for session in staged_sessions):
        raise StateUnavailable(
            "The saved grouped conversation inputs are incomplete; retry this operation."
        )
    inline, omitted = bounded_session_metadata(staged_sessions)
    return context.model_copy(
        update={
            "sessions": staged_sessions,
            "sessions_inline": inline,
            "sessions_omitted": omitted,
            "session_routing_index": None,
        }
    )


def _conversation_roots(context: RunContext) -> dict[str, str]:
    roots: dict[str, str] = {}
    for session in context.sessions:
        path = PurePosixPath(session.path)
        if len(path.parents) < 3:
            raise ValueError(f"staged conversation path has no provider root: {path}")
        root = str(path.parents[2])
        previous = roots.setdefault(session.provider, root)
        if previous != root:
            raise ValueError(f"provider {session.provider!r} has more than one visible root")
    return roots
