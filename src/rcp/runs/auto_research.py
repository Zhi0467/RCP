from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.agents.command_protocol import (
    MUTATING_COMMAND_VERBS,
    CommandRequest,
    CommandResponse,
    FinishCommandRequest,
    MessageArguments,
    MessageCommandRequest,
    PauseCommandRequest,
    ResumeCommandRequest,
    SpawnArguments,
    SpawnCommandRequest,
    StatusArguments,
    StopCommandRequest,
    ValidateArguments,
    WatchGraphArguments,
    WatchGraphCommandRequest,
    command_requires_idempotency_key,
)
from rcp.limits import AGENT_COMMAND_EVENT_MAX_BYTES, AGENT_TASK_RECEIPT_MAX_BYTES
from rcp.providers import ProviderId, ProviderSkillReference
from rcp.skill_registry import SkillReference
from rcp.storage import (
    AgentCommandInvocationRecord,
    AgentTaskRecord,
    AppStore,
    AutoResearchMessageRecord,
    EpisodeNotRunning,
    EpisodeRecord,
)

if TYPE_CHECKING:
    from rcp.runs.episode_wrapup import EpisodeWrapupSpec

AutoResearchActorRole = Literal["orchestrator", "worker"]
AutoResearchWakeCause = Literal["watcher", "graph_condition", "message"]
AutoResearchWakeAdmission = Callable[
    [AgentTaskRecord, AutoResearchActorRole, AutoResearchWakeCause],
    AgentTaskRecord | None,
]

class AutoResearchStartRequest(BaseModel):
    """Human-supplied and profile-resolved inputs for one new Auto-research episode."""

    model_config = ConfigDict(extra="forbid")

    invocation_ceiling: int = Field(ge=1)
    starting_instruction: str | None = Field(default=None, max_length=16_000)
    provider: ProviderId | None = None
    run_truth_scope: list[str] | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    workflow_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    invoked_provider_skill_names: list[str] = Field(default_factory=list)
    resolved_provider_skills: list[ProviderSkillReference] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] | None = None

    @model_validator(mode="after")
    def normalize_starting_instruction(self) -> AutoResearchStartRequest:
        if self.starting_instruction is not None:
            instruction = self.starting_instruction.strip()
            self.starting_instruction = instruction or None
        return self


class AutoResearchRunRequest(BaseModel):
    """One operational provider invocation inside an Auto-research episode.

    ``role`` is actor attribution, not a wake category. Watcher, graph-condition,
    and message delivery resume the same orchestrator or worker and keep that
    actor's role while spending another unit from the auto_research pot.
    """

    model_config = ConfigDict(extra="forbid")

    episode_id: str = Field(min_length=1)
    role: AutoResearchActorRole
    provider: ProviderId | None = None
    run_truth_scope: list[str] | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    session_id: str | None = None
    actor_operation_id: str | None = None
    instruction: str | None = Field(default=None, max_length=16_000)
    control_node_id: str | None = None
    wake_cause: AutoResearchWakeCause | None = None
    watcher_ids: list[str] = Field(default_factory=list)
    workflow_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    invoked_provider_skill_names: list[str] = Field(default_factory=list)
    resolved_provider_skills: list[ProviderSkillReference] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] | None = None

    @model_validator(mode="after")
    def role_fields_are_coherent(self) -> AutoResearchRunRequest:
        if self.actor_operation_id is not None:
            actor_operation_id = self.actor_operation_id.strip()
            if not actor_operation_id:
                raise ValueError("an Auto-research actor operation id must not be blank")
            self.actor_operation_id = actor_operation_id
        if self.instruction is not None:
            instruction = self.instruction.strip()
            self.instruction = instruction or None
        if self.role == "worker" and not self.control_node_id:
            raise ValueError(
                "an Auto-research worker must name the Experiment or Blocker seating it"
            )
        if self.wake_cause is not None and self.session_id is None:
            raise ValueError("an Auto-research wake must resume its saved native session")
        if len(self.watcher_ids) != len(set(self.watcher_ids)):
            raise ValueError("an Auto-research wake cannot repeat watcher ids")
        if self.watcher_ids and self.wake_cause not in {"watcher", "graph_condition"}:
            raise ValueError("only an Auto-research watcher wake may carry watcher ids")
        return self


def auto_research_root_request(
    request: AutoResearchStartRequest,
    *,
    episode_id: str,
) -> AutoResearchRunRequest:
    """Capture a resolved Auto-research start as its first orchestrator turn."""

    values = request.model_dump(mode="json", exclude={"invocation_ceiling", "starting_instruction"})
    return AutoResearchRunRequest.model_validate(
        {
            **values,
            "episode_id": episode_id,
            "role": "orchestrator",
            "instruction": request.starting_instruction,
        }
    )


class AutoResearchEndingSignal(BaseModel):
    """One durable mode ending handed to central episode settlement."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str = Field(min_length=1)
    ending: Literal["completed", "exhausted", "failed", "human_pause"]
    partial: bool
    diagnostic: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def partial_matches_ending(self) -> AutoResearchEndingSignal:
        if self.partial != (self.ending != "completed"):
            raise ValueError("an Auto-research ending signal has inconsistent partial state")
        return self


def fence_auto_research_ending(
    store: AppStore,
    episode_id: str,
    ending: Literal["completed", "exhausted", "failed", "human_pause"],
    *,
    diagnostic: str | None = None,
) -> AutoResearchEndingSignal:
    """Fence new work and return the mode signal central settlement consumes."""

    episode = _auto_research_episode(store, episode_id)
    store.fence_episode_ending(episode.episode_id, ending, diagnostic=diagnostic)
    store.settle_auto_research_watchers(episode.episode_id)
    return AutoResearchEndingSignal(
        episode_id=episode_id,
        ending=ending,
        partial=ending != "completed",
        diagnostic=diagnostic,
    )


def auto_research_completion_signal(
    store: AppStore,
    episode_id: str,
    *,
    diagnostic: str | None = None,
) -> AutoResearchEndingSignal:
    return fence_auto_research_ending(
        store,
        episode_id,
        "completed",
        diagnostic=diagnostic,
    )


def auto_research_exhaustion_signal(
    store: AppStore,
    episode_id: str,
    *,
    diagnostic: str | None = None,
) -> AutoResearchEndingSignal:
    return fence_auto_research_ending(
        store,
        episode_id,
        "exhausted",
        diagnostic=diagnostic,
    )


def auto_research_failure_signal(
    store: AppStore,
    episode_id: str,
    *,
    diagnostic: str,
) -> AutoResearchEndingSignal:
    return fence_auto_research_ending(
        store,
        episode_id,
        "failed",
        diagnostic=diagnostic,
    )


def request_auto_research_stop(store: AppStore, episode_id: str) -> EpisodeRecord:
    """Persist Stop first, then retain every Auto watcher as stopped."""

    _auto_research_episode(store, episode_id)
    episode = store.request_episode_stop(episode_id)
    store.settle_auto_research_watchers(episode_id)
    return episode


def settle_auto_research_stop(
    store: AppStore,
    episode_id: str,
    *,
    diagnostic: str | None = None,
) -> EpisodeRecord | None:
    """Settle Stop once all already-authorized Auto work is quiescent."""

    episode = _auto_research_episode(store, episode_id)
    if episode.stop_requested_at is None:
        raise EpisodeNotRunning("the Auto-research episode has no durable Stop request")
    if not store.auto_research_is_quiescent(episode_id):
        return None
    return store.mark_episode_stop_skipped(episode_id, diagnostic=diagnostic)


def auto_research_wrapup_spec(
    store: AppStore,
    signal: AutoResearchEndingSignal,
) -> EpisodeWrapupSpec:
    """Build a compact receipt and select the root actor's exact latest task."""

    from rcp.runs.episode_wrapup import EpisodeWrapupSpec

    episode = _auto_research_episode(store, signal.episode_id)
    if episode.ending != signal.ending or episode.ending_diagnostic != signal.diagnostic:
        raise ValueError("the Auto-research ending signal differs from its durable fence")
    if episode.root_operation_id is None:
        raise ValueError("the Auto-research episode has no root orchestrator actor")
    binding = store.auto_research_actor_binding(episode.root_operation_id)
    if binding.episode_id != episode.episode_id or binding.role != "orchestrator":
        raise ValueError("the Auto-research root actor binding is inconsistent")
    continuation = store.agent_task(binding.current_operation_id)
    if continuation is None:
        raise ValueError("the Auto-research root actor lost its latest continuation task")

    state = store.auto_research_state(episode.episode_id)
    meter = store.episode_budget_meter(episode.episode_id)
    tasks = store.auto_research_tasks(episode.episode_id)
    task_statuses: dict[str, int] = {}
    actor_rows: dict[str, dict[str, object]] = {}
    graph_results: list[dict[str, object]] = []
    for task in tasks:
        task_statuses[task.status] = task_statuses.get(task.status, 0) + 1
        invocation = store.auto_research_invocation(task.operation_id)
        if invocation is not None:
            actor_rows[invocation.actor_operation_id] = {
                "actor_operation_id": _receipt_text(invocation.actor_operation_id, 160),
                "role": invocation.role,
                "control_node_id": _receipt_text(invocation.control_node_id, 240),
                "latest_operation_id": _receipt_text(task.operation_id, 160),
                "latest_status": task.status,
                "latest_attempt": task.attempt,
            }
        graph_update = task.result.get("graph_update") if isinstance(task.result, dict) else None
        if isinstance(graph_update, dict):
            graph_results.append(
                {
                    "operation_id": _receipt_text(task.operation_id, 160),
                    "status": _receipt_text(graph_update.get("status"), 80),
                    "applied_revision": graph_update.get("applied_revision"),
                }
            )

    _, events = store.auto_research_event_history(episode.episode_id, limit=64)
    command_facts = [
        {
            "operation_id": _receipt_text(event.operation_id, 160),
            "verb": _receipt_text(event.command_verb, 80),
            "phase": event.command_phase,
            "level": event.level,
        }
        for event in events
        if event.event_kind == "command"
    ][-16:]
    receipt: dict[str, object] = {
        "starting_instruction": _receipt_text(
            state.starting_instruction if state is not None else None,
            1_200,
        ),
        "operational_meter": {
            "ceiling": meter.invocation_ceiling,
            "used": meter.invocations_used,
            "remaining": meter.invocations_remaining,
            "observed_input_tokens": meter.observed_input_tokens,
            "observed_generated_tokens": meter.observed_generated_tokens,
        },
        "task_status_counts": dict(sorted(task_statuses.items())),
        "actors": list(actor_rows.values())[-16:],
        "omitted_actor_count": max(0, len(actor_rows) - 16),
        "command_facts": command_facts,
        "graph_results": graph_results[-16:],
    }
    if _receipt_size(receipt) > AGENT_TASK_RECEIPT_MAX_BYTES:
        receipt["actors"] = list(actor_rows.values())[-8:]
        receipt["command_facts"] = command_facts[-8:]
        receipt["graph_results"] = graph_results[-8:]
        receipt["starting_instruction"] = _receipt_text(
            state.starting_instruction if state is not None else None,
            480,
        )
    if _receipt_size(receipt) > AGENT_TASK_RECEIPT_MAX_BYTES:
        raise ValueError("the compact Auto-research ending receipt exceeds its storage boundary")
    return EpisodeWrapupSpec(
        episode_id=episode.episode_id,
        ending=signal.ending,
        partial=signal.partial,
        continuation_operation_id=continuation.operation_id,
        receipt=receipt,
        diagnostic=signal.diagnostic,
    )


def _auto_research_episode(store: AppStore, episode_id: str) -> EpisodeRecord:
    episode = store.episode(episode_id)
    if episode is None:
        raise KeyError(episode_id)
    if episode.mode != "auto_research" or store.auto_research_state(episode_id) is None:
        raise ValueError("the episode is not a canonical Auto-research episode")
    return episode


def _receipt_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _receipt_size(value: dict[str, object]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


class PendingAutoResearchMail(BaseModel):
    """Unclaimed hearsay-only messages awaiting one atomic wake admission."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    recipient_task_id: str
    messages: list[AutoResearchMessageRecord]
    graph_authority: Literal["none"] = "none"

    @property
    def message_ids(self) -> list[str]:
        return [message.message_id for message in self.messages]


def pending_auto_research_mail(
    store: AppStore,
    *,
    episode_id: str,
    recipient_task_id: str,
) -> PendingAutoResearchMail:
    """Read one recipient's undelivered mail without claiming a wake path."""

    recipient = store.agent_task(recipient_task_id)
    if recipient is None:
        raise KeyError(recipient_task_id)
    if recipient.episode_id != episode_id:
        raise ValueError("auto_research mail recipient is outside the auto_research")
    messages = store.pending_auto_research_messages(episode_id, recipient_task_id)
    return PendingAutoResearchMail(
        episode_id=episode_id,
        recipient_task_id=recipient_task_id,
        messages=messages,
    )


class AutoResearchCommandInvalid(ValueError):
    """A staged command is well-formed but not permitted or applicable."""


class AutoResearchCommandUnavailable(RuntimeError):
    """A staged command could not reach the authoritative effect boundary."""


class AutoResearchCommandEffectResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "invalid", "unavailable"] = "ok"
    message: str | None = Field(default=None, max_length=2_000)
    result: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unsuccessful_result_has_a_diagnostic(self) -> AutoResearchCommandEffectResult:
        if self.status != "ok" and not (self.message or "").strip():
            raise ValueError("an unsuccessful auto_research command requires a diagnostic")
        try:
            encoded = json.dumps(
                {
                    "status": self.status,
                    "result": self.result,
                    **({"diagnostic": self.message} if self.message else {}),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("auto_research command result must be valid JSON") from exc
        if len(encoded) > AGENT_COMMAND_EVENT_MAX_BYTES:
            raise ValueError("auto_research command result exceeds the event ledger limit")
        return self


@dataclass(frozen=True)
class AutoResearchCommandContext:
    episode: EpisodeRecord
    task: AgentTaskRecord
    request: AutoResearchRunRequest


AutoResearchValidateCommand = Callable[
    [AutoResearchCommandContext, ValidateArguments],
    AutoResearchCommandEffectResult,
]
AutoResearchStatusCommand = Callable[
    [AutoResearchCommandContext, StatusArguments],
    AutoResearchCommandEffectResult,
]
AutoResearchSpawnCommand = Callable[
    [AutoResearchCommandContext, SpawnArguments, str],
    AutoResearchCommandEffectResult,
]
AutoResearchWorkerCommand = Callable[
    [AutoResearchCommandContext, str],
    AutoResearchCommandEffectResult,
]
AutoResearchMessageCommand = Callable[
    [AutoResearchCommandContext, MessageArguments, str],
    AutoResearchCommandEffectResult,
]
AutoResearchWatchGraphCommand = Callable[
    [AutoResearchCommandContext, WatchGraphArguments, str],
    AutoResearchCommandEffectResult,
]
AutoResearchFinishCommand = Callable[[AutoResearchCommandContext], AutoResearchCommandEffectResult]
AutoResearchUnknownCommandReconciler = Callable[
    [AutoResearchCommandContext, CommandRequest, str | None],
    AutoResearchCommandEffectResult | None,
]
AutoResearchSeatNodeType = Callable[[str, str], str | None]


@dataclass(frozen=True)
class AutoResearchCommandEffects:
    """Injected graph/run effects behind the staged transport protocol.

    This seam lets API composition bind existing validator, watcher, and
    BackgroundAgentTasks behavior without making an execution host call RCP over
    HTTP and without adding another wake implementation here.
    """

    validate: AutoResearchValidateCommand
    status: AutoResearchStatusCommand
    spawn: AutoResearchSpawnCommand
    pause: AutoResearchWorkerCommand
    resume: AutoResearchWorkerCommand
    stop: AutoResearchWorkerCommand
    message: AutoResearchMessageCommand
    watch_graph: AutoResearchWatchGraphCommand
    finish: AutoResearchFinishCommand
    seat_node_type: AutoResearchSeatNodeType
    reconcile_unknown: AutoResearchUnknownCommandReconciler


class AutoResearchCommandDispatcher:
    """Audit, deduplicate, reconcile, and dispatch one staged client call."""

    def __init__(self, store: AppStore, effects: AutoResearchCommandEffects) -> None:
        self.store = store
        self.effects = effects

    def dispatch(self, operation_id: str, request: CommandRequest) -> CommandResponse:
        context = self._context(operation_id)
        if request.mailbox_id == "":  # already schema-validated; keeps the binding explicit here
            raise AutoResearchCommandInvalid("auto_research command mailbox is missing")

        planned_worker_id = (
            _planned_worker_id(context.episode.episode_id, request.idempotency_key)
            if isinstance(request, SpawnCommandRequest) and request.idempotency_key is not None
            else None
        )
        planned_message_id = (
            _planned_effect_id(
                context.episode.episode_id,
                "message",
                request.idempotency_key,
            )
            if isinstance(request, MessageCommandRequest) and request.idempotency_key is not None
            else None
        )
        planned_watcher_id = (
            _planned_effect_id(
                context.episode.episode_id,
                "watch_graph",
                request.idempotency_key,
            )
            if isinstance(request, WatchGraphCommandRequest) and request.idempotency_key is not None
            else None
        )
        arguments = request.arguments.model_dump(mode="json")
        if request.verb == "validate":
            patch = request.arguments.patch
            encoded_patch = patch.encode("utf-8")
            arguments = {
                "patch_byte_length": len(encoded_patch),
                "patch_sha256": hashlib.sha256(encoded_patch).hexdigest(),
            }
        start_payload = {
            "request_id": request.request_id,
            "arguments": arguments,
        }
        if planned_worker_id is not None:
            start_payload["planned_worker_id"] = planned_worker_id
        if planned_message_id is not None:
            start_payload["planned_message_id"] = planned_message_id
        if planned_watcher_id is not None:
            start_payload["planned_watcher_id"] = planned_watcher_id
        prior = (
            self.store.agent_command_by_key(
                context.episode.episode_id,
                request.idempotency_key,
            )
            if request.idempotency_key is not None
            else None
        )

        if prior is not None:
            attempt = self._start_retry_attempt(context, request, start_payload, prior)
            return self._dispatch_retry(context, request, prior, attempt)

        command_id = self._unused_command_id(request.request_id)
        try:
            invocation = self.store.start_agent_command(
                operation_id=operation_id,
                command_id=command_id,
                episode_id=context.episode.episode_id,
                verb=request.verb,
                idempotency_key=request.idempotency_key,
                payload=start_payload,
            )
        except ValueError:
            # Another client may have won the auto_research-wide key between the
            # read above and this insert. Preserve that invocation as the
            # effect record while giving this client call its own ledger pair.
            raced = (
                self.store.agent_command_by_key(
                    context.episode.episode_id,
                    request.idempotency_key,
                )
                if request.idempotency_key is not None
                else None
            )
            if raced is None:
                raise
            attempt = self._start_retry_attempt(context, request, start_payload, raced)
            return self._dispatch_retry(context, request, raced, attempt)
        if invocation.command_id != command_id:
            # ``start_agent_command`` returns the winning keyed invocation on
            # a race without inserting this call. Record this call separately.
            attempt = self._start_retry_attempt(context, request, start_payload, invocation)
            return self._dispatch_retry(context, request, invocation, attempt)

        if command_requires_idempotency_key(request.verb) and request.idempotency_key is None:
            return self._finish(
                invocation.command_id,
                request.request_id,
                AutoResearchCommandEffectResult(
                    status="invalid",
                    message=f"Agent command {request.verb} requires an idempotency key.",
                ),
            )

        try:
            outcome = self._execute(
                context,
                request,
                planned_worker_id=planned_worker_id,
                planned_message_id=planned_message_id,
                planned_watcher_id=planned_watcher_id,
            )
        except AutoResearchCommandInvalid as exc:
            outcome = AutoResearchCommandEffectResult(status="invalid", message=str(exc))
        except (AutoResearchCommandUnavailable, OSError) as exc:
            outcome = AutoResearchCommandEffectResult(status="unavailable", message=str(exc))
        except (KeyError, ValueError) as exc:
            outcome = AutoResearchCommandEffectResult(status="invalid", message=str(exc))
        return self._finish(invocation.command_id, request.request_id, outcome)

    def _start_retry_attempt(
        self,
        context: AutoResearchCommandContext,
        request: CommandRequest,
        start_payload: dict[str, object],
        prior: AgentCommandInvocationRecord,
    ) -> AgentCommandInvocationRecord:
        """Give every client retry its own start/exit pair without owning the key."""

        attempt_payload = {
            **start_payload,
            "idempotency_key": request.idempotency_key,
            "deduplicates_command_id": prior.command_id,
        }
        return self.store.start_agent_command(
            operation_id=context.task.operation_id,
            command_id=self._unused_command_id(request.request_id),
            episode_id=context.episode.episode_id,
            verb=request.verb,
            idempotency_key=None,
            payload=attempt_payload,
        )

    def _dispatch_retry(
        self,
        retry_context: AutoResearchCommandContext,
        request: CommandRequest,
        prior: AgentCommandInvocationRecord,
        attempt: AgentCommandInvocationRecord,
    ) -> CommandResponse:
        """Resolve one keyed retry from the original durable request intent."""

        try:
            original_context = self._context(prior.operation_id)
            if original_context.episode.episode_id != retry_context.episode.episode_id:
                raise AutoResearchCommandUnavailable(
                    "The original command task no longer belongs to this auto_research."
                )
        except (AutoResearchCommandInvalid, AutoResearchCommandUnavailable, KeyError, ValueError) as exc:
            return self._finish(
                attempt.command_id,
                request.request_id,
                AutoResearchCommandEffectResult(status="unavailable", message=str(exc)),
            )

        try:
            original_actor = self._canonical_command_actor(original_context)
            retry_actor = self._canonical_command_actor(retry_context)
        except (AutoResearchCommandInvalid, AutoResearchCommandUnavailable, KeyError, ValueError) as exc:
            return self._finish(
                attempt.command_id,
                request.request_id,
                AutoResearchCommandEffectResult(status="unavailable", message=str(exc)),
            )
        if retry_actor != original_actor:
            return self._finish(
                attempt.command_id,
                request.request_id,
                AutoResearchCommandEffectResult(
                    status="invalid",
                    message=(
                        "An idempotency key may be replayed only by the same canonical "
                        "auto_research actor and role."
                    ),
                ),
            )

        try:
            recorded_request = self._recorded_request(request, prior)
        except AutoResearchCommandInvalid as exc:
            return self._finish(
                attempt.command_id,
                request.request_id,
                AutoResearchCommandEffectResult(status="invalid", message=str(exc)),
            )
        except (AutoResearchCommandUnavailable, KeyError, ValueError) as exc:
            outcome = AutoResearchCommandEffectResult(status="unavailable", message=str(exc))
            if prior.exited_at is None:
                outcome = self._finish_original_unknown(prior.command_id, outcome)
            return self._finish(attempt.command_id, request.request_id, outcome)

        if prior.exited_at is not None:
            try:
                outcome = self._completed_retry_outcome(original_context, recorded_request, prior)
            except (
                AutoResearchCommandInvalid,
                AutoResearchCommandUnavailable,
                KeyError,
                ValueError,
            ) as exc:
                outcome = AutoResearchCommandEffectResult(status="unavailable", message=str(exc))
            return self._finish(attempt.command_id, request.request_id, outcome)

        try:
            reconciled = self._reconcile_unknown(
                original_context,
                recorded_request,
                prior.start_payload,
            )
            if reconciled is None:
                if not isinstance(recorded_request, SpawnCommandRequest):
                    raise AutoResearchCommandUnavailable(
                        "Interrupted command outcome is unknown and could not be proven; "
                        "it was not re-executed."
                    )
                planned_worker_id = self._recorded_planned_worker_id(
                    original_context,
                    recorded_request,
                    prior.start_payload,
                )
                reconciled = self._execute(
                    original_context,
                    recorded_request,
                    planned_worker_id=planned_worker_id,
                    planned_message_id=None,
                    planned_watcher_id=None,
                )
            outcome = reconciled
        except AutoResearchCommandInvalid as exc:
            outcome = AutoResearchCommandEffectResult(status="invalid", message=str(exc))
        except (AutoResearchCommandUnavailable, OSError, KeyError, ValueError) as exc:
            outcome = AutoResearchCommandEffectResult(status="unavailable", message=str(exc))

        outcome = self._finish_original_unknown(prior.command_id, outcome)
        return self._finish(attempt.command_id, request.request_id, outcome)

    @staticmethod
    def _recorded_request(
        request: CommandRequest,
        prior: AgentCommandInvocationRecord,
    ) -> CommandRequest:
        if prior.verb != request.verb:
            raise AutoResearchCommandInvalid(
                "This idempotency key was already used for another command verb."
            )
        recorded_arguments = prior.start_payload.get("arguments")
        current_arguments = request.arguments.model_dump(mode="json")
        if recorded_arguments != current_arguments:
            raise AutoResearchCommandInvalid(
                "This idempotency key was already used with different command arguments."
            )
        if not isinstance(recorded_arguments, dict):
            raise AutoResearchCommandUnavailable(
                "The original command has no valid recorded arguments to reconcile."
            )
        try:
            arguments = type(request.arguments).model_validate(recorded_arguments)
        except ValueError as exc:
            raise AutoResearchCommandUnavailable(
                "The original command's recorded arguments are invalid."
            ) from exc
        return request.model_copy(
            update={
                "arguments": arguments,
                "idempotency_key": prior.idempotency_key,
            }
        )

    def _completed_retry_outcome(
        self,
        context: AutoResearchCommandContext,
        request: CommandRequest,
        prior: AgentCommandInvocationRecord,
    ) -> AutoResearchCommandEffectResult:
        self._recorded_planned_effect_id(context, request, prior.start_payload)
        recorded = _effect_from_recorded_invocation(prior)
        if not isinstance(request, SpawnCommandRequest):
            return recorded
        planned_worker_id = self._recorded_planned_worker_id(
            context,
            request,
            prior.start_payload,
        )
        worker = self.store.agent_task(planned_worker_id)
        if worker is not None:
            worker = self._verify_spawn_worker(context, request.arguments, planned_worker_id)
            return AutoResearchCommandEffectResult(
                message="Existing auto_research worker returned for this spawn idempotency key.",
                result=_worker_command_result(worker, disposition="existing"),
            )
        if recorded.status == "ok":
            raise AutoResearchCommandUnavailable(
                "Completed spawn has no durable canonical worker to return."
            )
        return recorded

    def _finish_original_unknown(
        self,
        command_id: str,
        outcome: AutoResearchCommandEffectResult,
    ) -> AutoResearchCommandEffectResult:
        try:
            self._record_finish(command_id, outcome)
            return outcome
        except ValueError:
            # A concurrent retry may have resolved the original first. Its
            # durable exit is authoritative for this retry too.
            recorded = self.store.agent_command(command_id)
            if recorded is None or recorded.exited_at is None:
                raise
            return _effect_from_recorded_invocation(recorded)

    def _unused_command_id(self, preferred: str) -> str:
        if self.store.agent_command(preferred) is None:
            return preferred
        while True:
            candidate = uuid.uuid4().hex
            if self.store.agent_command(candidate) is None:
                return candidate

    def _context(self, operation_id: str) -> AutoResearchCommandContext:
        task = self.store.agent_task(operation_id)
        if task is None:
            raise KeyError(operation_id)
        if task.kind != "auto_research" or task.episode_id is None:
            raise AutoResearchCommandInvalid("agent command requires an Auto-research task")
        episode = self.store.episode(task.episode_id)
        if episode is None or episode.mode != "auto_research":
            raise KeyError(task.episode_id)
        request = AutoResearchRunRequest.model_validate(task.request)
        return AutoResearchCommandContext(episode=episode, task=task, request=request)

    def _canonical_command_actor(
        self,
        context: AutoResearchCommandContext,
    ) -> tuple[str, str]:
        binding = self.store.auto_research_actor_binding(context.task.operation_id)
        role = self.store.auto_research_invocation_role(context.task.operation_id)
        if (
            role is None
            or role != binding.role
            or binding.episode_id != context.episode.episode_id
        ):
            raise AutoResearchCommandUnavailable(
                "AutoResearch command task has no coherent canonical actor role."
            )
        return binding.actor_operation_id, role

    def _reconcile_unknown(
        self,
        context: AutoResearchCommandContext,
        request: CommandRequest,
        start_payload: dict[str, object],
    ) -> AutoResearchCommandEffectResult | None:
        if isinstance(request, SpawnCommandRequest):
            planned_worker_id = self._recorded_planned_worker_id(
                context,
                request,
                start_payload,
            )
            worker = self.store.agent_task(planned_worker_id)
            if worker is None:
                # The durable task row is the spawn commit point. Its absence means
                # the earlier attempt did not create a worker, so the same planned
                # id may be attempted; an existing row is never restarted.
                return None
            worker = self._verify_spawn_worker(context, request.arguments, planned_worker_id)
            return AutoResearchCommandEffectResult(
                message="Existing auto_research worker returned after interrupted spawn.",
                result=_worker_command_result(worker, disposition="existing"),
            )
        planned_effect_id = self._recorded_planned_effect_id(
            context,
            request,
            start_payload,
        )
        return self.effects.reconcile_unknown(context, request, planned_effect_id)

    @staticmethod
    def _recorded_planned_worker_id(
        context: AutoResearchCommandContext,
        request: SpawnCommandRequest,
        start_payload: dict[str, object],
    ) -> str:
        planned_worker_id = start_payload.get("planned_worker_id")
        expected_worker_id = _planned_worker_id(
            context.episode.episode_id,
            request.idempotency_key,
        )
        if planned_worker_id != expected_worker_id:
            raise AutoResearchCommandUnavailable(
                "Interrupted spawn has no valid deterministic worker id to reconcile."
            )
        return expected_worker_id

    @staticmethod
    def _recorded_planned_effect_id(
        context: AutoResearchCommandContext,
        request: CommandRequest,
        start_payload: dict[str, object],
    ) -> str | None:
        if isinstance(request, MessageCommandRequest):
            field = "planned_message_id"
            verb: Literal["message", "watch_graph"] = "message"
        elif isinstance(request, WatchGraphCommandRequest):
            field = "planned_watcher_id"
            verb = "watch_graph"
        else:
            return None
        planned_effect_id = start_payload.get(field)
        expected_effect_id = _planned_effect_id(
            context.episode.episode_id,
            verb,
            request.idempotency_key,
        )
        if planned_effect_id != expected_effect_id:
            raise AutoResearchCommandUnavailable(
                f"Interrupted {verb} has no valid deterministic effect id to reconcile."
            )
        return expected_effect_id

    def _execute(
        self,
        context: AutoResearchCommandContext,
        request: CommandRequest,
        *,
        planned_worker_id: str | None,
        planned_message_id: str | None,
        planned_watcher_id: str | None,
    ) -> AutoResearchCommandEffectResult:
        retrospective_worker_reply = request.verb == "message" and context.request.role == "worker"
        if request.verb in MUTATING_COMMAND_VERBS and not retrospective_worker_reply:
            episode = self.store.episode(context.episode.episode_id)
            if episode is None or episode.mode != "auto_research":
                raise AutoResearchCommandUnavailable(
                    "The Auto-research episode is no longer available."
                )
            if (
                episode.status != "running"
                or episode.ending is not None
                or episode.stop_requested_at is not None
            ):
                raise AutoResearchCommandUnavailable(
                    "The auto_research is no longer accepting mutating commands."
                )
        if request.verb == "validate":
            return self.effects.validate(context, request.arguments)
        if request.verb == "status":
            return self.effects.status(context, request.arguments)
        if request.verb == "message":
            assert planned_message_id is not None
            recipient_task_id = request.arguments.recipient_task_id
            if context.request.role == "worker":
                if recipient_task_id not in {None, context.episode.root_operation_id}:
                    raise AutoResearchCommandInvalid(
                        "A auto_research worker may reply only to its orchestrator."
                    )
                return self.effects.message(context, request.arguments, planned_message_id)
            if context.request.role == "orchestrator":
                if recipient_task_id is None:
                    raise AutoResearchCommandInvalid(
                        "The auto_research orchestrator must name the worker it is messaging."
                    )
                worker = self._require_worker(context, recipient_task_id)
                binding = self.store.auto_research_actor_binding(worker.operation_id)
                if recipient_task_id != binding.actor_operation_id:
                    raise AutoResearchCommandInvalid(
                        "The auto_research orchestrator must address a worker by its stable worker id."
                    )
                return self.effects.message(context, request.arguments, planned_message_id)
            raise AutoResearchCommandInvalid("Only an Auto-research actor can send messages.")
        if context.request.role != "orchestrator":
            raise AutoResearchCommandInvalid(
                "Only the auto_research orchestrator may issue mutating staged commands."
            )
        if request.verb == "spawn":
            assert planned_worker_id is not None
            node_type = self.effects.seat_node_type(
                context.task.project_id,
                request.arguments.seat_node_id,
            )
            if node_type is None or node_type.casefold() not in {"experiment", "blocker"}:
                raise AutoResearchCommandInvalid(
                    "AutoResearch workers may be seated only on Experiments and Blockers."
                )
            outcome = self.effects.spawn(context, request.arguments, planned_worker_id)
            if outcome.status != "ok":
                return outcome
            worker = self._verify_spawn_worker(context, request.arguments, planned_worker_id)
            if not outcome.result:
                outcome = outcome.model_copy(
                    update={"result": _worker_command_result(worker, disposition="created")}
                )
            return outcome
        if request.verb == "pause":
            assert isinstance(request, PauseCommandRequest)
            self._require_worker(context, request.arguments.worker_id)
            return self.effects.pause(context, request.arguments.worker_id)
        if request.verb == "resume":
            assert isinstance(request, ResumeCommandRequest)
            self._require_worker(context, request.arguments.worker_id)
            return self.effects.resume(context, request.arguments.worker_id)
        if request.verb == "stop":
            assert isinstance(request, StopCommandRequest)
            self._require_worker(context, request.arguments.worker_id)
            return self.effects.stop(context, request.arguments.worker_id)
        if request.verb == "watch_graph":
            assert planned_watcher_id is not None
            return self.effects.watch_graph(context, request.arguments, planned_watcher_id)
        if request.verb == "finish":
            assert isinstance(request, FinishCommandRequest)
            return self.effects.finish(context)
        raise AssertionError(f"unhandled auto_research command verb: {request.verb}")

    def _require_worker(
        self,
        context: AutoResearchCommandContext,
        operation_id: str,
    ) -> AgentTaskRecord:
        worker = self.store.agent_task(operation_id)
        if worker is None or worker.episode_id != context.episode.episode_id:
            raise AutoResearchCommandInvalid("Worker control target is outside this auto_research.")
        worker_request = AutoResearchRunRequest.model_validate(worker.request)
        if worker_request.role != "worker":
            raise AutoResearchCommandInvalid("Worker control target is not a auto_research worker.")
        return worker

    def _verify_spawn_worker(
        self,
        context: AutoResearchCommandContext,
        arguments: SpawnArguments,
        planned_worker_id: str,
    ) -> AgentTaskRecord:
        """Mechanically prove the canonical worker row matches the spawn intent."""

        worker = self.store.agent_task(planned_worker_id)
        if worker is None:
            raise AutoResearchCommandUnavailable(
                "AutoResearch spawn returned without durably creating its planned worker."
            )
        if (
            worker.kind != "auto_research"
            or worker.project_id != context.episode.project_id
            or worker.episode_id != context.episode.episode_id
            or worker.parent_operation_id != context.task.operation_id
        ):
            raise AutoResearchCommandUnavailable(
                "AutoResearch spawn created a worker with incorrect auto_research parent lineage."
            )
        if self.store.auto_research_invocation_role(worker.operation_id) != "worker":
            raise AutoResearchCommandUnavailable(
                "AutoResearch spawn did not record the canonical worker invocation role."
            )
        try:
            worker_request = AutoResearchRunRequest.model_validate(worker.request)
        except ValueError as exc:
            raise AutoResearchCommandUnavailable(
                "AutoResearch spawn created a worker with an invalid run request."
            ) from exc
        if (
            worker_request.episode_id != context.episode.episode_id
            or worker_request.role != "worker"
            or worker_request.control_node_id != arguments.seat_node_id
            or worker_request.instruction != arguments.instruction
        ):
            raise AutoResearchCommandUnavailable(
                "AutoResearch spawn created a worker that does not match its recorded seat or "
                "instruction."
            )
        return worker

    def _finish(
        self,
        command_id: str,
        response_request_id: str,
        outcome: AutoResearchCommandEffectResult,
    ) -> CommandResponse:
        self._record_finish(command_id, outcome)
        return CommandResponse(
            request_id=response_request_id,
            status=outcome.status,
            message=outcome.message,
            result=outcome.result,
        )

    def _record_finish(
        self,
        command_id: str,
        outcome: AutoResearchCommandEffectResult,
    ) -> None:
        payload: dict[str, object] = {"result": outcome.result}
        if outcome.message:
            payload["diagnostic"] = outcome.message
        self.store.finish_agent_command(
            command_id,
            status=outcome.status,
            payload=payload,
            message=outcome.message or f"Agent command completed with {outcome.status}.",
        )


def _planned_worker_id(episode_id: str, idempotency_key: str | None) -> str:
    if idempotency_key is None:
        raise ValueError("a spawn command requires an idempotency key")
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:auto_research:{episode_id}:spawn:{idempotency_key}",
        )
    )


def _planned_effect_id(
    episode_id: str,
    verb: Literal["message", "watch_graph"],
    idempotency_key: str | None,
) -> str:
    if idempotency_key is None:
        raise ValueError(f"a {verb} command requires an idempotency key")
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:auto_research:{episode_id}:{verb}:{idempotency_key}",
        )
    )


def _worker_command_result(
    worker: AgentTaskRecord,
    *,
    disposition: Literal["created", "existing"],
) -> dict[str, object]:
    return {
        "worker_id": worker.operation_id,
        "status": worker.status,
        "disposition": disposition,
    }


def _effect_from_recorded_invocation(
    invocation: AgentCommandInvocationRecord,
) -> AutoResearchCommandEffectResult:
    status = invocation.status
    payload = invocation.exit_payload
    if status not in {"ok", "invalid", "unavailable"} or not isinstance(payload, dict):
        raise AutoResearchCommandUnavailable("Recorded auto_research command exit is incomplete.")
    recorded_result = payload.get("result")
    result = (
        dict(recorded_result)
        if isinstance(recorded_result, dict)
        else {key: value for key, value in payload.items() if key not in {"status", "diagnostic"}}
    )
    diagnostic = payload.get("diagnostic")
    message = diagnostic if isinstance(diagnostic, str) else None
    if status != "ok" and message is None:
        message = "Recorded auto_research command did not complete successfully."
    return AutoResearchCommandEffectResult(
        status=status,
        message=message,
        result=result,
    )
