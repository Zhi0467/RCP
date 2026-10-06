from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rcp.control import derive_experiment_control_state
from rcp.core.models import Experiment
from rcp.loop_status import LoopStatusRow, other_branch_loops
from rcp.runs.auto_research_admission import (
    ensure_auto_research_child_experiment_spawned,
    resume_auto_research_child_experiment,
    start_auto_research_child_experiment,
)
from rcp.runs.experiment_admission import fresh_experiment_run_request
from rcp.service import ProjectService, RunRequest
from rcp.storage import (
    AppStore,
    AutoResearchChildExperimentRecord,
    AutoResearchExperimentAllowance,
    AutoResearchExperimentAllowanceReached,
)

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


ProjectServiceLookup = Callable[[str, str], ProjectService]
ExperimentOperationLock = Callable[[str], AbstractContextManager[object]]


@dataclass(frozen=True)
class AutoResearchExperimentAction:
    disposition: Literal[
        "created",
        "stopping",
        "stopped",
        "existing",
        "resumed",
        "resume_unavailable",
    ]
    episode_id: str
    status: str
    allowance: AutoResearchExperimentAllowance
    operation_id: str | None = None
    reason: str | None = None
    replacement_command: str | None = None
    live_elsewhere: tuple[LoopStatusRow, ...] = ()


class AutoResearchExperimentLimitInvalid(ValueError):
    def __init__(self, allowance: AutoResearchExperimentAllowance) -> None:
        self.allowance = allowance
        super().__init__(
            "The requested Experiment invocation limit exceeds the Auto-research "
            f"allowance of {allowance.total}; lower --invocation-limit to "
            f"{allowance.total} or less."
        )


class AutoResearchExperimentCoordinator:
    """Graceful Auto-research child Experiment admission above existing paths."""

    def __init__(
        self,
        store: AppStore,
        background: BackgroundAgentTasks,
        *,
        project_service: ProjectServiceLookup,
        operation_lock: ExperimentOperationLock,
    ) -> None:
        self.store = store
        self.background = background
        self.project_service = project_service
        self.operation_lock = operation_lock

    def kick_off(
        self,
        *,
        auto_research_episode_id: str,
        parent_operation_id: str,
        child_episode_id: str,
        node_id: str,
        goal: str | None,
        goal_sha256: str | None,
        invocation_limit: int | None,
        admission_id: str,
    ) -> AutoResearchExperimentAction:
        parent = self._parent(auto_research_episode_id, parent_operation_id)
        allowance = self.store.auto_research_experiment_allowance(auto_research_episode_id)
        if invocation_limit is not None and invocation_limit > allowance.total:
            raise AutoResearchExperimentLimitInvalid(allowance)
        if goal is not None:
            actual = hashlib.sha256(goal.encode("utf-8")).hexdigest()
            if goal_sha256 != actual or not goal.strip():
                raise ValueError("The Experiment goal snapshot is blank or has the wrong digest.")
        elif goal_sha256 is not None:
            raise ValueError("An absent Experiment goal cannot carry a digest.")

        service = self.project_service(parent.project_id, auto_research_episode_id)
        now = self.store.now()
        intent = {
            "goal": goal,
            "invocation_limit": invocation_limit,
        }
        existing = self.store.auto_research_child_experiment(child_episode_id)
        if existing is not None:
            if (
                existing.auto_research_episode_id != auto_research_episode_id
                or existing.project_id != parent.project_id
                or existing.control_node_id != node_id
                or existing.request != intent
                or existing.goal_sha256 != goal_sha256
                or existing.parent_operation_id != parent_operation_id
            ):
                raise ValueError(
                    "The Experiment kickoff identity already names another durable intent."
                )
            child = self.store.episode(child_episode_id)
            task = (
                self.store.agent_task(child.root_operation_id)
                if child is not None and child.root_operation_id is not None
                else None
            )
            if existing.state == "running" and task is not None:
                task = ensure_auto_research_child_experiment_spawned(
                    self.background,
                    auto_research_episode_id,
                    child_episode_id,
                    operation_id=task.operation_id,
                    continuation="fresh",
                )
            return AutoResearchExperimentAction(
                disposition="existing",
                episode_id=child_episode_id,
                operation_id=task.operation_id if task is not None else None,
                status=(
                    existing.state
                    if existing.state in {"cancelled", "terminal"}
                    else child.status
                    if child is not None
                    else existing.state
                ),
                allowance=self.store.auto_research_experiment_allowance(auto_research_episode_id),
                live_elsewhere=tuple(
                    other_branch_loops(
                        self.store,
                        parent.project_id,
                        graph_target=parent.graph_target,
                        node_id=node_id,
                    )
                ),
            )
        if allowance.remaining == 0:
            raise AutoResearchExperimentAllowanceReached(allowance)
        admission = self.store.auto_research_child_admission(admission_id)
        if (
            admission is None
            or admission.episode_id != auto_research_episode_id
            or admission.project_id != parent.project_id
            or admission.child_kind != "experiment"
            or admission.child_id != child_episode_id
            or admission.state != "accepted"
        ):
            raise ValueError("Experiment kickoff has no matching durable command admission.")
        try:
            with self.operation_lock(parent.project_id):
                locked_allowance = self.store.auto_research_experiment_allowance(
                    auto_research_episode_id
                )
                if locked_allowance.remaining == 0:
                    raise AutoResearchExperimentAllowanceReached(locked_allowance)
                request = self._fresh_request(
                    service,
                    child_episode_id=child_episode_id,
                    node_id=node_id,
                    goal=goal,
                    invocation_limit=invocation_limit,
                )
                route = AutoResearchChildExperimentRecord(
                    child_episode_id=child_episode_id,
                    auto_research_episode_id=auto_research_episode_id,
                    project_id=parent.project_id,
                    control_node_id=node_id,
                    state="running",
                    request=intent,
                    goal_sha256=goal_sha256,
                    parent_operation_id=parent_operation_id,
                    created_at=now,
                    updated_at=now,
                )
                task = start_auto_research_child_experiment(
                    self.background,
                    route,
                    request,
                    admission_id=admission_id,
                )
                return AutoResearchExperimentAction(
                    disposition="created",
                    episode_id=child_episode_id,
                    operation_id=task.operation_id,
                    status=task.status,
                    allowance=self.store.auto_research_experiment_allowance(
                        auto_research_episode_id
                    ),
                    live_elsewhere=tuple(
                        other_branch_loops(
                            self.store,
                            parent.project_id,
                            graph_target=parent.graph_target,
                            node_id=node_id,
                        )
                    ),
                )
        except ValueError:
            if self.store.auto_research_child_experiment(child_episode_id) is None:
                self._cancel_unreferenced_admission(admission_id)
            raise

    def stop(
        self,
        auto_research_episode_id: str,
        child_episode_id: str,
        *,
        operation_id: str | None = None,
    ) -> AutoResearchExperimentAction:
        initiated_by = f"orchestrator:{operation_id}" if operation_id is not None else None
        route = self._child(auto_research_episode_id, child_episode_id)
        if route.state in {"cancelled", "terminal"}:
            return AutoResearchExperimentAction(
                disposition="stopped",
                episode_id=child_episode_id,
                status=route.state,
                allowance=self.store.auto_research_experiment_allowance(auto_research_episode_id),
            )
        with self.operation_lock(route.project_id):
            child = self.store.episode(child_episode_id)
            if child is None or child.mode != "experiment_loop":
                raise ValueError("The child Experiment route lost its episode parent.")
            runtime = self.store.experiment_loop_runtime(
                route.project_id,
                route.control_node_id,
                graph_target=child.graph_target,
            )
            if runtime.episode_id != child_episode_id and child.stop_requested_at is None:
                raise ValueError("The requested child is no longer the current Experiment episode.")
            if runtime.episode_id == child_episode_id:
                self.store.request_experiment_loop_stop(
                    route.project_id,
                    route.control_node_id,
                    episode_id=child.episode_id,
                    graph_target=child.graph_target,
                    initiated_by=initiated_by,
                )
        child = self.store.episode(child_episode_id)
        status = child.status if child is not None else "stopped"
        return AutoResearchExperimentAction(
            disposition="stopped" if status == "stopped" else "stopping",
            episode_id=child_episode_id,
            status=status,
            allowance=self.store.auto_research_experiment_allowance(auto_research_episode_id),
        )

    def resume(
        self,
        auto_research_episode_id: str,
        child_episode_id: str,
        *,
        operation_id: str | None = None,
    ) -> AutoResearchExperimentAction:
        route = self._child(auto_research_episode_id, child_episode_id)
        existing = self.store.agent_task(operation_id) if operation_id is not None else None
        if route.state != "running" and existing is None:
            raise ValueError("Only a running child Experiment episode can be resumed.")
        result = resume_auto_research_child_experiment(
            self.background,
            auto_research_episode_id,
            child_episode_id,
            operation_id=operation_id,
        )
        return AutoResearchExperimentAction(
            disposition=result.disposition,
            episode_id=child_episode_id,
            operation_id=result.task.operation_id if result.task is not None else None,
            status=result.task.status if result.task is not None else "resume_unavailable",
            allowance=self.store.auto_research_experiment_allowance(auto_research_episode_id),
            reason=result.reason,
            replacement_command=result.replacement_command,
        )

    @staticmethod
    def _fresh_request(
        service: ProjectService,
        *,
        child_episode_id: str,
        node_id: str,
        goal: str | None,
        invocation_limit: int | None,
    ) -> RunRequest:
        state = service.history.state()
        node = state.nodes.get(node_id)
        if not isinstance(node, Experiment):
            raise ValueError(f"Node {node_id!r} is not an Experiment.")
        control = derive_experiment_control_state(state, node_id)
        if not control.ready:
            raise ValueError(" ".join(control.reasons))
        supplied = RunRequest(
            chat_scope="node",
            node_id=node_id,
            message=goal,
            chat_id=child_episode_id,
            mode="work",
            trigger="orchestrator",
            patch_kind="experiment_loop",
        )
        return fresh_experiment_run_request(
            service,
            supplied,
            node=node,
            state_revision=state.revision,
            control=control,
            episode_id=child_episode_id,
            invocation_ceiling=invocation_limit,
            trigger="orchestrator",
        )

    def _parent(self, episode_id: str, operation_id: str):
        episode = self.store.episode(episode_id)
        task = self.store.agent_task(operation_id)
        if (
            episode is None
            or episode.mode != "auto_research"
            or task is None
            or task.episode_id != episode_id
            or task.project_id != episode.project_id
            or self.store.auto_research_invocation_role(operation_id) != "orchestrator"
        ):
            raise ValueError("Only the canonical Auto-research orchestrator can start a child.")
        return task

    def _child(
        self,
        auto_research_episode_id: str,
        child_episode_id: str,
    ) -> AutoResearchChildExperimentRecord:
        route = self.store.auto_research_child_experiment(child_episode_id)
        if route is None or route.auto_research_episode_id != auto_research_episode_id:
            raise ValueError("Experiment control target is outside this Auto-research episode.")
        return route

    def _cancel_unreferenced_admission(self, admission_id: str) -> None:
        admission = self.store.auto_research_child_admission(admission_id)
        if admission is not None and admission.state == "accepted":
            self.store.cancel_auto_research_child_admission(admission_id)
