from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast, get_args

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.write_scope import ProjectWriteScope
from rcp.artifacts import AgentArtifactDescriptor
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope, require_dispatch
from rcp.core.models import AuthorizedHuman, GraphState
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.limits import CHAT_ARTIFACT_MAX_COUNT, GRAPH_UPDATE_HISTORY_MAX_COUNT
from rcp.providers import classify_terminal_error
from rcp.runs.auto_research import (
    AutoResearchRunRequest,
    AutoResearchStartRequest,
    AutoResearchWakeAdmission,
    PendingAutoResearchMail,
    auto_research_exhaustion_signal,
    auto_research_root_request,
    pending_auto_research_mail,
    request_auto_research_stop,
    settle_auto_research_stop,
)
from rcp.runs.auto_research_mail import auto_research_mail_claim_prefix
from rcp.runs.auto_research_recovery import (
    AutoResearchOrchestratorTerminalFailure,
    record_structural_failure,
)
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.runs.experiment_admission import experiment_start_message
from rcp.runs.task_policy import task_graph_capable
from rcp.runs.tasks.episode_report import EpisodeReportRunRequest
from rcp.service import (
    CoachRequest,
    GraphUpdateResult,
    RunRequest,
    resolve_dispatch_authority,
)
from rcp.skill_registry import SkillSelection
from rcp.storage import (
    ACTIVE_AGENT_TASK_STATUSES,
    AgentTaskKind,
    AgentTaskRecord,
    AppStore,
    AutoResearchChildExperimentRecord,
    AutoResearchChildWorkRecord,
    AutoResearchStateRecord,
    EpisodeInvocationCeilingReached,
    EpisodeNotRunning,
    EpisodeRecord,
)
from rcp.transport import RemoteRunStage

AgentTaskRequest = (
    RunRequest
    | CoachRequest
    | AutoResearchRunRequest
    | BranchMergeRunRequest
    | EpisodeReportRunRequest
)
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
    "lifecycle_wake",
    "auto_research_continuation",
    "episode_report",
]
_AGENT_TASK_CONTINUATIONS = frozenset(get_args(AgentTaskContinuation))

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
        "lifecycle_wake",
        "auto_research_continuation",
        "episode_report",
    }
)
_EXPERIMENT_SESSION_LIMIT_DIAGNOSTIC = (
    "The provider session reached its limit. Retry the same provider to recheck the limit and "
    "resume this episode, or switch provider to continue this same episode and invocation."
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
    write_scope_fingerprint: str | None = None
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

    def bind_write_scope(self, scope: ProjectWriteScope) -> None:
        if self.stage_root is None:
            raise ValueError(
                "agent task must checkpoint its exact stage before write-scope binding"
            )
        self.store.bind_agent_task_write_scope(
            self.operation_id,
            project_id=scope.project_id,
            stage_host=self.stage_host or "",
            stage_root=scope.stage_root,
            fingerprint=scope.fingerprint,
        )
        self.write_scope_fingerprint = scope.fingerprint


AgentTaskStream = Callable[
    [str, AgentTaskKind, AgentTaskRequest, AgentTaskExecution], AsyncIterator[str]
]
AgentTaskStreamClosedHook = Callable[
    [str, AgentTaskKind, AgentTaskRequest, AgentTaskExecution], None
]
AgentTaskSettledHook = Callable[[str, AgentTaskKind, AgentTaskRequest, AgentTaskExecution], None]
AutoResearchTaskSettledHook = Callable[
    [EpisodeRecord, AutoResearchRunRequest, AgentTaskExecution],
    None,
]
AutoResearchAdmissionExhaustedHook = Callable[[EpisodeRecord], None]


@dataclass(frozen=True)
class AgentTaskOutcome:
    applied_revision: int | None
    messages: list[str]
    artifacts: list[AgentArtifactDescriptor]
    graph_update: GraphUpdateResult | None = None
    graph_updates: list[GraphUpdateResult] = field(default_factory=list)


@dataclass(frozen=True)
class _CommittedAutoResearchDispatch:
    kind: Literal["actor_wake", "child_work", "child_experiment"]
    episode_id: str
    operation_id: str
    child_id: str | None = None
    continuation: Literal["fresh", "resume", "graph_repair", "message_wake", "watcher_wake"] = (
        "fresh"
    )


@dataclass(frozen=True)
class AutoResearchChildResumeResult:
    """Outcome of an orchestrator's exact child-allocation Resume request."""

    disposition: Literal["resumed", "resume_unavailable"]
    child_kind: Literal["work", "experiment"]
    child_id: str
    current_operation_id: str
    task: AgentTaskRecord | None = None
    reason: str | None = None
    replacement_command: Literal["spawn", "episode --kick-off-experiment"] | None = None


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
        on_auto_research_task_settled: AutoResearchTaskSettledHook | None = None,
        on_auto_research_admission_exhausted: AutoResearchAdmissionExhaustedHook | None = None,
        dispatch_authority_resolver: DispatchAuthorityResolver | None = None,
    ) -> None:
        self.store = store
        self.stream = stream
        self.on_stream_closed = on_stream_closed
        self.on_task_settled = on_task_settled
        self.on_auto_research_task_settled = on_auto_research_task_settled
        self.on_auto_research_admission_exhausted = on_auto_research_admission_exhausted
        self.dispatch_authority_resolver = dispatch_authority_resolver or resolve_dispatch_authority
        self._controls: dict[str, AgentProcessControl] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._controls_lock = threading.Lock()
        self._watcher_delivery_lock = threading.Lock()
        self._accepting_watcher_deliveries = True
        preserved_dispatches = self._proven_committed_auto_research_dispatches()
        reserved_roots = self._proven_reserved_auto_research_roots()
        self.store.interrupt_active_agent_tasks(
            preserve_operation_ids={
                *[item.operation_id for item in preserved_dispatches],
                *[task.operation_id for _episode, task, _request in reserved_roots],
            }
        )
        self._restart_stopping_experiment_recoveries()
        self.store.settle_ready_experiment_loop_stops()
        self._restart_interrupted_episode_reports()

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
        if kind == "auto_research":
            raise ValueError(
                "Use start_auto_research so its episode and root are created atomically."
            )
        if kind == "branch_merge":
            raise ValueError(
                "Use start_branch_merge so its ended episode and exact branch are checked "
                "atomically."
            )
        if kind == "episode_report":
            raise ValueError(
                "Use start_episode_report so the existing hidden allocation is preserved."
            )
        experiment_root = (
            isinstance(request, RunRequest)
            and request.patch_kind == "experiment_loop"
            and request.trigger == "experiment_run"
        )
        if (
            isinstance(request, RunRequest)
            and request.patch_kind == "experiment_loop"
            and not experiment_root
        ):
            raise ValueError(
                "An Experiment watcher wake or recovery must use its dedicated admission path."
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

    def start_branch_merge(
        self,
        project_id: str,
        request: BranchMergeRunRequest,
        *,
        authorized_by: AuthorizedHuman,
        operation_id: str | None = None,
    ) -> AgentTaskRecord:
        """Dispatch one graph-only merge without reopening or spending the episode."""

        self._validate_request_type("branch_merge", request)
        episode = self.store.episode(request.episode_id)
        if (
            episode is None
            or episode.project_id != project_id
            or episode.mode != "auto_research"
            or episode.graph_target.kind != "branch"
            or episode.graph_target.branch_id != episode.episode_id
        ):
            raise ValueError("branch merge requires its exact Auto-research episode branch")
        if episode.ending is None or not self.store.auto_research_is_quiescent(episode.episode_id):
            raise ValueError("the Auto-research branch is not ended and quiescent")
        active_branch_writers = [
            item
            for item in self.store.graph_target_tasks(
                project_id,
                episode.graph_target,
                include_hidden=True,
            )
            if item.kind != "branch_merge"
            and item.status in {*ACTIVE_AGENT_TASK_STATUSES, "paused"}
            and task_graph_capable(item.kind, item.request)
        ]
        if active_branch_writers:
            raise ValueError("the Auto-research branch still has an active graph writer")
        if not authorized_by.display_name.strip():
            raise ValueError("branch merge requires a named human authorizer snapshot")

        operation_id = operation_id or str(uuid.uuid4())
        authority = self._resolved_dispatch_authority(
            "branch_merge",
            request,
            project_id=project_id,
            operation_id=operation_id,
        )
        assert authority is not None
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            project_id,
            "branch_merge",
            request_data,
        )
        now = self.store.now()
        record = AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            episode_id=episode.episode_id,
            graph_target=episode.graph_target,
            kind="branch_merge",
            status="queued",
            request=request_data,
            created_at=now,
            updated_at=now,
            status_message="Waiting for the graph branch merge agent to start.",
            estimate_seconds=estimate,
            estimate_samples=samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=authorized_by,
            dispatch_authority=authority,
        )
        stored = self.store.create_branch_merge_task(record)
        return self.launch_admitted(stored.operation_id)

    def start_auto_research(
        self,
        project_id: str,
        request: AutoResearchStartRequest,
        *,
        authorized_by: AuthorizedHuman,
        graph_base_head: GraphHeadRef,
        ensure_graph_target: Callable[[EpisodeRecord], None],
        episode_id: str | None = None,
        operation_id: str | None = None,
    ) -> tuple[EpisodeRecord, AgentTaskRecord]:
        """Reserve SQLite identity, establish its branch, then spend the first invocation."""

        episode, task, run_request = self.reserve_auto_research(
            project_id,
            request,
            authorized_by=authorized_by,
            graph_base_head=graph_base_head,
            episode_id=episode_id,
            operation_id=operation_id,
        )
        try:
            ensure_graph_target(episode)
        except Exception as exc:
            self._fail_reserved_auto_research_root(episode, task, exc)
            raise
        episode = self.store.activate_auto_research_reservation(
            episode.episode_id,
            task.operation_id,
        )
        return episode, self.launch_admitted(task.operation_id)

    def reserve_auto_research(
        self,
        project_id: str,
        request: AutoResearchStartRequest,
        *,
        authorized_by: AuthorizedHuman,
        graph_base_head: GraphHeadRef,
        episode_id: str | None = None,
        operation_id: str | None = None,
    ) -> tuple[EpisodeRecord, AgentTaskRecord, AutoResearchRunRequest]:
        """Atomically reserve one episode/root before any canonical branch publication."""

        if not authorized_by.display_name.strip():
            raise ValueError("Auto-research requires a named human authorizer snapshot.")
        episode_id = episode_id or str(uuid.uuid4())
        if graph_base_head.target.kind != "main":
            raise ValueError("Auto-research must branch from an exact main graph head.")
        graph_target = GraphTargetRef(kind="branch", branch_id=episode_id)
        operation_id = operation_id or str(uuid.uuid4())
        run_request = auto_research_root_request(request, episode_id=episode_id).model_copy(
            update={"actor_operation_id": operation_id}
        )
        dispatch_authority = self._resolved_dispatch_authority(
            "auto_research",
            run_request,
            project_id=project_id,
            operation_id=operation_id,
        )
        assert dispatch_authority is not None
        request_data = run_request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            project_id,
            "auto_research",
            request_data,
        )
        now = self.store.now()
        episode = EpisodeRecord(
            episode_id=episode_id,
            project_id=project_id,
            mode="auto_research",
            graph_target=graph_target,
            graph_base_head=graph_base_head,
            status="queued",
            invocation_ceiling=request.invocation_ceiling,
            authorized_by=authorized_by,
            created_at=now,
            updated_at=now,
        )
        task = AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            episode_id=episode_id,
            graph_target=graph_target,
            kind="auto_research",
            status="queued",
            request=request_data,
            created_at=now,
            updated_at=now,
            status_message="Waiting for the Auto-research orchestrator to start.",
            estimate_seconds=estimate,
            estimate_samples=samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=authorized_by,
            dispatch_authority=dispatch_authority,
        )
        stored_episode, stored_task = self.store.create_auto_research_episode_with_root_task(
            episode,
            AutoResearchStateRecord(
                episode_id=episode_id,
                starting_instruction=request.starting_instruction,
                created_at=now,
                updated_at=now,
            ),
            task,
            activate=False,
        )
        return stored_episode, stored_task, run_request

    def reconcile_reserved_auto_research_roots(
        self,
        ensure_graph_target: Callable[[EpisodeRecord], None],
    ) -> list[str]:
        """Finish branch creation and launch roots reserved before a process interruption."""

        started: list[str] = []
        for episode, task, _request in self._proven_reserved_auto_research_roots():
            try:
                ensure_graph_target(episode)
            except Exception as exc:
                self._fail_reserved_auto_research_root(episode, task, exc)
                continue
            self.store.activate_auto_research_reservation(
                episode.episode_id,
                task.operation_id,
            )
            self.launch_admitted(task.operation_id)
            started.append(task.operation_id)
        return started

    def _proven_reserved_auto_research_roots(
        self,
    ) -> list[tuple[EpisodeRecord, AgentTaskRecord, AutoResearchRunRequest]]:
        reserved: list[tuple[EpisodeRecord, AgentTaskRecord, AutoResearchRunRequest]] = []
        for project in self.store.projects():
            for episode in self.store.episodes(project.project_id):
                if (
                    episode.mode != "auto_research"
                    or episode.root_operation_id is None
                    or episode.status not in {"queued", "running"}
                ):
                    continue
                task = self.store.agent_task(episode.root_operation_id)
                if (
                    task is None
                    or task.kind != "auto_research"
                    or task.episode_id != episode.episode_id
                    or task.project_id != episode.project_id
                    or task.graph_target != episode.graph_target
                    or task.parent_operation_id is not None
                    or task.status != "queued"
                    or not self.store.agent_task_dispatch_was_proven_not_started(task.operation_id)
                ):
                    continue
                try:
                    request = AutoResearchRunRequest.model_validate(task.request)
                except ValueError:
                    continue
                if (
                    request.episode_id != episode.episode_id
                    or request.role != "orchestrator"
                    or request.actor_operation_id != task.operation_id
                    or request.wake_cause is not None
                ):
                    continue
                reserved.append((episode, task, request))
        return reserved

    def _fail_reserved_auto_research_root(
        self,
        episode: EpisodeRecord,
        task: AgentTaskRecord,
        error: Exception,
    ) -> None:
        diagnostic = (
            "Auto-research could not establish its exact graph branch before provider launch: "
            f"{error}"
        )
        self.store.fail_agent_task(task.operation_id, diagnostic)
        from rcp.runs.episode_wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup

        begin_episode_report_wrapup(
            self.store,
            EpisodeWrapupSpec(
                episode_id=episode.episode_id,
                ending="failed",
                partial=True,
                continuation_operation_id=task.operation_id,
                receipt={"reason": "graph_branch_unavailable_before_launch"},
                diagnostic=diagnostic,
            ),
        )

    def start_auto_research_turn(
        self,
        episode_id: str,
        request: AutoResearchRunRequest,
        *,
        parent_operation_id: str | None = None,
        operation_id: str | None = None,
        mail_delivery: PendingAutoResearchMail | None = None,
        wake_admission: AutoResearchWakeAdmission | None = None,
    ) -> AgentTaskRecord | None:
        """Admit one operational actor turn from the episode invocation budget."""

        episode = self._auto_research_for_request(episode_id, request)
        operation_id = operation_id or str(uuid.uuid4())
        parent = self._auto_research_parent(episode, parent_operation_id)
        parent_role = self.store.auto_research_invocation_role(parent.operation_id)
        if parent_role not in {"orchestrator", "worker"}:
            raise ValueError("Auto-research continuation parent has no canonical actor role.")
        parent_request = AutoResearchRunRequest.model_validate(parent.request)
        if parent_request.role != parent_role:
            raise ValueError(
                "Auto-research continuation parent role disagrees with its durable actor."
            )
        parent_actor_id = parent_request.actor_operation_id or parent.operation_id
        requested_actor_id = request.actor_operation_id
        if request.wake_cause is not None:
            if request.role != parent_role:
                raise ValueError("An Auto-research wake cannot change its canonical actor role.")
            if requested_actor_id is not None and requested_actor_id != parent_actor_id:
                raise ValueError(
                    "An Auto-research wake cannot change its canonical actor identity."
                )
            if (
                parent_role == "worker"
                and request.control_node_id != parent_request.control_node_id
            ):
                raise ValueError("An Auto-research worker wake cannot change its canonical seat.")
            request = request.model_copy(update={"actor_operation_id": parent_actor_id})
        elif request.role == "worker":
            if parent_role != "orchestrator":
                raise ValueError("Only the Auto-research orchestrator may seat a worker.")
            if requested_actor_id is not None and requested_actor_id != operation_id:
                raise ValueError(
                    "A new Auto-research worker must use its own canonical actor identity."
                )
            if request.session_id is not None:
                raise ValueError("A new Auto-research worker must start a fresh native session.")
            request = request.model_copy(update={"actor_operation_id": operation_id})
        else:
            if parent_role != "orchestrator" or parent_actor_id != episode.root_operation_id:
                raise ValueError("Only the root Auto-research actor may continue as orchestrator.")
            if requested_actor_id is not None and requested_actor_id != parent_actor_id:
                raise ValueError(
                    "An Auto-research orchestrator turn cannot change its canonical actor identity."
                )
            request = request.model_copy(update={"actor_operation_id": parent_actor_id})

        authority_origin = self.store.agent_task(parent_actor_id)
        if authority_origin is None or authority_origin.dispatch_authority is None:
            raise ValueError(
                "Authority refused action 'dispatch': the canonical Auto-research actor has no "
                "durable authority binding."
            )
        canonical_scope = authority_origin.dispatch_authority.scope.run_truth_scope
        if request.run_truth_scope is not None and sorted(set(request.run_truth_scope)) != (
            canonical_scope
        ):
            raise ValueError(
                "Authority refused action 'dispatch': an Auto-research actor cannot change its "
                "project-wide run truth scope."
            )
        request = request.model_copy(update={"run_truth_scope": list(canonical_scope)})
        if episode.status != "running" or episode.stop_requested_at is not None:
            raise EpisodeNotRunning("the Auto-research episode is not admitting new work")
        if episode.invocations_used >= episode.invocation_ceiling:
            self._auto_research_admission_exhausted(episode)
            raise EpisodeInvocationCeilingReached(
                "the Auto-research operational invocation ceiling is exhausted"
            )

        existing_actor = request.actor_operation_id != operation_id
        stage_host: str | None = None
        stage_root: str | None = None
        if existing_actor:
            binding = self.store.auto_research_actor_binding(parent.operation_id)
            if (
                binding.actor_operation_id != request.actor_operation_id
                or binding.role != request.role
                or binding.control_node_id != request.control_node_id
            ):
                raise ValueError(
                    "An Auto-research continuation must preserve its canonical actor role and seat."
                )
            if not binding.native_session_id or not binding.stage_root:
                raise ValueError(
                    "An Auto-research continuation requires the actor's exact saved session "
                    "and stage."
                )
            if request.session_id not in {None, binding.native_session_id}:
                raise ValueError(
                    "An Auto-research continuation cannot change its saved native session."
                )
            request = request.model_copy(update={"session_id": binding.native_session_id})
            stage_host = binding.stage_host
            stage_root = binding.stage_root

        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            episode.project_id,
            "auto_research",
            request_data,
        )
        continuation: AgentTaskContinuation = {
            None: "fresh",
            "watcher": "watcher_wake",
            "graph_condition": "graph_condition_wake",
            "message": "message_wake",
            "lifecycle": "lifecycle_wake",
        }[request.wake_cause]
        if request.wake_cause is None and existing_actor:
            continuation = "auto_research_continuation"
        if request.wake_cause is not None:
            assert stage_root is not None
        if request.wake_cause == "message":
            if (
                mail_delivery is None
                or not mail_delivery.messages
                or mail_delivery.episode_id != episode_id
                or mail_delivery.recipient_task_id != parent_actor_id
            ):
                raise ValueError(
                    "An Auto-research message wake requires its exact non-empty mail batch."
                )
            if wake_admission is not None:
                raise ValueError(
                    "Auto-research message wake admission is owned by the mail transaction."
                )
        elif request.wake_cause in {"watcher", "graph_condition"}:
            if wake_admission is None:
                raise ValueError(
                    "Auto-research watcher wakes require their atomic wake-admission hook."
                )
        elif request.wake_cause == "lifecycle":
            if parent_role != "orchestrator" or parent_actor_id != episode.root_operation_id:
                raise ValueError(
                    "Auto-research lifecycle delivery may wake only the root orchestrator."
                )
            if mail_delivery is not None:
                raise ValueError(
                    "Auto-research lifecycle mail is claimed by the lifecycle transaction."
                )
            if wake_admission is None:
                raise ValueError(
                    "Auto-research lifecycle wakes require their atomic wake-admission hook."
                )
        elif mail_delivery is not None:
            raise ValueError("Only an Auto-research message wake may claim a mail batch.")
        elif wake_admission is not None:
            raise ValueError("Only an Auto-research watcher wake may use wake admission.")
        assert episode.authorized_by is not None
        return self._create_and_spawn(
            episode.project_id,
            "auto_research",
            request,
            parent=parent,
            continuation=continuation,
            stage_host=stage_host,
            stage_root=stage_root,
            estimate_seconds=estimate,
            estimate_samples=samples,
            operation_id=operation_id,
            authorized_by=episode.authorized_by,
            auto_research_mail_delivery=mail_delivery,
            auto_research_wake_admission=wake_admission,
        )

    def ensure_auto_research_wake_spawned(
        self,
        episode_id: str,
        *,
        operation_id: str,
    ) -> AgentTaskRecord:
        """Dispatch one already-committed automatic actor wake exactly once in-process."""

        existing = self._require_operation(operation_id)
        request = self._request_from_record(existing)
        if not isinstance(request, AutoResearchRunRequest) or request.wake_cause is None:
            raise ValueError("The committed task is not an automatic Auto-research wake.")
        self._validate_existing_auto_research_wake(
            episode_id,
            operation_id,
            existing,
            request,
        )
        return self.launch_admitted(existing.operation_id)

    def reconcile_committed_auto_research_dispatches(self) -> list[str]:
        """Start exact paid child/wake rows durably proven never to have run."""

        started: list[str] = []
        for dispatch in self._proven_committed_auto_research_dispatches():
            if dispatch.kind == "actor_wake":
                task = self.ensure_auto_research_wake_spawned(
                    dispatch.episode_id,
                    operation_id=dispatch.operation_id,
                )
            elif dispatch.kind == "child_work":
                assert dispatch.child_id is not None
                task = self.ensure_auto_research_child_work_spawned(
                    dispatch.episode_id,
                    dispatch.child_id,
                    operation_id=dispatch.operation_id,
                    continuation=dispatch.continuation,
                )
            else:
                assert dispatch.child_id is not None
                task = self.ensure_auto_research_child_experiment_spawned(
                    dispatch.episode_id,
                    dispatch.child_id,
                    operation_id=dispatch.operation_id,
                    continuation=dispatch.continuation,
                )
            started.append(task.operation_id)
        return list(dict.fromkeys(started))

    def _proven_committed_auto_research_dispatches(
        self,
    ) -> list[_CommittedAutoResearchDispatch]:
        """Find admitted queued rows whose dispatch attempt durably never started."""

        dispatches: list[_CommittedAutoResearchDispatch] = []
        for project in self.store.projects():
            episodes = [
                item
                for item in self.store.episodes(project.project_id)
                if item.mode == "auto_research"
            ]
            for episode in episodes:
                for task in self.store.auto_research_tasks(episode.episode_id):
                    if (
                        task.status != "queued"
                        or not self.store.agent_task_dispatch_was_proven_not_started(
                            task.operation_id
                        )
                    ):
                        continue
                    try:
                        request = self._request_from_record(task)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(request, AutoResearchRunRequest):
                        continue
                    if request.wake_cause is None:
                        continue
                    try:
                        self._validate_existing_auto_research_wake(
                            episode.episode_id,
                            task.operation_id,
                            task,
                            request,
                        )
                    except (KeyError, ValueError):
                        continue
                    dispatches.append(
                        _CommittedAutoResearchDispatch(
                            kind="actor_wake",
                            episode_id=episode.episode_id,
                            operation_id=task.operation_id,
                        )
                    )
                delivered_operation_ids = {
                    message.delivery_operation_id
                    for message in self.store.auto_research_messages(episode.episode_id)
                    if message.delivery_operation_id is not None
                }
                for route in self.store.auto_research_child_works(episode.episode_id):
                    task = self.store.agent_task(route.current_operation_id)
                    if (
                        task is None
                        or task.status != "queued"
                        or not self.store.agent_task_dispatch_was_proven_not_started(
                            task.operation_id
                        )
                    ):
                        continue
                    if route.root_operation_id == task.operation_id:
                        continuation: Literal["fresh", "resume", "message_wake"] = "fresh"
                        validator = self._validate_existing_child_work_fresh
                    elif task.operation_id in delivered_operation_ids:
                        continuation = "message_wake"
                        validator = self._validate_existing_child_work_message_wake
                    elif self.store.auto_research_child_resume_command_owns_operation(
                        episode.episode_id,
                        child_kind="work",
                        child_id=route.worker_id,
                        operation_id=task.operation_id,
                    ):
                        continuation = "resume"
                        validator = self._validate_existing_child_work_resume
                    else:
                        continue
                    try:
                        validator(
                            episode.episode_id,
                            route.worker_id,
                            task.operation_id,
                            task,
                        )
                    except (KeyError, ValueError):
                        continue
                    dispatches.append(
                        _CommittedAutoResearchDispatch(
                            kind="child_work",
                            episode_id=episode.episode_id,
                            operation_id=task.operation_id,
                            child_id=route.worker_id,
                            continuation=continuation,
                        )
                    )
                for route in self.store.auto_research_child_experiments(episode.episode_id):
                    child_episode = self.store.episode(route.child_episode_id)
                    if route.state != "running" or child_episode is None:
                        continue
                    for task in self.store.episode_tasks(route.child_episode_id):
                        if (
                            task.status != "queued"
                            or not self.store.agent_task_dispatch_was_proven_not_started(
                                task.operation_id
                            )
                        ):
                            continue
                        try:
                            request = self._request_from_record(task)
                        except (TypeError, ValueError):
                            continue
                        if task.operation_id == child_episode.root_operation_id:
                            continuation = "fresh"
                            experiment_validator = self._validate_existing_child_experiment_fresh
                        elif isinstance(request, RunRequest) and request.trigger == "watcher":
                            continuation = "watcher_wake"
                            experiment_validator = (
                                self._validate_existing_child_experiment_watcher_wake
                            )
                        elif (
                            self.store.agent_task_continuation_cause(task.operation_id)
                            == "graph_repair"
                        ):
                            continuation = "graph_repair"
                            experiment_validator = (
                                self._validate_existing_child_experiment_graph_repair
                            )
                        elif self.store.auto_research_child_resume_command_owns_operation(
                            episode.episode_id,
                            child_kind="experiment",
                            child_id=route.child_episode_id,
                            operation_id=task.operation_id,
                        ):
                            continuation = "resume"
                            experiment_validator = self._validate_existing_child_experiment_resume
                        else:
                            continue
                        try:
                            experiment_validator(
                                episode.episode_id,
                                route.child_episode_id,
                                task.operation_id,
                                task,
                            )
                        except (KeyError, ValueError):
                            continue
                        dispatches.append(
                            _CommittedAutoResearchDispatch(
                                kind="child_experiment",
                                episode_id=episode.episode_id,
                                operation_id=task.operation_id,
                                child_id=route.child_episode_id,
                                continuation=continuation,
                            )
                        )
        return dispatches

    def start_auto_research_child_work(
        self,
        episode_id: str,
        request: RunRequest,
        *,
        admitted_by_operation_id: str,
        worker_id: str,
        instruction: str,
        instruction_sha256: str,
        admission_id: str | None = None,
    ) -> AgentTaskRecord:
        """Atomically spend B and launch one routed ordinary node Work task."""

        episode = self._auto_research_parent_episode(episode_id)
        if (
            request.mode != "work"
            or request.trigger != "orchestrator"
            or request.patch_kind != "work"
            or request.chat_scope != "node"
            or not request.node_id
            or request.message != instruction
            or request.chat_id != worker_id
            or request.session_id is not None
            or request.result_view is not None
            or request.watcher_ids
        ):
            raise ValueError(
                "An Auto-research spawn must be a fresh ordinary node Work request with its "
                "exact snapshotted instruction and stable worker conversation id."
            )
        self._validate_request_type("node_chat", request)
        operation_id = worker_id
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            episode.project_id,
            "node_chat",
            request_data,
        )
        dispatch_authority = self._resolved_dispatch_authority(
            "node_chat",
            request,
            project_id=episode.project_id,
            operation_id=operation_id,
        )
        assert episode.authorized_by is not None
        now = self.store.now()
        task = AgentTaskRecord(
            operation_id=operation_id,
            project_id=episode.project_id,
            episode_id=episode_id,
            graph_target=episode.graph_target,
            kind="node_chat",
            status="queued",
            request=request_data,
            created_at=now,
            updated_at=now,
            status_message="Waiting for the spawned Work task to start.",
            estimate_seconds=estimate,
            estimate_samples=samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=episode.authorized_by,
            dispatch_authority=dispatch_authority,
        )
        route = AutoResearchChildWorkRecord(
            worker_id=worker_id,
            episode_id=episode_id,
            project_id=episode.project_id,
            control_node_id=request.node_id,
            root_operation_id=operation_id,
            current_operation_id=operation_id,
            admitted_by_operation_id=admitted_by_operation_id,
            instruction=instruction,
            instruction_sha256=instruction_sha256,
            created_at=now,
            updated_at=now,
        )
        _, stored = self.store.create_auto_research_child_work(
            route,
            task,
            admission_id=admission_id,
        )
        return self.ensure_auto_research_child_work_spawned(
            episode_id,
            worker_id,
            operation_id=stored.operation_id,
            continuation="fresh",
        )

    def ensure_auto_research_child_work_spawned(
        self,
        episode_id: str,
        worker_id: str,
        *,
        operation_id: str,
        continuation: Literal["fresh", "resume", "message_wake"],
    ) -> AgentTaskRecord:
        """Dispatch one already-committed child Work row exactly once in this process.

        Child admission and process launch are deliberately separate durability
        boundaries.  A command replay that finds the first boundary committed
        must therefore repair the second rather than merely return the queued
        row.  The in-process worker registry is the dispatch claim; terminal or
        already-live tasks are returned without another launch.
        """

        existing = self._require_operation(operation_id)
        request = self._request_from_record(existing)
        if not isinstance(request, RunRequest):
            raise ValueError("The routed child Work task lost its ordinary Work request.")
        if continuation == "fresh":
            self._validate_existing_child_work_fresh(
                episode_id,
                worker_id,
                operation_id,
                existing,
            )
        elif continuation == "resume":
            self._validate_existing_child_work_resume(
                episode_id,
                worker_id,
                operation_id,
                existing,
            )
        else:
            self._validate_existing_child_work_message_wake(
                episode_id,
                worker_id,
                operation_id,
                existing,
            )
        return self.launch_admitted(existing.operation_id)

    def auto_research_child_work_task(
        self,
        episode_id: str,
        worker_id: str,
    ) -> tuple[AutoResearchChildWorkRecord, AgentTaskRecord]:
        """Resolve a routed worker and exactly its current attempt."""

        route = self.store.auto_research_child_work(worker_id)
        if route is None:
            raise KeyError(worker_id)
        if route.episode_id != episode_id:
            raise ValueError("The worker is not registered to this Auto-research episode.")
        current = self._require_operation(route.current_operation_id)
        if (
            current.project_id != route.project_id
            or current.episode_id != route.episode_id
            or current.kind != "node_chat"
        ):
            raise ValueError("The worker route lost its ordinary Work task lineage.")
        return route, current

    def start_auto_research_child_work_message_wake(
        self,
        episode_id: str,
        worker_id: str,
        message_ids: list[str],
        *,
        operation_id: str | None = None,
        created_at: str | None = None,
    ) -> AgentTaskRecord | None:
        """Spend B to deliver mail through the routed Work task's exact saved session."""

        episode = self._auto_research_parent_episode(episode_id)
        _, current = self.auto_research_child_work_task(episode_id, worker_id)
        if current.status != "succeeded":
            return None
        if not current.native_session_id or not current.stage_root:
            return None
        request = self._request_from_record(current)
        if not isinstance(request, RunRequest):
            raise ValueError("The routed worker task is not an ordinary Work request.")
        request = request.model_copy(
            update={
                "session_id": current.native_session_id,
                "message": None,
                "watcher_ids": [],
                "result_view": None,
            }
        )
        operation_id = operation_id or str(uuid.uuid4())
        dispatch_authority = self._resolved_dispatch_authority(
            "node_chat",
            request,
            project_id=current.project_id,
            parent=current,
            operation_id=operation_id,
            continuation="message_wake",
        )
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            current.project_id,
            "node_chat",
            request_data,
        )
        assert episode.authorized_by is not None
        now = created_at or self.store.now()
        task = AgentTaskRecord(
            operation_id=operation_id,
            project_id=current.project_id,
            episode_id=episode_id,
            graph_target=episode.graph_target,
            kind="node_chat",
            status="queued",
            request=request_data,
            created_at=now,
            updated_at=now,
            status_message="Waiting for the spawned Work task to receive its message batch.",
            attempt=current.attempt + 1,
            parent_operation_id=current.operation_id,
            native_session_id=current.native_session_id,
            stage_host=current.stage_host,
            stage_root=current.stage_root,
            estimate_seconds=estimate,
            estimate_samples=samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=episode.authorized_by,
            dispatch_authority=dispatch_authority,
        )
        stored = self.store.create_auto_research_child_work_message_wake_task(
            task,
            worker_id=worker_id,
            message_ids=message_ids,
        )
        if stored is None:
            return None
        return self.ensure_auto_research_child_work_spawned(
            episode_id,
            worker_id,
            operation_id=stored.operation_id,
            continuation="message_wake",
        )

    def pause_auto_research_child_work(
        self,
        episode_id: str,
        worker_id: str,
    ) -> AgentTaskRecord:
        """Gracefully pause the current attempt of one routed Work child."""

        route, current = self.auto_research_child_work_task(episode_id, worker_id)
        if current.status == "paused":
            return current
        if current.status == "pausing":
            self._signal_agent_task_pause(current.operation_id)
            return current
        if route.stop_requested_at is not None:
            raise ValueError("The worker has already been stopped and cannot be paused.")
        return self.pause(current.operation_id)

    def stop_auto_research_child_work(
        self,
        episode_id: str,
        worker_id: str,
    ) -> AgentTaskRecord:
        """Durably stop one route and gracefully pause its current live attempt."""

        route, current = self.auto_research_child_work_task(episode_id, worker_id)
        self.store.request_auto_research_child_work_stop(route.worker_id)
        current = self._require_operation(current.operation_id)
        if current.status in {"queued", "running"}:
            return self.pause(current.operation_id)
        if current.status == "pausing":
            self._signal_agent_task_pause(current.operation_id)
        return current

    def resume_auto_research_child_work(
        self,
        episode_id: str,
        worker_id: str,
        *,
        operation_id: str | None = None,
    ) -> AutoResearchChildResumeResult:
        """Resume a routed Work attempt only from its exact usable checkpoint."""

        route, previous = self.auto_research_child_work_task(episode_id, worker_id)
        if operation_id is not None:
            existing = self.store.agent_task(operation_id)
            if existing is not None:
                existing = self.ensure_auto_research_child_work_spawned(
                    episode_id,
                    worker_id,
                    operation_id=operation_id,
                    continuation="resume",
                )
                return AutoResearchChildResumeResult(
                    disposition="resumed",
                    child_kind="work",
                    child_id=worker_id,
                    current_operation_id=existing.operation_id,
                    task=existing,
                )
        problem = (
            "the worker was stopped"
            if route.stop_requested_at is not None
            else self._exact_child_resume_problem(previous)
        )
        if problem is not None:
            return AutoResearchChildResumeResult(
                disposition="resume_unavailable",
                child_kind="work",
                child_id=worker_id,
                current_operation_id=previous.operation_id,
                reason=problem,
                replacement_command="spawn",
            )
        episode = self._auto_research_parent_episode(episode_id)
        assert previous.native_session_id is not None
        assert previous.stage_root is not None
        request = self._request_from_record(previous)
        if not isinstance(request, RunRequest):
            raise ValueError("The routed worker task is not an ordinary Work request.")
        request = request.model_copy(update={"session_id": previous.native_session_id})
        operation_id = operation_id or str(uuid.uuid4())
        dispatch_authority = self._resolved_dispatch_authority(
            "node_chat",
            request,
            project_id=previous.project_id,
            parent=previous,
            operation_id=operation_id,
            continuation="resume",
        )
        assert episode.authorized_by is not None
        now = self.store.now()
        task = AgentTaskRecord(
            operation_id=operation_id,
            project_id=previous.project_id,
            episode_id=episode_id,
            graph_target=episode.graph_target,
            kind="node_chat",
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Waiting for the spawned Work task to resume.",
            attempt=previous.attempt + 1,
            parent_operation_id=previous.operation_id,
            native_session_id=previous.native_session_id,
            stage_host=previous.stage_host,
            stage_root=previous.stage_root,
            estimate_seconds=previous.estimate_seconds,
            estimate_samples=previous.estimate_samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=episode.authorized_by,
            dispatch_authority=dispatch_authority,
        )
        try:
            _, stored = self.store.create_auto_research_child_work_recovery(worker_id, task)
        except ValueError:
            existing = self.store.agent_task(operation_id)
            if existing is None:
                raise
            self._validate_existing_child_work_resume(
                episode_id,
                worker_id,
                operation_id,
                existing,
            )
            existing = self.ensure_auto_research_child_work_spawned(
                episode_id,
                worker_id,
                operation_id=operation_id,
                continuation="resume",
            )
            return AutoResearchChildResumeResult(
                disposition="resumed",
                child_kind="work",
                child_id=worker_id,
                current_operation_id=existing.operation_id,
                task=existing,
            )
        spawned = self.ensure_auto_research_child_work_spawned(
            episode_id,
            worker_id,
            operation_id=stored.operation_id,
            continuation="resume",
        )
        return AutoResearchChildResumeResult(
            disposition="resumed",
            child_kind="work",
            child_id=worker_id,
            current_operation_id=spawned.operation_id,
            task=spawned,
        )

    def start_auto_research_child_experiment(
        self,
        route: AutoResearchChildExperimentRecord,
        request: RunRequest,
        *,
        admission_id: str | None = None,
    ) -> AgentTaskRecord:
        """Launch invocation one through the ordinary Experiment task stream."""

        parent = self._auto_research_parent_episode(route.auto_research_episode_id)
        if set(route.request) != {"goal", "invocation_limit"}:
            raise ValueError("The child Experiment route has an invalid launch intent.")
        goal = route.request["goal"]
        invocation_limit = route.request["invocation_limit"]
        if goal is not None and (not isinstance(goal, str) or not goal.strip()):
            raise ValueError("The child Experiment route has an invalid goal snapshot.")
        if invocation_limit is not None and (
            not isinstance(invocation_limit, int)
            or isinstance(invocation_limit, bool)
            or invocation_limit < 1
        ):
            raise ValueError("The child Experiment route has an invalid invocation limit.")
        expected_goal_sha256 = (
            hashlib.sha256(goal.encode("utf-8")).hexdigest() if isinstance(goal, str) else None
        )
        if (
            route.state != "running"
            or route.project_id != parent.project_id
            or request.mode != "work"
            or request.trigger != "orchestrator"
            or request.patch_kind != "experiment_loop"
            or request.chat_scope != "node"
            or request.node_id != route.control_node_id
            or request.control_node_id != route.control_node_id
            or request.control_episode_id != route.child_episode_id
            or request.control_invocation != 1
            or request.message != experiment_start_message(goal, route.control_node_id)
            or (
                invocation_limit is not None
                and request.control_invocation_ceiling != invocation_limit
            )
            or request.session_id is not None
            or request.watcher_ids
        ):
            raise ValueError(
                "An Auto-research child Experiment must be its routed fresh invocation one."
            )
        if route.goal_sha256 != expected_goal_sha256:
            raise ValueError("The child Experiment goal changed after command admission.")
        operation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"rcp:auto-research-child-experiment:{route.child_episode_id}",
            )
        )
        dispatch_authority = self._resolved_dispatch_authority(
            "node_chat",
            request,
            project_id=route.project_id,
            operation_id=operation_id,
        )
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(
            route.project_id,
            "node_chat",
            request_data,
        )
        assert parent.authorized_by is not None
        now = self.store.now()
        task = AgentTaskRecord(
            operation_id=operation_id,
            project_id=route.project_id,
            episode_id=route.child_episode_id,
            graph_target=parent.graph_target,
            kind="node_chat",
            status="queued",
            request=request_data,
            created_at=now,
            updated_at=now,
            status_message="Waiting for the bounded Experiment to start.",
            estimate_seconds=estimate,
            estimate_samples=samples,
            phase="queued",
            last_activity_at=now,
            authorized_by=parent.authorized_by,
            dispatch_authority=dispatch_authority,
        )
        stored = self.store.create_experiment_episode_with_invocation(
            task,
            request.watcher_ids,
            auto_research_route=route,
            auto_research_admission_id=admission_id,
        )
        return self.ensure_auto_research_child_experiment_spawned(
            route.auto_research_episode_id,
            route.child_episode_id,
            operation_id=stored.operation_id,
            continuation="fresh",
        )

    def ensure_auto_research_child_experiment_spawned(
        self,
        parent_episode_id: str,
        child_episode_id: str,
        *,
        operation_id: str,
        continuation: Literal["fresh", "resume", "graph_repair", "watcher_wake"],
    ) -> AgentTaskRecord:
        """Dispatch one committed child Experiment attempt exactly once in-process."""

        existing = self._require_operation(operation_id)
        request = self._request_from_record(existing)
        if not isinstance(request, RunRequest) or request.patch_kind != "experiment_loop":
            raise ValueError("The child Experiment task lost its Work request contract.")
        if continuation == "fresh":
            self._validate_existing_child_experiment_fresh(
                parent_episode_id,
                child_episode_id,
                operation_id,
                existing,
            )
        elif continuation == "resume":
            self._validate_existing_child_experiment_resume(
                parent_episode_id,
                child_episode_id,
                operation_id,
                existing,
            )
        elif continuation == "graph_repair":
            self._validate_existing_child_experiment_graph_repair(
                parent_episode_id,
                child_episode_id,
                operation_id,
                existing,
            )
        else:
            self._validate_existing_child_experiment_watcher_wake(
                parent_episode_id,
                child_episode_id,
                operation_id,
                existing,
            )
        return self.launch_admitted(existing.operation_id)

    def resume_auto_research_child_experiment(
        self,
        parent_episode_id: str,
        child_episode_id: str,
        *,
        operation_id: str | None = None,
    ) -> AutoResearchChildResumeResult:
        """Resume the newest child Experiment attempt without spending E again."""

        route = self.store.auto_research_child_experiment(child_episode_id)
        if route is None:
            raise KeyError(child_episode_id)
        if route.auto_research_episode_id != parent_episode_id:
            raise ValueError("The Experiment is not registered to this Auto-research episode.")
        if operation_id is not None:
            existing = self.store.agent_task(operation_id)
            if existing is not None:
                existing = self.ensure_auto_research_child_experiment_spawned(
                    parent_episode_id,
                    child_episode_id,
                    operation_id=operation_id,
                    continuation="resume",
                )
                return AutoResearchChildResumeResult(
                    disposition="resumed",
                    child_kind="experiment",
                    child_id=child_episode_id,
                    current_operation_id=existing.operation_id,
                    task=existing,
                )
        tasks = self.store.episode_tasks(child_episode_id)
        if not tasks:
            raise ValueError("The child Experiment route has no task to resume.")
        previous = tasks[-1]
        problem = (
            f"the child Experiment route is {route.state}"
            if route.state != "running"
            else self._exact_child_resume_problem(previous)
        )
        if problem is None:
            problem = self.store.experiment_episode_recovery_context_problem(previous.operation_id)
        if problem is not None:
            return AutoResearchChildResumeResult(
                disposition="resume_unavailable",
                child_kind="experiment",
                child_id=child_episode_id,
                current_operation_id=previous.operation_id,
                reason=problem,
                replacement_command="episode --kick-off-experiment",
            )
        assert previous.native_session_id is not None
        request = self._request_from_record(previous)
        if not isinstance(request, RunRequest) or request.patch_kind != "experiment_loop":
            raise ValueError("The child Experiment task lost its Work request contract.")
        request = request.model_copy(update={"session_id": previous.native_session_id})
        operation_id = operation_id or str(uuid.uuid4())
        try:
            resumed = self._create_and_spawn(
                previous.project_id,
                previous.kind,
                request,
                parent=previous,
                continuation="resume",
                estimate_seconds=previous.estimate_seconds,
                estimate_samples=previous.estimate_samples,
                stage_host=previous.stage_host,
                stage_root=previous.stage_root,
                operation_id=operation_id,
            )
        except ValueError:
            existing = self.store.agent_task(operation_id)
            if existing is None:
                raise
            self._validate_existing_child_experiment_resume(
                parent_episode_id,
                child_episode_id,
                operation_id,
                existing,
            )
            resumed = self.ensure_auto_research_child_experiment_spawned(
                parent_episode_id,
                child_episode_id,
                operation_id=operation_id,
                continuation="resume",
            )
        assert resumed is not None
        return AutoResearchChildResumeResult(
            disposition="resumed",
            child_kind="experiment",
            child_id=child_episode_id,
            current_operation_id=resumed.operation_id,
            task=resumed,
        )

    def stop_auto_research(self, episode_id: str) -> EpisodeRecord:
        """Persist Stop without cancelling the already-authorized actor turn."""

        before = self.store.episode(episode_id)
        if before is None or before.mode != "auto_research":
            raise KeyError(episode_id)
        stopped = request_auto_research_stop(self.store, episode_id)
        if (
            before.stop_requested_at is None
            and stopped.stop_requested_at is not None
            and stopped.root_operation_id is not None
        ):
            self.store.record_agent_task_event(
                stopped.root_operation_id,
                "Auto-research Stop requested; current turns will finish and no new work will "
                "start.",
            )
        return settle_auto_research_stop(self.store, episode_id) or stopped

    def pending_auto_research_mail(
        self,
        *,
        episode_id: str,
        recipient_task_id: str,
    ) -> PendingAutoResearchMail:
        return pending_auto_research_mail(
            self.store,
            episode_id=episode_id,
            recipient_task_id=recipient_task_id,
        )

    def start_episode_report(self, episode_id: str) -> AgentTaskRecord | None:
        """Launch or restart the one durable hidden allocation for an episode."""

        episode = self.store.episode(episode_id)
        if episode is None:
            raise KeyError(episode_id)
        if episode.status != "wrapping_up" or episode.wrapup_state not in {"pending", "running"}:
            return None
        wrapup = self.store.episode_wrapup(episode_id)
        if wrapup is None or wrapup.allocation_operation_id is None:
            raise ValueError("The episode report lost its durable allocation fence.")
        existing = self.store.agent_task(wrapup.allocation_operation_id)
        if existing is not None and existing.status in {"queued", "running", "pausing"}:
            with self._controls_lock:
                worker = self._workers.get(existing.operation_id)
                if worker is not None:
                    return existing
            if existing.status != "queued":
                return None
        task = self.store.requeue_interrupted_episode_report_allocation(episode_id)
        if task.status != "queued":
            return None
        if task.kind != "episode_report" or task.visible or task.episode_id != episode_id:
            raise ValueError("The episode report allocation lost its hidden task boundary.")
        request = EpisodeReportRunRequest.model_validate(task.request)
        if request.episode_id != episode_id:
            raise ValueError("The episode report request changed its parent episode.")
        self._require_operation(task.parent_operation_id or "")
        return self.launch_admitted(task.operation_id)

    def _restart_interrupted_episode_reports(self) -> None:
        for episode in self.store.episodes_awaiting_report():
            with suppress(KeyError, RuntimeError, ValueError):
                self.start_episode_report(episode.episode_id)

    def _restart_stopping_experiment_recoveries(self) -> None:
        """Let an already-authorized Experiment turn finish behind its Stop fence.

        Process restart converts a live turn to ``interrupted``.  Stop must keep
        new invocations fenced, but that interruption must not strand the turn
        that was already authorized: when its exact RCP-owned session and stage
        are still usable, recover it without spending another invocation.  A
        concrete continuation problem is persisted so the existing Stop adapter
        can take its established abandonment path instead.
        """

        for previous in self.store.stopping_experiment_recovery_candidates():
            try:
                request = self._request_from_record(previous)
                if (
                    not isinstance(request, RunRequest)
                    or request.patch_kind != "experiment_loop"
                    or not request.control_episode_id
                    or not request.control_node_id
                ):
                    continue
                episode = self.store.episode(request.control_episode_id)
                if (
                    episode is None
                    or episode.mode != "experiment_loop"
                    or episode.project_id != previous.project_id
                    or episode.control_node_id != request.control_node_id
                    or episode.stop_requested_at is None
                    or episode.stop_settled_at is not None
                ):
                    continue
                problem = self.store.experiment_episode_recovery_context_problem(
                    previous.operation_id
                )
                if problem is None:
                    if self._failure_is_session_limit(previous):
                        problem = "the saved provider session reached its limit"
                    elif self._continuation_context_is_unavailable(previous):
                        problem = "the saved continuation context is unavailable"
                    elif (
                        not previous.native_session_id
                        or not previous.stage_root
                        or not self._session_is_rcp_owned(previous)
                    ):
                        problem = "the turn has no complete RCP-owned session and stage"
                    elif episode.authorized_by is None:
                        problem = "the episode lost its human authorizer snapshot"
                    else:
                        try:
                            if previous.stage_host:
                                available = RemoteRunStage(previous.stage_host).directory_exists(
                                    previous.stage_root
                                )
                            else:
                                stage = Path(previous.stage_root)
                                available = stage.is_dir() and not stage.is_symlink()
                        except Exception:
                            # A remote transport outage is not evidence that the
                            # saved continuation is unusable. Leave Stop pending
                            # for a later process/reconciliation pass.
                            continue
                        if available is None:
                            continue
                        if available is not True:
                            problem = "the saved provider workspace is unavailable"
                if problem is not None:
                    self.store.record_experiment_episode_diagnostic(
                        episode_id=episode.episode_id,
                        project_id=episode.project_id,
                        control_node_id=request.control_node_id,
                        diagnostic=(
                            "Stop loop cannot finish its already-authorized turn because "
                            + problem.rstrip(".")
                            + "."
                        ),
                    )
                    continue
                recovered = (
                    self.retry(
                        previous.operation_id,
                        authorized_by=episode.authorized_by,
                    )
                    if previous.status == "failed"
                    else self.resume(previous.operation_id)
                )
                self.store.record_agent_task_receipt(
                    recovered.operation_id,
                    "experiment_stop_recovery",
                    {
                        "episode_id": episode.episode_id,
                        "recovered_operation_id": previous.operation_id,
                    },
                    tier="summary",
                )
                self.store.record_agent_task_event(
                    recovered.operation_id,
                    "Resuming the already-authorized Experiment turn so its graceful Stop can "
                    "settle.",
                )
            except (KeyError, RuntimeError, ValueError):
                # Startup recovery is best effort. A transaction race or a
                # temporarily unreachable remote stage remains retryable on the
                # next reconciliation rather than preventing the app from opening.
                continue

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

        source_watchers = [self.store.watcher(watcher_id) for watcher_id in watcher_ids]
        if any(item is None for item in source_watchers):
            raise ValueError("A watcher notification requires every durable watcher record.")
        resolved_watchers = [item for item in source_watchers if item is not None]
        graph_targets = {item.graph_target.key: item.graph_target for item in resolved_watchers}
        if len(graph_targets) != 1:
            raise ValueError("A watcher notification cannot cross graph targets.")
        graph_target = next(iter(graph_targets.values()))
        branch_episode_ids = {
            item.episode_id for item in resolved_watchers if item.episode_id is not None
        }
        if graph_target.kind == "branch" and len(branch_episode_ids) != 1:
            raise ValueError("A branch watcher notification requires one exact episode lineage.")

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
        episode: EpisodeRecord | None = None
        if experiment_wake:
            episode = self.store.episode(request.control_episode_id or "")
            if (
                episode is None
                or episode.mode != "experiment_loop"
                or episode.project_id != project_id
                or episode.graph_target != graph_target
            ):
                raise ValueError("The Experiment watcher wake lost its episode parent.")
            if episode.authorized_by is None:
                raise ValueError("The Experiment episode lost its human authorizer snapshot.")
            authorized_by = episode.authorized_by
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
            episode_id=request.control_episode_id
            if experiment_wake or experiment_reauthorization
            else next(iter(branch_episode_ids))
            if graph_target.kind == "branch"
            else None,
            graph_target=graph_target,
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
                if experiment_reauthorization:
                    stored = self.store.create_experiment_episode_with_invocation(
                        record,
                        watcher_ids,
                    )
                elif experiment_wake:
                    stored = self.store.create_experiment_watcher_invocation(
                        record,
                        watcher_ids,
                    )
                else:
                    stored = self.store.create_watcher_notification_task(
                        record,
                        watcher_ids,
                        continuation_cause="fresh",
                    )
                if stored is None:
                    return
                started = self.launch_admitted(stored.operation_id)

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
        if previous.kind == "episode_report":
            raise ValueError("Episode report recovery is automatic and has no Resume control.")
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
        if previous.kind == "episode_report":
            raise ValueError("Episode report recovery is automatic and has no Retry control.")
        if not previous.can_retry:
            raise ValueError("Only a paused, interrupted, or failed task can be retried.")
        original = self._request_from_record(previous)
        if isinstance(original, AutoResearchRunRequest):
            return self._retry_auto_research_task(
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

    def _retry_auto_research_task(
        self,
        previous: AgentTaskRecord,
        original: AutoResearchRunRequest,
        *,
        provider: str | None,
        model: str | None,
        reasoning: str | None,
        run_on: str | None,
        skills: SkillSelection | None,
    ) -> AgentTaskRecord:
        """Recover one paid Auto-research allocation without changing its binding."""

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
                "Auto-research recovery cannot change its pinned " + ", ".join(changed) + "."
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
            episode = self.store.episode(original.episode_id)
            if episode is not None and episode.stop_requested_at is not None:
                self.store.abandon_auto_research_recovery(
                    previous.operation_id,
                    diagnostic=problem,
                )
                settle_auto_research_stop(self.store, episode.episode_id, diagnostic=problem)
            raise ValueError(
                "Auto-research recovery cannot start a fresh provider session because "
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
        request = AutoResearchRunRequest.model_validate(
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
                "auto_research_orchestrator_clean_retry",
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
                        episode_id=episode.episode_id,
                        graph_target=episode.graph_target,
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
        request = request.model_copy(
            update={"session_id": previous.native_session_id, "message": None}
        )
        return self._create_and_spawn(
            previous.project_id,
            previous.kind,
            request,
            parent=previous,
            continuation="graph_repair",
            claim_graph_repair_parent=True,
            estimate_seconds=previous.estimate_seconds,
            estimate_samples=previous.estimate_samples,
            stage_host=previous.stage_host,
            stage_root=previous.stage_root,
            authorized_by=authorized_by,
        )

    def pause(self, operation_id: str) -> AgentTaskRecord:
        current = self._require_operation(operation_id)
        if current.kind == "episode_report":
            raise ValueError("Episode report generation has no manual Pause control.")
        record = self.store.request_agent_task_pause(operation_id)
        self._signal_agent_task_pause(operation_id)
        return record

    def _signal_agent_task_pause(self, operation_id: str) -> None:
        """Best-effort re-signal of a pause whose durable intent already exists."""

        with self._controls_lock:
            control = self._controls.get(operation_id)
        if control is not None:
            control.request_pause()

    def pause_auto_research_worker(
        self,
        operation_id: str,
        episode_id: str,
    ) -> AgentTaskRecord:
        """Commit the episode gate before signalling the worker process."""

        record = self.store.request_auto_research_worker_pause(operation_id, episode_id)
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

    def _auto_research_for_request(
        self,
        episode_id: str,
        request: AutoResearchRunRequest,
    ) -> EpisodeRecord:
        if request.episode_id != episode_id:
            raise ValueError("Auto-research request does not match its episode lineage.")
        episode = self.store.episode(episode_id)
        if (
            episode is None
            or episode.mode != "auto_research"
            or self.store.auto_research_state(episode_id) is None
        ):
            raise KeyError(episode_id)
        if (
            episode.graph_target.kind != "branch"
            or episode.graph_target.branch_id != episode.episode_id
            or episode.graph_base_head is None
            or episode.graph_base_head.target.kind != "main"
        ):
            raise ValueError(
                "The Auto-research episode has no exact canonical graph-branch binding."
            )
        return episode

    def _auto_research_parent_episode(self, episode_id: str) -> EpisodeRecord:
        episode = self.store.episode(episode_id)
        if (
            episode is None
            or episode.mode != "auto_research"
            or self.store.auto_research_state(episode_id) is None
        ):
            raise KeyError(episode_id)
        if (
            episode.graph_target.kind != "branch"
            or episode.graph_target.branch_id != episode.episode_id
            or episode.graph_base_head is None
            or episode.graph_base_head.target.kind != "main"
        ):
            raise ValueError(
                "The Auto-research episode has no exact canonical graph-branch binding."
            )
        if episode.authorized_by is None:
            raise ValueError("The Auto-research episode lost its human authorizer snapshot.")
        return episode

    def _exact_child_resume_problem(self, record: AgentTaskRecord) -> str | None:
        if record.status not in {"paused", "interrupted", "failed"}:
            return "only a paused, interrupted, or failed attempt can be resumed"
        if self._failure_is_session_limit(record):
            return "the saved provider session reached its limit"
        if self._continuation_context_is_unavailable(record):
            return "the saved continuation context is unavailable"
        if (
            not record.native_session_id
            or not record.stage_root
            or not self._session_is_rcp_owned(record)
        ):
            return "the attempt has no complete RCP-owned session and stage"
        if record.stage_host:
            try:
                available = RemoteRunStage(record.stage_host).directory_exists(record.stage_root)
            except Exception as exc:
                raise OSError(
                    "The saved provider workspace could not be checked because its remote "
                    "infrastructure is unavailable."
                ) from exc
            if available is None:
                raise OSError(
                    "The saved provider workspace could not be checked because its remote "
                    "infrastructure is unavailable."
                )
        else:
            try:
                stage = Path(record.stage_root)
                available = stage.is_dir() and not stage.is_symlink()
            except Exception:
                available = False
        if available is not True:
            return "the saved provider workspace is unavailable"
        return None

    def _validate_existing_child_work_fresh(
        self,
        episode_id: str,
        worker_id: str,
        operation_id: str,
        existing: AgentTaskRecord,
    ) -> None:
        """Prove a committed row is the routed worker's immutable fresh launch."""

        route = self.store.auto_research_child_work(worker_id)
        request = self._request_from_record(existing)
        if (
            route is None
            or route.episode_id != episode_id
            or route.worker_id != worker_id
            or route.root_operation_id != operation_id
            or existing.operation_id != operation_id
            or existing.project_id != route.project_id
            or existing.episode_id != episode_id
            or existing.kind != "node_chat"
            or existing.parent_operation_id is not None
            or not isinstance(request, RunRequest)
            or request.mode != "work"
            or request.trigger != "orchestrator"
            or request.patch_kind != "work"
            or request.chat_scope != "node"
            or request.chat_id != worker_id
            or request.node_id != route.control_node_id
            or request.message != route.instruction
            or hashlib.sha256(route.instruction.encode("utf-8")).hexdigest()
            != route.instruction_sha256
            or request.session_id is not None
        ):
            raise ValueError("The deterministic worker operation belongs to another fresh launch.")

    def _validate_existing_auto_research_wake(
        self,
        episode_id: str,
        operation_id: str,
        existing: AgentTaskRecord,
        request: AutoResearchRunRequest,
    ) -> AgentTaskRecord:
        """Prove a queued wake retains its paid allocation and exact delivery binding."""

        episode = self.store.episode(episode_id)
        invocation = self.store.auto_research_invocation(operation_id)
        parent = (
            self.store.agent_task(existing.parent_operation_id)
            if existing.parent_operation_id is not None
            else None
        )
        parent_invocation = (
            self.store.auto_research_invocation(parent.operation_id) if parent is not None else None
        )
        messages = [
            item
            for item in self.store.auto_research_messages(episode_id)
            if item.delivery_operation_id == operation_id
        ]
        lifecycle = self.store.auto_research_lifecycle_delivery(operation_id)
        watchers = [
            item
            for item in self.store.watchers(existing.project_id)
            if item.notification_operation_id == operation_id
        ]
        actor_operation_id = request.actor_operation_id
        binding_is_exact = False
        if request.wake_cause == "message":
            binding_is_exact = bool(messages) and all(
                item.episode_id == episode_id and item.recipient_task_id == actor_operation_id
                for item in messages
            )
        elif request.wake_cause == "lifecycle":
            binding_is_exact = bool(lifecycle) and all(
                item.episode_id == episode_id for item in lifecycle
            )
            binding_is_exact = binding_is_exact and all(
                item.episode_id == episode_id and item.recipient_task_id == actor_operation_id
                for item in messages
            )
        elif request.wake_cause in {"watcher", "graph_condition"}:
            binding_is_exact = bool(watchers) and {item.watcher_id for item in watchers} == set(
                request.watcher_ids
            )
            binding_is_exact = binding_is_exact and all(
                item.episode_id == episode_id
                and item.origin_task_kind == "auto_research"
                and item.chat_id == actor_operation_id
                for item in watchers
            )
        if (
            episode is None
            or episode.mode != "auto_research"
            or existing.operation_id != operation_id
            or existing.project_id != episode.project_id
            or existing.episode_id != episode_id
            or existing.kind != "auto_research"
            or existing.parent_operation_id is None
            or existing.authorized_by != episode.authorized_by
            or parent is None
            or parent.project_id != existing.project_id
            or parent.episode_id != episode_id
            or parent.kind != "auto_research"
            or invocation is None
            or invocation.episode_id != episode_id
            or invocation.operation_id != operation_id
            or invocation.allocation_operation_id != operation_id
            or invocation.role != request.role
            or invocation.actor_operation_id != actor_operation_id
            or invocation.control_node_id != request.control_node_id
            or parent_invocation is None
            or parent_invocation.episode_id != episode_id
            or parent_invocation.role != invocation.role
            or parent_invocation.actor_operation_id != invocation.actor_operation_id
            or parent_invocation.control_node_id != invocation.control_node_id
            or not request.session_id
            or existing.native_session_id != request.session_id
            or existing.native_session_id != parent.native_session_id
            or not existing.stage_root
            or existing.stage_root != parent.stage_root
            or (existing.stage_host or "") != (parent.stage_host or "")
            or not binding_is_exact
        ):
            raise ValueError(
                "The committed Auto-research wake lost its allocation or delivery binding."
            )
        return parent

    def _validate_existing_child_experiment_fresh(
        self,
        parent_episode_id: str,
        child_episode_id: str,
        operation_id: str,
        existing: AgentTaskRecord,
    ) -> None:
        """Prove a committed row is the child Experiment's immutable invocation one."""

        route = self.store.auto_research_child_experiment(child_episode_id)
        episode = self.store.episode(child_episode_id)
        request = self._request_from_record(existing)
        goal = route.request.get("goal") if route is not None else None
        invocation_limit = route.request.get("invocation_limit") if route is not None else None
        expected_goal_sha256 = (
            hashlib.sha256(goal.encode("utf-8")).hexdigest() if isinstance(goal, str) else None
        )
        if (
            route is None
            or route.auto_research_episode_id != parent_episode_id
            or route.child_episode_id != child_episode_id
            or route.state != "running"
            or episode is None
            or episode.mode != "experiment_loop"
            or episode.root_operation_id != operation_id
            or episode.project_id != route.project_id
            or episode.control_node_id != route.control_node_id
            or existing.operation_id != operation_id
            or existing.project_id != route.project_id
            or existing.episode_id != child_episode_id
            or existing.kind != "node_chat"
            or existing.parent_operation_id is not None
            or not isinstance(request, RunRequest)
            or request.mode != "work"
            or request.trigger != "orchestrator"
            or request.patch_kind != "experiment_loop"
            or request.chat_scope != "node"
            or request.node_id != route.control_node_id
            or request.control_node_id != route.control_node_id
            or request.control_episode_id != child_episode_id
            or request.control_invocation != 1
            or request.message != experiment_start_message(goal, route.control_node_id)
            or (
                invocation_limit is not None
                and request.control_invocation_ceiling != invocation_limit
            )
            or route.goal_sha256 != expected_goal_sha256
            or request.session_id is not None
        ):
            raise ValueError(
                "The deterministic Experiment operation belongs to another fresh launch."
            )

    def _validate_existing_child_work_resume(
        self,
        episode_id: str,
        worker_id: str,
        operation_id: str,
        existing: AgentTaskRecord,
    ) -> None:
        """Prove a deterministic operation id is this worker's exact Resume."""

        route = self.store.auto_research_child_work_for_operation(operation_id)
        parent = (
            self.store.agent_task(existing.parent_operation_id)
            if existing.parent_operation_id is not None
            else None
        )
        request = self._request_from_record(existing)
        if (
            route is None
            or route.worker_id != worker_id
            or route.episode_id != episode_id
            or existing.operation_id != operation_id
            or existing.project_id != route.project_id
            or existing.episode_id != episode_id
            or existing.kind != "node_chat"
            or parent is None
            or parent.project_id != existing.project_id
            or parent.episode_id != episode_id
            or existing.attempt != parent.attempt + 1
            or existing.native_session_id != parent.native_session_id
            or (existing.stage_host or "") != (parent.stage_host or "")
            or existing.stage_root != parent.stage_root
            or not isinstance(request, RunRequest)
            or request.session_id != parent.native_session_id
            or request.chat_id != parent.request.get("chat_id")
        ):
            raise ValueError(
                "The deterministic worker Resume operation belongs to another recovery."
            )

    def _validate_existing_child_work_message_wake(
        self,
        episode_id: str,
        worker_id: str,
        operation_id: str,
        existing: AgentTaskRecord,
    ) -> None:
        """Prove one queued ordinary Work continuation owns its exact claimed mail."""

        route = self.store.auto_research_child_work_for_operation(operation_id)
        parent = (
            self.store.agent_task(existing.parent_operation_id)
            if existing.parent_operation_id is not None
            else None
        )
        request = self._request_from_record(existing)
        messages = [
            item
            for item in self.store.auto_research_messages(episode_id)
            if item.delivery_operation_id == operation_id
        ]
        episode_invocations = {
            item.operation_id for item in self.store.episode_invocations(episode_id)
        }
        pinned_request_fields = (
            "provider",
            "model",
            "reasoning",
            "run_on",
            "run_truth_scope",
            "chat_scope",
            "node_id",
            "chat_id",
            "mode",
            "patch_kind",
        )
        if (
            route is None
            or route.worker_id != worker_id
            or route.episode_id != episode_id
            or route.current_operation_id != operation_id
            or existing.operation_id != operation_id
            or existing.project_id != route.project_id
            or existing.episode_id != episode_id
            or existing.kind != "node_chat"
            or operation_id not in episode_invocations
            or parent is None
            or parent.project_id != existing.project_id
            or parent.episode_id != episode_id
            or parent.kind != "node_chat"
            or parent.status != "succeeded"
            or existing.attempt != parent.attempt + 1
            or not existing.native_session_id
            or existing.native_session_id != parent.native_session_id
            or (existing.stage_host or "") != (parent.stage_host or "")
            or existing.stage_root != parent.stage_root
            or not existing.stage_root
            or not isinstance(request, RunRequest)
            or request.session_id != parent.native_session_id
            or request.trigger != "orchestrator"
            or request.mode != "work"
            or request.patch_kind != "work"
            or request.message is not None
            or request.watcher_ids
            or request.result_view is not None
            or any(
                existing.request.get(field) != parent.request.get(field)
                for field in pinned_request_fields
            )
            or not messages
            or any(
                item.episode_id != episode_id
                or item.recipient_task_id != worker_id
                or item.delivered_at != existing.created_at
                for item in messages
            )
        ):
            raise ValueError(
                "The committed child Work message wake lost its allocation or mail binding."
            )

    def _validate_existing_child_experiment_resume(
        self,
        parent_episode_id: str,
        child_episode_id: str,
        operation_id: str,
        existing: AgentTaskRecord,
    ) -> None:
        """Prove a deterministic operation id is this child Experiment Resume."""

        route = self.store.auto_research_child_experiment(child_episode_id)
        parent = (
            self.store.agent_task(existing.parent_operation_id)
            if existing.parent_operation_id is not None
            else None
        )
        request = self._request_from_record(existing)
        if (
            route is None
            or route.auto_research_episode_id != parent_episode_id
            or route.child_episode_id != child_episode_id
            or existing.operation_id != operation_id
            or existing.project_id != route.project_id
            or existing.episode_id != child_episode_id
            or existing.kind != "node_chat"
            or parent is None
            or parent.project_id != existing.project_id
            or parent.episode_id != child_episode_id
            or existing.attempt != parent.attempt + 1
            or existing.native_session_id != parent.native_session_id
            or (existing.stage_host or "") != (parent.stage_host or "")
            or existing.stage_root != parent.stage_root
            or not isinstance(request, RunRequest)
            or request.patch_kind != "experiment_loop"
            or request.control_episode_id != child_episode_id
            or request.session_id != parent.native_session_id
        ):
            raise ValueError(
                "The deterministic Experiment Resume operation belongs to another recovery."
            )

    def _validate_existing_child_experiment_graph_repair(
        self,
        parent_episode_id: str,
        child_episode_id: str,
        operation_id: str,
        existing: AgentTaskRecord,
    ) -> None:
        """Prove a committed child continuation is the same patch-only graph repair."""

        self._validate_existing_child_experiment_resume(
            parent_episode_id,
            child_episode_id,
            operation_id,
            existing,
        )
        route = self.store.auto_research_child_experiment(child_episode_id)
        parent = self._require_operation(existing.parent_operation_id or "")
        request = self._request_from_record(existing)
        graph_update = (
            parent.result.get("graph_update") if isinstance(parent.result, dict) else None
        )
        if (
            route is None
            or route.state != "running"
            or self.store.agent_task_continuation_cause(operation_id) != "graph_repair"
            or not isinstance(request, RunRequest)
            or request.message is not None
            or parent.status != "succeeded"
            or not isinstance(graph_update, dict)
            or graph_update.get("status") != "rejected"
            or graph_update.get("repairable") is not False
        ):
            raise ValueError(
                "The committed Experiment graph repair lost its patch-only recovery binding."
            )

    def _validate_existing_child_experiment_watcher_wake(
        self,
        parent_episode_id: str,
        child_episode_id: str,
        operation_id: str,
        existing: AgentTaskRecord,
    ) -> None:
        """Prove a paid child Experiment watcher allocation and exact session binding."""

        route = self.store.auto_research_child_experiment(child_episode_id)
        episode = self.store.episode(child_episode_id)
        request = self._request_from_record(existing)
        invocations = {
            item.operation_id: item.invocation_number
            for item in self.store.episode_invocations(child_episode_id)
        }
        watchers = [
            item
            for item in self.store.watchers(existing.project_id)
            if item.notification_operation_id == operation_id
        ]
        if (
            route is None
            or route.auto_research_episode_id != parent_episode_id
            or route.child_episode_id != child_episode_id
            or route.state != "running"
            or episode is None
            or episode.mode != "experiment_loop"
            or episode.project_id != route.project_id
            or episode.control_node_id != route.control_node_id
            or existing.operation_id != operation_id
            or existing.project_id != route.project_id
            or existing.episode_id != child_episode_id
            or existing.kind != "node_chat"
            or existing.parent_operation_id is not None
            or not isinstance(request, RunRequest)
            or request.mode != "work"
            or request.trigger != "watcher"
            or request.patch_kind != "experiment_loop"
            or request.control_node_id != route.control_node_id
            or request.control_episode_id != child_episode_id
            or request.control_invocation != invocations.get(operation_id)
            or not request.session_id
            or request.session_id != existing.native_session_id
            or not existing.stage_root
            or set(request.watcher_ids) != {item.watcher_id for item in watchers}
            or not watchers
            or any(
                item.project_id != route.project_id
                or item.origin_task_kind != "node_chat"
                or item.chat_id != request.chat_id
                or item.continuation.control_episode_id != child_episode_id
                for item in watchers
            )
        ):
            raise ValueError(
                "The committed child Experiment watcher wake lost its allocation, watcher, "
                "or native-session binding."
            )

    def _auto_research_parent(
        self,
        episode: EpisodeRecord,
        operation_id: str | None,
    ) -> AgentTaskRecord:
        parent_id = operation_id or episode.root_operation_id
        if parent_id is None:
            raise ValueError("Auto-research has no root operation for child lineage.")
        parent = self._require_operation(parent_id)
        if parent.project_id != episode.project_id or parent.episode_id != episode.episode_id:
            raise ValueError("Auto-research child parent is outside the episode lineage.")
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
        auto_research_mail_delivery: PendingAutoResearchMail | None = None,
        auto_research_wake_admission: AutoResearchWakeAdmission | None = None,
        claim_graph_repair_parent: bool = False,
    ) -> AgentTaskRecord | None:
        episode: EpisodeRecord | None = None
        task_graph_target = parent.graph_target if parent is not None else GraphTargetRef()
        if isinstance(request, BranchMergeRunRequest):
            raise TypeError("BranchMergeRunRequest requires start_branch_merge.")
        if isinstance(request, AutoResearchRunRequest):
            if kind != "auto_research":
                raise TypeError("AutoResearchRunRequest requires auto_research task kind.")
            episode = self._auto_research_for_request(request.episode_id, request)
            authorized_by = episode.authorized_by
            task_graph_target = episode.graph_target
        elif isinstance(request, EpisodeReportRunRequest):
            raise TypeError("EpisodeReportRunRequest requires an existing hidden allocation.")
        elif isinstance(request, RunRequest) and request.patch_kind == "experiment_loop":
            stored_episode = self.store.episode(request.control_episode_id or "")
            if stored_episode is not None:
                if (
                    stored_episode.mode != "experiment_loop"
                    or stored_episode.project_id != project_id
                    or stored_episode.control_node_id != request.control_node_id
                ):
                    raise ValueError("The Experiment task changed its episode parent scope.")
                authorized_by = stored_episode.authorized_by
                task_graph_target = stored_episode.graph_target
            elif parent is not None or request.trigger != "experiment_run":
                raise ValueError("The Experiment continuation lost its episode parent.")
        elif auto_research_mail_delivery is not None or auto_research_wake_admission is not None:
            raise ValueError("Only Auto-research may use Auto-research wake admission.")
        if task_graph_capable(kind, request) and authorized_by is None:
            raise ValueError("A patch-capable agent task requires a human authorizer snapshot.")
        if authorized_by is None:
            raise ValueError("An ordinary agent task requires a human authorizer snapshot.")
        if authorized_by is not None and not authorized_by.display_name.strip():
            raise ValueError("A human authorizer snapshot must include a nonblank display name.")
        if claim_graph_repair_parent and (parent is None or continuation != "graph_repair"):
            raise ValueError("Only an initial graph-repair admission can claim its parent.")
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
            episode_id=(
                request.episode_id
                if isinstance(request, AutoResearchRunRequest)
                else request.control_episode_id
                if isinstance(request, RunRequest) and request.patch_kind == "experiment_loop"
                else None
            ),
            graph_target=task_graph_target,
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
        if auto_research_mail_delivery is not None:
            selected_messages = auto_research_mail_claim_prefix(
                episode_id=auto_research_mail_delivery.episode_id,
                recipient_task_id=auto_research_mail_delivery.recipient_task_id,
                delivery_operation_id=task_record.operation_id,
                delivered_at=task_record.created_at,
                messages=auto_research_mail_delivery.messages,
            )
            if not selected_messages:
                return None
            auto_research_mail_delivery = auto_research_mail_delivery.model_copy(
                update={"messages": selected_messages}
            )
        if isinstance(request, AutoResearchRunRequest):
            assert episode is not None
            try:
                if continuation in {"resume", "retry"}:
                    record = self.store.create_auto_research_recovery_task(
                        task_record,
                        continuation_cause=continuation,
                    )
                elif auto_research_mail_delivery is not None:
                    record = self.store.create_auto_research_message_wake_task(
                        task_record,
                        role=request.role,
                        recipient_task_id=auto_research_mail_delivery.recipient_task_id,
                        message_ids=auto_research_mail_delivery.message_ids,
                    )
                elif auto_research_wake_admission is not None:
                    assert request.wake_cause is not None
                    record = auto_research_wake_admission(
                        task_record,
                        request.role,
                        request.wake_cause,
                    )
                else:
                    record = self.store.create_auto_research_agent_task(
                        task_record,
                        role=request.role,
                        continuation_cause=continuation,
                    )
            except EpisodeInvocationCeilingReached:
                self._auto_research_admission_exhausted(episode)
                raise
            if record is None:
                if auto_research_wake_admission is None and auto_research_mail_delivery is None:
                    raise RuntimeError(
                        "Auto-research admission returned no task outside a watcher or mail wake"
                    )
                return None
            if (
                record.operation_id != task_record.operation_id
                or record.project_id != episode.project_id
                or record.episode_id != episode.episode_id
            ):
                raise ValueError("Auto-research wake admission returned another task lineage.")
        elif isinstance(request, RunRequest) and request.patch_kind == "experiment_loop":
            if request.trigger == "experiment_run" and parent is None:
                record = self.store.create_experiment_episode_with_invocation(
                    task_record,
                    request.watcher_ids,
                )
            elif parent is not None and continuation in {
                "resume",
                "retry",
                "handoff",
                "graph_repair",
            }:
                if claim_graph_repair_parent:
                    record = self.store.create_experiment_graph_repair_task(
                        parent.operation_id,
                        task_record,
                    )
                else:
                    record = self.store.create_experiment_recovery_task(
                        task_record,
                        continuation_cause=continuation,
                    )
            else:
                raise ValueError("An Experiment watcher wake must use start_watcher_notification.")
        else:
            if claim_graph_repair_parent:
                assert parent is not None
                record = self.store.create_agent_task_graph_repair(
                    parent.operation_id,
                    task_record,
                )
            else:
                record = self.store.create_agent_task(
                    task_record,
                    continuation_cause=continuation,
                )
        if isinstance(request, AutoResearchRunRequest) and request.wake_cause is not None:
            return self.ensure_auto_research_wake_spawned(
                request.episode_id,
                operation_id=record.operation_id,
            )
        return self.launch_admitted(record.operation_id)

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
        if kind == "branch_merge":
            if not isinstance(request, BranchMergeRunRequest):
                raise TypeError("branch_merge dispatch requires a BranchMergeRunRequest")
            authority = AgentDispatchAuthority(
                profile="orchestrator",
                task_contract="orchestrate",
                scope=AgentDispatchScope(
                    run_truth_scope=list(request.run_truth_scope or ()),
                    episode_id=request.episode_id,
                    patch_kind="work",
                ),
            )
        else:
            authority = self.dispatch_authority_resolver(kind, request)
        if kind == "episode_report":
            if not isinstance(request, EpisodeReportRunRequest):
                raise TypeError("episode_report dispatch requires an EpisodeReportRunRequest")
            if authority is not None:
                raise ValueError(
                    "Authority refused action 'dispatch': an episode report has no graph "
                    "authority binding."
                )
            return None
        if kind == "auto_research":
            if not isinstance(request, AutoResearchRunRequest):
                raise TypeError("auto_research dispatch requires an AutoResearchRunRequest")
            if authority is None:
                raise ValueError(
                    "Authority refused action 'dispatch': the Auto-research actor has no "
                    "authority binding."
                )
        elif authority is None:
            raise ValueError(
                "Authority refused action 'dispatch': the task has no authority binding."
            )
        assert authority is not None
        require_dispatch(authority)

        if kind == "auto_research":
            assert isinstance(request, AutoResearchRunRequest)
            if operation_id is None:
                raise ValueError(
                    "Authority refused action 'dispatch': Auto-research admission has no "
                    "operation id."
                )
            actor_operation_id = request.actor_operation_id
            if parent is None:
                if (
                    request.role != "orchestrator"
                    or actor_operation_id != operation_id
                    or request.wake_cause is not None
                ):
                    raise ValueError(
                        "Authority refused action 'dispatch': an Auto-research root must be its "
                        "sole orchestrator actor."
                    )
                return authority

            stored_parent = self.store.agent_task(parent.operation_id)
            if stored_parent is None:
                raise ValueError(
                    "Authority refused action 'dispatch': the Auto-research parent is missing."
                )
            if (
                stored_parent.project_id != project_id
                or stored_parent.kind != "auto_research"
                or stored_parent.episode_id != request.episode_id
            ):
                raise ValueError(
                    "Authority refused action 'dispatch': an Auto-research continuation must "
                    "preserve its parent project and episode."
                )
            binding = self.store.auto_research_actor_binding(parent.operation_id)
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
                    "Authority refused action 'dispatch': an Auto-research continuation cannot "
                    "change its canonical actor or role."
                )
            origin = self.store.agent_task(binding.actor_operation_id)
            if origin is None:
                raise ValueError(
                    "Authority refused action 'dispatch': the canonical Auto-research actor is "
                    "missing."
                )
            if origin.dispatch_authority is None:
                if continuation not in {"resume", "retry"}:
                    raise ValueError(
                        "Authority refused action 'dispatch': the canonical Auto-research actor "
                        "has no durable authority binding."
                    )
                # A pre-authority Auto-research allocation remains recoverable. Its recovery is
                # still checked against today's closed profile contract before launch.
                return authority
            if authority != origin.dispatch_authority:
                raise ValueError(
                    "Authority refused action 'dispatch': an Auto-research continuation cannot "
                    "change its canonical actor's authority binding."
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

    def launch_admitted(self, operation_id: str) -> AgentTaskRecord:
        """Launch one task whose durable admission already committed.

        Admission and provider dispatch are separate durability boundaries.  The
        caller therefore supplies only the operation identity: the request,
        continuation cause, parent, and launch bindings all come back from the
        durable row and its admission receipt.  This is also the startup repair
        seam for a task that was admitted before the process disappeared.
        """

        record = self._require_operation(operation_id)
        with self._controls_lock:
            if operation_id in self._workers:
                return self._require_operation(operation_id)
        if record.status != "queued":
            return record

        try:
            request = self._request_from_record(record)
            self._validate_request_type(record.kind, request)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "The admitted task has no valid persisted request for its kind."
            ) from exc
        if request.model_dump(mode="json") != record.request:
            raise ValueError("The admitted task request failed its persisted roundtrip.")

        intent = self.store.agent_task_admission_intent(operation_id)
        if intent is None:
            raise ValueError("The admitted task has no durable admission intent.")
        cause = intent.get("continuation_cause")
        if not isinstance(cause, str) or cause not in _AGENT_TASK_CONTINUATIONS:
            raise ValueError("The admitted task has no valid continuation cause.")
        intent_parent = intent.get("parent_operation_id")
        if "parent_operation_id" in intent:
            if intent_parent != record.parent_operation_id:
                raise ValueError("The admission intent changed its exact parent operation.")
        elif intent.get("has_parent") != (record.parent_operation_id is not None):
            raise ValueError("The legacy admission intent changed its parent presence.")

        parent = None
        if record.parent_operation_id is not None:
            parent = self.store.agent_task(record.parent_operation_id)
            if parent is None:
                raise ValueError("The admitted task lost its exact persisted parent.")

        continuation = cast(AgentTaskContinuation, cause)
        self._validate_launch_admission(
            record,
            request,
            parent=parent,
        )

        # The proof can race another in-process launcher.  Re-check the registry
        # after a negative proof so a claimed or already-advanced row is treated
        # as an idempotent duplicate rather than as an ambiguous dispatch.
        if not self.store.agent_task_dispatch_was_proven_not_started(operation_id):
            latest = self._require_operation(operation_id)
            with self._controls_lock:
                if operation_id in self._workers:
                    return self._require_operation(operation_id)
            if latest.status != "queued":
                return latest
            raise ValueError(
                "The admitted task has an ambiguous or already-started dispatch attempt."
            )

        return self._spawn_record(
            record,
            request,
            continuation=continuation,
            parent=parent,
        )

    def _validate_launch_admission(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        *,
        parent: AgentTaskRecord | None,
    ) -> None:
        """Validate immutable bindings before the first dispatch receipt."""

        if record.authorized_by is None or not record.authorized_by.display_name.strip():
            raise ValueError("The admitted task lost its human authorizer snapshot.")
        if record.native_session_id != request.session_id:
            raise ValueError("The admitted task request and native session do not agree.")
        if record.stage_host is not None and record.stage_root is None:
            raise ValueError("The admitted task has an incoherent execution stage binding.")
        if record.stage_root is not None and not record.stage_root.strip():
            raise ValueError("The admitted task has an empty execution stage root.")
        if record.write_scope_fingerprint is not None:
            raise ValueError("A queued admitted task cannot already carry a write-scope binding.")

        if record.kind == "episode_report":
            if record.dispatch_authority is not None:
                raise ValueError("An episode report task cannot carry dispatch authority.")
        else:
            if record.dispatch_authority is None:
                raise ValueError("The admitted task has no dispatch authority.")
            require_dispatch(record.dispatch_authority)

        request_episode_id = (
            request.episode_id
            if isinstance(
                request,
                (AutoResearchRunRequest, BranchMergeRunRequest, EpisodeReportRunRequest),
            )
            else request.control_episode_id
            if isinstance(request, RunRequest) and request.patch_kind == "experiment_loop"
            else None
        )
        if request_episode_id is not None and request_episode_id != record.episode_id:
            raise ValueError("The admitted task request changed its exact episode identity.")
        if record.episode_id is None:
            if record.graph_target.kind != "main":
                raise ValueError("A task without an episode must target the main graph.")
        else:
            episode = self.store.episode(record.episode_id)
            if episode is None:
                raise ValueError("The admitted task lost its exact episode parent.")
            if (
                episode.project_id != record.project_id
                or episode.graph_target != record.graph_target
            ):
                raise ValueError("The admitted task changed its episode project or graph target.")

        expected_parent = record.parent_operation_id
        if (expected_parent is None) != (parent is None):
            raise ValueError("The admitted task changed its parent presence.")
        if parent is not None:
            if expected_parent != parent.operation_id:
                raise ValueError("The admitted task changed its exact parent operation.")
            if parent.project_id != record.project_id or parent.graph_target != record.graph_target:
                raise ValueError("The admitted task changed its parent project or graph target.")

    def _spawn_record(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        *,
        continuation: AgentTaskContinuation,
        parent: AgentTaskRecord | None = None,
    ) -> AgentTaskRecord:
        operation_id = record.operation_id
        with self._controls_lock:
            if operation_id in self._workers:
                return self._require_operation(operation_id)
            current = self._validated_spawn_record(record, request, parent=parent)
            if current.status != "queued":
                return current
            control = AgentProcessControl()
            worker = threading.Thread(
                target=self._run,
                args=(current, request, control, continuation),
                name=f"rcp-{current.kind}-{operation_id[:8]}",
                daemon=True,
            )
            # Registry membership is the one in-process dispatch claim.  It is
            # installed before any observable launch work so concurrent command
            # reconciliation cannot create a second thread for the same row.
            self._controls[operation_id] = control
            self._workers[operation_id] = worker
            dispatch_attempt_id = str(uuid.uuid4())
            try:
                self.store.record_agent_task_receipt(
                    operation_id,
                    "operation_dispatch_attempt",
                    {"dispatch_attempt_id": dispatch_attempt_id},
                    tier="diagnostic",
                )
                self._record_spawn_dispatch(
                    current,
                    request,
                    continuation=continuation,
                    parent=parent,
                )
                worker.start()
            except Exception:
                with suppress(Exception):
                    self.store.record_agent_task_receipt(
                        operation_id,
                        "operation_dispatch_failed_before_start",
                        {"dispatch_attempt_id": dispatch_attempt_id},
                        tier="diagnostic",
                    )
                if self._workers.get(operation_id) is worker:
                    self._controls.pop(operation_id, None)
                    self._workers.pop(operation_id, None)
                raise
            with suppress(Exception):
                self.store.record_agent_task_receipt(
                    operation_id,
                    "operation_dispatch_started",
                    {"dispatch_attempt_id": dispatch_attempt_id},
                    tier="diagnostic",
                )
        return current

    def _validated_spawn_record(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        *,
        parent: AgentTaskRecord | None,
    ) -> AgentTaskRecord:
        current = self._require_operation(record.operation_id)
        if (
            current.project_id != record.project_id
            or current.episode_id != record.episode_id
            or current.graph_target != record.graph_target
            or current.kind != record.kind
            or current.request != record.request
            or current.request != request.model_dump(mode="json")
            or current.attempt != record.attempt
            or current.parent_operation_id != record.parent_operation_id
            or (parent is None) != (current.parent_operation_id is None)
            or (parent is not None and current.parent_operation_id != parent.operation_id)
            or current.native_session_id != record.native_session_id
            or current.stage_host != record.stage_host
            or current.stage_root != record.stage_root
            or current.write_scope_fingerprint != record.write_scope_fingerprint
            or current.authorized_by != record.authorized_by
            or current.dispatch_authority != record.dispatch_authority
        ):
            raise ValueError("The committed task changed before background dispatch.")
        try:
            current_request = self._request_from_record(current)
            self._validate_request_type(current.kind, current_request)
        except (TypeError, ValueError) as exc:
            raise ValueError("The committed task lost its persisted request contract.") from exc
        if current_request.model_dump(mode="json") != current.request:
            raise ValueError("The committed task request failed its persisted roundtrip.")
        return current

    def _record_spawn_dispatch(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        *,
        continuation: AgentTaskContinuation,
        parent: AgentTaskRecord | None,
    ) -> None:
        operation_id = record.operation_id
        reuses_native_checkpoint = continuation in _NATIVE_CHECKPOINT_CONTINUATIONS
        already_recorded = any(
            receipt.category == "operation_created"
            for receipt in self.store.agent_task_receipts(operation_id)
        )
        if already_recorded:
            return
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
            and isinstance(request, AutoResearchRunRequest)
            and continuation not in {"resume", "retry", "handoff", "graph_repair"}
        ):
            label = {
                "fresh": f"Auto-research {request.role} turn",
                "watcher_wake": "Auto-research watcher wake",
                "graph_condition_wake": "Auto-research graph-condition wake",
                "message_wake": "Auto-research message wake",
                "lifecycle_wake": "Auto-research lifecycle wake",
                "auto_research_continuation": "Auto-research human-authorized continuation",
            }[continuation]
            self.store.record_agent_task_event(
                operation_id,
                f"{label} queued from task {parent.operation_id[:8]}.",
            )
        elif parent and isinstance(request, EpisodeReportRunRequest):
            self.store.record_agent_task_event(
                operation_id,
                "Wrapping up visualization and report",
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
            self._forget_control(operation_id)
            return
        if current.status == "pausing" or control.pause_requested.is_set():
            self.store.pause_agent_task(operation_id)
            execution = AgentTaskExecution(
                operation_id=operation_id,
                store=self.store,
                control=control,
                stage_host=record.stage_host,
                stage_root=record.stage_root,
                write_scope_fingerprint=record.write_scope_fingerprint,
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
            write_scope_fingerprint=record.write_scope_fingerprint,
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
                isinstance(request, AutoResearchRunRequest)
                and request.role == "orchestrator"
                and isinstance(exc, AutoResearchOrchestratorTerminalFailure)
            ):
                record_structural_failure(
                    self,
                    operation_id=operation_id,
                    diagnostic=str(exc),
                )
            elif (
                isinstance(request, EpisodeReportRunRequest)
                or (isinstance(request, AutoResearchRunRequest) and request.role == "orchestrator")
            ) and isinstance(exc, TaskFailed):
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
            current = self.store.agent_task(operation_id)
            report_already_finalized = (
                isinstance(request, EpisodeReportRunRequest)
                and current is not None
                and current.status in {"succeeded", "failed"}
            )
            if not report_already_finalized:
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
                if outcome.graph_updates:
                    result["graph_updates"] = [
                        item.model_dump(mode="json") for item in outcome.graph_updates
                    ]
                current = self.store.agent_task(operation_id)
                report_already_finalized = (
                    isinstance(request, EpisodeReportRunRequest)
                    and current is not None
                    and current.status in {"succeeded", "failed"}
                )
                if not report_already_finalized:
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
        if isinstance(request, EpisodeReportRunRequest):
            return
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
        if isinstance(request, AutoResearchRunRequest):
            episode = self.store.episode(request.episode_id)
            if episode is None:
                return
            if episode.stop_requested_at is not None:
                settled = settle_auto_research_stop(self.store, episode.episode_id)
                if settled is not None:
                    episode = settled
            if self.on_auto_research_task_settled is None:
                return
            try:
                self.on_auto_research_task_settled(episode, request, execution)
            except Exception as exc:
                with suppress(Exception):
                    self.store.record_agent_task_receipt(
                        execution.operation_id,
                        "auto_research_task_settled_callback_failed",
                        {"exception_type": type(exc).__name__},
                        tier="diagnostic",
                    )

    def _auto_research_admission_exhausted(self, episode: EpisodeRecord) -> None:
        # Hitting the ceiling refuses the *next* paid invocation.  It must not
        # revoke authority from an invocation that was already admitted at the
        # ceiling: that turn may still finish, emit its final patch, or declare
        # completion.  Normal task settlement performs the terminal
        # completion/exhaustion choice once all admitted work is quiescent.
        if not self.store.auto_research_is_quiescent(episode.episode_id):
            return
        auto_research_exhaustion_signal(
            self.store,
            episode.episode_id,
            diagnostic="The Auto-research operational invocation ceiling was exhausted.",
        )
        current = self.store.episode(episode.episode_id)
        assert current is not None
        if current.root_operation_id is not None:
            with suppress(Exception):
                self.store.record_agent_task_event(
                    current.root_operation_id,
                    "Auto-research operational invocation ceiling exhausted.",
                    level="warning",
                )
        if self.on_auto_research_admission_exhausted is not None:
            with suppress(Exception):
                self.on_auto_research_admission_exhausted(current)

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
        graph_updates: list[GraphUpdateResult] = []
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
                    parsed_graph_updates = _graph_updates(event.text)
                    if parsed_graph_updates is not None:
                        graph_updates.extend(parsed_graph_updates)
                        graph_updates = graph_updates[-GRAPH_UPDATE_HISTORY_MAX_COUNT:]
                        if parsed_graph_updates:
                            graph_update = parsed_graph_updates[-1]
                    parsed_graph_update = _graph_update(event.text)
                    if parsed_graph_update is not None:
                        graph_update = parsed_graph_update
                    if revision is None:
                        revision_candidates = (
                            [parsed_graph_update] if parsed_graph_update is not None else []
                        ) + list(reversed(parsed_graph_updates or []))
                        revision = next(
                            (
                                update.applied_revision
                                for update in revision_candidates
                                if update.applied_revision is not None
                            ),
                            None,
                        )
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
                    elif (
                        parsed_graph_update is None
                        and parsed_graph_updates is None
                        and event.text.strip()
                        and len(messages) < 32
                    ):
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
            applied_revision=(
                applied_revision if applied_revision is not None else execution.applied_revision
            ),
            messages=messages,
            artifacts=artifacts,
            graph_update=graph_update,
            graph_updates=graph_updates,
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
                episode_id=episode.episode_id,
                graph_target=episode.graph_target,
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
        if record.kind == "auto_research":
            return AutoResearchRunRequest.model_validate(record.request)
        if record.kind == "branch_merge":
            return BranchMergeRunRequest.model_validate(record.request)
        if record.kind == "episode_report":
            return EpisodeReportRunRequest.model_validate(record.request)
        return RunRequest.model_validate(record.request)

    @staticmethod
    def _validate_request_type(kind: AgentTaskKind, request: AgentTaskRequest) -> None:
        if kind == "paper_coach" and not isinstance(request, CoachRequest):
            raise TypeError("paper_coach requires a CoachRequest")
        if kind == "auto_research" and not isinstance(request, AutoResearchRunRequest):
            raise TypeError("auto_research requires an AutoResearchRunRequest")
        if kind == "branch_merge" and not isinstance(request, BranchMergeRunRequest):
            raise TypeError("branch_merge requires a BranchMergeRunRequest")
        if kind == "episode_report" and not isinstance(request, EpisodeReportRunRequest):
            raise TypeError("episode_report requires an EpisodeReportRunRequest")
        if kind not in {
            "paper_coach",
            "auto_research",
            "branch_merge",
            "episode_report",
        } and not isinstance(request, RunRequest):
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


def _graph_updates(text: str) -> list[GraphUpdateResult] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or "graph_updates" not in value:
        return None
    raw_updates = value["graph_updates"]
    if not isinstance(raw_updates, list):
        return None
    try:
        return [GraphUpdateResult.model_validate(item) for item in raw_updates]
    except (TypeError, ValueError):
        return None
