from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import (
    CommandRequest,
    FinishCommandRequest,
    MessageArguments,
    MessageCommandRequest,
    SpawnArguments,
    StatusArguments,
    WatchGraphArguments,
    WatchGraphCommandRequest,
)
from rcp.core.models import GraphState
from rcp.runs.campaign import (
    CampaignCommandContext,
    CampaignCommandEffectResult,
    CampaignCommandEffects,
    CampaignCommandInvalid,
    CampaignCommandUnavailable,
    CampaignRunRequest,
    CampaignValidateCommand,
)
from rcp.runs.campaign_delivery import (
    CampaignWatcherReadyHook,
    arm_campaign_graph_condition,
    deliver_pending_campaign_mail,
    reconcile_campaign_graph_condition,
    record_campaign_message,
)
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    CampaignActorBinding,
    CampaignMessageRecord,
    CampaignNotRunning,
    GraphWatcherRecord,
)

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


CampaignWorkerRequestFactory = Callable[
    [CampaignCommandContext, SpawnArguments],
    CampaignRunRequest,
]
CampaignGraphState = Callable[[], GraphState]


def campaign_command_effects(
    *,
    store: AppStore,
    background: BackgroundAgentTasks,
    validate: CampaignValidateCommand,
    worker_request_factory: CampaignWorkerRequestFactory,
    graph_state: CampaignGraphState,
    execution_host: str,
    on_watcher_ready: CampaignWatcherReadyHook | None = None,
) -> CampaignCommandEffects:
    """Bind staged campaign commands to the existing durable runtime seams.

    Semantic Patch authority and worker profile selection remain injected. This
    module only composes already-authoritative graph, task, watcher, and mail
    primitives behind the staged command dispatcher.
    """

    if background.store is not store:
        raise ValueError("campaign command effects require one shared task store")

    def status(
        context: CampaignCommandContext,
        arguments: StatusArguments,
    ) -> CampaignCommandEffectResult:
        campaign = store.campaign(context.campaign.campaign_id)
        if campaign is None:
            raise CampaignCommandUnavailable("The campaign status is no longer available.")
        meter = store.campaign_budget_meter(campaign.campaign_id)
        result: dict[str, object] = {
            "campaign": {
                "campaign_id": campaign.campaign_id,
                "status": campaign.status,
                "ending": campaign.ending,
                "stop_requested": campaign.stop_requested_at is not None,
                "research_invocations_remaining": campaign.research_invocations_remaining,
            },
            "budget": meter.model_dump(mode="json"),
        }
        if arguments.worker_id is not None:
            binding, leaf = _worker_leaf(store, context, arguments.worker_id)
            result["worker"] = _worker_status(binding, leaf)
        return CampaignCommandEffectResult(result=result)

    def spawn(
        context: CampaignCommandContext,
        arguments: SpawnArguments,
        planned_worker_id: str,
    ) -> CampaignCommandEffectResult:
        request = worker_request_factory(context, arguments)
        _validate_worker_request(context, arguments, planned_worker_id, request)
        request = request.model_copy(update={"actor_operation_id": planned_worker_id})
        worker = background.start_campaign_turn(
            context.campaign.campaign_id,
            request,
            parent_operation_id=context.task.operation_id,
            operation_id=planned_worker_id,
        )
        if worker is None:
            raise CampaignCommandUnavailable(
                "Campaign worker admission returned no durable task record."
            )
        return CampaignCommandEffectResult(
            message="Campaign worker was seated and queued.",
            result={
                "worker_id": worker.operation_id,
                "status": worker.status,
                "disposition": "created",
            },
        )

    def pause(
        context: CampaignCommandContext,
        worker_id: str,
    ) -> CampaignCommandEffectResult:
        binding, leaf = _worker_leaf(store, context, worker_id)
        try:
            paused = background.pause_campaign_worker(
                leaf.operation_id,
                context.campaign.campaign_id,
            )
        except CampaignNotRunning as exc:
            raise CampaignCommandUnavailable(str(exc)) from exc
        return CampaignCommandEffectResult(
            message="Pause was requested for the current worker task attempt.",
            result=_worker_control_result(binding, paused),
        )

    def resume(
        context: CampaignCommandContext,
        worker_id: str,
    ) -> CampaignCommandEffectResult:
        binding, leaf = _worker_leaf(store, context, worker_id)
        try:
            resumed = background.resume(leaf.operation_id)
        except CampaignNotRunning as exc:
            raise CampaignCommandUnavailable(str(exc)) from exc
        return CampaignCommandEffectResult(
            message=(
                "The current worker task attempt is resuming from its exact saved "
                "session and allocation."
            ),
            result=_worker_control_result(binding, resumed),
        )

    def stop(
        _context: CampaignCommandContext,
        _worker_id: str,
    ) -> CampaignCommandEffectResult:
        return CampaignCommandEffectResult(
            status="unavailable",
            message=(
                "Individual campaign worker Stop is unavailable because RCP has no durable "
                "worker-stop primitive; it was not mapped to campaign Stop or task Pause."
            ),
        )

    def message(
        context: CampaignCommandContext,
        arguments: MessageArguments,
        planned_message_id: str,
    ) -> CampaignCommandEffectResult:
        if context.request.role not in {"orchestrator", "worker"}:
            raise CampaignCommandInvalid("A campaign report turn cannot send messages.")
        recipient_task_id = arguments.recipient_task_id or context.campaign.root_operation_id
        if recipient_task_id is None:
            raise CampaignCommandUnavailable("The campaign has no orchestrator mail recipient.")
        try:
            saved = record_campaign_message(
                store,
                message_id=planned_message_id,
                campaign_id=context.campaign.campaign_id,
                sender_role=context.request.role,
                sender_task_id=context.task.operation_id,
                authorized_by=None,
                recipient_task_id=recipient_task_id,
                control_node_id=context.request.control_node_id,
                body=arguments.body,
            )
        except CampaignNotRunning as exc:
            raise CampaignCommandUnavailable(str(exc)) from exc
        started_operation_id = deliver_pending_campaign_mail(
            background,
            campaign_id=context.campaign.campaign_id,
            recipient_task_id=recipient_task_id,
        )
        canonical = store.campaign_message(saved.message_id)
        if canonical is None:
            raise CampaignCommandUnavailable(
                "The persisted campaign message disappeared before delivery was recorded."
            )
        delivery_operation_id = canonical.delivery_operation_id
        return CampaignCommandEffectResult(
            message=(
                "Campaign message was persisted and a paid delivery turn started."
                if delivery_operation_id is not None
                else (
                    "Campaign message was persisted for a later paid delivery; an older pending "
                    "batch started first."
                    if started_operation_id is not None
                    else "Campaign message was persisted for the recipient's next paid delivery."
                )
            ),
            result=_message_command_result(
                canonical,
                delivery_operation_id=delivery_operation_id,
                disposition="created",
            ),
        )

    def watch_graph(
        context: CampaignCommandContext,
        arguments: WatchGraphArguments,
        planned_watcher_id: str,
    ) -> CampaignCommandEffectResult:
        watcher = arm_campaign_graph_condition(
            store,
            context,
            arguments,
            watcher_id=planned_watcher_id,
            state=graph_state(),
            execution_host=execution_host,
            on_ready=on_watcher_ready,
        )
        return CampaignCommandEffectResult(
            message="Campaign graph condition was armed.",
            result=_watcher_command_result(watcher, disposition="created"),
        )

    def finish(context: CampaignCommandContext) -> CampaignCommandEffectResult:
        try:
            campaign = store.finish_campaign_from_orchestrator(
                context.campaign.campaign_id,
                context.task.operation_id,
            )
        except CampaignNotRunning as exc:
            raise CampaignCommandUnavailable(str(exc)) from exc
        return CampaignCommandEffectResult(
            message=(
                "Campaign completion was fenced. Its concluding report will start after every "
                "already-admitted turn settles."
            ),
            result={
                "campaign_id": campaign.campaign_id,
                "status": campaign.status,
                "ending": campaign.ending,
            },
        )

    def reconcile_unknown(
        context: CampaignCommandContext,
        request: CommandRequest,
        planned_effect_id: str | None,
    ) -> CampaignCommandEffectResult | None:
        if isinstance(request, FinishCommandRequest):
            campaign = store.campaign(context.campaign.campaign_id)
            if (
                campaign is None
                or campaign.ending != "completed"
                or campaign.status not in {"wrapping_up", "succeeded"}
            ):
                return None
            return CampaignCommandEffectResult(
                message=("Existing campaign completion fence returned after interrupted finish."),
                result={
                    "campaign_id": campaign.campaign_id,
                    "status": campaign.status,
                    "ending": campaign.ending,
                },
            )
        if not _is_canonical_uuid(planned_effect_id):
            return None
        assert planned_effect_id is not None
        if isinstance(request, MessageCommandRequest):
            saved = store.campaign_message(planned_effect_id)
            if saved is None or not _campaign_message_matches(
                store,
                context,
                request.arguments,
                saved,
            ):
                return None
            return CampaignCommandEffectResult(
                message="Existing campaign message returned after interrupted delivery.",
                result=_message_command_result(
                    saved,
                    delivery_operation_id=saved.delivery_operation_id,
                    disposition="existing",
                ),
            )
        if isinstance(request, WatchGraphCommandRequest):
            watcher = reconcile_campaign_graph_condition(
                store,
                context,
                request.arguments,
                watcher_id=planned_effect_id,
                execution_host=execution_host,
            )
            if watcher is None:
                return None
            return CampaignCommandEffectResult(
                message="Existing campaign graph condition returned after interrupted arming.",
                result=_watcher_command_result(watcher, disposition="existing"),
            )
        return None

    def seat_node_type(_project_id: str, node_id: str) -> str | None:
        node = graph_state().nodes.get(node_id)
        return node.type if node is not None else None

    return CampaignCommandEffects(
        validate=validate,
        status=status,
        spawn=spawn,
        pause=pause,
        resume=resume,
        stop=stop,
        message=message,
        watch_graph=watch_graph,
        finish=finish,
        seat_node_type=seat_node_type,
        reconcile_unknown=reconcile_unknown,
    )


def _is_canonical_uuid(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _campaign_message_matches(
    store: AppStore,
    context: CampaignCommandContext,
    arguments: MessageArguments,
    saved: CampaignMessageRecord,
) -> bool:
    if context.request.role not in {"orchestrator", "worker"}:
        return False
    recipient_task_id = arguments.recipient_task_id or context.campaign.root_operation_id
    if recipient_task_id is None:
        return False
    try:
        sender = store.campaign_actor_binding(context.task.operation_id)
        recipient = store.campaign_actor_binding(recipient_task_id)
    except KeyError:
        return False
    expected_actor_id = context.request.actor_operation_id or context.task.operation_id
    if (
        sender.campaign_id != context.campaign.campaign_id
        or sender.actor_operation_id != expected_actor_id
        or sender.role != context.request.role
        or store.campaign_invocation_role(context.task.operation_id) != sender.role
        or recipient.campaign_id != context.campaign.campaign_id
        or recipient.actor_operation_id != recipient_task_id
    ):
        return False
    if sender.role == "worker":
        if (
            recipient.role != "orchestrator"
            or recipient_task_id != context.campaign.root_operation_id
        ):
            return False
    elif recipient.role != "worker":
        return False
    return (
        saved.campaign_id == context.campaign.campaign_id
        and saved.sender_role == sender.role
        and saved.sender_task_id == context.task.operation_id
        and saved.authorized_by is None
        and saved.recipient_task_id == recipient_task_id
        and saved.control_node_id == context.request.control_node_id
        and saved.body == arguments.body
    )


def _message_command_result(
    saved: CampaignMessageRecord,
    *,
    delivery_operation_id: str | None,
    disposition: str,
) -> dict[str, object]:
    return {
        "message_id": saved.message_id,
        "recipient_task_id": saved.recipient_task_id,
        "delivery_operation_id": delivery_operation_id,
        "delivery": "started" if delivery_operation_id is not None else "pending",
        "graph_authority": "none",
        "epistemic_status": "hearsay",
        "disposition": disposition,
    }


def _watcher_command_result(
    watcher: GraphWatcherRecord,
    *,
    disposition: str,
) -> dict[str, object]:
    completed_immediately = (
        watcher.status == "completed"
        and watcher.completed_at is not None
        and watcher.completed_at == watcher.created_at
        and watcher.last_evaluated_at == watcher.created_at
    )
    return {
        "watcher_id": watcher.watcher_id,
        "status": watcher.status,
        "completed_immediately": completed_immediately,
        "disposition": disposition,
    }


def _validate_worker_request(
    context: CampaignCommandContext,
    arguments: SpawnArguments,
    planned_worker_id: str,
    request: CampaignRunRequest,
) -> None:
    if (
        request.campaign_id != context.campaign.campaign_id
        or request.role != "worker"
        or request.control_node_id != arguments.seat_node_id
        or request.instruction != arguments.instruction
    ):
        raise CampaignCommandInvalid(
            "The resolved worker request changed its campaign, role, seat, or instruction."
        )
    if request.provider is None or request.run_on is None:
        raise CampaignCommandUnavailable(
            "The campaign worker profile did not resolve a provider and execution machine."
        )
    if request.actor_operation_id not in {None, planned_worker_id}:
        raise CampaignCommandInvalid(
            "The resolved worker request did not preserve the planned worker id."
        )
    if (
        request.session_id is not None
        or request.wake_cause is not None
        or request.ending is not None
        or request.watcher_ids
    ):
        raise CampaignCommandInvalid(
            "A newly seated campaign worker must start with a fresh session and no wake state."
        )


def _worker_leaf(
    store: AppStore,
    context: CampaignCommandContext,
    worker_id: str,
) -> tuple[CampaignActorBinding, AgentTaskRecord]:
    try:
        binding = store.campaign_actor_binding(worker_id)
    except KeyError as exc:
        raise CampaignCommandInvalid("Worker control target is outside this campaign.") from exc
    if binding.campaign_id != context.campaign.campaign_id or binding.role != "worker":
        raise CampaignCommandInvalid("Worker control target is outside this campaign.")
    leaf = store.agent_task(binding.current_operation_id)
    if leaf is None:
        raise CampaignCommandUnavailable(
            "The campaign worker's current task attempt is no longer available."
        )
    return binding, leaf


def _worker_status(
    binding: CampaignActorBinding,
    leaf: AgentTaskRecord,
) -> dict[str, object]:
    return {
        "worker_id": binding.actor_operation_id,
        "current_operation_id": leaf.operation_id,
        "control_node_id": binding.control_node_id,
        "status": leaf.status,
        "status_message": leaf.status_message[:2_000],
        "can_pause": leaf.can_pause,
        "can_resume": leaf.can_resume,
        "can_retry": leaf.can_retry,
    }


def _worker_control_result(
    binding: CampaignActorBinding,
    task: AgentTaskRecord,
) -> dict[str, object]:
    return {
        "worker_id": binding.actor_operation_id,
        "current_operation_id": task.operation_id,
        "status": task.status,
    }
