from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.artifacts import AgentArtifactDescriptor
from rcp.core.authority import AgentDispatchAuthority, require_dispatch
from rcp.core.models import AuthorizedHuman, GraphState
from rcp.limits import CHAT_ARTIFACT_MAX_COUNT
from rcp.providers import classify_terminal_error
from rcp.runs.campaign import (
    CampaignReportCorrection,
    CampaignReportRequestFactory,
    CampaignRunRequest,
    CampaignStartRequest,
    CampaignWakeAdmission,
    PendingCampaignMail,
    begin_campaign_wrapup,
    campaign_non_report_turns_settled,
    campaign_report_correction,
    campaign_root_request,
    complete_campaign_report,
    pending_campaign_mail,
)
from rcp.runs.campaign_mail import campaign_mail_claim_prefix
from rcp.runs.campaign_recovery import (
    CampaignOrchestratorTerminalFailure,
    record_structural_failure,
)
from rcp.service import (
    CoachRequest,
    GraphUpdateResult,
    RunRequest,
    resolve_dispatch_authority,
)
from rcp.skill_registry import SkillSelection, official_registry
from rcp.storage import (
    AgentTaskKind,
    AgentTaskRecord,
    AppStore,
    CampaignBudgetExhausted,
    CampaignEnding,
    CampaignNotRunning,
    CampaignRecord,
    CampaignReportRecord,
)
from rcp.transport import RemoteRunStage

AgentTaskRequest = RunRequest | CoachRequest | CampaignRunRequest
DispatchAuthorityResolver = Callable[
    [AgentTaskKind, AgentTaskRequest],
    AgentDispatchAuthority | None,
]
AgentTaskContinuation = Literal[
    "fresh",
    "resume",
    "retry",
    "handoff",
    "graph_repair",
    "watcher_wake",
    "graph_condition_wake",
    "message_wake",
    "campaign_continuation",
]

# A watcher wake reuses a native session without being task Resume: it is a new
# task at the next invocation, so it must never inherit Resume's same-invocation
# parent/child recovery semantics.
_NATIVE_CHECKPOINT_CONTINUATIONS = frozenset(
    {
        "resume",
        "retry",
        "graph_repair",
        "watcher_wake",
        "graph_condition_wake",
        "message_wake",
        "campaign_continuation",
    }
)
_EXPERIMENT_SESSION_LIMIT_DIAGNOSTIC = (
    "The provider session reached its limit. Retry the same provider to recheck the limit and "
    "resume this episode, or switch provider to continue this same episode and invocation."
)


def _task_is_patch_capable(kind: AgentTaskKind, request: AgentTaskRequest) -> bool:
    if kind in {"seed", "refresh"}:
        return True
    if kind == "campaign" and isinstance(request, CampaignRunRequest):
        return request.role != "report"
    return (
        kind in {"node_chat", "project_chat"}
        and isinstance(request, RunRequest)
        and (request.mode == "work")
    )


def _skill_update(
    skills: SkillSelection | None,
    *,
    mode: Literal["python", "json"] = "python",
) -> dict[str, object]:
    """Refresh a continued attempt's recorded packages with what it will stage.

    Every launch re-resolves the selected ids, so a record that kept the earlier
    attempt's versions would misreport an upgraded package.
    """

    if skills is None:
        return {}
    if mode == "json":
        return skills.model_dump(mode="json")
    return {
        "workflow_ids": list(skills.workflow_ids),
        "skill_ids": list(skills.skill_ids),
        "resolved_skill_packages": list(skills.resolved_skill_packages),
    }


@dataclass
class AgentTaskExecution:
    operation_id: str
    store: AppStore
    control: AgentProcessControl
    stage_host: str | None = None
    stage_root: str | None = None
    continuation: AgentTaskContinuation = "fresh"
    retry_feedback: tuple[str, ...] = ()
    applied_revision: int | None = None
    applied_graph_state: GraphState | None = None
    armed_graph_watchers: bool = False

    @property
    def reuses_native_checkpoint(self) -> bool:
        return self.continuation in _NATIVE_CHECKPOINT_CONTINUATIONS

    def checkpoint_stage(self, host: str, root: str) -> None:
        self.stage_host = host or None
        self.stage_root = root
        self.store.checkpoint_agent_task(
            self.operation_id,
            stage_host=host or None,
            stage_root=root,
        )
        self.store.record_agent_task_receipt(
            self.operation_id,
            "stage_checkpoint",
            {"remote": bool(host), "stage_available": bool(root)},
            tier="diagnostic",
        )


AgentTaskStream = Callable[
    [str, AgentTaskKind, AgentTaskRequest, AgentTaskExecution], AsyncIterator[str]
]
AgentTaskStreamClosedHook = Callable[
    [str, AgentTaskKind, AgentTaskRequest, AgentTaskExecution], None
]
AgentTaskSettledHook = Callable[[str, AgentTaskKind, AgentTaskRequest, AgentTaskExecution], None]
CampaignTaskSettledHook = Callable[
    [CampaignRecord, CampaignRunRequest, AgentTaskExecution],
    None,
]
CampaignAdmissionExhaustedHook = Callable[[CampaignRecord], None]
CampaignReauthorizationPreflight = Callable[[CampaignRunRequest], CampaignRunRequest]


@dataclass(frozen=True)
class AgentTaskOutcome:
    applied_revision: int | None
    messages: list[str]
    artifacts: list[AgentArtifactDescriptor]
    graph_update: GraphUpdateResult | None = None


class TaskPaused(RuntimeError):
    def __init__(
        self,
        message: str,
        messages: list[str] | None = None,
        artifacts: list[AgentArtifactDescriptor] | None = None,
    ) -> None:
        self.messages = list(messages or [])
        self.artifacts = list(artifacts or [])
        super().__init__(message)


class TaskFailed(RuntimeError):
    """A task that failed after producing output worth keeping.

    A chat turn can answer the human and only then have its graph change rejected.
    The answer is already written and already useful, so it travels with the
    failure instead of being dropped with the stream.
    """

    def __init__(
        self,
        message: str,
        messages: list[str],
        artifacts: list[AgentArtifactDescriptor],
    ) -> None:
        super().__init__(message)
        self.messages = messages
        self.artifacts = artifacts


class BackgroundAgentTasks:
    def __init__(
        self,
        store: AppStore,
        stream: AgentTaskStream,
        on_stream_closed: AgentTaskStreamClosedHook | None = None,
        on_task_settled: AgentTaskSettledHook | None = None,
        on_campaign_task_settled: CampaignTaskSettledHook | None = None,
        on_campaign_admission_exhausted: CampaignAdmissionExhaustedHook | None = None,
        dispatch_authority_resolver: DispatchAuthorityResolver | None = None,
    ) -> None:
        self.store = store
        self.stream = stream
        self.on_stream_closed = on_stream_closed
        self.on_task_settled = on_task_settled
        self.on_campaign_task_settled = on_campaign_task_settled
        self.on_campaign_admission_exhausted = on_campaign_admission_exhausted
        self.dispatch_authority_resolver = dispatch_authority_resolver or resolve_dispatch_authority
        self._controls: dict[str, AgentProcessControl] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._controls_lock = threading.Lock()
        self._watcher_delivery_lock = threading.Lock()
        self._accepting_watcher_deliveries = True
        self.store.interrupt_active_agent_tasks()
        self.store.settle_ready_experiment_loop_stops()
        self.store.settle_ready_campaign_stops()

    def start(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        *,
        operation_id: str | None = None,
        authorized_by: AuthorizedHuman | None = None,
        stage_host: str | None = None,
        stage_root: str | None = None,
    ) -> AgentTaskRecord:
        if kind == "campaign":
            raise ValueError(
                "Use start_campaign so the shared budget and root are created atomically."
            )
        if kind in {"seed", "refresh"} and request.session_id:
            raise ValueError(
                "Seed and refresh sessions can only be resumed from an RCP background "
                "task checkpoint."
            )
        result_view_revision = (
            isinstance(request, RunRequest)
            and request.result_view is not None
            and request.result_view.action == "revise"
        )
        if result_view_revision and (not request.session_id or not stage_root):
            raise ValueError(
                "A result-view revision requires its saved native session and exact stage."
            )
        if not result_view_revision and (stage_host is not None or stage_root is not None):
            raise ValueError("Only a result-view revision may inherit a saved stage on start.")
        self._validate_request_type(kind, request)
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(project_id, kind, request_data)
        return self._create_and_spawn(
            project_id,
            kind,
            request,
            estimate_seconds=estimate,
            estimate_samples=samples,
            operation_id=operation_id,
            authorized_by=authorized_by,
            stage_host=stage_host,
            stage_root=stage_root,
        )

    def start_campaign(
        self,
        project_id: str,
        request: CampaignStartRequest,
        *,
        authorized_by: AuthorizedHuman,
        campaign_id: str | None = None,
        operation_id: str | None = None,
    ) -> tuple[CampaignRecord, AgentTaskRecord]:
        """Create the sole live project campaign and spend its root turn atomically."""

        if not authorized_by.display_name.strip():
            raise ValueError("A campaign requires a named human authorizer snapshot.")
        campaign_id = campaign_id or str(uuid.uuid4())
        operation_id = operation_id or str(uuid.uuid4())
        run_request = campaign_root_request(request, campaign_id=campaign_id).model_copy(
            update={"actor_operation_id": operation_id}
        )
        dispatch_authority = self._resolved_dispatch_authority(
            "campaign",
            run_request,
            project_id=project_id,
            operation_id=operation_id,
        )
        assert dispatch_authority is not None
        request_data = run_request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            project_id,
            "campaign",
            request_data,
        )
        now = self.store.now()
        campaign = CampaignRecord(
            campaign_id=campaign_id,
            project_id=project_id,
            root_operation_id=operation_id,
            status="queued",
            starting_instruction=request.starting_instruction,
            invocation_ceiling=request.invocation_ceiling,
            invocations_used=0,
            authorized_by=authorized_by,
            created_at=now,
            updated_at=now,
        )
        task = AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            campaign_id=campaign_id,
            kind="campaign",
            status="queued",
            request=request_data,
            created_at=now,
            updated_at=now,
            status_message="Waiting for the campaign orchestrator to start.",
            estimate_seconds=estimate,
            estimate_samples=samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=authorized_by,
            dispatch_authority=dispatch_authority,
        )
        stored_campaign, stored_task = self.store.create_campaign_with_root_task(campaign, task)
        return stored_campaign, self._spawn_record(
            stored_task,
            run_request,
            continuation="fresh",
        )

    def start_campaign_turn(
        self,
        campaign_id: str,
        request: CampaignRunRequest,
        *,
        parent_operation_id: str | None = None,
        operation_id: str | None = None,
        mail_delivery: PendingCampaignMail | None = None,
        wake_admission: CampaignWakeAdmission | None = None,
    ) -> AgentTaskRecord | None:
        """Admit one orchestrator/worker turn or wake from the campaign's single pot."""

        campaign = self._campaign_for_request(campaign_id, request)
        if request.role == "report":
            raise ValueError("Use start_campaign_report for the reserved report turn.")
        operation_id = operation_id or str(uuid.uuid4())
        parent = self._campaign_parent(campaign, parent_operation_id)
        parent_role = self.store.campaign_invocation_role(parent.operation_id)
        if parent_role not in {"orchestrator", "worker"}:
            raise ValueError("Campaign continuation parent has no canonical actor role.")
        parent_request = CampaignRunRequest.model_validate(parent.request)
        if parent_request.role != parent_role:
            raise ValueError("Campaign continuation parent role disagrees with its durable actor.")
        parent_actor_id = parent_request.actor_operation_id or parent.operation_id
        requested_actor_id = request.actor_operation_id
        if request.wake_cause is not None:
            if request.role != parent_role:
                raise ValueError("A campaign wake cannot change its canonical actor role.")
            if requested_actor_id is not None and requested_actor_id != parent_actor_id:
                raise ValueError("A campaign wake cannot change its canonical actor identity.")
            if (
                parent_role == "worker"
                and request.control_node_id != parent_request.control_node_id
            ):
                raise ValueError("A campaign worker wake cannot change its canonical seat.")
            request = request.model_copy(update={"actor_operation_id": parent_actor_id})
        elif request.role == "worker":
            if parent_role != "orchestrator":
                raise ValueError("Only the campaign orchestrator may seat a worker.")
            if requested_actor_id is not None and requested_actor_id != operation_id:
                raise ValueError("A new campaign worker must use its own canonical actor identity.")
            if request.session_id is not None:
                raise ValueError("A new campaign worker must start a fresh native session.")
            request = request.model_copy(update={"actor_operation_id": operation_id})
        else:
            if parent_role != "orchestrator" or parent_actor_id != campaign.root_operation_id:
                raise ValueError("Only the root campaign actor may continue as orchestrator.")
            if requested_actor_id is not None and requested_actor_id != parent_actor_id:
                raise ValueError(
                    "A campaign orchestrator turn cannot change its canonical actor identity."
                )
            request = request.model_copy(update={"actor_operation_id": parent_actor_id})
        authority_origin = self.store.agent_task(parent_actor_id)
        if authority_origin is None or authority_origin.dispatch_authority is None:
            raise ValueError(
                "Authority refused action 'dispatch': the canonical campaign actor has no "
                "durable authority binding."
            )
        canonical_scope = authority_origin.dispatch_authority.scope.run_truth_scope
        if request.run_truth_scope is not None and sorted(set(request.run_truth_scope)) != (
            canonical_scope
        ):
            raise ValueError(
                "Authority refused action 'dispatch': a campaign actor cannot change its "
                "project-wide run truth scope."
            )
        request = request.model_copy(update={"run_truth_scope": list(canonical_scope)})
        existing_actor = request.actor_operation_id != operation_id
        stage_host: str | None = None
        stage_root: str | None = None
        research_admission_possible = (
            campaign.status == "running"
            and campaign.stop_requested_at is None
            and campaign.invocations_used < campaign.invocation_ceiling - 1
        )
        if existing_actor and not research_admission_possible:
            if campaign.status != "running" or campaign.stop_requested_at is not None:
                raise CampaignNotRunning("the campaign is not admitting new work")
            self._campaign_admission_exhausted(campaign)
            raise CampaignBudgetExhausted(
                "the campaign research budget is exhausted; the report invocation is reserved"
            )
        if existing_actor:
            binding = self.store.campaign_actor_binding(parent.operation_id)
            if (
                binding.actor_operation_id != request.actor_operation_id
                or binding.role != request.role
                or binding.control_node_id != request.control_node_id
            ):
                raise ValueError(
                    "A campaign continuation must preserve its canonical actor role and seat."
                )
            if not binding.native_session_id or not binding.stage_root:
                raise ValueError(
                    "A campaign continuation requires the actor's exact saved session and stage."
                )
            if request.session_id not in {None, binding.native_session_id}:
                raise ValueError("A campaign continuation cannot change its saved native session.")
            request = request.model_copy(update={"session_id": binding.native_session_id})
            stage_host = binding.stage_host
            stage_root = binding.stage_root
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            campaign.project_id,
            "campaign",
            request_data,
        )
        continuation: AgentTaskContinuation = {
            None: "fresh",
            "watcher": "watcher_wake",
            "graph_condition": "graph_condition_wake",
            "message": "message_wake",
        }[request.wake_cause]
        if request.wake_cause is None and existing_actor:
            continuation = "campaign_continuation"
        if request.wake_cause is not None:
            assert stage_root is not None
        if request.wake_cause == "message":
            if (
                mail_delivery is None
                or not mail_delivery.messages
                or mail_delivery.campaign_id != campaign_id
                or mail_delivery.recipient_task_id != parent.operation_id
            ):
                raise ValueError(
                    "A campaign message wake requires its exact non-empty coalesced mail batch."
                )
            if wake_admission is not None:
                raise ValueError(
                    "Campaign message wake admission is owned by the mail transaction."
                )
        elif request.wake_cause in {"watcher", "graph_condition"}:
            if wake_admission is None:
                raise ValueError(
                    "Campaign watcher wakes require the existing atomic wake-admission hook."
                )
        elif mail_delivery is not None:
            raise ValueError("Only a campaign message wake may claim a mail batch.")
        elif wake_admission is not None:
            raise ValueError("Only a campaign watcher wake may use wake admission.")
        return self._create_and_spawn(
            campaign.project_id,
            "campaign",
            request,
            parent=parent,
            continuation=continuation,
            stage_host=stage_host,
            stage_root=stage_root,
            estimate_seconds=estimate,
            estimate_samples=samples,
            operation_id=operation_id,
            authorized_by=campaign.authorized_by,
            campaign_mail_delivery=mail_delivery,
            campaign_wake_admission=wake_admission,
        )

    def start_campaign_report(
        self,
        campaign_id: str,
        ending: CampaignEnding,
        *,
        request_factory: CampaignReportRequestFactory,
        error: str | None = None,
        operation_id: str | None = None,
    ) -> AgentTaskRecord:
        """Spend the reserved unit in the sole orchestrator's exact saved session."""

        current = self.store.campaign(campaign_id)
        if current is None:
            raise KeyError(campaign_id)
        campaign = begin_campaign_wrapup(
            self.store,
            campaign_id,
            ending,
            error=error,
        )
        if campaign.root_operation_id is None:
            raise ValueError("A campaign report requires its sole orchestrator actor.")
        binding = self.store.campaign_actor_binding(campaign.root_operation_id)
        if (
            binding.actor_operation_id != campaign.root_operation_id
            or binding.role != "orchestrator"
            or not binding.native_session_id
            or not binding.stage_root
        ):
            raise ValueError(
                "A campaign report requires the orchestrator's exact saved session and stage."
            )
        parent = self._require_operation(binding.current_operation_id)
        parent_request = CampaignRunRequest.model_validate(parent.request)
        operation_id = operation_id or str(uuid.uuid4())
        request = request_factory(campaign)
        self._campaign_for_request(campaign_id, request)
        if request.role != "report" or request.ending != ending:
            raise ValueError("The report request factory returned another campaign role or ending.")
        pinned = {
            "provider": parent_request.provider,
            "run_truth_scope": parent_request.run_truth_scope,
            "model": parent_request.model,
            "reasoning": parent_request.reasoning,
            "run_on": parent_request.run_on,
            "session_id": binding.native_session_id,
            "actor_operation_id": campaign.root_operation_id,
        }
        for field, value in pinned.items():
            supplied = getattr(request, field)
            if supplied is not None and supplied != value:
                raise ValueError(
                    f"A campaign report cannot change the orchestrator's saved {field}."
                )
        report_skills = official_registry().resolve(
            workflow_ids=[],
            skill_ids=["campaign-report"],
        )
        request = request.model_copy(
            update={
                **pinned,
                "workflow_ids": [],
                "skill_ids": ["campaign-report"],
                "invoked_workflow_ids": [],
                "invoked_skill_ids": ["campaign-report"],
                "invoked_provider_skill_names": [],
                "resolved_provider_skills": [],
                "resolved_skill_packages": report_skills.resolved_skill_packages,
            }
        )
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            campaign.project_id,
            "campaign",
            request_data,
        )
        return self._create_and_spawn(
            campaign.project_id,
            "campaign",
            request,
            parent=parent,
            continuation="campaign_continuation",
            stage_host=binding.stage_host,
            stage_root=binding.stage_root,
            estimate_seconds=estimate,
            estimate_samples=samples,
            operation_id=operation_id,
            authorized_by=campaign.authorized_by,
            campaign_report_ending=ending,
            campaign_report_error=error,
        )

    def reconcile_campaign_report(
        self,
        campaign_id: str,
        *,
        request_factory: CampaignReportRequestFactory,
        error: str | None = None,
        operation_id: str | None = None,
    ) -> AgentTaskRecord | None:
        """Idempotently admit a fenced ending's report once all prior turns settle."""

        campaign = self.store.campaign(campaign_id)
        if campaign is None or campaign.status != "wrapping_up" or campaign.ending is None:
            return None
        if not campaign_non_report_turns_settled(self.store, campaign_id):
            return None
        try:
            return self.start_campaign_report(
                campaign_id,
                campaign.ending,
                request_factory=request_factory,
                error=error if error is not None else campaign.error,
                operation_id=operation_id,
            )
        except CampaignNotRunning:
            # A same-allocation recovery may have raced the settlement read.
            return None

    def stop_campaign(self, campaign_id: str) -> CampaignRecord:
        """Persist Stop intent without cancelling an already-authorized turn."""

        before = self.store.campaign(campaign_id)
        if before is None:
            raise KeyError(campaign_id)
        stopped = self.store.request_campaign_stop(campaign_id)
        if (
            before.stop_requested_at is None
            and stopped.stop_requested_at is not None
            and stopped.root_operation_id is not None
        ):
            self.store.record_agent_task_event(
                stopped.root_operation_id,
                "Campaign Stop requested; current turns will finish and no new work will start.",
            )
        return stopped

    def reauthorize_campaign(
        self,
        campaign_id: str,
        additional_invocations: int,
        *,
        request_preflight: CampaignReauthorizationPreflight,
        operation_id: str | None = None,
    ) -> tuple[CampaignRecord, AgentTaskRecord]:
        """Extend an exhausted campaign and resume its sole orchestrator atomically."""

        campaign = self.store.campaign(campaign_id)
        if campaign is None or campaign.root_operation_id is None:
            raise KeyError(campaign_id)
        binding = self.store.campaign_actor_binding(campaign.root_operation_id)
        if (
            binding.role != "orchestrator"
            or binding.actor_operation_id != campaign.root_operation_id
        ):
            raise ValueError("campaign reauthorization cannot replace its orchestrator actor")
        if not binding.native_session_id or not binding.stage_root:
            raise ValueError(
                "campaign reauthorization requires the orchestrator's exact saved session and stage"
            )
        parent = self._require_operation(binding.current_operation_id)
        request = CampaignRunRequest.model_validate(parent.request).model_copy(
            update={
                "campaign_id": campaign_id,
                "role": "orchestrator",
                "actor_operation_id": campaign.root_operation_id,
                "control_node_id": None,
                "session_id": binding.native_session_id,
                "instruction": None,
                "wake_cause": None,
                "watcher_ids": [],
                "ending": None,
            }
        )
        resolved_request = request_preflight(request)
        if not isinstance(resolved_request, CampaignRunRequest):
            raise TypeError("Campaign reauthorization preflight changed the request type.")
        resolution_receipt_fields = {
            "workflow_ids",
            "skill_ids",
            "resolved_provider_skills",
            "resolved_skill_packages",
        }
        if resolved_request.model_dump(exclude=resolution_receipt_fields) != request.model_dump(
            exclude=resolution_receipt_fields
        ):
            raise ValueError(
                "Campaign reauthorization preflight cannot change the saved actor, profile, "
                "scope, session, requested skills, or continuation lineage."
            )
        request = resolved_request
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            campaign.project_id,
            "campaign",
            request_data,
        )
        operation_id = operation_id or str(uuid.uuid4())
        now = self.store.now()
        dispatch_authority = self._resolved_dispatch_authority(
            "campaign",
            request,
            project_id=campaign.project_id,
            parent=parent,
            operation_id=operation_id,
            continuation="campaign_continuation",
        )
        assert dispatch_authority is not None
        task = AgentTaskRecord(
            operation_id=operation_id,
            project_id=campaign.project_id,
            campaign_id=campaign_id,
            kind="campaign",
            status="queued",
            request=request_data,
            created_at=now,
            updated_at=now,
            status_message="Waiting for the reauthorized campaign orchestrator to continue.",
            parent_operation_id=parent.operation_id,
            native_session_id=binding.native_session_id,
            stage_host=binding.stage_host,
            stage_root=binding.stage_root,
            estimate_seconds=estimate,
            estimate_samples=samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=campaign.authorized_by,
            dispatch_authority=dispatch_authority,
        )
        reauthorized, stored = self.store.reauthorize_campaign_with_task(
            campaign_id,
            additional_invocations,
            task,
        )
        return reauthorized, self._spawn_record(
            stored,
            request,
            continuation="campaign_continuation",
            parent=parent,
        )

    def complete_campaign_report(
        self,
        *,
        campaign_id: str,
        operation_id: str,
        ending: CampaignEnding,
        candidate: str | bytes | None,
    ) -> tuple[CampaignRecord, CampaignReportRecord]:
        return complete_campaign_report(
            self.store,
            campaign_id=campaign_id,
            operation_id=operation_id,
            ending=ending,
            candidate=candidate,
        )

    def campaign_report_correction(
        self,
        operation_id: str,
        *,
        round: int,
        diagnostic: str,
    ) -> CampaignReportCorrection:
        return campaign_report_correction(
            self.store,
            operation_id,
            round=round,
            diagnostic=diagnostic,
        )

    def pending_campaign_mail(
        self,
        *,
        campaign_id: str,
        recipient_task_id: str,
    ) -> PendingCampaignMail:
        return pending_campaign_mail(
            self.store,
            campaign_id=campaign_id,
            recipient_task_id=recipient_task_id,
        )

    def start_watcher_notification(
        self,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        request: RunRequest,
        watcher_ids: list[str],
        *,
        authorized_by: AuthorizedHuman,
        episode_stage_host: str | None = None,
        episode_stage_root: str | None = None,
        admission_fence: Callable[[Callable[[], None]], bool] | None = None,
    ) -> AgentTaskRecord | None:
        """Atomically consume completed watchers and start their attributed Work turn.

        An Experiment-loop wake carries the episode's native session so the turn
        continues that bounded session. It is still a new task at the next
        invocation, so it uses the `watcher_wake` cause rather than Resume.
        """

        if not authorized_by.display_name.strip():
            raise ValueError("A watcher notification requires a named human authorizer snapshot.")

        experiment_reauthorization = (
            request.trigger == "experiment_run"
            and request.patch_kind == "experiment_loop"
            and request.control_invocation == 1
            and bool(request.watcher_ids)
        )
        experiment_wake = request.trigger == "watcher" and request.patch_kind == "experiment_loop"
        if (
            (request.trigger != "watcher" and not experiment_reauthorization)
            or request.mode != "work"
            or (request.session_id and not experiment_wake)
        ):
            raise ValueError("A watcher notification must be a fresh watcher-attributed Work turn.")
        if experiment_wake and (not request.session_id or not episode_stage_root):
            raise ValueError(
                "An Experiment watcher wake requires its episode's session and exact stage."
            )
        if request.watcher_ids != watcher_ids:
            raise ValueError("The watcher notification request must name its watcher records.")
        self._validate_request_type(kind, request)
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(project_id, kind, request_data)
        dispatch_authority = self._resolved_dispatch_authority(
            kind,
            request,
            project_id=project_id,
        )
        operation_id = str(uuid.uuid4())
        now = self.store.now()
        record = AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind=kind,
            status="queued",
            request=request_data,
            created_at=now,
            updated_at=now,
            status_message="Waiting to deliver a watcher update.",
            native_session_id=request.session_id if experiment_wake else None,
            stage_host=episode_stage_host if experiment_wake else None,
            stage_root=episode_stage_root if experiment_wake else None,
            estimate_seconds=estimate,
            estimate_samples=samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=authorized_by,
            dispatch_authority=dispatch_authority,
        )
        started: AgentTaskRecord | None = None

        def claim_and_spawn() -> None:
            nonlocal started
            with self._watcher_delivery_lock:
                if not self._accepting_watcher_deliveries:
                    return
                stored = self.store.create_watcher_notification_task(record, watcher_ids)
                if stored is None:
                    return
                started = self._spawn_record(
                    stored,
                    request,
                    continuation="watcher_wake" if experiment_wake else "fresh",
                )

        if admission_fence is not None:
            if not admission_fence(claim_and_spawn):
                return None
        else:
            claim_and_spawn()
        return started

    def resume(
        self,
        operation_id: str,
        *,
        skills: SkillSelection | None = None,
        authorized_by: AuthorizedHuman | None = None,
    ) -> AgentTaskRecord:
        previous = self._require_operation(operation_id)
        if not previous.can_resume or not previous.native_session_id:
            raise ValueError(
                "This task has no resumable native agent checkpoint. Retry it instead."
            )
        if not self._session_is_rcp_owned(previous):
            raise ValueError(
                "This task's native session was not checkpointed or validated by RCP. "
                "Retry it instead."
            )
        self._preflight_experiment_episode_recovery(previous)
        request = self._request_from_record(previous).model_copy(
            update={"session_id": previous.native_session_id, **_skill_update(skills)}
        )
        continuation: AgentTaskContinuation = (
            "graph_repair"
            if self.store.agent_task_continuation_cause(previous.operation_id) == "graph_repair"
            else "resume"
        )
        return self._create_and_spawn(
            previous.project_id,
            previous.kind,
            request,
            parent=previous,
            continuation=continuation,
            estimate_seconds=previous.estimate_seconds,
            estimate_samples=previous.estimate_samples,
            stage_host=previous.stage_host,
            stage_root=previous.stage_root,
            authorized_by=authorized_by,
        )

    def retry(
        self,
        operation_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        run_on: str | None = None,
        skills: SkillSelection | None = None,
        authorized_by: AuthorizedHuman | None = None,
    ) -> AgentTaskRecord:
        previous = self._require_operation(operation_id)
        if not previous.can_retry:
            raise ValueError("Only a paused, interrupted, or failed task can be retried.")
        original = self._request_from_record(previous)
        if isinstance(original, CampaignRunRequest):
            return self._retry_campaign_task(
                previous,
                original,
                provider=provider,
                model=model,
                reasoning=reasoning,
                run_on=run_on,
                skills=skills,
            )
        self._preflight_experiment_episode_recovery(previous, request=original)
        graph_repair = (
            self.store.agent_task_continuation_cause(previous.operation_id) == "graph_repair"
        )
        if isinstance(original, RunRequest) and original.patch_kind == "experiment_loop":
            if run_on is not None:
                raise ValueError(
                    "Experiment-loop recovery cannot change its pinned execution machine."
                )
            if not graph_repair:
                return self._retry_experiment_loop(
                    previous,
                    original,
                    provider=provider,
                    model=model,
                    reasoning=reasoning,
                    skills=skills,
                    authorized_by=authorized_by,
                )
        updates = {
            key: value
            for key, value in {
                "provider": provider,
                "model": model,
                "reasoning": reasoning,
                "run_on": run_on,
            }.items()
            if value is not None
        }
        request = type(original).model_validate(
            {
                **original.model_dump(mode="json"),
                **updates,
                **_skill_update(skills, mode="json"),
                "session_id": None,
            }
        )
        same_provider = request.provider == original.provider
        same_model = request.model == original.model
        same_reasoning = request.reasoning == original.reasoning
        same_execution_host = request.run_on == original.run_on
        session_limit = self._failure_is_session_limit(previous)
        continuation_context_unavailable = self._continuation_context_is_unavailable(previous)
        owned_checkpoint = (
            bool(previous.native_session_id)
            and bool(previous.stage_root)
            and self._session_is_rcp_owned(previous)
        )
        result_view_revision = bool(
            isinstance(original, RunRequest)
            and original.result_view is not None
            and original.result_view.action == "revise"
        )
        must_reuse_saved_session = graph_repair or result_view_revision
        if must_reuse_saved_session:
            problem = None
            stage_available: bool | None = True
            if owned_checkpoint and previous.stage_host:
                stage_available = RemoteRunStage(previous.stage_host).directory_exists(
                    previous.stage_root or ""
                )
            elif owned_checkpoint and previous.stage_root:
                stage = Path(previous.stage_root)
                stage_available = stage.is_dir() and not stage.is_symlink()
            if session_limit:
                problem = "the native provider session reached its limit"
            elif continuation_context_unavailable:
                problem = "the saved continuation context is unavailable"
            elif result_view_revision and (
                not same_provider or not same_model or not same_reasoning or not same_execution_host
            ):
                problem = "the pinned provider, model, reasoning, or execution machine changed"
            elif not same_provider or not same_execution_host:
                problem = "the pinned provider or execution machine changed"
            elif not owned_checkpoint:
                problem = "the prior task has no complete RCP-owned session and stage"
            elif stage_available is not True:
                problem = "the saved provider workspace is unavailable"
            if problem is not None:
                detail = (
                    "This result-view revision cannot start a fresh provider session because "
                    f"{problem}. The existing view was not redrawn; start a new result view "
                    "instead."
                    if result_view_revision
                    else "This patch-only graph repair cannot start a full Work turn because "
                    f"{problem}. Start a new Work turn instead."
                )
                raise ValueError(detail)
            assert previous.native_session_id is not None
            request = request.model_copy(update={"session_id": previous.native_session_id})
            return self._create_and_spawn(
                previous.project_id,
                previous.kind,
                request,
                parent=previous,
                continuation="graph_repair" if graph_repair else "retry",
                estimate_seconds=previous.estimate_seconds,
                estimate_samples=previous.estimate_samples,
                stage_host=previous.stage_host,
                stage_root=previous.stage_root,
                authorized_by=authorized_by,
            )
        retry_same_provider = (
            previous.status == "failed"
            and same_provider
            and same_execution_host
            and owned_checkpoint
            and not session_limit
            and not continuation_context_unavailable
        )
        if retry_same_provider:
            request = request.model_copy(update={"session_id": previous.native_session_id})
            return self._create_and_spawn(
                previous.project_id,
                previous.kind,
                request,
                parent=previous,
                continuation="retry",
                estimate_seconds=previous.estimate_seconds,
                estimate_samples=previous.estimate_samples,
                stage_host=previous.stage_host,
                stage_root=previous.stage_root,
                authorized_by=authorized_by,
            )
        estimate, samples = self.store.agent_task_estimate(
            previous.project_id,
            previous.kind,
            request.model_dump(mode="json"),
        )
        retried = self._create_and_spawn(
            previous.project_id,
            previous.kind,
            request,
            parent=previous,
            continuation="handoff",
            estimate_seconds=estimate,
            estimate_samples=samples,
            authorized_by=authorized_by,
        )
        if same_provider and session_limit:
            self.store.record_agent_task_receipt(
                retried.operation_id,
                "native_resume_skipped",
                {"classification": "session_limit"},
                tier="diagnostic",
            )
            self.store.record_agent_task_event(
                retried.operation_id,
                "The provider session limit was exhausted; starting a clean retry.",
                level="warning",
            )
        elif previous.status == "failed" and same_provider and not retry_same_provider:
            reason = (
                "execution host changed"
                if not same_execution_host
                else "the saved continuation context is unavailable"
                if continuation_context_unavailable
                else "the prior task has no complete RCP-owned native checkpoint and stage"
            )
            self.store.record_agent_task_receipt(
                retried.operation_id,
                "native_resume_unavailable",
                {"reason": reason},
                tier="diagnostic",
            )
            self.store.record_agent_task_event(
                retried.operation_id,
                f"Native resume is unavailable because {reason}; starting a clean retry.",
                level="warning",
            )
        return retried

    def _retry_campaign_task(
        self,
        previous: AgentTaskRecord,
        original: CampaignRunRequest,
        *,
        provider: str | None,
        model: str | None,
        reasoning: str | None,
        run_on: str | None,
        skills: SkillSelection | None,
    ) -> AgentTaskRecord:
        """Recover one paid campaign allocation without changing its execution binding."""

        requested = {
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "run_on": run_on,
        }
        changed = [
            key
            for key, value in requested.items()
            if value is not None and value != getattr(original, key)
        ]
        if changed:
            raise ValueError(
                "Campaign recovery cannot change its pinned " + ", ".join(changed) + "."
            )
        session_limit = self._failure_is_session_limit(previous)
        continuation_unavailable = self._continuation_context_is_unavailable(previous)
        owned_checkpoint = bool(
            previous.native_session_id
            and previous.stage_root
            and self._session_is_rcp_owned(previous)
        )
        clean_orchestrator_retry = original.role == "orchestrator" and (
            session_limit or continuation_unavailable or not owned_checkpoint
        )
        problem: str | None = None
        if previous.stage_root:
            if previous.stage_host:
                if (
                    RemoteRunStage(previous.stage_host).directory_exists(previous.stage_root)
                    is not True
                ):
                    problem = "the saved provider workspace is unavailable"
            else:
                stage = Path(previous.stage_root)
                if not stage.is_dir() or stage.is_symlink():
                    problem = "the saved provider workspace is unavailable"
        elif not clean_orchestrator_retry:
            problem = "the prior task has no complete RCP-owned session and stage"
        if not clean_orchestrator_retry:
            if session_limit:
                problem = "the native provider session reached its limit"
            elif continuation_unavailable:
                problem = "the saved continuation context is unavailable"
            elif not owned_checkpoint:
                problem = "the prior task has no complete RCP-owned session and stage"
        if problem is not None:
            campaign = self.store.campaign(original.campaign_id)
            if campaign is not None and campaign.stop_requested_at is not None:
                self.store.abandon_campaign_recovery(
                    previous.operation_id,
                    diagnostic=problem,
                )
                self.store.settle_campaign_stop(campaign.campaign_id)
            raise ValueError(
                "Campaign recovery cannot start a fresh provider session because "
                f"{problem}. Its original allocation and operational history were preserved."
            )
        if clean_orchestrator_retry:
            session_id = None
            classification = (
                "session_limit"
                if session_limit
                else "continuation_unavailable"
                if continuation_unavailable
                else "checkpoint_missing"
            )
        else:
            assert previous.native_session_id is not None
            session_id = previous.native_session_id
            classification = None
        request = CampaignRunRequest.model_validate(
            {
                **original.model_dump(mode="json"),
                "session_id": session_id,
                **_skill_update(skills, mode="json"),
            }
        )
        retried = self._create_and_spawn(
            previous.project_id,
            previous.kind,
            request,
            parent=previous,
            continuation="retry",
            estimate_seconds=previous.estimate_seconds,
            estimate_samples=previous.estimate_samples,
            stage_host=previous.stage_host,
            stage_root=previous.stage_root,
        )
        if classification is not None:
            self.store.record_agent_task_receipt(
                retried.operation_id,
                "campaign_orchestrator_clean_retry",
                {
                    "classification": classification,
                    "same_allocation": True,
                    "actor_operation_id": original.actor_operation_id,
                    "retry_mode": "clean_native_session",
                },
                tier="summary",
            )
            self.store.record_agent_task_event(
                retried.operation_id,
                "The orchestrator is retrying this same paid allocation with a clean native "
                "session after its prior continuation became unavailable.",
                level="warning",
            )
        return retried

    def _retry_experiment_loop(
        self,
        previous: AgentTaskRecord,
        original: RunRequest,
        *,
        provider: str | None,
        model: str | None,
        reasoning: str | None,
        skills: SkillSelection | None,
        authorized_by: AuthorizedHuman | None,
    ) -> AgentTaskRecord:
        """Recover an Experiment attempt without starting or spending a new episode turn."""

        if not original.control_episode_id:
            raise ValueError("Experiment-loop recovery is missing its episode id.")
        episode = self.store.experiment_episode(original.control_episode_id)
        binding_task = (
            self.store.agent_task(episode.last_turn_operation_id)
            if episode is not None and episode.last_turn_operation_id
            else None
        )
        binding_request = (
            self._request_from_record(binding_task) if binding_task is not None else original
        )
        if not isinstance(binding_request, RunRequest):
            raise ValueError("The Experiment episode binding does not belong to a Work task.")

        active_run_on = (
            episode.execution_machine if episode is not None and episode.session_bound else None
        )
        baseline = {
            **original.model_dump(mode="json"),
            "run_on": active_run_on or binding_request.run_on,
            "session_id": None,
            **_skill_update(skills, mode="json"),
        }
        requested_config = {
            key: value
            for key, value in {
                "provider": provider,
                "model": model,
                "reasoning": reasoning,
            }.items()
            if value is not None
        }
        request = RunRequest.model_validate({**baseline, **requested_config})
        config_changed = any(
            requested_config.get(field, baseline.get(field)) != baseline.get(field)
            for field in requested_config
        )
        if config_changed:
            estimate, samples = self.store.agent_task_estimate(
                previous.project_id,
                previous.kind,
                request.model_dump(mode="json"),
            )
            return self._create_and_spawn(
                previous.project_id,
                previous.kind,
                request,
                parent=previous,
                continuation="handoff",
                estimate_seconds=estimate,
                estimate_samples=samples,
                authorized_by=authorized_by,
            )

        active_config = (
            binding_request.provider,
            binding_request.model,
            binding_request.reasoning,
        )
        current_config = (original.provider, original.model, original.reasoning)
        previous_checkpoint = bool(
            previous.native_session_id
            and previous.stage_root
            and self._session_is_rcp_owned(previous)
        )
        use_active_binding = bool(
            not previous_checkpoint
            and episode is not None
            and episode.session_bound
            and current_config == active_config
        )
        session_id = episode.native_session_id if use_active_binding else previous.native_session_id
        stage_host = episode.stage_host if use_active_binding else previous.stage_host
        stage_root = episode.stage_root if use_active_binding else previous.stage_root
        owned_checkpoint = previous_checkpoint or use_active_binding
        stage_available: bool | None = True
        if owned_checkpoint and stage_host:
            stage_available = RemoteRunStage(stage_host).directory_exists(stage_root or "")
        elif owned_checkpoint and stage_root:
            stage = Path(stage_root)
            stage_available = stage.is_dir() and not stage.is_symlink()
        if owned_checkpoint and stage_available is True:
            return self._create_and_spawn(
                previous.project_id,
                previous.kind,
                request.model_copy(update={"session_id": session_id}),
                parent=previous,
                continuation="retry",
                estimate_seconds=previous.estimate_seconds,
                estimate_samples=previous.estimate_samples,
                stage_host=stage_host,
                stage_root=stage_root,
                authorized_by=authorized_by,
            )

        reason = (
            "the saved provider workspace is unavailable"
            if owned_checkpoint
            else "the episode has no complete RCP-owned native checkpoint and stage"
        )
        if episode is not None and episode.session_bound and current_config == active_config:
            detail = (
                f"This continuation cannot start a fresh provider session because {reason}. "
                "Switch provider to continue this same episode, or use Stop loop to abandon it."
            )
            if original.control_node_id:
                self.store.record_experiment_episode_diagnostic(
                    episode_id=original.control_episode_id,
                    project_id=previous.project_id,
                    control_node_id=original.control_node_id,
                    diagnostic=detail,
                )
                if episode.stop_requested_at is not None:
                    self.store.settle_experiment_loop_stop(
                        previous.project_id,
                        original.control_node_id,
                    )
            raise ValueError(detail)
        estimate, samples = self.store.agent_task_estimate(
            previous.project_id,
            previous.kind,
            request.model_dump(mode="json"),
        )
        retried = self._create_and_spawn(
            previous.project_id,
            previous.kind,
            request,
            parent=previous,
            continuation="handoff",
            estimate_seconds=estimate,
            estimate_samples=samples,
            authorized_by=authorized_by,
        )
        self.store.record_agent_task_receipt(
            retried.operation_id,
            "native_resume_unavailable",
            {"reason": reason},
            tier="diagnostic",
        )
        self.store.record_agent_task_event(
            retried.operation_id,
            f"Native resume is unavailable because {reason}; continuing this episode with a "
            "provisional provider session.",
            level="warning",
        )
        return retried

    def repair_graph_update(
        self,
        operation_id: str,
        *,
        authorized_by: AuthorizedHuman | None = None,
    ) -> AgentTaskRecord:
        """Create one idempotent patch-only continuation for a rejected Work result."""

        previous = self._require_operation(operation_id)
        if previous.kind not in {"node_chat", "project_chat"}:
            raise ValueError("Only a conversation Work task can repair a graph update.")
        request = self._request_from_record(previous)
        if not isinstance(request, RunRequest) or request.mode != "work":
            raise ValueError("Only a Work turn can repair a graph update.")
        if (
            not previous.native_session_id
            or not previous.stage_root
            or not self._session_is_rcp_owned(previous)
        ):
            raise ValueError(
                "The rejected graph update has no retained RCP-owned session and stage. "
                "Start a new Work turn instead."
            )
        previous = self.store.claim_agent_task_graph_repair(operation_id)
        request = request.model_copy(
            update={"session_id": previous.native_session_id, "message": None}
        )
        try:
            return self._create_and_spawn(
                previous.project_id,
                previous.kind,
                request,
                parent=previous,
                continuation="graph_repair",
                estimate_seconds=previous.estimate_seconds,
                estimate_samples=previous.estimate_samples,
                stage_host=previous.stage_host,
                stage_root=previous.stage_root,
                authorized_by=authorized_by,
            )
        except Exception:
            self.store.restore_agent_task_graph_repair(operation_id)
            raise

    def pause(self, operation_id: str) -> AgentTaskRecord:
        record = self.store.request_agent_task_pause(operation_id)
        with self._controls_lock:
            control = self._controls.get(operation_id)
        if control is not None:
            control.request_pause()
        return record

    def pause_campaign_worker(
        self,
        operation_id: str,
        campaign_id: str,
    ) -> AgentTaskRecord:
        """Commit the campaign gate before signalling the worker process."""

        record = self.store.request_campaign_worker_pause(operation_id, campaign_id)
        with self._controls_lock:
            control = self._controls.get(operation_id)
        if control is not None:
            control.request_pause()
        return record

    def shutdown(self, *, timeout: float = 7.0) -> None:
        """Pause live subprocesses before the web process exits."""
        with self._watcher_delivery_lock:
            self._accepting_watcher_deliveries = False
            with self._controls_lock:
                active = list(self._controls.items())
                workers = [self._workers.get(operation_id) for operation_id, _ in active]
        for operation_id, control in active:
            with suppress(ValueError):
                self.store.request_agent_task_pause(operation_id, requested_by="shutdown")
            control.request_pause()
        deadline = time.monotonic() + timeout
        for worker in workers:
            if worker is None or worker is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)

    def accept_watcher_notifications(self) -> None:
        """Open automatic delivery admission for one app lifespan."""

        with self._watcher_delivery_lock:
            self._accepting_watcher_deliveries = True

    def _campaign_for_request(
        self,
        campaign_id: str,
        request: CampaignRunRequest,
    ) -> CampaignRecord:
        if request.campaign_id != campaign_id:
            raise ValueError("Campaign request does not match its campaign lineage.")
        campaign = self.store.campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        return campaign

    def _campaign_parent(
        self,
        campaign: CampaignRecord,
        operation_id: str | None,
    ) -> AgentTaskRecord:
        parent_id = operation_id or campaign.root_operation_id
        if parent_id is None:
            raise ValueError("Campaign has no root operation for child lineage.")
        parent = self._require_operation(parent_id)
        if parent.project_id != campaign.project_id or parent.campaign_id != campaign.campaign_id:
            raise ValueError("Campaign child parent is outside the campaign lineage.")
        return parent

    def _create_and_spawn(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        *,
        estimate_seconds: float,
        estimate_samples: int,
        parent: AgentTaskRecord | None = None,
        continuation: AgentTaskContinuation = "fresh",
        stage_host: str | None = None,
        stage_root: str | None = None,
        operation_id: str | None = None,
        authorized_by: AuthorizedHuman | None = None,
        campaign_mail_delivery: PendingCampaignMail | None = None,
        campaign_wake_admission: CampaignWakeAdmission | None = None,
        campaign_report_ending: CampaignEnding | None = None,
        campaign_report_error: str | None = None,
    ) -> AgentTaskRecord | None:
        campaign: CampaignRecord | None = None
        if isinstance(request, CampaignRunRequest):
            if kind != "campaign":
                raise TypeError("CampaignRunRequest requires campaign task kind.")
            campaign = self._campaign_for_request(request.campaign_id, request)
            authorized_by = campaign.authorized_by
        elif (
            campaign_mail_delivery is not None
            or campaign_wake_admission is not None
            or campaign_report_ending is not None
        ):
            raise ValueError("Only a campaign task may use campaign wake admission.")
        if _task_is_patch_capable(kind, request) and authorized_by is None:
            raise ValueError("A patch-capable agent task requires a human authorizer snapshot.")
        if kind != "campaign" and authorized_by is None:
            raise ValueError("An ordinary agent task requires a human authorizer snapshot.")
        if authorized_by is not None and not authorized_by.display_name.strip():
            raise ValueError("A human authorizer snapshot must include a nonblank display name.")
        operation_id = operation_id or str(uuid.uuid4())
        dispatch_authority = self._resolved_dispatch_authority(
            kind,
            request,
            project_id=project_id,
            parent=parent,
            operation_id=operation_id,
            continuation=continuation,
        )
        now = self.store.now()
        verb = (
            "repair its graph update"
            if continuation == "graph_repair"
            else "resume"
            if continuation == "resume"
            else "retry"
            if continuation in {"retry", "handoff"}
            else "start"
        )
        recovery = continuation in {"resume", "retry", "handoff", "graph_repair"}
        task_record = AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            campaign_id=(request.campaign_id if isinstance(request, CampaignRunRequest) else None),
            kind=kind,
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message=f"Waiting for the background worker to {verb}.",
            attempt=(parent.attempt + 1) if parent and recovery else 1,
            parent_operation_id=parent.operation_id if parent else None,
            native_session_id=request.session_id,
            stage_host=stage_host,
            stage_root=stage_root,
            estimate_seconds=estimate_seconds,
            estimate_samples=estimate_samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=authorized_by,
            dispatch_authority=dispatch_authority,
        )
        if campaign_mail_delivery is not None:
            selected_messages = campaign_mail_claim_prefix(
                campaign_id=campaign_mail_delivery.campaign_id,
                recipient_task_id=campaign_mail_delivery.recipient_task_id,
                delivery_operation_id=task_record.operation_id,
                delivered_at=task_record.created_at,
                messages=campaign_mail_delivery.messages,
            )
            if not selected_messages:
                return None
            campaign_mail_delivery = campaign_mail_delivery.model_copy(
                update={"messages": selected_messages}
            )
        if isinstance(request, CampaignRunRequest):
            assert campaign is not None
            try:
                if campaign_report_ending is not None:
                    if request.role != "report":
                        raise ValueError("Only a campaign report may claim the reserved unit.")
                    _, record = self.store.allocate_campaign_report_task(
                        task_record,
                        ending=campaign_report_ending,
                        error=campaign_report_error,
                    )
                    if record.operation_id != task_record.operation_id:
                        return record
                elif continuation in {"resume", "retry"}:
                    record = self.store.create_campaign_recovery_task(task_record)
                elif campaign_mail_delivery is not None:
                    record = self.store.create_campaign_message_wake_task(
                        task_record,
                        role=request.role,
                        recipient_task_id=campaign_mail_delivery.recipient_task_id,
                        message_ids=campaign_mail_delivery.message_ids,
                    )
                elif campaign_wake_admission is not None:
                    assert request.wake_cause is not None
                    record = campaign_wake_admission(
                        task_record,
                        request.role,
                        request.wake_cause,
                    )
                else:
                    record = self.store.create_campaign_agent_task(task_record, role=request.role)
            except CampaignBudgetExhausted:
                self._campaign_admission_exhausted(campaign)
                raise
            if record is None:
                if campaign_wake_admission is None and campaign_mail_delivery is None:
                    raise RuntimeError(
                        "campaign admission returned no task outside a watcher or mail wake"
                    )
                return None
            if (
                record.operation_id != task_record.operation_id
                or record.project_id != campaign.project_id
                or record.campaign_id != campaign.campaign_id
            ):
                raise ValueError("Campaign wake admission returned another task lineage.")
        else:
            record = self.store.create_agent_task(task_record)
        return self._spawn_record(record, request, continuation=continuation, parent=parent)

    def _resolved_dispatch_authority(
        self,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        *,
        project_id: str,
        parent: AgentTaskRecord | None = None,
        operation_id: str | None = None,
        continuation: AgentTaskContinuation = "fresh",
    ) -> AgentDispatchAuthority | None:
        authority = self.dispatch_authority_resolver(kind, request)
        if kind == "campaign":
            if not isinstance(request, CampaignRunRequest):
                raise TypeError("campaign dispatch requires a CampaignRunRequest")
            if request.role == "report":
                if authority is not None:
                    raise ValueError(
                        "Authority refused action 'dispatch': a campaign report has no graph "
                        "authority binding."
                    )
                return None
            if authority is None:
                raise ValueError(
                    "Authority refused action 'dispatch': the campaign actor has no authority "
                    "binding."
                )
        elif authority is None:
            raise ValueError(
                "Authority refused action 'dispatch': the task has no authority binding."
            )
        assert authority is not None
        require_dispatch(authority)

        if kind == "campaign":
            assert isinstance(request, CampaignRunRequest)
            if operation_id is None:
                raise ValueError(
                    "Authority refused action 'dispatch': campaign admission has no operation id."
                )
            actor_operation_id = request.actor_operation_id
            if parent is None:
                if (
                    request.role != "orchestrator"
                    or actor_operation_id != operation_id
                    or request.wake_cause is not None
                ):
                    raise ValueError(
                        "Authority refused action 'dispatch': a campaign root must be its sole "
                        "orchestrator actor."
                    )
                return authority

            stored_parent = self.store.agent_task(parent.operation_id)
            if stored_parent is None:
                raise ValueError(
                    "Authority refused action 'dispatch': the campaign parent is missing."
                )
            if (
                stored_parent.project_id != project_id
                or stored_parent.kind != "campaign"
                or stored_parent.campaign_id != request.campaign_id
            ):
                raise ValueError(
                    "Authority refused action 'dispatch': a campaign continuation must preserve "
                    "its parent project and campaign."
                )
            binding = self.store.campaign_actor_binding(parent.operation_id)
            if actor_operation_id == operation_id:
                if (
                    request.role != "worker"
                    or binding.role != "orchestrator"
                    or request.wake_cause is not None
                ):
                    raise ValueError(
                        "Authority refused action 'dispatch': only the orchestrator may seat one "
                        "new ordinary worker actor."
                    )
                return authority
            if actor_operation_id != binding.actor_operation_id or request.role != binding.role:
                raise ValueError(
                    "Authority refused action 'dispatch': a campaign continuation cannot change "
                    "its canonical actor or role."
                )
            origin = self.store.agent_task(binding.actor_operation_id)
            if origin is None:
                raise ValueError(
                    "Authority refused action 'dispatch': the canonical campaign actor is missing."
                )
            if origin.dispatch_authority is None:
                if continuation not in {"resume", "retry"}:
                    raise ValueError(
                        "Authority refused action 'dispatch': the canonical campaign actor has no "
                        "durable authority binding."
                    )
                # A pre-authority campaign allocation remains recoverable. Its recovery is
                # still checked against today's closed profile contract before launch.
                return authority
            if authority != origin.dispatch_authority:
                raise ValueError(
                    "Authority refused action 'dispatch': a campaign continuation cannot change "
                    "its canonical actor's authority binding."
                )
            return origin.dispatch_authority

        if parent is not None:
            stored_parent = self.store.agent_task(parent.operation_id)
            if stored_parent is None:
                raise ValueError(
                    "Authority refused action 'dispatch': the continuation parent is missing."
                )
            if stored_parent.project_id != project_id or stored_parent.kind != kind:
                raise ValueError(
                    "Authority refused action 'dispatch': a continuation must preserve its "
                    "parent's project and task kind."
                )
            # A parent recorded before dispatch authority existed carries none. The
            # continuation still resolved and gated its own authority above; there is
            # simply no earlier binding to hold it to.
            if (
                stored_parent.dispatch_authority is not None
                and authority != stored_parent.dispatch_authority
            ):
                raise ValueError(
                    "Authority refused action 'dispatch': a continuation cannot change its "
                    "parent's authority binding."
                )
        return authority

    def _spawn_record(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        *,
        continuation: AgentTaskContinuation,
        parent: AgentTaskRecord | None = None,
    ) -> AgentTaskRecord:
        operation_id = record.operation_id
        reuses_native_checkpoint = continuation in _NATIVE_CHECKPOINT_CONTINUATIONS
        self.store.record_agent_task_receipt(
            operation_id,
            "operation_created",
            {
                "kind": record.kind,
                "attempt": record.attempt,
                "has_parent": parent is not None,
                "continuation_cause": continuation,
                "resumed": reuses_native_checkpoint,
            },
        )
        if (
            parent
            and isinstance(request, CampaignRunRequest)
            and continuation not in {"resume", "retry", "handoff", "graph_repair"}
        ):
            label = {
                "fresh": f"Campaign {request.role} turn",
                "watcher_wake": "Campaign watcher wake",
                "graph_condition_wake": "Campaign graph-condition wake",
                "message_wake": "Campaign message wake",
                "campaign_continuation": "Campaign human-authorized continuation",
            }[continuation]
            self.store.record_agent_task_event(
                operation_id,
                f"{label} queued from task {parent.operation_id[:8]}.",
            )
        elif parent:
            action = (
                "Repairing the graph update from"
                if continuation == "graph_repair"
                else "Resuming"
                if continuation == "resume"
                else "Retrying"
            )
            feedback = "" if continuation == "resume" else " with prior failure diagnostics"
            self.store.record_agent_task_event(
                operation_id,
                f"{action} task {parent.operation_id[:8]} as attempt {record.attempt}{feedback}.",
            )
        elif (
            isinstance(request, RunRequest)
            and bool(request.watcher_ids)
            and request.trigger in {"watcher", "experiment_run"}
        ):
            self.store.record_agent_task_receipt(
                operation_id,
                "watcher_notification",
                {"watcher_ids": request.watcher_ids},
            )
            event = (
                "Pending watcher completion reauthorized by human Run."
                if request.trigger == "experiment_run"
                else "Watcher completion queued."
            )
            self.store.record_agent_task_event(operation_id, event)
        else:
            self.store.record_agent_task_event(operation_id, "Agent task queued.")
        control = AgentProcessControl()
        with self._controls_lock:
            self._controls[operation_id] = control
        worker = threading.Thread(
            target=self._run,
            args=(record, request, control, continuation),
            name=f"rcp-{record.kind}-{operation_id[:8]}",
            daemon=True,
        )
        with self._controls_lock:
            self._workers[operation_id] = worker
        worker.start()
        return record

    def _run(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        control: AgentProcessControl,
        continuation: AgentTaskContinuation,
    ) -> None:
        operation_id = record.operation_id
        current = self.store.agent_task(operation_id)
        if current is None:
            return
        if current.status == "pausing" or control.pause_requested.is_set():
            self.store.pause_agent_task(operation_id)
            execution = AgentTaskExecution(
                operation_id=operation_id,
                store=self.store,
                control=control,
                stage_host=record.stage_host,
                stage_root=record.stage_root,
                continuation=continuation,
            )
            try:
                self._task_settled(record, request, execution)
            finally:
                self._forget_control(operation_id)
            return
        self.store.mark_agent_task_running(operation_id)
        execution = AgentTaskExecution(
            operation_id=operation_id,
            store=self.store,
            control=control,
            stage_host=record.stage_host,
            stage_root=record.stage_root,
            continuation=continuation,
            retry_feedback=(
                self._retry_feedback(record) if continuation in {"retry", "handoff"} else ()
            ),
        )
        try:
            try:
                outcome = asyncio.run(
                    self._consume(record.project_id, record.kind, request, execution)
                )
            finally:
                self._stream_closed(record, request, execution)
        except TaskPaused as exc:
            result: dict[str, object] | None = None
            if exc.messages or exc.artifacts:
                result = {"messages": exc.messages}
                if exc.artifacts:
                    result["artifacts"] = [item.model_dump(mode="json") for item in exc.artifacts]
            self.store.pause_agent_task(
                operation_id,
                detail=str(exc) or None,
                result=result,
            )
        except Exception as exc:  # The persisted task is the API error boundary.
            if (
                isinstance(request, CampaignRunRequest)
                and request.role == "orchestrator"
                and isinstance(exc, CampaignOrchestratorTerminalFailure)
            ):
                record_structural_failure(
                    self,
                    operation_id=operation_id,
                    diagnostic=str(exc),
                )
            elif (
                isinstance(request, CampaignRunRequest)
                and request.role in {"orchestrator", "report"}
                and isinstance(exc, TaskFailed)
            ):
                self.store.record_agent_task_receipt(
                    operation_id,
                    "provider_terminal_error",
                    {
                        "provider": request.provider,
                        "classification": classify_terminal_error(str(exc)),
                    },
                    tier="diagnostic",
                )
            self.store.record_agent_task_receipt(
                operation_id,
                "operation_exception",
                {"exception_type": type(exc).__name__},
                tier="diagnostic",
            )
            partial = exc.messages if isinstance(exc, TaskFailed) else []
            artifacts = exc.artifacts if isinstance(exc, TaskFailed) else []
            result: dict[str, object] = {"messages": partial}
            if artifacts:
                result["artifacts"] = [item.model_dump(mode="json") for item in artifacts]
            if isinstance(exc, TaskFailed):
                self._record_bound_experiment_session_limit(record, request, str(exc))
            self.store.fail_agent_task(
                operation_id,
                str(exc),
                result=result if partial or artifacts else None,
            )
        else:
            # Only ingest runs owe a graph revision. A chat turn answers a
            # question; changing the graph is the exception, not the contract.
            if record.kind in {"seed", "refresh"} and outcome.applied_revision is None:
                if control.pause_requested.is_set():
                    self.store.pause_agent_task(operation_id)
                else:
                    self.store.record_agent_task_receipt(
                        operation_id,
                        "missing_applied_revision",
                        {"agent_stream_completed": True},
                        tier="diagnostic",
                    )
                    self.store.fail_agent_task(
                        operation_id,
                        "The agent stopped without applying a graph revision.",
                    )
            else:
                result: dict[str, object] = {"messages": outcome.messages}
                if outcome.artifacts:
                    result["artifacts"] = [
                        item.model_dump(mode="json") for item in outcome.artifacts
                    ]
                if outcome.graph_update is not None:
                    result["graph_update"] = outcome.graph_update.model_dump(mode="json")
                self.store.complete_agent_task(
                    operation_id,
                    applied_revision=outcome.applied_revision,
                    result=result,
                )
        finally:
            try:
                if isinstance(request, RunRequest) and request.patch_kind == "experiment_loop":
                    self.store.settle_ready_experiment_loop_stops()
            finally:
                try:
                    self._task_settled(record, request, execution)
                finally:
                    self._forget_control(operation_id)

    def _stream_closed(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> None:
        if self.on_stream_closed is None:
            return
        try:
            self.on_stream_closed(record.project_id, record.kind, request, execution)
        except Exception as exc:
            # An observer must never replace the stream's actual paused, failed,
            # or completed verdict.
            with suppress(Exception):
                self.store.record_agent_task_receipt(
                    execution.operation_id,
                    "stream_closed_callback_failed",
                    {"exception_type": type(exc).__name__},
                    tier="diagnostic",
                )

    def _task_settled(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> None:
        if self.on_task_settled is not None:
            try:
                self.on_task_settled(record.project_id, record.kind, request, execution)
            except Exception as exc:
                # Delivery observation runs after the task verdict and must never
                # replace it, including when the agent stream itself failed.
                with suppress(Exception):
                    self.store.record_agent_task_receipt(
                        execution.operation_id,
                        "task_settled_callback_failed",
                        {"exception_type": type(exc).__name__},
                        tier="diagnostic",
                    )
        if isinstance(request, CampaignRunRequest):
            campaign = self.store.campaign(request.campaign_id)
            if campaign is None:
                return
            if campaign.stop_requested_at is not None:
                campaign = self.store.settle_campaign_stop(campaign.campaign_id)
            if self.on_campaign_task_settled is None:
                return
            try:
                self.on_campaign_task_settled(campaign, request, execution)
            except Exception as exc:
                with suppress(Exception):
                    self.store.record_agent_task_receipt(
                        execution.operation_id,
                        "campaign_task_settled_callback_failed",
                        {"exception_type": type(exc).__name__},
                        tier="diagnostic",
                    )

    def _campaign_admission_exhausted(self, campaign: CampaignRecord) -> None:
        campaign = begin_campaign_wrapup(
            self.store,
            campaign.campaign_id,
            "exhausted",
        )
        if campaign.root_operation_id is not None:
            with suppress(Exception):
                self.store.record_agent_task_event(
                    campaign.root_operation_id,
                    "Campaign research budget exhausted; the report unit remains reserved.",
                    level="warning",
                )
        if self.on_campaign_admission_exhausted is not None:
            with suppress(Exception):
                self.on_campaign_admission_exhausted(campaign)

    async def _consume(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> AgentTaskOutcome:
        applied_revision: int | None = None
        messages: list[str] = []
        artifacts: list[AgentArtifactDescriptor] = []
        graph_update: GraphUpdateResult | None = None
        # `aclosing` so an error or a pause closes the run generator here rather
        # than leaving it suspended for the garbage collector: its `finally` is
        # what releases the canonical run lock and retains the scratch folder.
        async with aclosing(self.stream(project_id, kind, request, execution)) as stream:
            async for frame in stream:
                event = _event_from_sse(frame)
                if event.usage is not None:
                    usage_record = self.store.record_agent_usage(
                        execution.operation_id,
                        event.usage,
                    )
                    self.store.record_agent_task_receipt(
                        execution.operation_id,
                        "provider_usage",
                        {
                            "usage_id": usage_record.usage_id,
                            "counted": usage_record.counted,
                            "count_reason": usage_record.count_reason,
                            "provider_profile": usage_record.provider_profile,
                            "processed_input_tokens": usage_record.processed_input_tokens,
                            "generated_tokens": usage_record.generated_tokens,
                        },
                        tier="diagnostic",
                    )
                if event.event == "error":
                    raise TaskFailed(event.text or "The agent task failed.", messages, artifacts)
                if event.event == "paused":
                    raise TaskPaused(event.text, messages, artifacts)
                if event.event == "session" and event.session_id:
                    self.store.checkpoint_agent_task(
                        execution.operation_id,
                        native_session_id=event.session_id,
                    )
                    self.store.record_agent_task_receipt(
                        execution.operation_id,
                        "native_agent_checkpoint",
                        {
                            "provider": request.provider,
                            "run_on": request.run_on,
                            "native_session_id": event.session_id,
                            "continuation_cause": execution.continuation,
                            "resumed": execution.reuses_native_checkpoint,
                        },
                        tier="diagnostic",
                    )
                    self.store.update_agent_task_message(
                        execution.operation_id,
                        "Agent task is running.",
                        phase="agent",
                    )
                if event.event == "message":
                    revision = _applied_revision(event.text)
                    parsed_graph_update = _graph_update(event.text)
                    if parsed_graph_update is not None:
                        graph_update = parsed_graph_update
                    if revision is not None:
                        applied_revision = revision
                        # A retained Experiment patch can report its historical
                        # commit id while the captured state is the current
                        # idempotent result. Keep that state and revision paired.
                        if execution.applied_graph_state is None:
                            execution.applied_revision = revision
                        self.store.update_agent_task_message(
                            execution.operation_id,
                            "Applying the graph update.",
                            phase="applying",
                            event=True,
                        )
                    elif parsed_graph_update is None and event.text.strip() and len(messages) < 32:
                        messages.append(event.text.strip()[:16_000])
                if event.event == "answer" and event.text.strip() and len(messages) < 32:
                    messages.append(event.text.strip()[:16_000])
                if (
                    event.event == "artifact"
                    and event.artifact is not None
                    and len(artifacts) < CHAT_ARTIFACT_MAX_COUNT
                    and event.artifact.artifact_id not in {item.artifact_id for item in artifacts}
                ):
                    artifacts.append(event.artifact)
                if event.event == "raw" and event.text.startswith(
                    "Omitted oversized provider event"
                ):
                    self.store.record_agent_task_event(
                        execution.operation_id,
                        event.text,
                        level="warning",
                    )
                    self.store.record_agent_task_receipt(
                        execution.operation_id,
                        "provider_event_omitted",
                        {"reason": "provider_event_exceeded_stream_limit"},
                        tier="trace",
                    )
        return AgentTaskOutcome(
            applied_revision=applied_revision,
            messages=messages,
            artifacts=artifacts,
            graph_update=graph_update,
        )

    def _require_operation(self, operation_id: str) -> AgentTaskRecord:
        record = self.store.agent_task(operation_id)
        if record is None:
            raise KeyError(operation_id)
        return record

    def _session_is_rcp_owned(self, record: AgentTaskRecord) -> bool:
        if record.kind in {"node_chat", "project_chat", "paper_coach"}:
            return bool(record.native_session_id)
        seen: set[str] = set()
        current = record
        while current.operation_id not in seen:
            seen.add(current.operation_id)
            request = self._request_from_record(current)
            if request.session_id is None:
                return bool(current.native_session_id)
            if not current.parent_operation_id:
                return False
            parent = self.store.agent_task(current.parent_operation_id)
            if (
                parent is None
                or parent.project_id != current.project_id
                or parent.kind != current.kind
                or parent.native_session_id != request.session_id
            ):
                return False
            current = parent
        return False

    def _retry_feedback(self, record: AgentTaskRecord) -> tuple[str, ...]:
        feedback: list[str] = []
        seen_operations: set[str] = set()
        seen_errors: set[str] = set()
        parent_id = record.parent_operation_id
        while parent_id and parent_id not in seen_operations and len(feedback) < 3:
            seen_operations.add(parent_id)
            parent = self.store.agent_task(parent_id)
            if (
                parent is None
                or parent.project_id != record.project_id
                or parent.kind != record.kind
            ):
                break
            if parent.error:
                detail = " ".join(parent.error.split())[:1600]
                if detail and detail not in seen_errors:
                    feedback.append(
                        f"Attempt {parent.attempt} ({parent.status}) failed with: {detail}"
                    )
                    seen_errors.add(detail)
            parent_id = parent.parent_operation_id
        return tuple(feedback)

    def _failure_is_session_limit(self, record: AgentTaskRecord) -> bool:
        classified_receipt = any(
            receipt.category == "provider_terminal_error"
            and receipt.payload.get("classification") == "session_limit"
            for receipt in self.store.agent_task_receipts(record.operation_id)
        )
        return classified_receipt or (
            bool(record.error) and classify_terminal_error(record.error or "") == "session_limit"
        )

    def _record_bound_experiment_session_limit(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        error: str,
    ) -> None:
        """Persist a bound episode's terminal provider limit before human recovery acts."""

        if (
            not isinstance(request, RunRequest)
            or request.patch_kind != "experiment_loop"
            or not request.control_episode_id
            or not request.control_node_id
            or classify_terminal_error(error) != "session_limit"
        ):
            return
        episode = self.store.experiment_episode(request.control_episode_id)
        if (
            episode is None
            or episode.project_id != record.project_id
            or episode.control_node_id != request.control_node_id
            or not episode.session_bound
        ):
            return
        self.store.record_experiment_episode_diagnostic(
            episode_id=request.control_episode_id,
            project_id=episode.project_id,
            control_node_id=request.control_node_id,
            diagnostic=_EXPERIMENT_SESSION_LIMIT_DIAGNOSTIC,
        )

    def _preflight_experiment_episode_recovery(
        self,
        record: AgentTaskRecord,
        *,
        request: AgentTaskRequest | None = None,
    ) -> None:
        """Refuse a legacy Experiment recovery before it creates or launches a child."""

        original = request or self._request_from_record(record)
        if not isinstance(original, RunRequest) or original.patch_kind != "experiment_loop":
            return
        problem = self.store.experiment_episode_recovery_context_problem(record.operation_id)
        if problem is None:
            return
        assert original.control_episode_id is not None
        assert original.control_node_id is not None
        self.store.record_experiment_episode_diagnostic(
            episode_id=original.control_episode_id,
            project_id=record.project_id,
            control_node_id=original.control_node_id,
            diagnostic=problem,
        )
        episode = self.store.experiment_episode(original.control_episode_id)
        if episode is not None and episode.stop_requested_at is not None:
            self.store.settle_experiment_loop_stop(
                record.project_id,
                original.control_node_id,
            )
        raise ValueError(problem)

    def _continuation_context_is_unavailable(self, record: AgentTaskRecord) -> bool:
        return any(
            receipt.category == "continuation_context_unavailable"
            and receipt.payload.get("retry_required") is True
            for receipt in self.store.agent_task_receipts(record.operation_id)
        )

    @staticmethod
    def _request_from_record(record: AgentTaskRecord) -> AgentTaskRequest:
        if record.kind == "paper_coach":
            return CoachRequest.model_validate(record.request)
        if record.kind == "campaign":
            return CampaignRunRequest.model_validate(record.request)
        return RunRequest.model_validate(record.request)

    @staticmethod
    def _validate_request_type(kind: AgentTaskKind, request: AgentTaskRequest) -> None:
        if kind == "paper_coach" and not isinstance(request, CoachRequest):
            raise TypeError("paper_coach requires a CoachRequest")
        if kind == "campaign" and not isinstance(request, CampaignRunRequest):
            raise TypeError("campaign requires a CampaignRunRequest")
        if kind not in {"paper_coach", "campaign"} and not isinstance(request, RunRequest):
            raise TypeError(f"{kind} requires a RunRequest")

    def _forget_control(self, operation_id: str) -> None:
        with self._controls_lock:
            self._controls.pop(operation_id, None)
            self._workers.pop(operation_id, None)


def _event_from_sse(frame: str) -> AgentEvent:
    data = next(
        (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
        "",
    )
    if not data:
        raise ValueError("The background task emitted an invalid event.")
    return AgentEvent.model_validate_json(data)


def _applied_revision(text: str) -> int | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or "applied_revision" not in value:
        return None
    try:
        return int(value["applied_revision"])
    except (TypeError, ValueError):
        return None


def _graph_update(text: str) -> GraphUpdateResult | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or "graph_update" not in value:
        return None
    try:
        return GraphUpdateResult.model_validate(value["graph_update"])
    except (TypeError, ValueError):
        return None
