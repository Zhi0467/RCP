from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from rcp.agents.schema import parse_agent_patch_json
from rcp.background import AgentTaskExecution
from rcp.control import decision_drift
from rcp.core.models import ExperimentDecisionPin
from rcp.runs.shared import _stage_json_task_input
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.storage import (
    ExperimentEpisodeRecord,
    ExperimentLoopRuntime,
    ExperimentWatcherResourceRecord,
    WatcherRecord,
    WatcherStopRequest,
)
from rcp.transport import RemoteRunStage, StateUnavailable
from rcp.watchers import ExperimentWatchSpec, WatcherBinding, WatcherCheckResult

_EXIT_STATUSES = frozenset({"completed"})
_EPISODE_CONTEXT_CANDIDATE_ROLE = "experiment_episode_context_candidate"

EpisodeWakeReadiness = Literal["ready", "transient", "incompatible", "unavailable"]

ExperimentLoopPhase = Literal[
    "initial_run",
    "human_reauthorization",
    "watcher_wake",
    "resume",
    "retry",
]


@dataclass(frozen=True)
class EpisodeWakePreflight:
    """Whether a completed group may claim the current episode's native session.

    `transient` and `incompatible` both leave the watchers completed and
    unnotified for a later pass; only `unavailable` is a durable Needs-action
    fact about the episode itself.
    """

    readiness: EpisodeWakeReadiness
    diagnostic: str | None = None
    session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None


_EXPERIMENT_WATCH_OUTPUT_PREFIX = "experiment-watch-"
_EXPERIMENT_WATCH_OUTPUT_SUFFIX = ".json"


@dataclass(frozen=True)
class StagedExperimentWatcherResource:
    """One live node-and-episode watcher resource exposed to a chat turn."""

    resource: ExperimentWatcherResourceRecord
    watcher_state_path: str
    watch_path: str

    def prompt_value(self) -> dict[str, str]:
        return {
            "control_node_id": self.resource.control_node_id,
            "episode_id": self.resource.episode_id,
            "execution_host": self.resource.execution_host,
            "watcher_state_path": self.watcher_state_path,
            "watch_path": self.watch_path,
        }


def experiment_watcher_output_name(control_node_id: str) -> str:
    """Return the stable physical filename that selects one Experiment resource."""

    digest = hashlib.sha256(control_node_id.encode("utf-8")).hexdigest()
    return f"{_EXPERIMENT_WATCH_OUTPUT_PREFIX}{digest}{_EXPERIMENT_WATCH_OUTPUT_SUFFIX}"


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


def clear_stale_experiment_watcher_outputs(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    resources: list[ExperimentWatcherResourceRecord],
) -> None:
    """Remove prior-turn resource outputs from a reusable chat workspace."""

    if remote_stage is not None:
        names = set(_experiment_watcher_output_names(workspace, remote_stage))
    else:
        try:
            with os.scandir(workspace) as entries:
                names = {
                    entry.name
                    for entry in entries
                    if entry.name.startswith(_EXPERIMENT_WATCH_OUTPUT_PREFIX)
                    and entry.name.endswith(_EXPERIMENT_WATCH_OUTPUT_SUFFIX)
                }
        except OSError as exc:
            raise StateUnavailable(f"could not inspect stale chat watcher outputs: {exc}") from exc
    # Exact current paths are removed even when a previous agent replaced one
    # with a symlink; the remote regular-file listing deliberately omits symlinks.
    names.update(experiment_watcher_output_name(item.control_node_id) for item in resources)
    for name in sorted(names):
        if remote_stage is not None:
            remote_stage.remove_workspace_file(name)
        else:
            path = workspace / name
            if path.is_dir() and not path.is_symlink():
                raise ValueError(f"Experiment watcher output path is an unsafe directory: {name}")
            path.unlink(missing_ok=True)


def read_experiment_watcher_outputs(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> dict[str, str]:
    """Read each physical Experiment watcher file written by this turn."""

    outputs: dict[str, str] = {}
    for name in _experiment_watcher_output_names(workspace, remote_stage):
        if remote_stage is not None:
            outputs[name] = remote_stage.read_text(remote_stage.workspace / name)
        else:
            outputs[name] = (workspace / name).read_text(encoding="utf-8")
    return outputs


def preflight_episode_wake(
    runtime: ExperimentLoopRuntime,
    episode: ExperimentEpisodeRecord | None,
    group: list[WatcherRecord],
) -> EpisodeWakePreflight:
    """Prove the episode session and exact stage before any claim or budget spend.

    Watcher provenance never selects the session — the newest human-authorized
    episode does — so an older group that no longer matches the current binding
    stays pending instead of switching sessions.
    """

    if not group:
        raise ValueError("An Experiment watcher wake requires a completed watcher group.")
    if episode is None or not episode.session_bound:
        return EpisodeWakePreflight(
            readiness="unavailable",
            diagnostic=(
                "This episode has no validated native provider session to continue. "
                "Use Stop loop and press Run to start a fresh episode."
            ),
        )
    if runtime.episode_id != episode.episode_id:
        return EpisodeWakePreflight(
            readiness="unavailable",
            diagnostic=(
                "The completed watcher group no longer belongs to the current Experiment episode. "
                "Use Stop loop and press Run to start a fresh episode."
            ),
        )
    mismatched: list[str] = []
    for record in group:
        continuation = record.continuation
        for label, expected, actual in (
            ("project", episode.project_id, record.project_id),
            ("Experiment", episode.control_node_id, record.node_id),
            ("episode", episode.episode_id, record.experiment_episode_id),
            ("check host", episode.execution_host, record.execution_host),
            ("continuation Experiment", episode.control_node_id, continuation.control_node_id),
            ("continuation episode", episode.episode_id, continuation.control_episode_id),
            ("Patch authority", "experiment_loop", continuation.patch_kind),
        ):
            if expected != actual and label not in mismatched:
                mismatched.append(label)
    if mismatched:
        return EpisodeWakePreflight(
            readiness="incompatible",
            diagnostic=(
                "This completed watcher group does not match the current node-attached episode's "
                f"{', '.join(mismatched)}; it stays pending for an explicit human Run."
            ),
        )
    assert episode.stage_root is not None
    if episode.stage_host:
        exists = RemoteRunStage(episode.stage_host).directory_exists(episode.stage_root)
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
                "Use Stop loop and press Run to start a fresh episode."
            ),
        )
    return EpisodeWakePreflight(
        readiness="ready",
        session_id=episode.native_session_id,
        stage_host=episode.stage_host,
        stage_root=episode.stage_root,
    )


def experiment_watcher_delivery_request(
    group: list[WatcherRecord],
    *,
    trigger: Literal["experiment_run", "watcher"],
    episode_id: str,
    invocation: int,
    invocation_ceiling: int,
    control_revision: int,
    decision_bundle: list[ExperimentDecisionPin],
    completion_criteria: list[str],
    session_id: str | None = None,
) -> RunRequest:
    """Build one explicitly attributed Experiment watcher delivery request.

    An automatic wake carries the episode's session id; a human Run that
    reauthorizes pending completion carries none, because it is a fresh episode.
    """

    if trigger == "experiment_run" and session_id:
        raise ValueError("A human Experiment Run always starts a fresh native session.")

    first = group[0]
    continuation = first.continuation
    if continuation.patch_kind != "experiment_loop" or not continuation.control_node_id:
        raise ValueError("An Experiment watcher must retain its origin control binding.")
    try:
        UUID(continuation.control_episode_id or "")
    except ValueError as exc:
        raise ValueError("An Experiment watcher has an invalid origin episode id.") from exc
    if (
        continuation.control_invocation is None
        or continuation.control_invocation_ceiling is None
        or continuation.control_invocation > continuation.control_invocation_ceiling
    ):
        raise ValueError("An Experiment watcher has an invalid origin invocation binding.")
    return RunRequest(
        provider=continuation.provider,
        # Older persisted watcher envelopes used null for the provider default.
        # Make it explicit before profile resolution so a later Settings change
        # cannot reinterpret this frozen continuation.
        model=continuation.model if continuation.model is not None else "",
        reasoning=continuation.reasoning,
        run_on=continuation.run_on,
        run_truth_scope=continuation.run_truth_scope,
        chat_scope="node" if first.origin_task_kind == "node_chat" else "project",
        node_id=first.node_id,
        message="Continue the bounded Experiment loop from its staged watcher state.",
        chat_id=first.chat_id,
        session_id=session_id,
        mode="work",
        trigger=trigger,
        patch_kind="experiment_loop",
        control_node_id=continuation.control_node_id,
        control_revision=control_revision,
        control_episode_id=episode_id,
        control_invocation=invocation,
        control_invocation_ceiling=invocation_ceiling,
        control_decision_bundle=decision_bundle,
        control_completion_criteria=completion_criteria,
        workflow_ids=continuation.workflow_ids,
        skill_ids=continuation.skill_ids,
        invoked_workflow_ids=[],
        invoked_skill_ids=[],
        resolved_skill_packages=continuation.resolved_skill_packages,
        watcher_ids=[item.watcher_id for item in group],
    )


def patch_explicitly_exits(patch_text: str | None, control_node_id: str) -> bool:
    """Whether one semantic loop Patch explicitly records a finish or authority pause."""

    if patch_text is None:
        return False
    try:
        patch = parse_agent_patch_json(patch_text)
    except ValueError:
        return False
    operations = patch.model_dump(mode="python", exclude_none=True)["ops"]
    if any(op.get("op") == "create_proposals" and op.get("proposals") for op in operations):
        return True
    created_blockers = {
        node.get("id")
        for op in operations
        if op.get("op") == "create_nodes"
        for node in op.get("nodes", [])
        if node.get("type") == "blocker"
    }
    for op in operations:
        if op.get("op") == "update_nodes":
            for update in op.get("nodes", []):
                changes = update.get("changes", {})
                if (
                    update.get("id") != control_node_id
                    and isinstance(changes, dict)
                    and changes.get("status") in {"ready", "revisit"}
                ):
                    return True
                if (
                    update.get("id") == control_node_id
                    and isinstance(changes, dict)
                    and changes.get("status") in _EXIT_STATUSES
                ):
                    return True
        if op.get("op") == "create_edges" and any(
            edge.get("source") == control_node_id
            and edge.get("relation") == "blocked_by"
            and edge.get("target") in created_blockers
            for edge in op.get("edges", [])
        ):
            return True
    return False


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


def persist_experiment_watchers_idempotently(
    execution: AgentTaskExecution,
    specs: list[ExperimentWatchSpec],
    results: list[WatcherCheckResult],
    binding: WatcherBinding,
    stops: list[WatcherStopRequest] | None = None,
    *,
    expected_watcher_snapshot_token: str | None = None,
) -> list[WatcherRecord]:
    """Persist one validated handoff once across Retry/crash recovery."""

    if len(specs) != len(results):
        raise ValueError("Experiment-loop watcher checks do not match their specifications.")
    created_at = execution.store.now()
    stop_requests = list(stops or [])
    desired: list[WatcherRecord] = []
    for index, (spec, result) in enumerate(zip(specs, results, strict=True)):
        group = getattr(spec, "group", None)
        identity = json.dumps(
            {
                "origin": binding.origin_operation_id,
                "node_id": binding.node_id,
                "episode_id": binding.continuation.control_episode_id,
                "index": index,
                "check_command": spec.check_command,
                "log_path": spec.log_path,
                "cwd": spec.cwd,
                "group": group,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        completed = result.state == "complete"
        desired.append(
            WatcherRecord(
                watcher_id=str(uuid5(NAMESPACE_URL, f"rcp-experiment-watcher:{identity}")),
                project_id=binding.project_id,
                origin_operation_id=binding.origin_operation_id,
                origin_task_kind=binding.origin_task_kind,
                chat_id=binding.chat_id,
                node_id=binding.node_id,
                execution_host=binding.execution_host,
                check_command=spec.check_command,
                log_path=spec.log_path,
                cwd=spec.cwd,
                continuation=binding.continuation,
                status="completed" if completed else "active",
                created_at=created_at,
                last_checked_at=result.checked_at,
                last_exit_code=result.exit_code,
                completed_at=result.checked_at if completed else None,
                group_id=(
                    str(
                        uuid5(
                            NAMESPACE_URL,
                            "rcp-experiment-watcher-group:"
                            f"{binding.origin_operation_id}:{binding.node_id}:"
                            f"{binding.continuation.control_episode_id}:{spec.group}",
                        )
                    )
                    if group is not None
                    else None
                ),
                group_label=group,
            )
        )

    # The store owns the BEGIN IMMEDIATE boundary shared with Stop loop. It
    # atomically deduplicates this deterministic handoff and, when stop intent
    # won the race, persists/returns every watcher as stopped and notified.
    return execution.store.persist_experiment_watchers_idempotently(
        desired,
        stops=stop_requests,
        binding=binding,
        expected_watcher_snapshot_token=expected_watcher_snapshot_token,
    )


def _stopped_history_episode_id(
    execution: AgentTaskExecution,
    project_id: str,
    control_node_id: str,
    episode_id: str,
) -> str | None:
    """The immediately preceding episode's id, but only when a human stopped it.

    A fresh Run after S72 **Stop loop** stages that episode's watcher records so
    the agent can inspect external work that may still exist. Any other preceding
    episode contributes nothing: a stopped observer is context, never a trigger.
    """

    previous = execution.store.previous_experiment_episode(
        project_id,
        control_node_id,
        episode_id,
    )
    if previous is None or previous.stop_requested_at is None:
        return None
    return previous.episode_id


def _watcher_state(
    execution: AgentTaskExecution,
    control_node_id: str,
    delivered_watcher_ids: list[str],
    episode_id: str,
    phase: ExperimentLoopPhase,
) -> list[dict[str, object]]:
    """Return current operational watcher evidence without duplicating loop control.

    Every shape carries the relevant active, degraded, and completed-unnotified
    observers. A wake and a human reauthorization additionally retain their own
    delivered group after its atomic claim marked it notified, and a fresh Run
    after a human stop additionally retains that stopped episode's records.
    """

    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The Experiment-loop operation is no longer available.")
    delivered = set(delivered_watcher_ids)
    all_records = execution.store.watchers(task.project_id)
    delivered_group_ids = {
        record.group_id
        for record in all_records
        if record.watcher_id in delivered and record.group_id is not None
    }
    stopped_history_episode_id = (
        _stopped_history_episode_id(execution, task.project_id, control_node_id, episode_id)
        if phase == "initial_run"
        else None
    )
    records = [
        record
        for record in all_records
        if record.continuation.patch_kind == "experiment_loop"
        and record.continuation.control_node_id == control_node_id
        and (
            record.watcher_id in delivered
            or record.group_id in delivered_group_ids
            or (record.status in {"active", "degraded"} and not record.notified)
            or (record.status == "completed" and not record.notified)
            or (record.status == "stopped" and record.continuation.control_episode_id == episode_id)
            or (
                stopped_history_episode_id is not None
                and record.status == "stopped"
                and execution.store.experiment_watcher_compatible_with_episode(
                    record.watcher_id,
                    stopped_history_episode_id,
                )
            )
        )
    ]
    return [
        {
            "watcher_id": record.watcher_id,
            "origin_operation_id": record.origin_operation_id,
            "execution_host": record.execution_host,
            "check_command": record.check_command,
            "log_path": record.log_path,
            "cwd": record.cwd,
            "status": record.status,
            "created_at": record.created_at,
            "last_checked_at": record.last_checked_at,
            "last_exit_code": record.last_exit_code,
            "last_error": record.last_error,
            "completed_at": record.completed_at,
            "next_check_at": record.next_check_at,
            "consecutive_error_count": record.consecutive_error_count,
            "group_id": record.group_id,
            "group_label": record.group_label,
            "notified": record.notified,
            "notification_operation_id": record.notification_operation_id,
            "stopped_by": record.stopped_by,
            "stop_reason": record.stop_reason,
            "stopped_at": record.stopped_at,
            "stop_operation_id": record.stop_operation_id,
            "episode_id": record.continuation.control_episode_id,
            "invocation": record.continuation.control_invocation,
            "invocation_ceiling": record.continuation.control_invocation_ceiling,
            "control_revision": record.continuation.control_revision,
            "decision_bundle": record.continuation.control_decision_bundle,
        }
        for record in records
    ]


async def stage_chat_experiment_watcher_resources(
    request: RunRequest,
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    workspace: Path,
    token: str,
    clear_stale: bool,
) -> list[StagedExperimentWatcherResource]:
    """Stage live Experiment watcher state within the chat's resolved node scope."""

    if execution is None:
        return []
    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The chat operation is no longer available for watcher resource staging.")
    all_resources = execution.store.experiment_watcher_resources(task.project_id)
    if clear_stale:
        clear_stale_experiment_watcher_outputs(workspace, remote_stage, all_resources)

    def visible_resources(
        resources: list[ExperimentWatcherResourceRecord],
    ) -> list[ExperimentWatcherResourceRecord]:
        return (
            [item for item in resources if item.control_node_id == request.node_id]
            if request.chat_scope == "node"
            else resources
        )

    visible = visible_resources(all_resources)
    for _attempt in range(3):
        watcher_states = await asyncio.gather(
            *(
                asyncio.to_thread(
                    _watcher_state,
                    execution,
                    resource.control_node_id,
                    [],
                    resource.episode_id,
                    "resume",
                )
                for resource in visible
            )
        )
        refreshed = visible_resources(execution.store.experiment_watcher_resources(task.project_id))
        if {item.control_node_id: item.watcher_snapshot_token for item in visible} == {
            item.control_node_id: item.watcher_snapshot_token for item in refreshed
        }:
            break
        visible = refreshed
    else:
        raise StateUnavailable(
            "Experiment watcher state changed repeatedly while staging this chat turn."
        )
    staged: list[StagedExperimentWatcherResource] = []
    for resource, watcher_state in zip(visible, watcher_states, strict=True):
        digest = hashlib.sha256(resource.control_node_id.encode("utf-8")).hexdigest()[:16]
        watcher_state_path = _stage_json_task_input(
            local_stage,
            remote_stage,
            f"task-{token}-experiment-watchers-{digest}.json",
            watcher_state,
        )
        staged.append(
            StagedExperimentWatcherResource(
                resource=resource,
                watcher_state_path=watcher_state_path,
                watch_path=str(
                    workspace / experiment_watcher_output_name(resource.control_node_id)
                ),
            )
        )
    return staged


def _delivered_watcher_groups(
    watcher_state: list[dict[str, object]],
    delivered_watcher_ids: list[str],
) -> list[dict[str, object]]:
    """Retain each delivered group's identity and complete staged membership."""

    delivered = set(delivered_watcher_ids)
    delivered_group_ids = {
        record["group_id"]
        for record in watcher_state
        if isinstance(record.get("group_id"), str) and record.get("watcher_id") in delivered
    }
    return [
        {
            "group_id": group_id,
            "label": next(
                record.get("group_label")
                for record in watcher_state
                if record.get("group_id") == group_id
            ),
            "members": [record for record in watcher_state if record.get("group_id") == group_id],
        }
        for group_id in sorted(delivered_group_ids)
    ]


def experiment_loop_phase(request: RunRequest, continuation: str) -> ExperimentLoopPhase:
    """Name the agent-facing phase for one bounded-loop turn."""

    if continuation == "resume":
        return "resume"
    if continuation in {"retry", "handoff"}:
        return "retry"
    if request.trigger == "experiment_run" and request.watcher_ids:
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
    be current when recovery finishes. Fresh human turns and automatic wakes each
    establish their own immutable candidate.
    """

    if execution.continuation in {"resume", "retry"}:
        root_operation_id = root_experiment_loop_operation_id(execution)
        content = execution.store.agent_task_contract(
            root_operation_id,
            _EPISODE_CONTEXT_CANDIDATE_ROLE,
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
    return "no graph change"


def commit_experiment_episode_binding(
    execution: AgentTaskExecution,
    request: RunRequest,
    *,
    native_session_id: str | None,
    execution_host: str,
    stage_host: str | None,
    stage_root: str | None,
    graph_result: str,
    watcher_ids: list[str],
    context_baseline: dict[str, object],
) -> None:
    """Bind this episode to the session and stage a later automatic wake resumes.

    Only a turn with a mechanically successful joint Patch/watch handoff commits,
    so a provider, task, or handoff failure never moves the binding or baseline.
    A graph rejection is still recorded truthfully because the turn and its
    accepted watcher handoff completed.
    """

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
            or episode.stage_host != stage_host
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
                "stage_host": episode.stage_host,
                "stage_root": episode.stage_root,
            },
            "replacement": {
                "provider": request.provider,
                "native_session_id": native_session_id,
                "stage_host": stage_host,
                "stage_root": stage_root,
            },
        }
        if replacement_authorized and episode is not None
        else None
    )
    execution.store.commit_experiment_episode_turn(
        episode_id=request.control_episode_id,
        project_id=task.project_id,
        control_node_id=request.control_node_id,
        provider=request.provider,
        execution_machine=request.run_on,
        execution_host=execution_host,
        native_session_id=native_session_id,
        stage_host=stage_host,
        stage_root=stage_root,
        chat_id=request.chat_id,
        operation_id=execution.operation_id,
        invocation=request.control_invocation,
        graph_result=graph_result,
        watcher_ids=watcher_ids,
        context_baseline=context_baseline,
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
            "execution_machine": request.run_on,
            "stage_host": stage_host,
            "stage_root": stage_root,
            "graph_result": graph_result,
            "watcher_ids": watcher_ids,
            "binding_replaced": replacement_authorized,
        },
    )


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
        asyncio.to_thread(
            _watcher_state,
            execution,
            request.control_node_id,
            request.watcher_ids,
            request.control_episode_id,
            phase,
        ),
    )
    watcher_state_path = _stage_json_task_input(
        local_stage,
        remote_stage,
        f"task-{token}-experiment-watchers.json",
        watcher_state,
    )
    delivered_groups = _delivered_watcher_groups(watcher_state, request.watcher_ids)
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
            "delivered_watcher_groups": delivered_groups,
            "watcher_state_path": watcher_state_path,
        },
    )
    return control_path, watcher_state_path
