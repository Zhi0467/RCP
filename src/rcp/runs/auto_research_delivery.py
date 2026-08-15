from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import WatchGraphArguments
from rcp.core.models import AuthorizedHuman, GraphState
from rcp.runs.auto_research import AutoResearchCommandContext, AutoResearchRunRequest
from rcp.storage import (
    AppStore,
    AutoResearchActorBusy,
    AutoResearchMessageRecord,
    AutoResearchMessageRole,
    EpisodeInvocationCeilingReached,
    EpisodeNotRunning,
    GraphWatcherRecord,
    StoredWatcherRecord,
    WatcherContinuation,
)
from rcp.watchers import WatcherBinding, arm_watchers

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


AutoResearchWatcherReadyHook = Callable[[str], None]


def record_auto_research_message(
    store: AppStore,
    *,
    message_id: str | None = None,
    episode_id: str,
    sender_role: AutoResearchMessageRole,
    sender_task_id: str | None,
    authorized_by: AuthorizedHuman | None,
    recipient_task_id: str,
    body: str,
    control_node_id: str | None = None,
) -> AutoResearchMessageRecord:
    """Persist one hearsay-only message before attempting its paid wake."""

    if message_id is not None and (not isinstance(message_id, str) or not message_id.strip()):
        raise ValueError("a auto_research message id must not be blank")
    return store.record_auto_research_message(
        AutoResearchMessageRecord(
            message_id=message_id if message_id is not None else str(uuid.uuid4()),
            episode_id=episode_id,
            sender_role=sender_role,
            sender_task_id=sender_task_id,
            authorized_by=authorized_by,
            recipient_task_id=recipient_task_id,
            control_node_id=control_node_id,
            body=body,
            created_at=store.now(),
        )
    )


def pending_auto_research_mail_recipients(
    store: AppStore,
    *,
    episode_id: str | None = None,
) -> list[tuple[str, str]]:
    """Enumerate undelivered mail by stable canonical auto_research actor."""

    if episode_id is None:
        episode_ids = {
            episode.episode_id
            for project in store.projects()
            for episode in store.episodes(project.project_id)
            if episode.mode == "auto_research"
            and episode.status in {"queued", "running", "stopping"}
        }
    else:
        episode_ids = {episode_id}
    recipients: set[tuple[str, str]] = set()
    for current_episode_id in episode_ids:
        recipient_ids = {
            message.recipient_task_id
            for message in store.auto_research_messages(current_episode_id)
            if message.delivered_at is None
        }
        for recipient_task_id in sorted(recipient_ids):
            binding = store.auto_research_actor_binding(recipient_task_id)
            if binding.episode_id != current_episode_id:
                raise ValueError("auto_research mail recipient is outside the auto_research")
            if binding.actor_operation_id != recipient_task_id:
                raise ValueError("auto_research mail recipient is not its stable canonical actor")
            recipients.add((current_episode_id, binding.actor_operation_id))
    return sorted(recipients)


def reconcile_pending_auto_research_mail(
    background: BackgroundAgentTasks,
    *,
    episode_id: str | None = None,
) -> list[str]:
    """Retry the existing paid mail wake once for every pending canonical actor."""

    started: list[str] = []
    for current_episode_id, recipient_task_id in pending_auto_research_mail_recipients(
        background.store,
        episode_id=episode_id,
    ):
        operation_id = deliver_pending_auto_research_mail(
            background,
            episode_id=current_episode_id,
            recipient_task_id=recipient_task_id,
        )
        if operation_id is not None:
            started.append(operation_id)
    return started


def deliver_pending_auto_research_mail(
    background: BackgroundAgentTasks,
    *,
    episode_id: str,
    recipient_task_id: str,
) -> str | None:
    """Atomically claim one recipient's pending batch and start its saved actor.

    Busy, not-yet-checkpointed, stopped, and exhausted actors leave the durable
    messages untouched for a later settlement pass.
    """

    delivery = background.pending_auto_research_mail(
        episode_id=episode_id,
        recipient_task_id=recipient_task_id,
    )
    if not delivery.messages:
        return None
    binding = background.store.auto_research_actor_binding(recipient_task_id)
    if binding.episode_id != episode_id:
        raise ValueError("auto_research mail recipient is outside the auto_research")
    if not binding.native_session_id or not binding.stage_root:
        return None
    current = background.store.agent_task(binding.current_operation_id)
    if current is None:
        return None
    request = AutoResearchRunRequest.model_validate(current.request).model_copy(
        update={
            "actor_operation_id": binding.actor_operation_id,
            "role": binding.role,
            "control_node_id": binding.control_node_id,
            "session_id": binding.native_session_id,
            "instruction": None,
            "wake_cause": "message",
            "watcher_ids": [],
        }
    )
    try:
        task = background.start_auto_research_turn(
            episode_id,
            request,
            parent_operation_id=binding.current_operation_id,
            mail_delivery=delivery,
        )
    except (AutoResearchActorBusy, EpisodeInvocationCeilingReached, EpisodeNotRunning):
        return None
    return task.operation_id if task is not None else None


def arm_auto_research_graph_condition(
    store: AppStore,
    context: AutoResearchCommandContext,
    arguments: WatchGraphArguments,
    *,
    watcher_id: str,
    state: GraphState,
    execution_host: str,
    on_ready: AutoResearchWatcherReadyHook | None = None,
) -> GraphWatcherRecord:
    """Arm one orchestrator graph condition through the existing watcher store."""

    if not isinstance(watcher_id, str) or not watcher_id.strip():
        raise ValueError("a auto_research graph watcher id must not be blank")
    binding = _auto_research_graph_watcher_binding(store, context, execution_host=execution_host)
    records = arm_watchers(
        store,
        [],
        binding,
        graph_conditions=[arguments.condition],
        state=state,
        watcher_ids=[watcher_id],
    )
    if len(records) != 1 or not isinstance(records[0], GraphWatcherRecord):
        raise RuntimeError("auto_research graph condition did not produce one graph watcher")
    watcher = records[0]
    store.record_agent_task_event(
        context.task.operation_id,
        f"Graph condition {watcher.watcher_id[:8]} armed: {arguments.reason}",
    )
    store.record_agent_task_receipt(
        context.task.operation_id,
        "auto_research_graph_condition_armed",
        {
            "watcher_id": watcher.watcher_id,
            "condition": arguments.condition.model_dump(mode="json"),
            "reason": arguments.reason,
            "completed_immediately": watcher.status == "completed",
        },
    )
    if watcher.status == "completed" and on_ready is not None:
        on_ready(context.task.project_id)
    return watcher


def reconcile_auto_research_graph_condition(
    store: AppStore,
    context: AutoResearchCommandContext,
    arguments: WatchGraphArguments,
    *,
    watcher_id: str,
    execution_host: str,
) -> GraphWatcherRecord | None:
    """Read one planned graph watcher without arming or delivering it again."""

    if not isinstance(watcher_id, str) or not watcher_id.strip():
        return None
    binding = _auto_research_graph_watcher_binding(store, context, execution_host=execution_host)
    watcher = store.watcher(watcher_id)
    if not isinstance(watcher, GraphWatcherRecord):
        return None
    if (
        watcher.project_id != binding.project_id
        or watcher.origin_operation_id != binding.origin_operation_id
        or watcher.origin_task_kind != binding.origin_task_kind
        or watcher.chat_id != binding.chat_id
        or watcher.node_id != binding.node_id
        or watcher.episode_id != context.episode.episode_id
        or watcher.execution_host != binding.execution_host
        or watcher.condition != arguments.condition
        or watcher.continuation != binding.continuation
        or watcher.armed_revision is None
    ):
        return None
    return watcher


def _auto_research_graph_watcher_binding(
    store: AppStore,
    context: AutoResearchCommandContext,
    *,
    execution_host: str,
) -> WatcherBinding:
    request = context.request
    if request.role != "orchestrator":
        raise ValueError("only the auto_research orchestrator may arm a graph condition")
    if not request.provider or not request.run_on:
        raise ValueError("auto_research watcher continuation has no pinned launch profile")
    if (
        context.task.kind != "auto_research"
        or context.task.project_id != context.episode.project_id
        or context.task.episode_id != context.episode.episode_id
    ):
        raise ValueError("auto_research watcher context conflicts with its auto_research")
    canonical = store.auto_research_actor_binding(context.task.operation_id)
    actor_operation_id = request.actor_operation_id or context.task.operation_id
    if (
        canonical.episode_id != context.episode.episode_id
        or canonical.actor_operation_id != actor_operation_id
        or canonical.role != request.role
        or canonical.control_node_id != request.control_node_id
    ):
        raise ValueError("auto_research watcher context conflicts with its canonical actor")
    return WatcherBinding(
        project_id=context.task.project_id,
        origin_operation_id=context.task.operation_id,
        origin_task_kind="auto_research",
        chat_id=canonical.actor_operation_id,
        node_id=request.control_node_id,
        episode_id=context.episode.episode_id,
        execution_host=execution_host,
        continuation=WatcherContinuation(
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
            run_truth_scope=request.run_truth_scope,
            workflow_ids=request.workflow_ids or [],
            skill_ids=request.skill_ids or [],
            invoked_workflow_ids=request.invoked_workflow_ids,
            invoked_skill_ids=request.invoked_skill_ids,
            resolved_skill_packages=request.resolved_skill_packages or [],
        ),
    )


def deliver_auto_research_watcher_group(
    background: BackgroundAgentTasks,
    watchers: list[StoredWatcherRecord],
) -> str | None:
    """Claim one ready watcher group into the existing paid auto_research wake path."""

    if not watchers or any(item.origin_task_kind != "auto_research" for item in watchers):
        raise ValueError("auto_research watcher delivery requires one auto_research watcher group")
    first = watchers[0]
    if any(item.project_id != first.project_id for item in watchers):
        raise ValueError("auto_research watcher delivery cannot cross projects")
    watcher_ids = [item.watcher_id for item in watchers]
    if len(watcher_ids) != len(set(watcher_ids)):
        raise ValueError("auto_research watcher delivery cannot repeat watcher ids")
    binding = background.store.auto_research_actor_binding(first.origin_operation_id)
    if not binding.native_session_id or not binding.stage_root:
        return None
    current = background.store.agent_task(binding.current_operation_id)
    if current is None:
        return None
    request = AutoResearchRunRequest.model_validate(current.request).model_copy(
        update={
            "episode_id": binding.episode_id,
            "actor_operation_id": binding.actor_operation_id,
            "role": binding.role,
            "control_node_id": binding.control_node_id,
            "session_id": binding.native_session_id,
            "instruction": None,
            "wake_cause": (
                "graph_condition"
                if all(isinstance(item, GraphWatcherRecord) for item in watchers)
                else "watcher"
            ),
            "watcher_ids": watcher_ids,
        }
    )

    def admit(record, _role, _cause):
        return background.store.create_watcher_notification_task(record, watcher_ids)

    try:
        task = background.start_auto_research_turn(
            binding.episode_id,
            request,
            parent_operation_id=binding.current_operation_id,
            wake_admission=admit,
        )
    except (EpisodeInvocationCeilingReached, EpisodeNotRunning):
        return None
    return task.operation_id if task is not None else None
