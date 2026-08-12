from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

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
from rcp.artifacts import validate_artifact_bytes
from rcp.limits import AGENT_COMMAND_EVENT_MAX_BYTES, CHAT_ARTIFACT_MAX_FILE_BYTES
from rcp.providers import ProviderId, ProviderSkillReference
from rcp.skill_registry import SkillReference
from rcp.storage import (
    AgentCommandInvocationRecord,
    AgentTaskRecord,
    AppStore,
    CampaignEnding,
    CampaignMessageRecord,
    CampaignRecord,
    CampaignReportRecord,
)

CampaignActorRole = Literal["orchestrator", "worker", "report"]
CampaignWakeCause = Literal["watcher", "graph_condition", "message"]
CampaignWakeAdmission = Callable[
    [AgentTaskRecord, CampaignActorRole, CampaignWakeCause],
    AgentTaskRecord | None,
]

CAMPAIGN_REPORT_MAX_CORRECTION_ROUNDS = 2


class CampaignStartRequest(BaseModel):
    """Human-supplied and profile-resolved inputs for one new campaign."""

    model_config = ConfigDict(extra="forbid")

    invocation_ceiling: int = Field(ge=2)
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
    def normalize_starting_instruction(self) -> CampaignStartRequest:
        if self.starting_instruction is not None:
            instruction = self.starting_instruction.strip()
            self.starting_instruction = instruction or None
        return self


class CampaignRunRequest(BaseModel):
    """One paid provider invocation inside a campaign.

    ``role`` is actor attribution, not a wake category. Watcher, graph-condition,
    and message delivery resume the same orchestrator or worker and keep that
    actor's role while spending another unit from the campaign pot.
    """

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1)
    role: CampaignActorRole
    provider: ProviderId | None = None
    run_truth_scope: list[str] | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    session_id: str | None = None
    actor_operation_id: str | None = None
    instruction: str | None = Field(default=None, max_length=16_000)
    control_node_id: str | None = None
    wake_cause: CampaignWakeCause | None = None
    watcher_ids: list[str] = Field(default_factory=list)
    ending: CampaignEnding | None = None
    workflow_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    invoked_provider_skill_names: list[str] = Field(default_factory=list)
    resolved_provider_skills: list[ProviderSkillReference] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] | None = None

    @model_validator(mode="after")
    def role_fields_are_coherent(self) -> CampaignRunRequest:
        if self.actor_operation_id is not None:
            actor_operation_id = self.actor_operation_id.strip()
            if not actor_operation_id:
                raise ValueError("a campaign actor operation id must not be blank")
            self.actor_operation_id = actor_operation_id
        if self.instruction is not None:
            instruction = self.instruction.strip()
            self.instruction = instruction or None
        if self.role == "worker" and not self.control_node_id:
            raise ValueError("a campaign worker must name the Experiment or Blocker seating it")
        if self.role == "report":
            if self.ending is None:
                raise ValueError("a campaign report turn must name the campaign ending")
            if self.control_node_id is not None or self.wake_cause is not None:
                raise ValueError("a campaign report turn cannot be a seated worker or wake")
        elif self.ending is not None:
            raise ValueError("only a campaign report turn may name an ending")
        if self.wake_cause is not None and self.session_id is None:
            raise ValueError("a campaign wake must resume its saved native session")
        if len(self.watcher_ids) != len(set(self.watcher_ids)):
            raise ValueError("a campaign wake cannot repeat watcher ids")
        if self.watcher_ids and self.wake_cause not in {"watcher", "graph_condition"}:
            raise ValueError("only a campaign watcher wake may carry watcher ids")
        return self


def campaign_root_request(
    request: CampaignStartRequest,
    *,
    campaign_id: str,
) -> CampaignRunRequest:
    """Capture a resolved campaign start as its first orchestrator turn."""

    values = request.model_dump(mode="json", exclude={"invocation_ceiling", "starting_instruction"})
    return CampaignRunRequest.model_validate(
        {
            **values,
            "campaign_id": campaign_id,
            "role": "orchestrator",
            "instruction": request.starting_instruction,
        }
    )


CampaignReportRequestFactory = Callable[[CampaignRecord], CampaignRunRequest]


def begin_campaign_wrapup(
    store: AppStore,
    campaign_id: str,
    ending: CampaignEnding,
    *,
    error: str | None = None,
) -> CampaignRecord:
    """Persist the ending fence before waiting for admitted work to settle."""

    return store.begin_campaign_wrapup(campaign_id, ending, error=error)


class CampaignReportCorrectionRequired(ValueError):
    """A report candidate needs a bounded correction in its existing session."""

    def __init__(self, diagnostic: str) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic)


class CampaignReportCorrection(BaseModel):
    """Instruction boundary for one same-session report-only correction."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    operation_id: str
    round: int = Field(ge=1, le=CAMPAIGN_REPORT_MAX_CORRECTION_ROUNDS)
    diagnostic: str = Field(min_length=1, max_length=2_000)
    native_session_id: str = Field(min_length=1)
    stage_host: str | None = None
    stage_root: str = Field(min_length=1)
    reuse_native_session: Literal[True] = True
    repeat_operational_work: Literal[False] = False


def campaign_report_correction(
    store: AppStore,
    operation_id: str,
    *,
    round: int,
    diagnostic: str,
) -> CampaignReportCorrection:
    """Describe a correction without allocating another campaign invocation."""

    task = store.agent_task(operation_id)
    if task is None:
        raise KeyError(operation_id)
    if task.kind != "campaign" or task.campaign_id is None:
        raise ValueError("campaign report correction requires a campaign task")
    request = CampaignRunRequest.model_validate(task.request)
    if request.role != "report":
        raise ValueError("campaign report correction requires the reserved report turn")
    if not task.native_session_id or not task.stage_root:
        raise ValueError("campaign report correction has no saved native session and stage")
    return CampaignReportCorrection(
        campaign_id=task.campaign_id,
        operation_id=operation_id,
        round=round,
        diagnostic=diagnostic,
        native_session_id=task.native_session_id,
        stage_host=task.stage_host,
        stage_root=task.stage_root,
    )


def validate_campaign_report(candidate: str | bytes | None) -> str:
    """Validate report bytes at the same bounded HTML boundary as result views."""

    if candidate is None:
        raise CampaignReportCorrectionRequired(
            "Campaign report is missing. Return a non-empty UTF-8 HTML report."
        )
    data = candidate.encode("utf-8") if isinstance(candidate, str) else candidate
    if len(data) > CHAT_ARTIFACT_MAX_FILE_BYTES:
        raise CampaignReportCorrectionRequired(
            f"Campaign report exceeds the {CHAT_ARTIFACT_MAX_FILE_BYTES}-byte limit."
        )
    try:
        validate_artifact_bytes("campaign-report.html", data)
        text = data.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise CampaignReportCorrectionRequired(f"Campaign report is invalid: {exc}.") from exc
    if not text.strip():
        raise CampaignReportCorrectionRequired(
            "Campaign report is empty. Return a non-empty UTF-8 HTML report."
        )
    return text


def complete_campaign_report(
    store: AppStore,
    *,
    campaign_id: str,
    operation_id: str,
    ending: CampaignEnding,
    candidate: str | bytes | None,
) -> tuple[CampaignRecord, CampaignReportRecord]:
    """Validate and durably capture one immutable report for the current ending."""

    html = validate_campaign_report(candidate)
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    report = CampaignReportRecord(
        report_id=str(uuid.uuid4()),
        campaign_id=campaign_id,
        operation_id=operation_id,
        ending=ending,
        sha256=digest,
        html=html,
        created_at=store.now(),
    )
    return store.finish_campaign_wrapup(report)


class PendingCampaignMail(BaseModel):
    """Unclaimed hearsay-only messages awaiting one atomic wake admission."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    recipient_task_id: str
    messages: list[CampaignMessageRecord]
    graph_authority: Literal["none"] = "none"

    @property
    def message_ids(self) -> list[str]:
        return [message.message_id for message in self.messages]


def pending_campaign_mail(
    store: AppStore,
    *,
    campaign_id: str,
    recipient_task_id: str,
) -> PendingCampaignMail:
    """Read one recipient's undelivered mail without claiming a wake path."""

    recipient = store.agent_task(recipient_task_id)
    if recipient is None:
        raise KeyError(recipient_task_id)
    if recipient.campaign_id != campaign_id:
        raise ValueError("campaign mail recipient is outside the campaign")
    messages = store.pending_campaign_messages(campaign_id, recipient_task_id)
    return PendingCampaignMail(
        campaign_id=campaign_id,
        recipient_task_id=recipient_task_id,
        messages=messages,
    )


class CampaignCommandInvalid(ValueError):
    """A staged command is well-formed but not permitted or applicable."""


class CampaignCommandUnavailable(RuntimeError):
    """A staged command could not reach the authoritative effect boundary."""


class CampaignCommandEffectResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "invalid", "unavailable"] = "ok"
    message: str | None = Field(default=None, max_length=2_000)
    result: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unsuccessful_result_has_a_diagnostic(self) -> CampaignCommandEffectResult:
        if self.status != "ok" and not (self.message or "").strip():
            raise ValueError("an unsuccessful campaign command requires a diagnostic")
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
            raise ValueError("campaign command result must be valid JSON") from exc
        if len(encoded) > AGENT_COMMAND_EVENT_MAX_BYTES:
            raise ValueError("campaign command result exceeds the event ledger limit")
        return self


@dataclass(frozen=True)
class CampaignCommandContext:
    campaign: CampaignRecord
    task: AgentTaskRecord
    request: CampaignRunRequest


CampaignValidateCommand = Callable[
    [CampaignCommandContext, ValidateArguments],
    CampaignCommandEffectResult,
]
CampaignStatusCommand = Callable[
    [CampaignCommandContext, StatusArguments],
    CampaignCommandEffectResult,
]
CampaignSpawnCommand = Callable[
    [CampaignCommandContext, SpawnArguments, str],
    CampaignCommandEffectResult,
]
CampaignWorkerCommand = Callable[
    [CampaignCommandContext, str],
    CampaignCommandEffectResult,
]
CampaignMessageCommand = Callable[
    [CampaignCommandContext, MessageArguments, str],
    CampaignCommandEffectResult,
]
CampaignWatchGraphCommand = Callable[
    [CampaignCommandContext, WatchGraphArguments, str],
    CampaignCommandEffectResult,
]
CampaignFinishCommand = Callable[[CampaignCommandContext], CampaignCommandEffectResult]
CampaignUnknownCommandReconciler = Callable[
    [CampaignCommandContext, CommandRequest, str | None],
    CampaignCommandEffectResult | None,
]
CampaignSeatNodeType = Callable[[str, str], str | None]


@dataclass(frozen=True)
class CampaignCommandEffects:
    """Injected graph/run effects behind the staged transport protocol.

    This seam lets API composition bind existing validator, watcher, and
    BackgroundAgentTasks behavior without making an execution host call RCP over
    HTTP and without adding another wake implementation here.
    """

    validate: CampaignValidateCommand
    status: CampaignStatusCommand
    spawn: CampaignSpawnCommand
    pause: CampaignWorkerCommand
    resume: CampaignWorkerCommand
    stop: CampaignWorkerCommand
    message: CampaignMessageCommand
    watch_graph: CampaignWatchGraphCommand
    finish: CampaignFinishCommand
    seat_node_type: CampaignSeatNodeType
    reconcile_unknown: CampaignUnknownCommandReconciler


class CampaignCommandDispatcher:
    """Audit, deduplicate, reconcile, and dispatch one staged client call."""

    def __init__(self, store: AppStore, effects: CampaignCommandEffects) -> None:
        self.store = store
        self.effects = effects

    def dispatch(self, operation_id: str, request: CommandRequest) -> CommandResponse:
        context = self._context(operation_id)
        if request.mailbox_id == "":  # already schema-validated; keeps the binding explicit here
            raise CampaignCommandInvalid("campaign command mailbox is missing")

        planned_worker_id = (
            _planned_worker_id(context.campaign.campaign_id, request.idempotency_key)
            if isinstance(request, SpawnCommandRequest) and request.idempotency_key is not None
            else None
        )
        planned_message_id = (
            _planned_effect_id(
                context.campaign.campaign_id,
                "message",
                request.idempotency_key,
            )
            if isinstance(request, MessageCommandRequest) and request.idempotency_key is not None
            else None
        )
        planned_watcher_id = (
            _planned_effect_id(
                context.campaign.campaign_id,
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
                context.campaign.campaign_id,
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
                campaign_id=context.campaign.campaign_id,
                verb=request.verb,
                idempotency_key=request.idempotency_key,
                payload=start_payload,
            )
        except ValueError:
            # Another client may have won the campaign-wide key between the
            # read above and this insert. Preserve that invocation as the
            # effect record while giving this client call its own ledger pair.
            raced = (
                self.store.agent_command_by_key(
                    context.campaign.campaign_id,
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
                CampaignCommandEffectResult(
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
        except CampaignCommandInvalid as exc:
            outcome = CampaignCommandEffectResult(status="invalid", message=str(exc))
        except (CampaignCommandUnavailable, OSError) as exc:
            outcome = CampaignCommandEffectResult(status="unavailable", message=str(exc))
        except (KeyError, ValueError) as exc:
            outcome = CampaignCommandEffectResult(status="invalid", message=str(exc))
        return self._finish(invocation.command_id, request.request_id, outcome)

    def _start_retry_attempt(
        self,
        context: CampaignCommandContext,
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
            campaign_id=context.campaign.campaign_id,
            verb=request.verb,
            idempotency_key=None,
            payload=attempt_payload,
        )

    def _dispatch_retry(
        self,
        retry_context: CampaignCommandContext,
        request: CommandRequest,
        prior: AgentCommandInvocationRecord,
        attempt: AgentCommandInvocationRecord,
    ) -> CommandResponse:
        """Resolve one keyed retry from the original durable request intent."""

        try:
            original_context = self._context(prior.operation_id)
            if original_context.campaign.campaign_id != retry_context.campaign.campaign_id:
                raise CampaignCommandUnavailable(
                    "The original command task no longer belongs to this campaign."
                )
        except (CampaignCommandInvalid, CampaignCommandUnavailable, KeyError, ValueError) as exc:
            return self._finish(
                attempt.command_id,
                request.request_id,
                CampaignCommandEffectResult(status="unavailable", message=str(exc)),
            )

        try:
            original_actor = self._canonical_command_actor(original_context)
            retry_actor = self._canonical_command_actor(retry_context)
        except (CampaignCommandInvalid, CampaignCommandUnavailable, KeyError, ValueError) as exc:
            return self._finish(
                attempt.command_id,
                request.request_id,
                CampaignCommandEffectResult(status="unavailable", message=str(exc)),
            )
        if retry_actor != original_actor:
            return self._finish(
                attempt.command_id,
                request.request_id,
                CampaignCommandEffectResult(
                    status="invalid",
                    message=(
                        "An idempotency key may be replayed only by the same canonical "
                        "campaign actor and role."
                    ),
                ),
            )

        try:
            recorded_request = self._recorded_request(request, prior)
        except CampaignCommandInvalid as exc:
            return self._finish(
                attempt.command_id,
                request.request_id,
                CampaignCommandEffectResult(status="invalid", message=str(exc)),
            )
        except (CampaignCommandUnavailable, KeyError, ValueError) as exc:
            outcome = CampaignCommandEffectResult(status="unavailable", message=str(exc))
            if prior.exited_at is None:
                outcome = self._finish_original_unknown(prior.command_id, outcome)
            return self._finish(attempt.command_id, request.request_id, outcome)

        if prior.exited_at is not None:
            try:
                outcome = self._completed_retry_outcome(original_context, recorded_request, prior)
            except (
                CampaignCommandInvalid,
                CampaignCommandUnavailable,
                KeyError,
                ValueError,
            ) as exc:
                outcome = CampaignCommandEffectResult(status="unavailable", message=str(exc))
            return self._finish(attempt.command_id, request.request_id, outcome)

        try:
            reconciled = self._reconcile_unknown(
                original_context,
                recorded_request,
                prior.start_payload,
            )
            if reconciled is None:
                if not isinstance(recorded_request, SpawnCommandRequest):
                    raise CampaignCommandUnavailable(
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
        except CampaignCommandInvalid as exc:
            outcome = CampaignCommandEffectResult(status="invalid", message=str(exc))
        except (CampaignCommandUnavailable, OSError, KeyError, ValueError) as exc:
            outcome = CampaignCommandEffectResult(status="unavailable", message=str(exc))

        outcome = self._finish_original_unknown(prior.command_id, outcome)
        return self._finish(attempt.command_id, request.request_id, outcome)

    @staticmethod
    def _recorded_request(
        request: CommandRequest,
        prior: AgentCommandInvocationRecord,
    ) -> CommandRequest:
        if prior.verb != request.verb:
            raise CampaignCommandInvalid(
                "This idempotency key was already used for another command verb."
            )
        recorded_arguments = prior.start_payload.get("arguments")
        current_arguments = request.arguments.model_dump(mode="json")
        if recorded_arguments != current_arguments:
            raise CampaignCommandInvalid(
                "This idempotency key was already used with different command arguments."
            )
        if not isinstance(recorded_arguments, dict):
            raise CampaignCommandUnavailable(
                "The original command has no valid recorded arguments to reconcile."
            )
        try:
            arguments = type(request.arguments).model_validate(recorded_arguments)
        except ValueError as exc:
            raise CampaignCommandUnavailable(
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
        context: CampaignCommandContext,
        request: CommandRequest,
        prior: AgentCommandInvocationRecord,
    ) -> CampaignCommandEffectResult:
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
            return CampaignCommandEffectResult(
                message="Existing campaign worker returned for this spawn idempotency key.",
                result=_worker_command_result(worker, disposition="existing"),
            )
        if recorded.status == "ok":
            raise CampaignCommandUnavailable(
                "Completed spawn has no durable canonical worker to return."
            )
        return recorded

    def _finish_original_unknown(
        self,
        command_id: str,
        outcome: CampaignCommandEffectResult,
    ) -> CampaignCommandEffectResult:
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

    def _context(self, operation_id: str) -> CampaignCommandContext:
        task = self.store.agent_task(operation_id)
        if task is None:
            raise KeyError(operation_id)
        if task.kind != "campaign" or task.campaign_id is None:
            raise CampaignCommandInvalid("agent command requires a campaign task")
        campaign = self.store.campaign(task.campaign_id)
        if campaign is None:
            raise KeyError(task.campaign_id)
        request = CampaignRunRequest.model_validate(task.request)
        return CampaignCommandContext(campaign=campaign, task=task, request=request)

    def _canonical_command_actor(
        self,
        context: CampaignCommandContext,
    ) -> tuple[str, str]:
        binding = self.store.campaign_actor_binding(context.task.operation_id)
        role = self.store.campaign_invocation_role(context.task.operation_id)
        if (
            role is None
            or role != binding.role
            or binding.campaign_id != context.campaign.campaign_id
        ):
            raise CampaignCommandUnavailable(
                "Campaign command task has no coherent canonical actor role."
            )
        return binding.actor_operation_id, role

    def _reconcile_unknown(
        self,
        context: CampaignCommandContext,
        request: CommandRequest,
        start_payload: dict[str, object],
    ) -> CampaignCommandEffectResult | None:
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
            return CampaignCommandEffectResult(
                message="Existing campaign worker returned after interrupted spawn.",
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
        context: CampaignCommandContext,
        request: SpawnCommandRequest,
        start_payload: dict[str, object],
    ) -> str:
        planned_worker_id = start_payload.get("planned_worker_id")
        expected_worker_id = _planned_worker_id(
            context.campaign.campaign_id,
            request.idempotency_key,
        )
        if planned_worker_id != expected_worker_id:
            raise CampaignCommandUnavailable(
                "Interrupted spawn has no valid deterministic worker id to reconcile."
            )
        return expected_worker_id

    @staticmethod
    def _recorded_planned_effect_id(
        context: CampaignCommandContext,
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
            context.campaign.campaign_id,
            verb,
            request.idempotency_key,
        )
        if planned_effect_id != expected_effect_id:
            raise CampaignCommandUnavailable(
                f"Interrupted {verb} has no valid deterministic effect id to reconcile."
            )
        return expected_effect_id

    def _execute(
        self,
        context: CampaignCommandContext,
        request: CommandRequest,
        *,
        planned_worker_id: str | None,
        planned_message_id: str | None,
        planned_watcher_id: str | None,
    ) -> CampaignCommandEffectResult:
        retrospective_worker_reply = request.verb == "message" and context.request.role == "worker"
        if request.verb in MUTATING_COMMAND_VERBS and not retrospective_worker_reply:
            campaign = self.store.campaign(context.campaign.campaign_id)
            if campaign is None:
                raise CampaignCommandUnavailable("The campaign is no longer available.")
            if (
                campaign.status != "running"
                or campaign.ending is not None
                or campaign.stop_requested_at is not None
            ):
                raise CampaignCommandUnavailable(
                    "The campaign is no longer accepting mutating commands."
                )
        if request.verb == "validate":
            return self.effects.validate(context, request.arguments)
        if request.verb == "status":
            return self.effects.status(context, request.arguments)
        if request.verb == "message":
            assert planned_message_id is not None
            recipient_task_id = request.arguments.recipient_task_id
            if context.request.role == "worker":
                if recipient_task_id not in {None, context.campaign.root_operation_id}:
                    raise CampaignCommandInvalid(
                        "A campaign worker may reply only to its orchestrator."
                    )
                return self.effects.message(context, request.arguments, planned_message_id)
            if context.request.role == "orchestrator":
                if recipient_task_id is None:
                    raise CampaignCommandInvalid(
                        "The campaign orchestrator must name the worker it is messaging."
                    )
                worker = self._require_worker(context, recipient_task_id)
                binding = self.store.campaign_actor_binding(worker.operation_id)
                if recipient_task_id != binding.actor_operation_id:
                    raise CampaignCommandInvalid(
                        "The campaign orchestrator must address a worker by its stable worker id."
                    )
                return self.effects.message(context, request.arguments, planned_message_id)
            raise CampaignCommandInvalid("A campaign report turn cannot send messages.")
        if context.request.role != "orchestrator":
            raise CampaignCommandInvalid(
                "Only the campaign orchestrator may issue mutating staged commands."
            )
        if request.verb == "spawn":
            assert planned_worker_id is not None
            node_type = self.effects.seat_node_type(
                context.task.project_id,
                request.arguments.seat_node_id,
            )
            if node_type is None or node_type.casefold() not in {"experiment", "blocker"}:
                raise CampaignCommandInvalid(
                    "Campaign workers may be seated only on Experiments and Blockers."
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
        raise AssertionError(f"unhandled campaign command verb: {request.verb}")

    def _require_worker(
        self,
        context: CampaignCommandContext,
        operation_id: str,
    ) -> AgentTaskRecord:
        worker = self.store.agent_task(operation_id)
        if worker is None or worker.campaign_id != context.campaign.campaign_id:
            raise CampaignCommandInvalid("Worker control target is outside this campaign.")
        worker_request = CampaignRunRequest.model_validate(worker.request)
        if worker_request.role != "worker":
            raise CampaignCommandInvalid("Worker control target is not a campaign worker.")
        return worker

    def _verify_spawn_worker(
        self,
        context: CampaignCommandContext,
        arguments: SpawnArguments,
        planned_worker_id: str,
    ) -> AgentTaskRecord:
        """Mechanically prove the canonical worker row matches the spawn intent."""

        worker = self.store.agent_task(planned_worker_id)
        if worker is None:
            raise CampaignCommandUnavailable(
                "Campaign spawn returned without durably creating its planned worker."
            )
        if (
            worker.kind != "campaign"
            or worker.project_id != context.campaign.project_id
            or worker.campaign_id != context.campaign.campaign_id
            or worker.parent_operation_id != context.task.operation_id
        ):
            raise CampaignCommandUnavailable(
                "Campaign spawn created a worker with incorrect campaign parent lineage."
            )
        if self.store.campaign_invocation_role(worker.operation_id) != "worker":
            raise CampaignCommandUnavailable(
                "Campaign spawn did not record the canonical worker invocation role."
            )
        try:
            worker_request = CampaignRunRequest.model_validate(worker.request)
        except ValueError as exc:
            raise CampaignCommandUnavailable(
                "Campaign spawn created a worker with an invalid run request."
            ) from exc
        if (
            worker_request.campaign_id != context.campaign.campaign_id
            or worker_request.role != "worker"
            or worker_request.control_node_id != arguments.seat_node_id
            or worker_request.instruction != arguments.instruction
        ):
            raise CampaignCommandUnavailable(
                "Campaign spawn created a worker that does not match its recorded seat or "
                "instruction."
            )
        return worker

    def _finish(
        self,
        command_id: str,
        response_request_id: str,
        outcome: CampaignCommandEffectResult,
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
        outcome: CampaignCommandEffectResult,
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


def _planned_worker_id(campaign_id: str, idempotency_key: str | None) -> str:
    if idempotency_key is None:
        raise ValueError("a spawn command requires an idempotency key")
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign_id}:spawn:{idempotency_key}",
        )
    )


def _planned_effect_id(
    campaign_id: str,
    verb: Literal["message", "watch_graph"],
    idempotency_key: str | None,
) -> str:
    if idempotency_key is None:
        raise ValueError(f"a {verb} command requires an idempotency key")
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign_id}:{verb}:{idempotency_key}",
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
) -> CampaignCommandEffectResult:
    status = invocation.status
    payload = invocation.exit_payload
    if status not in {"ok", "invalid", "unavailable"} or not isinstance(payload, dict):
        raise CampaignCommandUnavailable("Recorded campaign command exit is incomplete.")
    recorded_result = payload.get("result")
    result = (
        dict(recorded_result)
        if isinstance(recorded_result, dict)
        else {key: value for key, value in payload.items() if key not in {"status", "diagnostic"}}
    )
    diagnostic = payload.get("diagnostic")
    message = diagnostic if isinstance(diagnostic, str) else None
    if status != "ok" and message is None:
        message = "Recorded campaign command did not complete successfully."
    return CampaignCommandEffectResult(
        status=status,
        message=message,
        result=result,
    )
