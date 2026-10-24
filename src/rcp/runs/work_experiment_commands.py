"""A Work turn starting an Experiment its human asked for, as their Run would."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import (
    CommandResponse,
    EpisodeCommandRequest,
    ExperimentKickoffArguments,
)
from rcp.agents.staged_command_client import experiment_kickoff_usage
from rcp.core.models import CLOSED_EXPERIMENT_STATUSES
from rcp.limits import AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES
from rcp.runs.experiment_admission import WorkExperimentStart

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.transport.workspace_mailbox import RunStageMailbox

_RECEIPT = "work_experiment_kickoff"


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


def handle_work_experiment_kickoff(
    execution: AgentTaskExecution,
    mailbox: RunStageMailbox | None,
    request: EpisodeCommandRequest,
) -> CommandResponse:
    """Start one Experiment episode, or answer a replay of the same key with it."""

    def refuse(message: str) -> CommandResponse:
        return CommandResponse(request_id=request.request_id, status="invalid", message=message)

    arguments = request.arguments
    if not isinstance(arguments, ExperimentKickoffArguments):
        return refuse("A Work turn may only use episode --kick-off-experiment.")
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
    """The kickoff usage a Work turn renders when its handler grants ``episode``."""

    closed = json.dumps(sorted(CLOSED_EXPERIMENT_STATUSES))
    return (
        "Experiment kickoff: only when the human asks you to run or start an Experiment "
        "(in a chat on that Experiment, 'run it' counts). Otherwise suggest one in your reply. "
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
