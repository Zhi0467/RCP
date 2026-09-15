from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from rcp.control import ExperimentControlState
from rcp.core.models import AuthorizedHuman, Experiment
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskRecord, EpisodeRecord

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


def experiment_start_message(message: str | None, node_id: str) -> str:
    """Preserve an explicit goal and use the canonical fallback only for blank input."""

    if message is not None and message.strip():
        return message
    return f"Begin a bounded Experiment-loop episode for {node_id}."


def resolve_experiment_node_work_request(
    service: ProjectService,
    request: RunRequest,
) -> RunRequest:
    """Resolve one Experiment turn from the current human node-Work profile."""

    profile = service.resolve_agent_profile("node_chat")
    resolved = request.model_copy(
        update={
            "provider": profile.provider,
            "model": profile.model,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
            "run_truth_scope": list(
                request.run_truth_scope or service.manifest.agent.default_run_truth_scope
            ),
        }
    )
    skill_resolved = service.resolve_skill_request(resolved)
    if not isinstance(skill_resolved, RunRequest):
        raise TypeError("Experiment skill resolution changed the task request type.")
    return skill_resolved


def fresh_experiment_run_request(
    service: ProjectService,
    supplied: RunRequest,
    *,
    node: Experiment,
    state_revision: int,
    control: ExperimentControlState,
    episode_id: str,
    invocation_ceiling: int | None = None,
    trigger: Literal["experiment_run", "orchestrator"] = "experiment_run",
) -> RunRequest:
    """Build the immutable invocation-one contract for a fresh Experiment episode."""

    ceiling = node.invocation_ceiling if invocation_ceiling is None else invocation_ceiling
    if ceiling < 1:
        raise ValueError("Experiment invocation limit must be positive.")
    request = supplied.model_copy(
        update={
            "chat_scope": "node",
            "node_id": node.id,
            "message": experiment_start_message(supplied.message, node.id),
            "session_id": None,
            "mode": "work",
            "trigger": trigger,
            "patch_kind": "experiment_loop",
            "control_node_id": node.id,
            "control_revision": state_revision,
            "control_episode_id": episode_id,
            "control_invocation": 1,
            "control_invocation_ceiling": ceiling,
            "control_decision_bundle": control.governing_decisions,
            "control_completion_criteria": list(node.completion_criteria),
            "watcher_ids": [],
        }
    )
    return service.resolve_compute_request(resolve_experiment_node_work_request(service, request))


def start_experiment_continuation(
    tasks: BackgroundAgentTasks,
    project_id: str,
    request: RunRequest,
    *,
    source: EpisodeRecord,
    authorized_by: AuthorizedHuman,
    stage_host: str | None,
    stage_root: str,
    continuation_request_id: str,
) -> AgentTaskRecord:
    """Admit invocation 1 of a continuation that resumes its source's session.

    ``start`` refuses a saved stage and a watcher notification refuses a session
    on a human Run, because both are fresh episodes; a continuation is the one
    Experiment Run that carries the ended episode's exact session and stage.
    """

    if not authorized_by.display_name.strip():
        raise ValueError("An Experiment continuation requires a named human authorizer snapshot.")
    if (
        request.patch_kind != "experiment_loop"
        or request.trigger != "experiment_run"
        or request.control_invocation != 1
        or not request.session_id
        or not request.chat_id
        or request.control_node_id != source.control_node_id
    ):
        raise ValueError("An Experiment continuation is invocation 1 on the source's session.")
    estimate, samples = tasks.store.agent_task_estimate(
        project_id, "node_chat", request.model_dump(mode="json")
    )
    record = tasks._create_and_spawn(
        project_id,
        "node_chat",
        request,
        estimate_seconds=estimate,
        estimate_samples=samples,
        authorized_by=authorized_by,
        stage_host=stage_host,
        stage_root=stage_root,
        graph_target=source.graph_target,
        continues_episode_id=source.episode_id,
        continuation_request_id=continuation_request_id,
    )
    if record is None:
        raise RuntimeError("Experiment continuation admission returned no task")
    return record
