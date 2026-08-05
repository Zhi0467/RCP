from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from rcp.agents.schema import parse_agent_patch_json
from rcp.background import AgentTaskExecution
from rcp.control import decision_drift
from rcp.core.models import ExperimentDecisionPin
from rcp.runs.shared import _stage_json_task_input
from rcp.service import ProjectService, RunRequest
from rcp.storage import WatcherRecord
from rcp.transport import RemoteRunStage
from rcp.watchers import WatcherBinding, WatcherCheckResult, WatchSpec

_EXIT_STATUSES = frozenset({"completed"})


def experiment_watcher_delivery_request(
    group: list[WatcherRecord],
    *,
    trigger: Literal["experiment_run", "watcher"],
    episode_id: str,
    invocation: int,
    invocation_ceiling: int,
    control_revision: int,
    decision_bundle: list[ExperimentDecisionPin],
    completion_criteria: list[str],
) -> RunRequest:
    """Build one explicitly attributed Experiment watcher delivery request."""

    first = group[0]
    continuation = first.continuation
    if continuation.patch_kind != "experiment_loop" or not continuation.control_node_id:
        raise ValueError("An Experiment watcher must retain its origin control binding.")
    try:
        UUID(continuation.control_episode_id or "")
    except ValueError as exc:
        raise ValueError("An Experiment watcher has an invalid origin episode id.") from exc
    if (
        continuation.control_invocation is None
        or continuation.control_invocation_ceiling is None
        or continuation.control_invocation > continuation.control_invocation_ceiling
    ):
        raise ValueError("An Experiment watcher has an invalid origin invocation binding.")
    return RunRequest(
        provider=continuation.provider,
        model=continuation.model,
        reasoning=continuation.reasoning,
        run_on=continuation.run_on,
        run_truth_scope=continuation.run_truth_scope,
        chat_scope="node" if first.origin_task_kind == "node_chat" else "project",
        node_id=first.node_id,
        message="Continue the bounded Experiment loop from its staged watcher state.",
        chat_id=first.chat_id,
        session_id=None,
        mode="work",
        trigger=trigger,
        patch_kind="experiment_loop",
        control_node_id=continuation.control_node_id,
        control_revision=control_revision,
        control_episode_id=episode_id,
        control_invocation=invocation,
        control_invocation_ceiling=invocation_ceiling,
        control_decision_bundle=decision_bundle,
        control_completion_criteria=completion_criteria,
        workflow_ids=continuation.workflow_ids,
        skill_ids=continuation.skill_ids,
        invoked_workflow_ids=continuation.invoked_workflow_ids,
        invoked_skill_ids=continuation.invoked_skill_ids,
        resolved_skill_packages=continuation.resolved_skill_packages,
        watcher_ids=[item.watcher_id for item in group],
    )


def patch_explicitly_exits(patch_text: str | None, control_node_id: str) -> bool:
    """Whether one semantic loop Patch explicitly records a finish or authority pause."""

    if patch_text is None:
        return False
    try:
        patch = parse_agent_patch_json(patch_text)
    except ValueError:
        return False
    operations = patch.model_dump(mode="python", exclude_none=True)["ops"]
    if any(op.get("op") == "create_proposals" and op.get("proposals") for op in operations):
        return True
    created_blockers = {
        node.get("id")
        for op in operations
        if op.get("op") == "create_nodes"
        for node in op.get("nodes", [])
        if node.get("type") == "blocker"
    }
    for op in operations:
        if op.get("op") == "update_nodes":
            for update in op.get("nodes", []):
                changes = update.get("changes", {})
                if (
                    update.get("id") == control_node_id
                    and isinstance(changes, dict)
                    and changes.get("status") in _EXIT_STATUSES
                ):
                    return True
        if op.get("op") == "create_edges" and any(
            edge.get("source") == control_node_id
            and edge.get("relation") == "blocked_by"
            and edge.get("target") in created_blockers
            for edge in op.get("edges", [])
        ):
            return True
    return False


def root_experiment_loop_operation_id(execution: AgentTaskExecution) -> str:
    """Resolve the durable root task through explicit parent links, failing on broken lineage."""

    operation_id = execution.operation_id
    seen: set[str] = set()
    while True:
        if operation_id in seen:
            raise ValueError("Experiment-loop task lineage contains a cycle.")
        seen.add(operation_id)
        record = execution.store.agent_task(operation_id)
        if record is None:
            raise ValueError("Experiment-loop task lineage is incomplete.")
        if record.parent_operation_id is None:
            return operation_id
        operation_id = record.parent_operation_id


def persist_experiment_watchers_idempotently(
    execution: AgentTaskExecution,
    specs: list[WatchSpec],
    results: list[WatcherCheckResult],
    binding: WatcherBinding,
) -> list[WatcherRecord]:
    """Persist one validated handoff once across Retry/crash recovery."""

    if len(specs) != len(results):
        raise ValueError("Experiment-loop watcher checks do not match their specifications.")
    created_at = execution.store.now()
    desired: list[WatcherRecord] = []
    for index, (spec, result) in enumerate(zip(specs, results, strict=True)):
        identity = json.dumps(
            {
                "origin": binding.origin_operation_id,
                "index": index,
                "check_command": spec.check_command,
                "log_path": spec.log_path,
                "cwd": spec.cwd,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        completed = result.state == "complete"
        desired.append(
            WatcherRecord(
                watcher_id=str(uuid5(NAMESPACE_URL, f"rcp-experiment-watcher:{identity}")),
                project_id=binding.project_id,
                origin_operation_id=binding.origin_operation_id,
                origin_task_kind=binding.origin_task_kind,
                chat_id=binding.chat_id,
                node_id=binding.node_id,
                execution_host=binding.execution_host,
                check_command=spec.check_command,
                log_path=spec.log_path,
                cwd=spec.cwd,
                continuation=binding.continuation,
                status="completed" if completed else "active",
                created_at=created_at,
                last_checked_at=result.checked_at,
                last_exit_code=result.exit_code,
                completed_at=result.checked_at if completed else None,
            )
        )

    stored: list[WatcherRecord] = []
    missing: list[WatcherRecord] = []
    for desired_record in desired:
        existing = execution.store.watcher(desired_record.watcher_id)
        if existing is None:
            missing.append(desired_record)
            continue
        immutable_fields = (
            "project_id",
            "origin_operation_id",
            "origin_task_kind",
            "chat_id",
            "node_id",
            "execution_host",
            "check_command",
            "log_path",
            "cwd",
            "continuation",
        )
        if any(
            getattr(existing, field) != getattr(desired_record, field) for field in immutable_fields
        ):
            raise ValueError("Experiment-loop watcher identity conflicts with stored state.")
        stored.append(existing)
    if missing:
        stored.extend(execution.store.create_watchers(missing))
    by_id = {record.watcher_id: record for record in stored}
    return [by_id[record.watcher_id] for record in desired]


def _watcher_state(
    execution: AgentTaskExecution,
    control_node_id: str,
    delivered_watcher_ids: list[str],
    episode_id: str,
) -> list[dict[str, object]]:
    """Return current operational watcher evidence without duplicating loop control."""

    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The Experiment-loop operation is no longer available.")
    delivered = set(delivered_watcher_ids)
    records = [
        record
        for record in execution.store.watchers(task.project_id)
        if record.continuation.patch_kind == "experiment_loop"
        and record.continuation.control_node_id == control_node_id
        and (
            record.watcher_id in delivered
            or record.status in {"active", "degraded"}
            or (record.status == "completed" and not record.notified)
            or (record.status == "stopped" and record.continuation.control_episode_id == episode_id)
        )
    ]
    return [
        {
            "watcher_id": record.watcher_id,
            "origin_operation_id": record.origin_operation_id,
            "execution_host": record.execution_host,
            "check_command": record.check_command,
            "log_path": record.log_path,
            "cwd": record.cwd,
            "status": record.status,
            "created_at": record.created_at,
            "last_checked_at": record.last_checked_at,
            "last_exit_code": record.last_exit_code,
            "last_error": record.last_error,
            "completed_at": record.completed_at,
            "notified": record.notified,
            "notification_operation_id": record.notification_operation_id,
            "episode_id": record.continuation.control_episode_id,
            "invocation": record.continuation.control_invocation,
            "invocation_ceiling": record.continuation.control_invocation_ceiling,
            "control_revision": record.continuation.control_revision,
            "decision_bundle": record.continuation.control_decision_bundle,
        }
        for record in records
    ]


async def stage_experiment_loop_context(
    service: ProjectService,
    request: RunRequest,
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    token: str,
    continuation: str,
) -> tuple[str, str]:
    """Stage irreducible loop control plus separately readable watcher state."""

    if not request.control_node_id or request.control_revision is None:
        raise ValueError("Experiment-loop work is missing its RCP control binding.")
    if (
        not request.control_episode_id
        or request.control_invocation is None
        or request.control_invocation_ceiling is None
    ):
        raise ValueError("Experiment-loop work is missing its episode invocation binding.")
    if execution is None:
        raise ValueError("Experiment-loop work requires a durable RCP operation.")

    state, watcher_state = await asyncio.gather(
        asyncio.to_thread(service.history.state),
        asyncio.to_thread(
            _watcher_state,
            execution,
            request.control_node_id,
            request.watcher_ids,
            request.control_episode_id,
        ),
    )
    watcher_state_path = _stage_json_task_input(
        local_stage,
        remote_stage,
        f"task-{token}-experiment-watchers.json",
        watcher_state,
    )
    if continuation == "resume":
        phase = "resume"
    elif continuation in {"retry", "handoff"}:
        phase = "retry"
    elif request.trigger == "experiment_run" and request.watcher_ids:
        phase = "human_reauthorization"
    elif request.trigger == "watcher":
        phase = "watcher_wake"
    else:
        phase = "initial_run"
    drift = decision_drift(state, request.control_decision_bundle)
    if request.control_invocation > request.control_invocation_ceiling:
        raise ValueError("Experiment-loop invocation exceeds its pinned ceiling.")
    control_path = _stage_json_task_input(
        local_stage,
        remote_stage,
        f"task-{token}-experiment-control-{phase}.json",
        {
            "phase": phase,
            "episode_id": request.control_episode_id,
            "invocation": request.control_invocation,
            "invocation_ceiling": request.control_invocation_ceiling,
            "remaining_invocations": (
                request.control_invocation_ceiling - request.control_invocation
            ),
            "decision_bundle": [
                item.model_dump(mode="json") for item in request.control_decision_bundle
            ],
            "decision_drift": [item.model_dump(mode="json") for item in drift],
            "completion_criteria": request.control_completion_criteria,
            "delivered_watcher_ids": request.watcher_ids,
            "watcher_state_path": watcher_state_path,
        },
    )
    return control_path, watcher_state_path
