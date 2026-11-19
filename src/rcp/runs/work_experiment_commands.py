"""A Work turn's ``episode`` command: Experiments its human asked for, and episode controls."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import (
    AutoResearchKickoffArguments,
    CommandResponse,
    EpisodeCommandRequest,
    EpisodeControlArguments,
    ExperimentKickoffArguments,
)
from rcp.agents.staged_command_client import (
    EPISODE_RESUME,
    episode_control_usage,
    experiment_kickoff_usage,
)
from rcp.approval_actions import ExperimentStartAction
from rcp.core.models import CLOSED_EXPERIMENT_STATUSES, Experiment
from rcp.runs.approval_cards import APPROVAL_OUTCOMES, approval_pins
from rcp.runs.experiment_admission import (
    WorkExperimentResume,
    WorkExperimentResumer,
)
from rcp.runs.work_episode_controls import (
    chat_started_episode,
    control_own_turn,
    handle_work_auto_research_kickoff,
    handle_work_episode_controls,
    intent_sha256,
    previous_receipt,
    read_workspace_text,
    stop_own_episode,
    turn_keyed_id,
    turn_lineage,
)
from rcp.service import ProjectService

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.projects import ProjectCatalog
    from rcp.storage import AppStore, EpisodeRecord
    from rcp.storage.question_models import QuestionRecord
    from rcp.transport.workspace_mailbox import RunStageMailbox

_RECEIPT = "work_experiment_kickoff"
_RESUME_RECEIPT = "work_experiment_resume"


def work_experiment_allowed(execution: AgentTaskExecution | None) -> bool:
    """Only a human's ordinary Work chat, or a wake in it, may raise an Experiment plan card."""

    return (
        execution is not None
        and execution.experiment_card_raiser is not None
        and member_work_chat_turn(execution)
    )


def member_work_chat_turn(execution: AgentTaskExecution | None) -> bool:
    """A human's ordinary Work chat turn, or a wake in it, acting for that human.

    An Experiment turn starting another would chain episodes nobody authorized,
    an artifact edit is scoped to its artifact, and a graph repair to its Patch.
    """

    if execution is None:
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
        and task.dispatch_authority is not None
        and task.dispatch_authority.is_work_chat
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
    """Route a Work turn's ``episode`` command to its kickoff or control.

    Resume of an Experiment that ended to wait for its human continues it with
    its unused turns; resume of a live episode resumes its paused turn.
    """

    arguments = request.arguments
    if isinstance(arguments, ExperimentKickoffArguments):
        return _handle_work_experiment_kickoff(execution, mailbox, request, arguments)
    if isinstance(arguments, AutoResearchKickoffArguments):
        return handle_work_auto_research_kickoff(execution, mailbox, request, arguments)
    if not isinstance(arguments, EpisodeControlArguments):
        return handle_work_episode_controls(execution, mailbox, request, arguments)
    if arguments.action == "stop":
        return stop_own_episode(execution, request, arguments.episode_id)
    episode = execution.store.episode(arguments.episode_id)
    if episode is not None and (episode.mode == "auto_research" or episode.ending is None):
        return control_own_turn(execution, request, arguments.episode_id, "resume")
    return _schedule_work_experiment_resume(execution, request, arguments)


def _schedule_work_experiment_resume(
    execution: AgentTaskExecution,
    request: EpisodeCommandRequest,
    arguments: EpisodeControlArguments,
) -> CommandResponse:
    """Record the resume; it starts once this turn succeeds.

    The paused episode resumes its own session, which is usually this chat's,
    and this turn's Patch lands only when the turn ends, so nothing starts now.
    """

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
    assert episode is not None and task is not None
    root = store.agent_task(episode.root_operation_id) if episode.root_operation_id else None
    runs_here = root is not None and root.request.get("chat_id") == task.request.get("chat_id")
    if not (runs_here or chat_started_episode(store, task, episode.episode_id)):
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="This chat may control only an episode it started; tell the human to use Runs.",
            result={"code": "episode_not_started_here"},
        )
    request_id = turn_keyed_id(turn_lineage(execution), "resume", request.idempotency_key)
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
    """Record a plan card request for when this turn lands, or replay the same key.

    A key that started an episode before plan cards still answers with that episode.
    """

    from rcp.runs.questions import work_ask_authorized

    def refuse(message: str, code: str | None = None) -> CommandResponse:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=message,
            result={"code": code} if code else {},
        )

    if arguments.at is not None or arguments.target is not None or arguments.seed_file is not None:
        return refuse(
            "A Work turn starts the Experiment on its own graph; omit the branch options."
        )
    if arguments.goal_file is None:
        return refuse("An Experiment plan card from Work requires --goal-file.")
    if not (member_work_chat_turn(execution) and work_ask_authorized(execution)):
        return refuse(
            "This turn cannot raise an Experiment plan card.", "experiment_card_unavailable"
        )
    if mailbox is None or execution.experiment_card_raiser is None:
        return CommandResponse(
            request_id=request.request_id,
            status="unavailable",
            message="Experiment plan cards are unavailable for this turn.",
        )
    try:
        goal = read_workspace_text(mailbox, arguments.goal_file, "Goal file")
    except ValueError as exc:
        return refuse(str(exc))

    store = execution.store
    lineage = turn_lineage(execution)
    kickoff_id = turn_keyed_id(lineage, "episode", request.idempotency_key)
    intent = intent_sha256({**arguments.model_dump(mode="json"), "goal": goal})
    episode = store.episode(kickoff_id)
    if episode is not None:
        started = previous_receipt(store, lineage, _RECEIPT, episode_id=kickoff_id)
        if started is not None and started.get("intent_sha256") != intent:
            return refuse("This key already started a different Experiment; use a new key.")
        return CommandResponse(
            request_id=request.request_id,
            status="ok",
            result={
                "disposition": "existing",
                "episode_id": kickoff_id,
                "node_id": arguments.node_id,
                "status": episode.status,
                "invocation_limit": episode.invocation_ceiling,
            },
        )
    previous = previous_receipt(store, lineage, _RECEIPT, kickoff_id=kickoff_id)
    if previous is not None and previous.get("intent_sha256") != intent:
        return refuse("This key already raised a different plan card; use a new key.")
    if previous_receipt(store, (execution.operation_id,), _RECEIPT, kickoff_id=kickoff_id) is None:
        store.record_agent_task_receipt(
            execution.operation_id,
            _RECEIPT,
            {
                "phase": "card_pending",
                "kickoff_id": kickoff_id,
                "intent_sha256": intent,
                "node_id": arguments.node_id,
                "goal": goal,
                "invocation_limit": arguments.invocation_limit,
                "provider": arguments.provider,
                "model": arguments.model,
                "reasoning": arguments.reasoning,
            },
            tier="diagnostic",
        )
    return CommandResponse(
        request_id=request.request_id,
        status="ok",
        result={"disposition": "card_pending", "node_id": arguments.node_id},
    )


def raise_experiment_plan_cards(execution: AgentTaskExecution, catalog: ProjectCatalog) -> None:
    """Raise the plan cards a finished Work turn's kickoffs asked for, from the settled graph.

    Only a succeeded turn raises them. A node that is missing or not an Experiment gets a
    task event instead, and a new card for a node replaces this chat's older pending one.
    """

    store = execution.store
    task = store.agent_task(execution.operation_id)
    if task is None or task.status != "succeeded":
        return
    requests = {
        str(receipt.payload["kickoff_id"]): receipt.payload
        for receipt in store.agent_task_receipts_by_category(execution.operation_id, _RECEIPT)
        if receipt.payload.get("phase") == "card_pending"
    }
    if not requests:
        return
    from rcp.runs.agent_threads import task_owner
    from rcp.runs.questions import question_origin

    origin = question_origin(execution)
    service = task_owner(store, task, catalog=catalog)
    assert isinstance(service, ProjectService) and task.authorized_by is not None
    state = service.history.state()
    for kickoff_id, payload in requests.items():
        node_id = str(payload["node_id"])
        node = state.nodes.get(node_id)
        if not isinstance(node, Experiment):
            store.record_agent_task_event(
                execution.operation_id,
                f"No plan card for {node_id}: it is not an Experiment on this chat's graph.",
                level="warning",
            )
            continue
        action = ExperimentStartAction.model_validate(
            {
                "kind": "experiment_start",
                "project_id": task.project_id,
                "node_id": node_id,
                "branch_id": task.graph_target.branch_id,
                "goal": payload["goal"],
                "invocation_ceiling": payload["invocation_limit"] or node.invocation_ceiling,
                "provider": payload["provider"],
                "model": payload["model"],
                "reasoning": payload["reasoning"],
                "run_truth_scope": task.request.get("run_truth_scope"),
            }
        )
        question = store.create_or_get_question(
            origin=origin,
            key=f"experiment-start:{kickoff_id}",
            question=f"Start {node.title}?",
            choices=["Approve", "Decline"],
            card={
                "action": action.model_dump(mode="json"),
                "pinned": approval_pins(service, store, action, task.authorized_by, state=state),
            },
        )
        _settle_replaced_cards(store, question, node_id)
        store.record_agent_task_event(
            execution.operation_id, f"Plan card raised for Experiment {node_id}."
        )


def _settle_replaced_cards(store: AppStore, newest: QuestionRecord, node_id: str) -> None:
    origin = newest.origin
    older = [
        item.question_id
        for item in store.list_questions(
            project_id=origin.project_id, owner_kind="chat", owner_id=origin.owner_id
        )
        if item.question_id != newest.question_id and item.state == "pending"
    ]
    for question_id, card in store.approval_cards(older).items():
        if (
            card["kind"] == "experiment_start"
            and card["action"]["node_id"] == node_id
            and store.claim_approval_card(question_id)
        ):
            store.settle_approval_card(question_id, {"outcome": "stale"})


def work_experiment_command_usage(client: str, profile_choices: str) -> str:
    """The Experiment plan card and human-pause resume usage of a Work turn granted ``episode``."""

    closed = json.dumps(sorted(CLOSED_EXPERIMENT_STATUSES))
    return (
        'Experiment resume: when this chat\'s loop status shows ending "human_pause", that '
        "episode stopped to wait for its human. If the human's message says what it waited "
        'for is done ("done" counts) or asks to continue, resolve what it waited for in your '
        "Patch, then run "
        f"`{client} {episode_control_usage(EPISODE_RESUME)}` with the loop status's "
        "episode_id. Never tell the human to press a button for it: the episode resumes its "
        "own session with its unused turns once this reply ends, after your Patch lands. Say "
        "so in your reply. To change an Experiment and run it again, stop it, fix the "
        "Experiment in your Patch, and propose it again on a new plan card in a later turn once "
        "the loop status shows it stopped.\n"
        "Experiment plan card: propose a new episode for the human to review, edit, and Start. "
        "Propose one only when the human asks you "
        "to run or start an Experiment (in a chat on that Experiment, 'run it' counts) and no "
        "paused episode should resume instead. Otherwise suggest one in your reply. "
        "Before proposing, audit the Experiment and every node one edge away (its Hypotheses, "
        "Decisions, Blockers, and Evidence). Write or fix the Experiment and its neighbors in "
        "this same turn's Patch: graph edits land when the turn ends, and the card appears "
        "then, drawn from the settled graph. A node that is missing or not an Experiment by "
        "then gets no card and a task event.\n"
        f"`{client} episode --key <key> "
        f"{experiment_kickoff_usage(goal_required=True, child_targets=False)}`\n"
        "Write the goal first as a direct regular UTF-8 file in this run workspace, beside "
        "the Patch, never a nested path or symlink: what the human asked to run and the "
        "specifics from this chat (settings, data, what to compare). How this episode runs "
        "(who builds what, which agent, receipts to collect) goes in the goal, never in the "
        "Experiment's fields, which state only its scientific design. The limit may not exceed "
        "the Experiment's own invocation limit; it defaults to that limit. Pick the provider, "
        "model, and effort that fit the work, or omit them for Settings. Ready profiles:\n"
        f"{profile_choices}\n"
        "The command only proposes: it answers card_pending and starts nothing. The run starts only when a human "
        "presses Start on the card, after reviewing and possibly editing the goal, limit, and "
        "profile. The same key and arguments replay the same request after a Resume or Retry. "
        "Tell the human the plan card will appear when this reply ends. The question snapshot "
        "shows the card's outcome: "
        + "; ".join(f"{outcome}: {meaning}" for outcome, meaning in APPROVAL_OUTCOMES.items())
        + " To revise the plan after the human comments, run the command again with a new key; the new card replaces the old one. Once the card is approved, "
        "tell the human what started (node, goal, limit, profile) and arm a "
        f'watch.json graph item {{"node_id":"<node-id>","status_in":{closed}}} so this chat '
        "resumes when the Experiment closes."
    )
