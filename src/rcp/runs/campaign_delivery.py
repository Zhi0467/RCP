from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import WatchGraphArguments
from rcp.core.models import AuthorizedHuman, GraphState
from rcp.runs.campaign import CampaignCommandContext, CampaignRunRequest
from rcp.storage import (
    AppStore,
    CampaignActorBusy,
    CampaignBudgetExhausted,
    CampaignMessageRecord,
    CampaignMessageRole,
    CampaignNotRunning,
    GraphWatcherRecord,
    StoredWatcherRecord,
    WatcherContinuation,
)
from rcp.watchers import WatcherBinding, arm_watchers

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


CampaignWatcherReadyHook = Callable[[str], None]


def record_campaign_message(
    store: AppStore,
    *,
    message_id: str | None = None,
    campaign_id: str,
    sender_role: CampaignMessageRole,
    sender_task_id: str | None,
    authorized_by: AuthorizedHuman | None,
    recipient_task_id: str,
    body: str,
    control_node_id: str | None = None,
) -> CampaignMessageRecord:
    """Persist one hearsay-only message before attempting its paid wake."""

    if message_id is not None and (not isinstance(message_id, str) or not message_id.strip()):
        raise ValueError("a campaign message id must not be blank")
    return store.record_campaign_message(
        CampaignMessageRecord(
            message_id=message_id if message_id is not None else str(uuid.uuid4()),
            campaign_id=campaign_id,
            sender_role=sender_role,
            sender_task_id=sender_task_id,
            authorized_by=authorized_by,
            recipient_task_id=recipient_task_id,
            control_node_id=control_node_id,
            body=body,
            created_at=store.now(),
        )
    )


def pending_campaign_mail_recipients(
    store: AppStore,
    *,
    campaign_id: str | None = None,
) -> list[tuple[str, str]]:
    """Enumerate undelivered mail by stable canonical campaign actor."""

    if campaign_id is None:
        campaign_ids = {
            campaign.campaign_id
            for project in store.projects()
            if (campaign := store.active_campaign(project.project_id)) is not None
        }
    else:
        campaign_ids = {campaign_id}
    recipients: set[tuple[str, str]] = set()
    for current_campaign_id in campaign_ids:
        recipient_ids = {
            message.recipient_task_id
            for message in store.campaign_messages(current_campaign_id)
            if message.delivered_at is None
        }
        for recipient_task_id in sorted(recipient_ids):
            binding = store.campaign_actor_binding(recipient_task_id)
            if binding.campaign_id != current_campaign_id:
                raise ValueError("campaign mail recipient is outside the campaign")
            if binding.actor_operation_id != recipient_task_id:
                raise ValueError("campaign mail recipient is not its stable canonical actor")
            if binding.role not in {"orchestrator", "worker"}:
                raise ValueError("campaign reports cannot receive campaign mail")
            recipients.add((current_campaign_id, binding.actor_operation_id))
    return sorted(recipients)


def reconcile_pending_campaign_mail(
    background: BackgroundAgentTasks,
    *,
    campaign_id: str | None = None,
) -> list[str]:
    """Retry the existing paid mail wake once for every pending canonical actor."""

    started: list[str] = []
    for current_campaign_id, recipient_task_id in pending_campaign_mail_recipients(
        background.store,
        campaign_id=campaign_id,
    ):
        operation_id = deliver_pending_campaign_mail(
            background,
            campaign_id=current_campaign_id,
            recipient_task_id=recipient_task_id,
        )
        if operation_id is not None:
            started.append(operation_id)
    return started


def deliver_pending_campaign_mail(
    background: BackgroundAgentTasks,
    *,
    campaign_id: str,
    recipient_task_id: str,
) -> str | None:
    """Atomically claim one recipient's pending batch and start its saved actor.

    Busy, not-yet-checkpointed, stopped, and exhausted actors leave the durable
    messages untouched for a later settlement or reauthorization pass.
    """

    delivery = background.pending_campaign_mail(
        campaign_id=campaign_id,
        recipient_task_id=recipient_task_id,
    )
    if not delivery.messages:
        return None
    binding = background.store.campaign_actor_binding(recipient_task_id)
    if binding.campaign_id != campaign_id:
        raise ValueError("campaign mail recipient is outside the campaign")
    if binding.role not in {"orchestrator", "worker"}:
        raise ValueError("campaign reports cannot receive campaign mail")
    if not binding.native_session_id or not binding.stage_root:
        return None
    current = background.store.agent_task(binding.current_operation_id)
    if current is None:
        return None
    request = CampaignRunRequest.model_validate(current.request).model_copy(
        update={
            "actor_operation_id": binding.actor_operation_id,
            "role": binding.role,
            "control_node_id": binding.control_node_id,
            "session_id": binding.native_session_id,
            "instruction": None,
            "wake_cause": "message",
            "watcher_ids": [],
            "ending": None,
        }
    )
    try:
        task = background.start_campaign_turn(
            campaign_id,
            request,
            parent_operation_id=recipient_task_id,
            mail_delivery=delivery,
        )
    except (CampaignActorBusy, CampaignBudgetExhausted, CampaignNotRunning):
        return None
    return task.operation_id if task is not None else None


def arm_campaign_graph_condition(
    store: AppStore,
    context: CampaignCommandContext,
    arguments: WatchGraphArguments,
    *,
    watcher_id: str,
    state: GraphState,
    execution_host: str,
    on_ready: CampaignWatcherReadyHook | None = None,
) -> GraphWatcherRecord:
    """Arm one orchestrator graph condition through the existing watcher store."""

    if not isinstance(watcher_id, str) or not watcher_id.strip():
        raise ValueError("a campaign graph watcher id must not be blank")
    binding = _campaign_graph_watcher_binding(store, context, execution_host=execution_host)
    records = arm_watchers(
        store,
        [],
        binding,
        graph_conditions=[arguments.condition],
        state=state,
        watcher_ids=[watcher_id],
    )
    if len(records) != 1 or not isinstance(records[0], GraphWatcherRecord):
        raise RuntimeError("campaign graph condition did not produce one graph watcher")
    watcher = records[0]
    store.record_agent_task_event(
        context.task.operation_id,
        f"Graph condition {watcher.watcher_id[:8]} armed: {arguments.reason}",
    )
    store.record_agent_task_receipt(
        context.task.operation_id,
        "campaign_graph_condition_armed",
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


def reconcile_campaign_graph_condition(
    store: AppStore,
    context: CampaignCommandContext,
    arguments: WatchGraphArguments,
    *,
    watcher_id: str,
    execution_host: str,
) -> GraphWatcherRecord | None:
    """Read one planned graph watcher without arming or delivering it again."""

    if not isinstance(watcher_id, str) or not watcher_id.strip():
        return None
    binding = _campaign_graph_watcher_binding(store, context, execution_host=execution_host)
    watcher = store.watcher(watcher_id)
    if not isinstance(watcher, GraphWatcherRecord):
        return None
    if (
        watcher.project_id != binding.project_id
        or watcher.origin_operation_id != binding.origin_operation_id
        or watcher.origin_task_kind != binding.origin_task_kind
        or watcher.chat_id != binding.chat_id
        or watcher.node_id != binding.node_id
        or watcher.experiment_episode_id is not None
        or watcher.execution_host != binding.execution_host
        or watcher.condition != arguments.condition
        or watcher.continuation != binding.continuation
        or watcher.armed_revision is None
    ):
        return None
    return watcher


def _campaign_graph_watcher_binding(
    store: AppStore,
    context: CampaignCommandContext,
    *,
    execution_host: str,
) -> WatcherBinding:
    request = context.request
    if request.role != "orchestrator":
        raise ValueError("only the campaign orchestrator may arm a graph condition")
    if not request.provider or not request.run_on:
        raise ValueError("campaign watcher continuation has no pinned launch profile")
    if (
        context.task.kind != "campaign"
        or context.task.project_id != context.campaign.project_id
        or context.task.campaign_id != context.campaign.campaign_id
    ):
        raise ValueError("campaign watcher context conflicts with its campaign")
    canonical = store.campaign_actor_binding(context.task.operation_id)
    actor_operation_id = request.actor_operation_id or context.task.operation_id
    if (
        canonical.campaign_id != context.campaign.campaign_id
        or canonical.actor_operation_id != actor_operation_id
        or canonical.role != request.role
        or canonical.control_node_id != request.control_node_id
    ):
        raise ValueError("campaign watcher context conflicts with its canonical actor")
    return WatcherBinding(
        project_id=context.task.project_id,
        origin_operation_id=context.task.operation_id,
        origin_task_kind="campaign",
        chat_id=canonical.actor_operation_id,
        node_id=request.control_node_id,
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


def deliver_campaign_watcher_group(
    background: BackgroundAgentTasks,
    watchers: list[StoredWatcherRecord],
) -> str | None:
    """Claim one ready watcher group into the existing paid campaign wake path."""

    if not watchers or any(item.origin_task_kind != "campaign" for item in watchers):
        raise ValueError("campaign watcher delivery requires one campaign watcher group")
    first = watchers[0]
    if any(item.project_id != first.project_id for item in watchers):
        raise ValueError("campaign watcher delivery cannot cross projects")
    watcher_ids = [item.watcher_id for item in watchers]
    if len(watcher_ids) != len(set(watcher_ids)):
        raise ValueError("campaign watcher delivery cannot repeat watcher ids")
    binding = background.store.campaign_actor_binding(first.origin_operation_id)
    if not binding.native_session_id or not binding.stage_root:
        return None
    current = background.store.agent_task(binding.current_operation_id)
    if current is None:
        return None
    request = CampaignRunRequest.model_validate(current.request).model_copy(
        update={
            "campaign_id": binding.campaign_id,
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
            "ending": None,
        }
    )

    def admit(record, _role, _cause):
        return background.store.create_watcher_notification_task(record, watcher_ids)

    try:
        task = background.start_campaign_turn(
            binding.campaign_id,
            request,
            parent_operation_id=first.origin_operation_id,
            wake_admission=admit,
        )
    except (CampaignBudgetExhausted, CampaignNotRunning):
        return None
    return task.operation_id if task is not None else None
