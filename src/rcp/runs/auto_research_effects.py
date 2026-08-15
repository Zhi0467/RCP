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
from rcp.runs.auto_research import (
    AutoResearchCommandContext,
    AutoResearchCommandEffectResult,
    AutoResearchCommandEffects,
    AutoResearchCommandInvalid,
    AutoResearchCommandUnavailable,
    AutoResearchRunRequest,
    AutoResearchValidateCommand,
    auto_research_completion_signal,
)
from rcp.runs.auto_research_delivery import (
    AutoResearchWatcherReadyHook,
    arm_auto_research_graph_condition,
    deliver_pending_auto_research_mail,
    reconcile_auto_research_graph_condition,
    record_auto_research_message,
)
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    AutoResearchActorBinding,
    AutoResearchMessageRecord,
    EpisodeNotRunning,
    GraphWatcherRecord,
)

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


AutoResearchWorkerRequestFactory = Callable[
    [AutoResearchCommandContext, SpawnArguments],
    AutoResearchRunRequest,
]
AutoResearchGraphState = Callable[[], GraphState]


def auto_research_command_effects(
    *,
    store: AppStore,
    background: BackgroundAgentTasks,
    validate: AutoResearchValidateCommand,
    worker_request_factory: AutoResearchWorkerRequestFactory,
    graph_state: AutoResearchGraphState,
    execution_host: str,
    on_watcher_ready: AutoResearchWatcherReadyHook | None = None,
) -> AutoResearchCommandEffects:
    """Bind staged auto_research commands to the existing durable runtime seams.

    Semantic Patch authority and worker profile selection remain injected. This
    module only composes already-authoritative graph, task, watcher, and mail
    primitives behind the staged command dispatcher.
    """

    if background.store is not store:
        raise ValueError("auto_research command effects require one shared task store")

    def status(
        context: AutoResearchCommandContext,
        arguments: StatusArguments,
    ) -> AutoResearchCommandEffectResult:
        episode = store.episode(context.episode.episode_id)
        if episode is None or episode.mode != "auto_research":
            raise AutoResearchCommandUnavailable(
                "The Auto-research episode status is no longer available."
            )
        meter = store.episode_budget_meter(episode.episode_id)
        result: dict[str, object] = {
            "episode": {
                "episode_id": episode.episode_id,
                "status": episode.status,
                "ending": episode.ending,
                "stop_requested": episode.stop_requested_at is not None,
                "operational_invocations_remaining": meter.invocations_remaining,
            },
            "budget": meter.model_dump(mode="json"),
        }
        if arguments.worker_id is not None:
            binding, leaf = _worker_leaf(store, context, arguments.worker_id)
            result["worker"] = _worker_status(binding, leaf)
        return AutoResearchCommandEffectResult(result=result)

    def spawn(
        context: AutoResearchCommandContext,
        arguments: SpawnArguments,
        planned_worker_id: str,
    ) -> AutoResearchCommandEffectResult:
        request = worker_request_factory(context, arguments)
        _validate_worker_request(context, arguments, planned_worker_id, request)
        request = request.model_copy(update={"actor_operation_id": planned_worker_id})
        worker = background.start_auto_research_turn(
            context.episode.episode_id,
            request,
            parent_operation_id=context.task.operation_id,
            operation_id=planned_worker_id,
        )
        if worker is None:
            raise AutoResearchCommandUnavailable(
                "AutoResearch worker admission returned no durable task record."
            )
        return AutoResearchCommandEffectResult(
            message="AutoResearch worker was seated and queued.",
            result={
                "worker_id": worker.operation_id,
                "status": worker.status,
                "disposition": "created",
            },
        )

    def pause(
        context: AutoResearchCommandContext,
        worker_id: str,
    ) -> AutoResearchCommandEffectResult:
        binding, leaf = _worker_leaf(store, context, worker_id)
        try:
            paused = background.pause_auto_research_worker(
                leaf.operation_id,
                context.episode.episode_id,
            )
        except EpisodeNotRunning as exc:
            raise AutoResearchCommandUnavailable(str(exc)) from exc
        return AutoResearchCommandEffectResult(
            message="Pause was requested for the current worker task attempt.",
            result=_worker_control_result(binding, paused),
        )

    def resume(
        context: AutoResearchCommandContext,
        worker_id: str,
    ) -> AutoResearchCommandEffectResult:
        binding, leaf = _worker_leaf(store, context, worker_id)
        try:
            resumed = background.resume(leaf.operation_id)
        except EpisodeNotRunning as exc:
            raise AutoResearchCommandUnavailable(str(exc)) from exc
        return AutoResearchCommandEffectResult(
            message=(
                "The current worker task attempt is resuming from its exact saved "
                "session and allocation."
            ),
            result=_worker_control_result(binding, resumed),
        )

    def stop(
        _context: AutoResearchCommandContext,
        _worker_id: str,
    ) -> AutoResearchCommandEffectResult:
        return AutoResearchCommandEffectResult(
            status="unavailable",
            message=(
                "Individual auto_research worker Stop is unavailable because RCP has no durable "
                "worker-stop primitive; it was not mapped to auto_research Stop or task Pause."
            ),
        )

    def message(
        context: AutoResearchCommandContext,
        arguments: MessageArguments,
        planned_message_id: str,
    ) -> AutoResearchCommandEffectResult:
        recipient_task_id = arguments.recipient_task_id or context.episode.root_operation_id
        if recipient_task_id is None:
            raise AutoResearchCommandUnavailable("The auto_research has no orchestrator mail recipient.")
        try:
            saved = record_auto_research_message(
                store,
                message_id=planned_message_id,
                episode_id=context.episode.episode_id,
                sender_role=context.request.role,
                sender_task_id=context.task.operation_id,
                authorized_by=None,
                recipient_task_id=recipient_task_id,
                control_node_id=context.request.control_node_id,
                body=arguments.body,
            )
        except EpisodeNotRunning as exc:
            raise AutoResearchCommandUnavailable(str(exc)) from exc
        started_operation_id = deliver_pending_auto_research_mail(
            background,
            episode_id=context.episode.episode_id,
            recipient_task_id=recipient_task_id,
        )
        canonical = store.auto_research_message(saved.message_id)
        if canonical is None:
            raise AutoResearchCommandUnavailable(
                "The persisted auto_research message disappeared before delivery was recorded."
            )
        delivery_operation_id = canonical.delivery_operation_id
        return AutoResearchCommandEffectResult(
            message=(
                "AutoResearch message was persisted and a paid delivery turn started."
                if delivery_operation_id is not None
                else (
                    "AutoResearch message was persisted for a later paid delivery; an older pending "
                    "batch started first."
                    if started_operation_id is not None
                    else "AutoResearch message was persisted for the recipient's next paid delivery."
                )
            ),
            result=_message_command_result(
                canonical,
                delivery_operation_id=delivery_operation_id,
                disposition="created",
            ),
        )

    def watch_graph(
        context: AutoResearchCommandContext,
        arguments: WatchGraphArguments,
        planned_watcher_id: str,
    ) -> AutoResearchCommandEffectResult:
        watcher = arm_auto_research_graph_condition(
            store,
            context,
            arguments,
            watcher_id=planned_watcher_id,
            state=graph_state(),
            execution_host=execution_host,
            on_ready=on_watcher_ready,
        )
        return AutoResearchCommandEffectResult(
            message="AutoResearch graph condition was armed.",
            result=_watcher_command_result(watcher, disposition="created"),
        )

    def finish(context: AutoResearchCommandContext) -> AutoResearchCommandEffectResult:
        try:
            signal = auto_research_completion_signal(
                store,
                context.episode.episode_id,
            )
            episode = store.episode(context.episode.episode_id)
            assert episode is not None
        except EpisodeNotRunning as exc:
            raise AutoResearchCommandUnavailable(str(exc)) from exc
        return AutoResearchCommandEffectResult(
            message=(
                "Auto-research completion was fenced. Episode settlement will begin after every "
                "already-admitted turn settles."
            ),
            result={
                "episode_id": episode.episode_id,
                "status": episode.status,
                "ending": signal.ending,
            },
        )

    def reconcile_unknown(
        context: AutoResearchCommandContext,
        request: CommandRequest,
        planned_effect_id: str | None,
    ) -> AutoResearchCommandEffectResult | None:
        if isinstance(request, FinishCommandRequest):
            episode = store.episode(context.episode.episode_id)
            if (
                episode is None
                or episode.mode != "auto_research"
                or episode.ending != "completed"
                or episode.status not in {"wrapping_up", "completed"}
            ):
                return None
            return AutoResearchCommandEffectResult(
                message=("Existing auto_research completion fence returned after interrupted finish."),
                result={
                    "episode_id": episode.episode_id,
                    "status": episode.status,
                    "ending": episode.ending,
                },
            )
        if not _is_canonical_uuid(planned_effect_id):
            return None
        assert planned_effect_id is not None
        if isinstance(request, MessageCommandRequest):
            saved = store.auto_research_message(planned_effect_id)
            if saved is None or not _auto_research_message_matches(
                store,
                context,
                request.arguments,
                saved,
            ):
                return None
            return AutoResearchCommandEffectResult(
                message="Existing auto_research message returned after interrupted delivery.",
                result=_message_command_result(
                    saved,
                    delivery_operation_id=saved.delivery_operation_id,
                    disposition="existing",
                ),
            )
        if isinstance(request, WatchGraphCommandRequest):
            watcher = reconcile_auto_research_graph_condition(
                store,
                context,
                request.arguments,
                watcher_id=planned_effect_id,
                execution_host=execution_host,
            )
            if watcher is None:
                return None
            return AutoResearchCommandEffectResult(
                message="Existing auto_research graph condition returned after interrupted arming.",
                result=_watcher_command_result(watcher, disposition="existing"),
            )
        return None

    def seat_node_type(_project_id: str, node_id: str) -> str | None:
        node = graph_state().nodes.get(node_id)
        return node.type if node is not None else None

    return AutoResearchCommandEffects(
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


def _auto_research_message_matches(
    store: AppStore,
    context: AutoResearchCommandContext,
    arguments: MessageArguments,
    saved: AutoResearchMessageRecord,
) -> bool:
    if context.request.role not in {"orchestrator", "worker"}:
        return False
    recipient_task_id = arguments.recipient_task_id or context.episode.root_operation_id
    if recipient_task_id is None:
        return False
    try:
        sender = store.auto_research_actor_binding(context.task.operation_id)
        recipient = store.auto_research_actor_binding(recipient_task_id)
    except KeyError:
        return False
    expected_actor_id = context.request.actor_operation_id or context.task.operation_id
    if (
        sender.episode_id != context.episode.episode_id
        or sender.actor_operation_id != expected_actor_id
        or sender.role != context.request.role
        or store.auto_research_invocation_role(context.task.operation_id) != sender.role
        or recipient.episode_id != context.episode.episode_id
        or recipient.actor_operation_id != recipient_task_id
    ):
        return False
    if sender.role == "worker":
        if (
            recipient.role != "orchestrator"
            or recipient_task_id != context.episode.root_operation_id
        ):
            return False
    elif recipient.role != "worker":
        return False
    return (
        saved.episode_id == context.episode.episode_id
        and saved.sender_role == sender.role
        and saved.sender_task_id == context.task.operation_id
        and saved.authorized_by is None
        and saved.recipient_task_id == recipient_task_id
        and saved.control_node_id == context.request.control_node_id
        and saved.body == arguments.body
    )


def _message_command_result(
    saved: AutoResearchMessageRecord,
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
    context: AutoResearchCommandContext,
    arguments: SpawnArguments,
    planned_worker_id: str,
    request: AutoResearchRunRequest,
) -> None:
    if (
        request.episode_id != context.episode.episode_id
        or request.role != "worker"
        or request.control_node_id != arguments.seat_node_id
        or request.instruction != arguments.instruction
    ):
        raise AutoResearchCommandInvalid(
            "The resolved worker request changed its auto_research, role, seat, or instruction."
        )
    if request.provider is None or request.run_on is None:
        raise AutoResearchCommandUnavailable(
            "The auto_research worker profile did not resolve a provider and execution machine."
        )
    if request.actor_operation_id not in {None, planned_worker_id}:
        raise AutoResearchCommandInvalid(
            "The resolved worker request did not preserve the planned worker id."
        )
    if (
        request.session_id is not None
        or request.wake_cause is not None
        or request.watcher_ids
    ):
        raise AutoResearchCommandInvalid(
            "A newly seated auto_research worker must start with a fresh session and no wake state."
        )


def _worker_leaf(
    store: AppStore,
    context: AutoResearchCommandContext,
    worker_id: str,
) -> tuple[AutoResearchActorBinding, AgentTaskRecord]:
    try:
        binding = store.auto_research_actor_binding(worker_id)
    except KeyError as exc:
        raise AutoResearchCommandInvalid("Worker control target is outside this auto_research.") from exc
    if binding.episode_id != context.episode.episode_id or binding.role != "worker":
        raise AutoResearchCommandInvalid("Worker control target is outside this auto_research.")
    leaf = store.agent_task(binding.current_operation_id)
    if leaf is None:
        raise AutoResearchCommandUnavailable(
            "The auto_research worker's current task attempt is no longer available."
        )
    return binding, leaf


def _worker_status(
    binding: AutoResearchActorBinding,
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
    binding: AutoResearchActorBinding,
    task: AgentTaskRecord,
) -> dict[str, object]:
    return {
        "worker_id": binding.actor_operation_id,
        "current_operation_id": task.operation_id,
        "status": task.status,
    }
