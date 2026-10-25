"""A Work turn starting or resuming an Experiment its human asked for, as their Run would."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import (
    CommandResponse,
    EpisodeCommandRequest,
    EpisodeControlArguments,
    ExperimentKickoffArguments,
)
from rcp.agents.staged_command_client import experiment_kickoff_usage
from rcp.core.models import CLOSED_EXPERIMENT_STATUSES
from rcp.limits import AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES
from rcp.runs.experiment_admission import (
    WorkExperimentResume,
    WorkExperimentResumer,
    WorkExperimentStart,
)

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.storage import AppStore, EpisodeRecord
    from rcp.transport.workspace_mailbox import RunStageMailbox

_RECEIPT = "work_experiment_kickoff"
_RESUME_RECEIPT = "work_experiment_resume"


def work_experiment_allowed(execution: AgentTaskExecution | None) -> bool:
    """Only a human's ordinary Work chat, or a wake in it, may start an Experiment.

    An Experiment turn starting another would chain episodes nobody authorized,
    an artifact edit is scoped to its artifact, and a graph repair to its Patch.
    """

    if execution is None or execution.experiment_starter is None:
        return False
    from rcp.runs.tasks.compute_commands import compute_recovery_lineage

    store = execution.store
    task = store.agent_task(execution.operation_id)
    lineage = compute_recovery_lineage(
        store, execution.operation_id, continuation=execution.continuation
    )
    # Graph repair only reflects the turn's finished work; it authorizes no new effect.
    if execution.continuation == "graph_repair" or any(
        store.agent_task_continuation_cause(operation_id) == "graph_repair"
        for operation_id in lineage
    ):
        return False
    return (
        task is not None
        and task.kind in {"node_chat", "project_chat"}
        and task.episode_id is None
        and task.request.get("mode") == "work"
        and task.request.get("patch_kind", "work") == "work"
        and not task.request.get("artifact_edit")
    )


def paused_experiment_refusal(
    project_id: str, episode: EpisodeRecord | None, store: AppStore
) -> str | None:
    """Why ``episode`` cannot resume with its unused turns, or None when it can.

    Only an Experiment that paused for its human resumes this way; one that ran
    out of turns or finished takes Add N turns or a new episode instead.
    """

    if episode is None or episode.project_id != project_id:
        return "No such episode in this project."
    if episode.mode != "experiment_loop":
        return "Only an Experiment episode can be resumed."
    if episode.ending != "human_pause":
        return "Only an Experiment that paused for its human can be resumed."
    if store.episode_continuation(episode.episode_id) is not None:
        return "This episode has already been continued."
    if unused_turns(episode) < 1:
        return "This episode has no unused turns."
    return None


def unused_turns(episode: EpisodeRecord) -> int:
    return episode.invocation_ceiling - episode.invocations_used


def handle_work_episode_command(
    execution: AgentTaskExecution,
    mailbox: RunStageMailbox | None,
    request: EpisodeCommandRequest,
) -> CommandResponse:
    """Route a Work turn's ``episode`` command to kickoff or resume."""

    arguments = request.arguments
    if isinstance(arguments, EpisodeControlArguments) and arguments.action == "resume":
        return _schedule_work_experiment_resume(execution, request, arguments)
    if not isinstance(arguments, ExperimentKickoffArguments):
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="A Work turn may only use episode --kick-off-experiment or --resume.",
        )
    return _handle_work_experiment_kickoff(execution, mailbox, request, arguments)


def _schedule_work_experiment_resume(
    execution: AgentTaskExecution,
    request: EpisodeCommandRequest,
    arguments: EpisodeControlArguments,
) -> CommandResponse:
    """Record the resume; it starts once this turn succeeds.

    The paused episode resumes its own session, which is usually this chat's,
    and this turn's Patch lands only when the turn ends, so nothing starts now.
    """

    from rcp.runs.tasks.compute_commands import compute_recovery_lineage

    store = execution.store
    task = store.agent_task(execution.operation_id)
    episode = store.episode(arguments.episode_id)
    refusal = (
        "Experiment resume is unavailable for this turn."
        if task is None
        else paused_experiment_refusal(task.project_id, episode, store)
    )
    if refusal is not None:
        return CommandResponse(request_id=request.request_id, status="invalid", message=refusal)
    assert episode is not None
    lineage = compute_recovery_lineage(
        store, execution.operation_id, continuation=execution.continuation
    )
    # The oldest attempt names the turn, so a Resume or Retry replays the same continuation.
    request_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:work:{lineage[-1]}:resume:{request.idempotency_key}")
    )
    store.record_agent_task_receipt(
        execution.operation_id,
        _RESUME_RECEIPT,
        {"episode_id": episode.episode_id, "request_id": request_id},
        tier="diagnostic",
    )
    return CommandResponse(
        request_id=request.request_id,
        status="ok",
        result={
            "disposition": "scheduled",
            "episode_id": episode.episode_id,
            "turns": unused_turns(episode),
        },
    )


def start_scheduled_experiment_resumes(
    store: AppStore, operation_id: str, resumer: WorkExperimentResumer
) -> None:
    """Resume what a finished Work turn scheduled, once it succeeded.

    A failure is recorded on the turn; the Resume button stays available.
    """

    task = store.agent_task(operation_id)
    if task is None or task.status != "succeeded":
        return
    scheduled: dict[str, str] = {}
    for receipt in store.agent_task_receipts_by_category(operation_id, _RESUME_RECEIPT):
        scheduled.setdefault(str(receipt.payload["episode_id"]), str(receipt.payload["request_id"]))
    for episode_id, request_id in scheduled.items():
        try:
            resumer(
                WorkExperimentResume(
                    source_operation_id=operation_id,
                    episode_id=episode_id,
                    request_id=request_id,
                )
            )
        except Exception as exc:
            store.record_agent_task_event(
                operation_id,
                f"Experiment episode {episode_id} did not resume: {exc}",
                level="warning",
            )
        else:
            store.record_agent_task_event(operation_id, f"Experiment episode {episode_id} resumed.")


def _handle_work_experiment_kickoff(
    execution: AgentTaskExecution,
    mailbox: RunStageMailbox | None,
    request: EpisodeCommandRequest,
    arguments: ExperimentKickoffArguments,
) -> CommandResponse:
    """Start one Experiment episode, or answer a replay of the same key with it."""

    def refuse(message: str) -> CommandResponse:
        return CommandResponse(request_id=request.request_id, status="invalid", message=message)

    if arguments.at is not None or arguments.target is not None or arguments.seed_file is not None:
        return refuse(
            "A Work turn starts the Experiment on its own graph; omit the branch options."
        )
    if arguments.goal_file is None:
        return refuse("Experiment kickoff from Work requires --goal-file.")
    if mailbox is None or execution.experiment_starter is None:
        return CommandResponse(
            request_id=request.request_id,
            status="unavailable",
            message="Experiment kickoff is unavailable for this turn.",
        )
    try:
        goal = mailbox.read_text(arguments.goal_file, max_bytes=AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES)
    except FileNotFoundError:
        return refuse(f"Goal file {arguments.goal_file!r} does not exist in this turn's workspace.")
    except ValueError as exc:
        return refuse(str(exc))
    if not goal.strip():
        return refuse(f"Goal file {arguments.goal_file!r} must not be blank.")

    from rcp.runs.tasks.compute_commands import compute_recovery_lineage

    store = execution.store
    lineage = compute_recovery_lineage(
        store, execution.operation_id, continuation=execution.continuation
    )
    # The oldest attempt names the turn, so a Resume or Retry replays the same episode.
    episode_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:work:{lineage[-1]}:episode:{request.idempotency_key}")
    )
    intent = hashlib.sha256(
        json.dumps(
            {**arguments.model_dump(mode="json"), "goal": goal},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    previous = next(
        (
            receipt.payload
            for operation_id in lineage
            for receipt in store.agent_task_receipts_by_category(operation_id, _RECEIPT)
            if receipt.payload.get("episode_id") == episode_id
        ),
        None,
    )
    if previous is not None and previous.get("intent_sha256") != intent:
        return refuse("This key already started a different Experiment; use a new key.")
    episode = store.episode(episode_id)
    if episode is None:
        if previous is None:
            store.record_agent_task_receipt(
                execution.operation_id,
                _RECEIPT,
                {"episode_id": episode_id, "intent_sha256": intent},
                tier="diagnostic",
            )
        try:
            execution.experiment_starter(
                WorkExperimentStart(
                    source_operation_id=execution.operation_id,
                    episode_id=episode_id,
                    node_id=arguments.node_id,
                    goal=goal,
                    invocation_limit=arguments.invocation_limit,
                    provider=arguments.provider,
                    model=arguments.model,
                    reasoning=arguments.reasoning,
                )
            )
        except ValueError as exc:
            return refuse(str(exc))
        except Exception as exc:
            return CommandResponse(
                request_id=request.request_id, status="unavailable", message=str(exc)
            )
        episode = store.episode(episode_id)
        # Admission commits the episode with its first task before returning.
        assert episode is not None
        disposition = "created"
    else:
        disposition = "existing"
    store.record_agent_task_event(
        execution.operation_id,
        f"Experiment {arguments.node_id} {disposition}: episode {episode_id}.",
    )
    return CommandResponse(
        request_id=request.request_id,
        status="ok",
        result={
            "disposition": disposition,
            "episode_id": episode_id,
            "node_id": arguments.node_id,
            "status": episode.status,
            "invocation_limit": episode.invocation_ceiling,
        },
    )


def work_experiment_command_usage(client: str, profile_choices: str) -> str:
    """The kickoff and resume usage a Work turn renders when its handler grants ``episode``."""

    closed = json.dumps(sorted(CLOSED_EXPERIMENT_STATUSES))
    return (
        'Experiment resume: when this chat\'s loop status shows ending "human_pause", that '
        "episode stopped to wait for its human. If the human's message says what it waited "
        'for is done ("done" counts) or asks to continue, resolve what it waited for in your '
        "Patch, then run "
        f"`{client} episode --key <key> --resume <episode-id>` with the loop status's "
        "episode_id. Never tell the human to press a button for it: the episode resumes its "
        "own session with its unused turns once this reply ends, after your Patch lands. Say "
        "so in your reply.\n"
        "Experiment kickoff starts a new episode: only when the human asks you to run or start "
        "an Experiment (in a chat on that Experiment, 'run it' counts) and no paused episode "
        "should resume instead. Otherwise suggest one in your reply. "
        "Before starting, audit the Experiment and every node one edge away (its Hypotheses, "
        "Decisions, Blockers, and Evidence). If anything is wrong or missing, fix it in this "
        "turn's Patch, do not start, and say so: graph edits land when the turn ends, so the "
        "human's next message starts it on the fixed graph. Only an Experiment already on the "
        "graph and ready can start.\n"
        f"`{client} episode --key <key> "
        f"{experiment_kickoff_usage(goal_required=True, child_targets=False)}`\n"
        "Write the goal first as a direct regular UTF-8 file in this run workspace, beside "
        "the Patch, never a nested path or symlink: what the human asked to run and the "
        "specifics from this chat (settings, data, what to compare). The limit may not exceed "
        "the Experiment's own invocation limit; it defaults to that limit. Pick the provider, "
        "model, and effort that fit the work, or omit them for Settings. Ready profiles:\n"
        f"{profile_choices}\n"
        "The same key and arguments replay the same episode after a Resume or Retry. After it "
        "starts, tell the human what you started (node, goal, limit, profile) and arm a "
        f'watch.json graph item {{"node_id":"<node-id>","status_in":{closed}}} so this chat '
        "resumes when the Experiment closes."
    )
