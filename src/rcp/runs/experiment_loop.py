from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from rcp.agents.schema import (
    EXPERIMENT_WATCH_OUTPUT_PREFIX as _EXPERIMENT_WATCH_OUTPUT_PREFIX,
)
from rcp.agents.schema import (
    EXPERIMENT_WATCH_OUTPUT_SUFFIX as _EXPERIMENT_WATCH_OUTPUT_SUFFIX,
)
from rcp.agents.schema import (
    CreateEdgesOperation as AgentCreateEdgesOperation,
)
from rcp.agents.schema import (
    CreateProposalsOperation as AgentCreateProposalsOperation,
)
from rcp.agents.schema import (
    UpdateNodesOperation as AgentUpdateNodesOperation,
)
from rcp.agents.schema import (
    parse_agent_patch_json,
)
from rcp.background import AgentTaskExecution
from rcp.control import (
    ExperimentInvocationAdmission,
    admit_experiment_watcher_invocation,
    decision_drift,
)
from rcp.core.models import AuthorizedHuman, ExperimentDecisionPin, GraphState, Patch
from rcp.core.operations import UpdateNodesOperation as CoreUpdateNodesOperation
from rcp.core.project_types import project_type_of
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import EPISODE_RECEIPT_MAX_BYTES
from rcp.loop_status import LoopStatusProjection, chat_loop_status, loop_status_projection
from rcp.machine_cards import BACKEND_MACHINE_ID, host_of
from rcp.runs.provider_login import AdmissionRefused
from rcp.runs.shared import _stage_json_task_input, _stage_or_reuse_task_input
from rcp.runs.task_policy import resolved_dispatch_authority
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.storage import (
    AgentTaskRecord,
    AutoResearchExperimentAllowanceReached,
    EpisodeInvocationCeilingReached,
    EpisodeNotRunning,
    EpisodeRecord,
    ExperimentEpisodeRecord,
    ExperimentLoopRuntime,
    GraphCondition,
    WatcherCheckRecord,
    WatcherRecord,
    WatcherStopRequest,
)
from rcp.transport import RemoteRunStage, StateUnavailable
from rcp.watchers import (
    AdmissionFence,
    ExperimentWatchSpec,
    WatcherBinding,
    WatcherCheckResult,
    external_check,
    graph_condition_result,
    validate_graph_conditions,
)

if TYPE_CHECKING:
    from rcp.agents.graph_read_scope import GraphReadScope
    from rcp.background import BackgroundAgentTasks
    from rcp.runs.episodes.wrapup import EpisodeWrapupSpec
    from rcp.storage import AppStore

_EXIT_STATUSES = frozenset({"completed"})
_COMPLETED_NEXT_ACTION_PROBLEM = (
    "Experiment status completed conflicts with a non-empty next_action. Continue useful work "
    "until next_action can truthfully be null, or keep a nonterminal status and arm a watcher "
    "for real detached work or explicitly pause for human authority."
)
_EPISODE_CONTEXT_CANDIDATE_ROLE = "experiment_episode_context_candidate"
_MAX_RECEIPT_ATTEMPTS = 8
_MAX_RECEIPT_TEXT = 400
_MAX_RECEIPT_LIST = 3
_MAX_RECEIPT_IDENTIFIERS = 4
_MAX_RECEIPT_DECISIONS = 4
_MAX_RECEIPT_ID = 100
_MAX_RECEIPT_LIST_TEXT = 120

EpisodeWakeReadiness = Literal["ready", "transient", "unavailable"]

ExperimentLoopPhase = Literal[
    "initial_run",
    "human_reauthorization",
    "watcher_wake",
    "resume",
    "retry",
]


@dataclass(frozen=True)
class EpisodeWakePreflight:
    """Whether a wake may claim the current episode's native session.

    `transient` leaves the watchers ready for a later pass; only `unavailable`
    is a durable Needs-action fact about the episode itself.
    """

    readiness: EpisodeWakeReadiness
    diagnostic: str | None = None
    session_id: str | None = None
    stage_machine_id: str | None = None
    stage_root: str | None = None


ExperimentLoopEnding = Literal["completed", "human_pause"]
ExperimentLoopEndingSignal = Literal[
    "experiment_completed",
    "proposal_created",
    "decision_awaits_human",
    "blocker_linked",
]


@dataclass(frozen=True)
class ExperimentLoopSemanticEnding:
    """One accepted Patch's explicit semantic ending, independent of loop mechanics."""

    ending: ExperimentLoopEnding
    signals: tuple[ExperimentLoopEndingSignal, ...]

    @property
    def partial(self) -> bool:
        return self.ending == "human_pause"


def _experiment_watcher_output_names(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> list[str]:
    if remote_stage is not None:
        return [
            name
            for name in remote_stage.list_workspace_files()
            if name.startswith(_EXPERIMENT_WATCH_OUTPUT_PREFIX)
            and name.endswith(_EXPERIMENT_WATCH_OUTPUT_SUFFIX)
        ]
    try:
        entries = list(os.scandir(workspace))
    except OSError as exc:
        raise StateUnavailable(f"could not inspect chat watcher outputs: {exc}") from exc
    names: list[str] = []
    for entry in entries:
        if not (
            entry.name.startswith(_EXPERIMENT_WATCH_OUTPUT_PREFIX)
            and entry.name.endswith(_EXPERIMENT_WATCH_OUTPUT_SUFFIX)
        ):
            continue
        if not entry.is_file(follow_symlinks=False):
            raise ValueError(
                f"Experiment watcher output is not a direct regular file: {entry.name}"
            )
        names.append(entry.name)
    return sorted(names)


def preflight_episode_wake(
    runtime: ExperimentLoopRuntime,
    episode: ExperimentEpisodeRecord | None,
    *,
    store: AppStore,
) -> EpisodeWakePreflight:
    """Prove the episode session and exact stage before any claim or budget spend."""

    if episode is None or not episode.session_bound:
        return EpisodeWakePreflight(
            readiness="unavailable",
            diagnostic=(
                "This episode has no validated native provider session to continue. "
                "Use Stop loop and press Start new episode."
            ),
        )
    if runtime.episode_id != episode.episode_id:
        return EpisodeWakePreflight(
            readiness="unavailable",
            diagnostic=(
                "This agent's episode is no longer the Experiment's current episode. "
                "Use Stop loop and press Start new episode."
            ),
        )
    assert episode.stage_root is not None
    if episode.stage_machine_id and episode.stage_machine_id != BACKEND_MACHINE_ID:
        exists = RemoteRunStage(host_of(store, episode.stage_machine_id)).directory_exists(
            episode.stage_root
        )
    else:
        stage = Path(episode.stage_root)
        exists = stage.is_dir() and not stage.is_symlink()
    if exists is None:
        return EpisodeWakePreflight(
            readiness="transient",
            diagnostic="The episode's execution machine could not be reached for this pass.",
        )
    if not exists:
        return EpisodeWakePreflight(
            readiness="unavailable",
            diagnostic=(
                "The episode's saved provider workspace is gone from its execution machine. "
                "Use Stop loop and press Start new episode."
            ),
        )
    return EpisodeWakePreflight(
        readiness="ready",
        session_id=episode.native_session_id,
        stage_machine_id=episode.stage_machine_id,
        stage_root=episode.stage_root,
    )


def experiment_watcher_delivery_request(
    watcher_ids: list[str],
    *,
    episode: EpisodeRecord,
    runtime: ExperimentLoopRuntime,
    admission: ExperimentInvocationAdmission,
    session_id: str,
) -> RunRequest:
    """Build one automatic wake from the episode's current runtime, never from a watcher."""

    if (
        episode.control_node_id is None
        or runtime.chat_id is None
        or runtime.control_revision is None
    ):
        raise ValueError("An Experiment watcher wake requires its episode's control binding.")
    return RunRequest(
        browser_requested=episode.browser_requested,
        code_worktree=episode.code_worktree,
        graph_isolation=episode.graph_isolation,
        provider=runtime.provider,
        model=runtime.model,
        reasoning=runtime.reasoning,
        run_on=runtime.run_on,
        run_truth_scope=runtime.run_truth_scope,
        chat_scope="node",
        node_id=episode.control_node_id,
        message="Continue the bounded Experiment loop from its staged watcher state.",
        chat_id=runtime.chat_id,
        session_id=session_id,
        mode="work",
        trigger="watcher",
        patch_kind="experiment_loop",
        control_node_id=episode.control_node_id,
        control_revision=runtime.control_revision,
        control_episode_id=admission.episode_id,
        control_invocation=admission.invocation,
        control_invocation_ceiling=admission.invocation_ceiling,
        control_decision_bundle=admission.decision_bundle,
        control_completion_criteria=runtime.completion_criteria,
        workflow_ids=runtime.workflow_ids,
        skill_ids=runtime.skill_ids,
        invoked_workflow_ids=[],
        invoked_skill_ids=[],
        resolved_skill_packages=runtime.resolved_skill_packages,
        watcher_ids=watcher_ids,
    )


def deliver_experiment_watchers(
    agent_id: str,
    episode: EpisodeRecord,
    ready: list[WatcherRecord],
    fence: AdmissionFence | None,
    *,
    tasks: BackgroundAgentTasks,
    graph_project_service: Callable[[str, GraphTargetRef], ProjectService],
    experiment_operation_lock: Callable[[str], AbstractContextManager[object]],
) -> None:
    """The Experiment episode's half of a wake: check, spend, claim, and launch at once.

    The session is proved before the claim and the spend, so an unusable binding
    never costs an invocation and never quietly becomes a fresh session.
    """

    store = tasks.store
    control_node_id = episode.control_node_id
    if control_node_id is None:
        raise ValueError("An Experiment episode is missing its control node.")
    ids = [watcher.watcher_id for watcher in ready]
    service = graph_project_service(episode.project_id, episode.graph_target)
    with experiment_operation_lock(episode.project_id):
        state = service.history.state()
        node = state.nodes.get(control_node_id)
        if node is None or not project_type_of(state).is_control_node(node.type):
            store.cancel_watchers(
                ids, by="rcp", reason="The Experiment this agent ran no longer exists."
            )
            return
        runtime = store.experiment_loop_runtime(
            episode.project_id, control_node_id, graph_target=episode.graph_target
        )
        if runtime.episode_id != episode.episode_id or runtime.chat_id != agent_id:
            return

        def diagnose(diagnostic: str | None) -> None:
            store.record_experiment_episode_diagnostic(
                episode_id=episode.episode_id,
                project_id=episode.project_id,
                control_node_id=control_node_id,
                diagnostic=diagnostic,
            )

        authorized_by = episode.authorized_by
        if authorized_by is None:
            diagnose(
                "This Experiment episode has no recorded human authorizer. "
                "Stop the loop and start a new episode."
            )
            return
        experiment = store.experiment_episode(episode.episode_id)
        if (
            runtime.run_on is None
            or experiment is None
            or experiment.machine_id != runtime.run_on
            or experiment.stage_machine_id != runtime.run_on
        ):
            diagnose(
                "The Experiment episode's saved execution host or stage host no longer "
                "matches the current project manifest. Stop the loop and start a new Run "
                "after confirming the execution target."
            )
            return
        preflight = preflight_episode_wake(runtime, experiment, store=store)
        if preflight.readiness == "unavailable":
            diagnose(preflight.diagnostic)
            return
        if preflight.readiness != "ready":
            return
        if runtime.session_diagnostic is not None:
            diagnose(None)
        admission = admit_experiment_watcher_invocation(
            state,
            control_node_id,
            episode_id=runtime.episode_id,
            invocations_used=runtime.invocations_used,
            invocation_ceiling=runtime.invocation_ceiling,
            decision_bundle=[
                ExperimentDecisionPin.model_validate(item) for item in runtime.decision_bundle
            ],
            task_active=runtime.task_active,
            episode_exited=runtime.episode_exited,
            stop_requested=runtime.stop_requested,
        )
        if admission is None:
            return
        assert preflight.session_id is not None
        request = experiment_watcher_delivery_request(
            ids,
            episode=episode,
            runtime=runtime,
            admission=admission,
            session_id=preflight.session_id,
        )
        _start_experiment_wake(
            tasks, episode, request, ids, authorized_by, preflight=preflight, fence=fence
        )


def _start_experiment_wake(
    tasks: BackgroundAgentTasks,
    episode: EpisodeRecord,
    request: RunRequest,
    watcher_ids: list[str],
    authorized_by: AuthorizedHuman,
    *,
    preflight: EpisodeWakePreflight,
    fence: AdmissionFence | None,
) -> None:
    store = tasks.store
    project_id = episode.project_id
    request_data = request.model_dump(mode="json")
    estimate, samples = store.agent_task_estimate(project_id, "node_chat", request_data)
    now = store.now()
    record = AgentTaskRecord(
        operation_id=str(uuid4()),
        project_id=project_id,
        episode_id=episode.episode_id,
        graph_target=episode.graph_target,
        kind="node_chat",
        status="queued",
        request=request_data,
        created_at=now,
        updated_at=now,
        status_message="Waiting to deliver a watcher update.",
        native_session_id=request.session_id,
        stage_machine_id=preflight.stage_machine_id,
        stage_root=preflight.stage_root,
        estimate_seconds=estimate,
        estimate_samples=samples,
        phase="queued",
        last_activity_at=now,
        authorized_by=authorized_by,
        dispatch_authority=resolved_dispatch_authority(
            store, tasks.dispatch_authority_resolver, "node_chat", request, project_id=project_id
        ),
    )

    def claim_and_launch() -> None:
        with tasks._watcher_delivery_lock:
            if not tasks._accepting_watcher_deliveries:
                return
            try:
                tasks.admit_provider_task(
                    project_id, request, authorized_by=authorized_by, automatic=True
                )
                stored = store.create_experiment_watcher_invocation(record, watcher_ids)
            except (
                AdmissionRefused,
                AutoResearchExperimentAllowanceReached,
                EpisodeInvocationCeilingReached,
                EpisodeNotRunning,
            ):
                return
            if stored is not None and stored.status == "queued":
                tasks.launch_admitted(stored.operation_id)

    if fence is not None:
        fence(claim_and_launch)
    else:
        claim_and_launch()


def _completion_problem(operations: list[object], control_node_id: str) -> str | None:
    for operation in operations:
        if not isinstance(operation, (AgentUpdateNodesOperation, CoreUpdateNodesOperation)):
            continue
        for update in operation.nodes:
            if update.id != control_node_id:
                continue
            changes = update.changes
            if changes.get("status") != "completed":
                continue
            next_action = changes.get("next_action")
            if isinstance(next_action, str) and next_action.strip():
                return _COMPLETED_NEXT_ACTION_PROBLEM
    return None


def validate_experiment_completion(patch: Patch, control_node_id: str) -> None:
    """Reject a terminal claim that still names unfinished Experiment work."""

    problem = _completion_problem(list(patch.ops), control_node_id)
    if problem is not None:
        raise ValueError(problem)


def experiment_exit_problem(patch_text: str | None, control_node_id: str) -> str | None:
    """Explain a contradictory attempted exit before watcher correction."""

    if patch_text is None:
        return None
    try:
        patch = parse_agent_patch_json(patch_text)
    except ValueError:
        return None
    return _completion_problem(list(patch.ops), control_node_id)


def experiment_loop_semantic_ending(
    patch_text: str | None,
    control_node_id: str,
) -> ExperimentLoopSemanticEnding | None:
    """Classify one semantic Patch without turning mechanical failure into an ending."""

    if patch_text is None:
        return None
    try:
        patch = parse_agent_patch_json(patch_text)
    except ValueError:
        return None
    operations = list(patch.ops)
    completed = _completion_problem(operations, control_node_id) is None and any(
        update.id == control_node_id and update.changes.get("status") in _EXIT_STATUSES
        for operation in operations
        if isinstance(operation, AgentUpdateNodesOperation)
        for update in operation.nodes
    )
    pause_signals: list[ExperimentLoopEndingSignal] = []
    if any(
        isinstance(operation, AgentCreateProposalsOperation) and operation.proposals
        for operation in operations
    ):
        pause_signals.append("proposal_created")
    for operation in operations:
        if isinstance(operation, AgentUpdateNodesOperation):
            for update in operation.nodes:
                changes = update.changes
                if update.id != control_node_id and changes.get("status") in {"ready", "revisit"}:
                    pause_signals.append("decision_awaits_human")
        if isinstance(operation, AgentCreateEdgesOperation) and any(
            edge.source == control_node_id and edge.relation == "blocked_by"
            for edge in operation.edges
        ):
            pause_signals.append("blocker_linked")
    signals = tuple(dict.fromkeys(pause_signals))
    if completed:
        return ExperimentLoopSemanticEnding(
            ending="completed",
            signals=("experiment_completed", *signals),
        )
    if signals:
        return ExperimentLoopSemanticEnding(ending="human_pause", signals=signals)
    return None


def experiment_loop_ending_signal(
    *,
    semantic_ending: ExperimentLoopSemanticEnding,
    episode_id: str,
    control_node_id: str,
    invocation: int,
    invocation_ceiling: int,
    control_snapshot: dict[str, object],
    patch_text: str,
    graph_update: GraphUpdateResult,
    watcher_ids: list[str],
    stopped_watcher_ids: list[str],
    decision_bundle: list[ExperimentDecisionPin],
) -> dict[str, object]:
    """Build the compact mode-owned facts persisted after a successful handoff."""

    if graph_update.status != "applied":
        raise ValueError("Only an applied Experiment Patch can carry a semantic ending.")
    if not episode_id or not control_node_id or invocation < 1 or invocation_ceiling < invocation:
        raise ValueError("Experiment ending signal is missing its bounded episode identity.")
    attempts, attempt_count, current_summary, next_action = _receipt_control_result(
        control_snapshot,
        patch_text,
        control_node_id,
    )
    candidate_attempts = attempts[-_MAX_RECEIPT_ATTEMPTS:]
    selected_attempts: list[dict[str, object]] = []
    selected_decisions = decision_bundle[:_MAX_RECEIPT_DECISIONS]
    method = {
        "design": _receipt_text(control_snapshot.get("design")),
        "proxies": _receipt_text_list(
            [
                f"{proxy.get('stands_for')}: {proxy.get('measure')}"
                for proxy in control_snapshot.get("proxies") or []
                if isinstance(proxy, dict)
            ]
        ),
        "limitations": _receipt_text_list(control_snapshot.get("limitations")),
        "expected_outcomes": _receipt_text_list(control_snapshot.get("expected_outcomes")),
        "interpretation_rules": _receipt_text_list(control_snapshot.get("interpretation_rules")),
        "completion_criteria": _receipt_text_list(control_snapshot.get("completion_criteria")),
    }
    receipt: dict[str, object] = {
        "control": {
            "node_id": control_node_id,
            "title": _receipt_text(control_snapshot.get("title")),
            "objective": _receipt_text(control_snapshot.get("objective")),
            "method": method,
            "current_summary": _receipt_text(current_summary),
            "next_action": _receipt_optional_text(next_action),
        },
        "invocation": {
            "number": invocation,
            "ceiling": invocation_ceiling,
            "decision_bundle": [
                {
                    "decision_id": _receipt_text(item.decision_id, limit=_MAX_RECEIPT_ID),
                    "decision_revision": item.decision_revision,
                    "selected_option": _receipt_text(item.selected_option, limit=240),
                }
                for item in selected_decisions
            ],
            "omitted_decision_count": len(decision_bundle) - len(selected_decisions),
        },
        "attempt_observations": selected_attempts,
        "omitted_attempt_count": attempt_count,
        "watcher_summary": {
            "armed_count": len(watcher_ids),
            "armed_ids": _receipt_identifier_list(watcher_ids),
            "stopped_count": len(stopped_watcher_ids),
            "stopped_ids": _receipt_identifier_list(stopped_watcher_ids),
        },
        "graph_result": {
            "status": graph_update.status,
            "applied_revision": graph_update.applied_revision,
        },
        "semantic_signals": list(semantic_ending.signals),
    }
    signal: dict[str, object] = {
        "episode_id": episode_id,
        "ending": semantic_ending.ending,
        "partial": semantic_ending.partial,
        "receipt": receipt,
    }
    for observation in reversed(candidate_attempts):
        selected_attempts.insert(0, observation)
        receipt["omitted_attempt_count"] = attempt_count - len(selected_attempts)
        if _receipt_payload_size(signal) > EPISODE_RECEIPT_MAX_BYTES:
            selected_attempts.pop(0)
            receipt["omitted_attempt_count"] = attempt_count - len(selected_attempts)
            break
    if _receipt_payload_size(signal) > EPISODE_RECEIPT_MAX_BYTES:
        raise ValueError("The compact Experiment ending receipt exceeds its storage boundary.")
    return signal


def experiment_loop_wrapup_spec(
    continuation_operation_id: str,
    signal: dict[str, object],
) -> EpisodeWrapupSpec:
    """Turn one persisted mode signal into the shared wrap-up admission contract."""

    from rcp.runs.episodes.wrapup import EpisodeWrapupSpec

    episode_id = signal.get("episode_id")
    ending = signal.get("ending")
    partial = signal.get("partial")
    receipt = signal.get("receipt")
    if not isinstance(episode_id, str) or not episode_id:
        raise ValueError("Experiment ending signal has no episode id.")
    if ending not in {"completed", "human_pause"}:
        raise ValueError("Experiment ending signal has no semantic ending.")
    if not isinstance(partial, bool) or partial != (ending == "human_pause"):
        raise ValueError("Experiment ending signal has inconsistent partial state.")
    if not isinstance(receipt, dict):
        raise ValueError("Experiment ending signal has no compact receipt.")
    return EpisodeWrapupSpec(
        episode_id=episode_id,
        ending=ending,
        partial=partial,
        continuation_operation_id=continuation_operation_id,
        receipt=receipt,
    )


_LAUNCH_FAILURE_DIAGNOSTIC = (
    "This Experiment turn failed before it started its agent session, so the episode has "
    "no session to continue. Press Start new episode."
)


def experiment_loop_launch_failure_diagnostic(continuation: AgentTaskRecord) -> str:
    """Explain a turn that died before binding a session, naming its real cause.

    The lineage recovery check cannot classify this: it looks for the retained
    episode context candidate, which a turn that never reached prompt assembly
    has not written yet, and so reports a pre-migration lineage defect for an
    episode created seconds ago.
    """

    cause = (continuation.error or continuation.status_message or "").strip()
    if not cause:
        return _LAUNCH_FAILURE_DIAGNOSTIC
    return f"{_LAUNCH_FAILURE_DIAGNOSTIC} It failed with: {cause}"


def experiment_loop_operational_ending_wrapup_spec(
    *,
    continuation: AgentTaskRecord,
    request: RunRequest,
    episode: ExperimentEpisodeRecord,
    ending: Literal["exhausted", "failed"],
    diagnostic: str,
) -> EpisodeWrapupSpec:
    """Adapt an operational ending without rebuilding the resumed session context."""

    from rcp.runs.episodes.wrapup import EpisodeWrapupSpec

    if (
        continuation.episode_id != episode.episode_id
        or continuation.project_id != episode.project_id
        or request.control_episode_id != episode.episode_id
        or request.control_node_id != episode.control_node_id
    ):
        raise ValueError("The Experiment operational ending lost its exact episode lineage.")
    invocation = request.control_invocation
    ceiling = request.control_invocation_ceiling
    if invocation is None or ceiling is None:
        raise ValueError("The Experiment operational ending lost its invocation boundary.")
    receipt: dict[str, object] = {
        "control": {
            "node_id": episode.control_node_id,
            "completion_criteria": _receipt_text_list(
                request.control_completion_criteria,
                limit=_MAX_RECEIPT_TEXT,
            ),
        },
        "invocation": {
            "number": invocation,
            "ceiling": ceiling,
            "decision_bundle": [
                {
                    "decision_id": _receipt_text(item.decision_id, limit=_MAX_RECEIPT_ID),
                    "decision_revision": item.decision_revision,
                    "selected_option": _receipt_text(
                        item.selected_option,
                        limit=_MAX_RECEIPT_TEXT,
                    ),
                }
                for item in request.control_decision_bundle[:_MAX_RECEIPT_DECISIONS]
            ],
        },
        "accepted_handoff": {
            "operation_id": continuation.operation_id,
            "last_graph_result": _receipt_optional_text(
                episode.last_graph_result,
                limit=_MAX_RECEIPT_TEXT,
            ),
            "watcher_ids": _receipt_identifier_list(episode.last_watcher_ids),
            "omitted_watcher_count": max(
                0,
                len(episode.last_watcher_ids) - _MAX_RECEIPT_IDENTIFIERS,
            ),
        },
        "operational_ending": {
            "status": continuation.status,
            "diagnostic": _receipt_text(diagnostic, limit=_MAX_RECEIPT_TEXT),
        },
    }
    return EpisodeWrapupSpec(
        episode_id=episode.episode_id,
        ending=ending,
        partial=True,
        continuation_operation_id=continuation.operation_id,
        receipt=receipt,
        diagnostic=diagnostic,
    )


def _receipt_control_result(
    control_snapshot: dict[str, object],
    patch_text: str,
    control_node_id: str,
) -> tuple[list[dict[str, object]], int, object, object]:
    attempts = control_snapshot.get("attempts")
    current_summary = control_snapshot.get("current_summary")
    next_action = control_snapshot.get("next_action")
    try:
        patch = parse_agent_patch_json(patch_text)
    except ValueError:
        patch = None
    if patch is not None:
        for operation in patch.ops:
            if not isinstance(operation, AgentUpdateNodesOperation):
                continue
            for update in operation.nodes:
                if update.id != control_node_id:
                    continue
                changes = update.changes
                if isinstance(changes.get("attempts"), list):
                    attempts = changes["attempts"]
                if "current_summary" in changes:
                    current_summary = changes["current_summary"]
                if "next_action" in changes:
                    next_action = changes["next_action"]
    if not isinstance(attempts, list):
        attempts = []
    attempt_count = len(attempts)
    observations: list[dict[str, object]] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        observation: dict[str, object] = {
            "id": _receipt_text(attempt.get("id"), limit=_MAX_RECEIPT_ID),
            "sequence": attempt.get("sequence"),
            "purpose": _receipt_text(attempt.get("purpose"), limit=200),
            "attempt_kind": attempt.get("attempt_kind", "external_run"),
            "configuration": _receipt_text(attempt.get("configuration"), limit=300),
            "status": attempt.get("status"),
            "observation": _receipt_optional_text(
                attempt.get("outcome") or attempt.get("failure_reason"),
                limit=300,
            ),
            "job_refs": _receipt_text_list(
                attempt.get("job_refs"),
                limit=_MAX_RECEIPT_ID,
            ),
        }
        debug = attempt.get("debug")
        if isinstance(debug, dict):
            observation["debug"] = {
                key: _receipt_text(debug.get(key), limit=160)
                for key in ("mechanical_fault", "change", "predicted_effect")
            }
        observations.append(observation)
    return observations, attempt_count, current_summary, next_action


def _receipt_text(value: object, *, limit: int = _MAX_RECEIPT_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _receipt_optional_text(value: object, *, limit: int = _MAX_RECEIPT_TEXT) -> str | None:
    text = _receipt_text(value, limit=limit)
    return text or None


def _receipt_text_list(
    value: object,
    *,
    limit: int = _MAX_RECEIPT_LIST_TEXT,
) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        text for item in value[:_MAX_RECEIPT_LIST] if (text := _receipt_text(item, limit=limit))
    ]


def _receipt_identifier_list(values: list[str]) -> list[str]:
    return [
        text
        for item in values[:_MAX_RECEIPT_IDENTIFIERS]
        if (text := _receipt_text(item, limit=_MAX_RECEIPT_ID))
    ]


def _receipt_payload_size(payload: dict[str, object]) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    )


def root_experiment_loop_operation_id(execution: AgentTaskExecution) -> str:
    """Resolve the durable root task through explicit parent links, failing on broken lineage."""

    operation_id = execution.operation_id
    seen: set[str] = set()
    while True:
        if operation_id in seen:
            raise ValueError("Experiment-loop task lineage contains a cycle.")
        seen.add(operation_id)
        record = execution.store.agent_task(operation_id)
        if record is None:
            raise ValueError("Experiment-loop task lineage is incomplete.")
        if record.parent_operation_id is None:
            return operation_id
        operation_id = record.parent_operation_id


@dataclass(frozen=True)
class ExperimentHandoffScope:
    """The Run and invocation a handoff's watchers are armed from."""

    binding: WatcherBinding
    node_id: str | None
    episode_id: str


def experiment_observer_watcher_id(
    scope: ExperimentHandoffScope,
    index: int,
    spec: ExperimentWatchSpec,
) -> str:
    """Derive one observer's deterministic check id so Retry re-arms the same check.

    The declaration's list position is part of the identity, so the same
    observer moved elsewhere in the list is a different check. An ungrouped
    observer's watcher id is its check id.
    """

    identity = json.dumps(
        {
            "origin": scope.binding.origin_turn_id,
            "node_id": scope.node_id,
            "episode_id": scope.episode_id,
            "index": index,
            "check_command": spec.check_command,
            "log_path": spec.log_path,
            "cwd": spec.cwd,
            "group": spec.group,
            "cancel_command": spec.cancel_command,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid5(NAMESPACE_URL, f"rcp-experiment-watcher:{identity}"))


def prepare_experiment_watcher_records(
    execution: AgentTaskExecution,
    specs: list[ExperimentWatchSpec],
    results: list[WatcherCheckResult],
    scope: ExperimentHandoffScope,
    *,
    graph_conditions: list[GraphCondition] | None = None,
    graph_state: GraphState | None = None,
    armed_revision: int | None = None,
) -> list[WatcherRecord]:
    """Prepare one deterministic watcher handoff without writing storage.

    Each ungrouped observer and each graph condition is one watcher; each group
    label is one watcher holding its observers as checks.
    """

    if len(specs) != len(results):
        raise ValueError("Experiment-loop watcher checks do not match their specifications.")
    conditions = list(graph_conditions or [])
    if conditions:
        if graph_state is None:
            raise ValueError("Experiment graph conditions require current canonical graph state.")
        if armed_revision is None:
            raise ValueError("Experiment graph conditions require their validated base revision.")
        if armed_revision > graph_state.revision:
            raise ValueError("Experiment graph condition baseline is ahead of canonical state.")
        serialized = [item.model_dump_json() for item in conditions]
        if len(serialized) != len(set(serialized)):
            raise ValueError("an Experiment handoff cannot repeat a graph condition")
        validate_graph_conditions(conditions, graph_state)
    binding = scope.binding
    created_at = execution.store.now()

    def watcher(watcher_id: str, purpose: str, checks: list[WatcherCheckRecord]) -> WatcherRecord:
        return WatcherRecord(
            watcher_id=watcher_id,
            agent_id=binding.agent_id,
            project_id=binding.project_id,
            purpose=purpose,
            origin_turn_id=binding.origin_turn_id,
            created_at=created_at,
            checks=[check.model_copy(update={"watcher_id": watcher_id}) for check in checks],
        )

    desired: list[WatcherRecord] = []
    groups: dict[str, list[WatcherCheckRecord]] = {}
    for index, (spec, result) in enumerate(zip(specs, results, strict=True)):
        check = external_check(
            experiment_observer_watcher_id(scope, index, spec),
            spec,
            result,
            machine_id=binding.machine_id,
            created_at=created_at,
        )
        if spec.group is None:
            desired.append(watcher(check.check_id, spec.purpose, [check]))
        else:
            groups.setdefault(spec.group, []).append(check)
    for label, checks in groups.items():
        group_id = str(
            uuid5(
                NAMESPACE_URL,
                "rcp-experiment-watcher-group:"
                f"{binding.origin_turn_id}:{scope.node_id}:{scope.episode_id}:{label}",
            )
        )
        desired.append(watcher(group_id, label, checks))
    for index, condition in enumerate(conditions):
        identity = json.dumps(
            {
                "origin": binding.origin_turn_id,
                "node_id": scope.node_id,
                "episode_id": scope.episode_id,
                "index": index,
                "condition": condition.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        assert graph_state is not None
        assert armed_revision is not None
        result = graph_condition_result(condition, graph_state, armed_revision=armed_revision)
        if result == "removed":
            raise ValueError(
                f"Experiment graph condition target {condition.node_id!r} is not canonical."
            )
        watcher_id = str(uuid5(NAMESPACE_URL, f"rcp-experiment-graph-watcher:{identity}"))
        check = WatcherCheckRecord(
            check_id=watcher_id,
            watcher_id=watcher_id,
            machine_id=binding.machine_id,
            definition="graph",
            condition=condition,
            graph_target=binding.graph_target,
            armed_head=armed_revision,
            created_at=created_at,
            last_checked_at=created_at,
            completed_at=created_at if result == "completed" else None,
        )
        desired.append(watcher(watcher_id, f"Graph condition on {condition.node_id}", [check]))
    return desired


def persist_experiment_watchers_idempotently(
    execution: AgentTaskExecution,
    specs: list[ExperimentWatchSpec],
    results: list[WatcherCheckResult],
    scope: ExperimentHandoffScope,
    stops: list[WatcherStopRequest] | None = None,
    *,
    graph_conditions: list[GraphCondition] | None = None,
    graph_state: GraphState | None = None,
    armed_revision: int | None = None,
) -> list[WatcherRecord]:
    """Persist one validated handoff once across Retry/crash recovery."""

    desired = prepare_experiment_watcher_records(
        execution,
        specs,
        results,
        scope,
        graph_conditions=graph_conditions,
        graph_state=graph_state,
        armed_revision=armed_revision,
    )
    return execution.store.persist_experiment_watchers_idempotently(
        desired,
        stops=stops,
        binding=scope.binding,
        episode_id=scope.episode_id,
    )


def _watcher_state(
    execution: AgentTaskExecution,
    agent_id: str | None,
    delivered_watcher_ids: list[str],
) -> list[dict[str, object]]:
    """The agent's open watchers and the ones that woke this turn, as staged evidence."""

    if not agent_id:
        return []
    delivered = set(delivered_watcher_ids)
    return [
        {**watcher.model_dump(mode="json"), "state": watcher.state}
        for watcher in execution.store.agent_watchers(agent_id)
        if watcher.open or watcher.watcher_id in delivered
    ]


def stage_chat_loop_status(
    request: RunRequest,
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    state: GraphState,
    graph_target: GraphTargetRef,
    read_scope: GraphReadScope | None,
) -> dict[str, object] | None:
    """Fresh read-only loop status for a chat turn on a control node."""

    if request.chat_scope != "node" or request.node_id is None:
        return None
    node = state.nodes.get(request.node_id)
    if node is None or not project_type_of(state).is_control_node(node.type):
        return None
    task = execution.store.agent_task(execution.operation_id) if execution is not None else None
    status = (
        loop_status_projection(
            execution.store,
            task.project_id,
            request.node_id,
            graph_target=task.graph_target,
            read_scope=read_scope,
        )
        if execution is not None and task is not None
        else LoopStatusProjection(
            node_id=request.node_id, graph_target=graph_target, state="unavailable"
        )
    )
    run_episode = (
        execution.store.experiment_episode(status.current.episode_id)
        if execution is not None and status.current is not None
        else None
    )
    watchers = (
        _watcher_state(execution, run_episode.chat_id, [])
        if execution is not None and run_episode is not None
        else []
    )
    content = json.dumps(watchers, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    path = _stage_or_reuse_task_input(
        local_stage, remote_stage, f"loop-status-watchers-{digest}.json", content
    )
    return {**chat_loop_status(status), "watcher_state_path": path}


def experiment_loop_phase(request: RunRequest, continuation: str) -> ExperimentLoopPhase:
    """Name the agent-facing phase for one bounded-loop turn."""

    if continuation == "resume":
        return "resume"
    if continuation in {"retry", "handoff"}:
        return "retry"
    if request.trigger == "experiment_run" and request.session_id:
        return "human_reauthorization"
    if request.trigger == "watcher":
        return "watcher_wake"
    return "initial_run"


def experiment_episode_context_values(
    *,
    ontology_extensions: bool,
    ontology: dict[str, object],
    repositories: list[dict[str, str]],
    skill_pointers: list[dict[str, object]],
) -> dict[str, object]:
    """The episode context that may change between turns of one native session.

    Provider, model, reasoning, machine, truth scope, and authority are pinned for
    the episode, and graph/research/schema/output pointers are refreshed in every
    turn's own message, so neither belongs in the replacement baseline.
    """

    normalized_ontology = json.dumps(
        ontology,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "ontology": {
            "extensions": ontology_extensions,
            "sha256": hashlib.sha256(normalized_ontology.encode("utf-8")).hexdigest(),
        },
        "repositories": repositories,
        "skills": {"pointers": skill_pointers},
    }


def prepare_experiment_episode_context_candidate(
    execution: AgentTaskExecution,
    current_values: dict[str, object],
) -> dict[str, object]:
    """Persist the context one invocation actually sent before it can succeed.

    Resume and in-session Retry keep their original narrow contract, so they must
    commit the originating invocation's candidate rather than whatever happens to
    be current when recovery finishes. The originating invocation is the newest
    attempt that recorded a candidate. Fresh human turns and automatic wakes each
    establish their own immutable candidate.
    """

    if execution.continuation in {"resume", "retry"}:
        context_root = execution.store.experiment_episode_context_root(execution.operation_id)
        content = (
            execution.store.agent_task_contract(context_root, _EPISODE_CONTEXT_CANDIDATE_ROLE)
            if context_root is not None
            else None
        )
        if content is None:
            raise ValueError(
                "The continued Experiment-loop turn has no retained episode context candidate."
            )
        try:
            candidate = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "The retained Experiment-loop episode context candidate is invalid."
            ) from exc
        if not isinstance(candidate, dict):
            raise ValueError("The retained Experiment-loop episode context must be an object.")
        return candidate

    content = json.dumps(
        current_values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    execution.store.record_agent_task_contract(
        execution.operation_id,
        _EPISODE_CONTEXT_CANDIDATE_ROLE,
        content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    return json.loads(content)


def experiment_graph_result_summary(graph_update: GraphUpdateResult) -> str:
    """Say truthfully what RCP did with this turn's Patch, for the next wake."""

    if graph_update.status == "applied":
        return f"applied as revision {graph_update.applied_revision}"
    if graph_update.status == "rejected":
        detail = (
            graph_update.validation_messages[0]
            if graph_update.validation_messages
            else "the graph rejected it"
        )
        return f"rejected: {detail[:400]}"
    if graph_update.status == "unavailable":
        detail = (
            graph_update.validation_messages[0]
            if graph_update.validation_messages
            else "canonical state was unavailable"
        )
        if graph_update.commit_status == "present":
            return f"committed, but canonical state was lost before confirming it: {detail[:400]}"
        if graph_update.commit_status == "unknown":
            return f"may have been committed; canonical state was lost at the commit point: {detail[:400]}"
        return f"not applied, canonical state was unreachable: {detail[:400]}"
    return "no graph change"


def _prepare_experiment_episode_binding_intent(
    execution: AgentTaskExecution,
    request: RunRequest,
    *,
    native_session_id: str | None,
    stage_machine_id: str | None,
    stage_root: str | None,
    ending_signal: dict[str, object] | None,
) -> tuple[
    AgentTaskRecord,
    ExperimentEpisodeRecord | None,
    bool,
    dict[str, object] | None,
]:
    """Validate a completed turn and prepare its binding-replacement intent."""

    if ending_signal is not None and ending_signal.get("episode_id") != request.control_episode_id:
        raise ValueError("Experiment ending signal names another episode.")
    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The completed Experiment-loop task record is unavailable.")
    if (
        not request.control_episode_id
        or not request.control_node_id
        or request.control_invocation is None
        or not request.provider
        or not request.run_on
        or not request.chat_id
    ):
        raise ValueError("A completed Experiment-loop turn is missing its episode binding.")
    if not native_session_id or not stage_root:
        raise ValueError(
            "A successful Experiment-loop turn did not retain its native session and exact stage."
        )
    episode = execution.store.experiment_episode(request.control_episode_id)
    binding_replacement = bool(
        episode is not None
        and episode.session_bound
        and (
            episode.provider != request.provider
            or episode.native_session_id != native_session_id
            or episode.stage_machine_id != stage_machine_id
            or episode.stage_root != stage_root
        )
    )
    recovery_ancestor_id = task.parent_operation_id
    explicit_handoff_lineage = execution.continuation == "handoff" and bool(recovery_ancestor_id)
    while binding_replacement and recovery_ancestor_id and not explicit_handoff_lineage:
        explicit_handoff_lineage = (
            execution.store.agent_task_continuation_cause(recovery_ancestor_id) == "handoff"
        )
        ancestor = execution.store.agent_task(recovery_ancestor_id)
        recovery_ancestor_id = ancestor.parent_operation_id if ancestor is not None else None
    replacement_authorized = binding_replacement and explicit_handoff_lineage
    if binding_replacement and not replacement_authorized:
        raise ValueError(
            "Only an explicit human Experiment recovery may replace the committed provider session."
        )
    if (
        request.trigger == "watcher"
        and execution.continuation == "watcher_wake"
        and (episode is None or episode.native_session_id != native_session_id)
    ):
        raise ValueError(
            "An automatic Experiment wake cannot replace its committed native session."
        )
    replacement_provenance = (
        {
            "episode_id": request.control_episode_id,
            "invocation": request.control_invocation,
            "parent_operation_id": task.parent_operation_id,
            "previous": {
                "provider": episode.provider,
                "native_session_id": episode.native_session_id,
                "stage_machine_id": episode.stage_machine_id,
                "stage_root": episode.stage_root,
            },
            "replacement": {
                "provider": request.provider,
                "native_session_id": native_session_id,
                "stage_machine_id": stage_machine_id,
                "stage_root": stage_root,
            },
        }
        if replacement_authorized and episode is not None
        else None
    )
    return task, episode, replacement_authorized, replacement_provenance


def commit_experiment_episode_binding(
    execution: AgentTaskExecution,
    request: RunRequest,
    *,
    native_session_id: str | None,
    stage_machine_id: str | None,
    stage_root: str | None,
    graph_result: str,
    watcher_ids: list[str],
    context_baseline: dict[str, object],
    ending_signal: dict[str, object] | None = None,
) -> None:
    """Bind this episode to the session and stage a later automatic wake resumes.

    Only a turn with a mechanically successful joint Patch/watch handoff commits,
    so a provider, task, or handoff failure never moves the binding or baseline.
    A graph rejection is still recorded truthfully because the turn and its
    accepted watcher handoff completed.
    """

    task, _episode, replacement_authorized, replacement_provenance = (
        _prepare_experiment_episode_binding_intent(
            execution,
            request,
            native_session_id=native_session_id,
            stage_machine_id=stage_machine_id,
            stage_root=stage_root,
            ending_signal=ending_signal,
        )
    )
    assert request.run_on is not None
    execution.store.commit_experiment_episode_turn(
        episode_id=request.control_episode_id,
        project_id=task.project_id,
        control_node_id=request.control_node_id,
        provider=request.provider,
        machine_id=request.run_on,
        native_session_id=native_session_id,
        stage_machine_id=stage_machine_id,
        stage_root=stage_root,
        chat_id=request.chat_id,
        operation_id=execution.operation_id,
        invocation=request.control_invocation,
        graph_result=graph_result,
        watcher_ids=watcher_ids,
        context_baseline=context_baseline,
        ending_signal=ending_signal,
        replace_binding=replacement_authorized,
        replacement_provenance=replacement_provenance,
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "experiment_episode_binding",
        {
            "episode_id": request.control_episode_id,
            "invocation": request.control_invocation,
            "provider": request.provider,
            "machine_id": request.run_on,
            "stage_machine_id": stage_machine_id,
            "stage_root": stage_root,
            "graph_result": graph_result,
            "watcher_ids": watcher_ids,
            "binding_replaced": replacement_authorized,
        },
    )


def commit_experiment_episode_handoff(
    execution: AgentTaskExecution,
    request: RunRequest,
    watcher_records: list[WatcherRecord],
    scope: ExperimentHandoffScope,
    *,
    native_session_id: str | None,
    stage_machine_id: str | None,
    stage_root: str | None,
    graph_result: str,
    context_baseline: dict[str, object],
    stops: list[WatcherStopRequest] | None = None,
    ending_signal: dict[str, object] | None = None,
) -> list[WatcherRecord]:
    """Commit prepared watchers and the episode binding as one handoff."""

    task, _episode, replacement_authorized, replacement_provenance = (
        _prepare_experiment_episode_binding_intent(
            execution,
            request,
            native_session_id=native_session_id,
            stage_machine_id=stage_machine_id,
            stage_root=stage_root,
            ending_signal=ending_signal,
        )
    )
    binding = scope.binding
    if (
        binding.origin_turn_id != root_experiment_loop_operation_id(execution)
        or binding.project_id != task.project_id
        or binding.agent_id != request.chat_id
        or binding.machine_id != request.run_on
        or scope.node_id != request.node_id
        or scope.episode_id != request.control_episode_id
        or request.patch_kind != "experiment_loop"
        or not request.control_node_id
        or request.control_invocation is None
        or request.provider is None
    ):
        raise ValueError("Experiment handoff binding does not match the current task scope.")
    stored_watchers, _stored_episode = execution.store.commit_experiment_episode_handoff(
        watcher_records,
        binding=binding,
        episode_id=scope.episode_id,
        control_node_id=request.control_node_id,
        invocation=request.control_invocation,
        provider=request.provider,
        operation_id=execution.operation_id,
        native_session_id=native_session_id,
        stage_machine_id=stage_machine_id,
        stage_root=stage_root,
        graph_result=graph_result,
        context_baseline=context_baseline,
        stops=stops,
        ending_signal=ending_signal,
        replace_binding=replacement_authorized,
        replacement_provenance=replacement_provenance,
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "experiment_episode_binding",
        {
            "episode_id": request.control_episode_id,
            "invocation": request.control_invocation,
            "provider": request.provider,
            "machine_id": request.run_on,
            "stage_machine_id": stage_machine_id,
            "stage_root": stage_root,
            "graph_result": graph_result,
            "watcher_ids": [item.watcher_id for item in stored_watchers],
            "binding_replaced": replacement_authorized,
        },
    )
    return stored_watchers


async def stage_experiment_loop_context(
    service: ProjectService,
    request: RunRequest,
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    token: str,
    continuation: str,
) -> tuple[str, str]:
    """Stage irreducible loop control plus separately readable watcher state."""

    if not request.control_node_id or request.control_revision is None:
        raise ValueError("Experiment-loop work is missing its RCP control binding.")
    if (
        not request.control_episode_id
        or request.control_invocation is None
        or request.control_invocation_ceiling is None
    ):
        raise ValueError("Experiment-loop work is missing its episode invocation binding.")
    if execution is None:
        raise ValueError("Experiment-loop work requires a durable RCP operation.")

    phase = experiment_loop_phase(request, continuation)
    state, watcher_state = await asyncio.gather(
        asyncio.to_thread(service.history.state),
        asyncio.to_thread(_watcher_state, execution, request.chat_id, request.watcher_ids),
    )
    watcher_state_path = _stage_json_task_input(
        local_stage,
        remote_stage,
        f"task-{token}-experiment-watchers.json",
        watcher_state,
    )
    drift = decision_drift(state, request.control_decision_bundle)
    if request.control_invocation > request.control_invocation_ceiling:
        raise ValueError("Experiment-loop invocation exceeds its pinned ceiling.")
    control_path = _stage_json_task_input(
        local_stage,
        remote_stage,
        f"task-{token}-experiment-control-{phase}.json",
        {
            "phase": phase,
            "episode_id": request.control_episode_id,
            "invocation": request.control_invocation,
            "invocation_ceiling": request.control_invocation_ceiling,
            "remaining_invocations": (
                request.control_invocation_ceiling - request.control_invocation
            ),
            "decision_bundle": [
                item.model_dump(mode="json") for item in request.control_decision_bundle
            ],
            "decision_drift": [item.model_dump(mode="json") for item in drift],
            "completion_criteria": request.control_completion_criteria,
            "delivered_watcher_ids": request.watcher_ids,
            "watcher_state_path": watcher_state_path,
        },
    )
    return control_path, watcher_state_path
