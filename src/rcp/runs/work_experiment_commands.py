"""A Work turn's ``episode`` command: Experiments its human asked for, and episode controls."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import (
    AutoResearchKickoffArguments,
    CommandResponse,
    EpisodeCommandRequest,
    EpisodeControlArguments,
    ExperimentKickoffArguments,
)
from rcp.agents.staged_command_client import (
    EPISODE_ADD_TURNS,
    EPISODE_RESUME,
    KICKOFF_LIMIT,
    episode_control_usage,
    experiment_kickoff_usage,
)
from rcp.approval_actions import AutoResearchAction, ExperimentStartAction
from rcp.limits import EPISODE_TEXT_MAX_LENGTH
from rcp.runs.approval_cards import (
    APPROVAL_OUTCOMES,
    PLAN_RUN_HERE,
    PLAN_RUN_HERE_MEANING,
    approval_pins,
)
from rcp.runs.experiment_admission import (
    WorkExperimentResume,
    WorkExperimentResumer,
    experiment_node,
)
from rcp.runs.work_episode_controls import (
    _AUTO_RESEARCH_RECEIPT,
    chat_started_episode,
    control_own_turn,
    handle_work_auto_research_kickoff,
    handle_work_episode_controls,
    intent_sha256,
    previous_receipt,
    read_workspace_text,
    stop_own_episode,
    turn_keyed_branch_id,
    turn_keyed_id,
    turn_lineage,
)
from rcp.service import ProjectService
from rcp.storage.approval_cards import PLAN_CARD_RAISED_RECEIPT

if TYPE_CHECKING:
    from collections.abc import Callable

    from rcp.background import AgentTaskExecution
    from rcp.projects import ProjectCatalog
    from rcp.storage import AgentTaskRecord, AppStore, EpisodeRecord
    from rcp.storage.question_models import QuestionOrigin
    from rcp.transport.workspace_mailbox import RunStageMailbox

logger = logging.getLogger(__name__)

_RECEIPT = "work_experiment_kickoff"
_RESUME_RECEIPT = "work_experiment_resume"
EXPERIMENT_RUN_LIVE = "experiment_run_live"


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

    if arguments.at is not None or arguments.target is not None or arguments.seed_file is not None:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="A Work turn starts the Experiment on its own graph; omit the branch options.",
        )
    if arguments.goal_file is None:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="An Experiment plan card from Work requires --handoff-file.",
        )
    return record_plan_card_request(execution, mailbox, request, arguments)


def record_plan_card_request(
    execution: AgentTaskExecution,
    mailbox: RunStageMailbox | None,
    request: EpisodeCommandRequest,
    arguments: ExperimentKickoffArguments | AutoResearchKickoffArguments,
) -> CommandResponse:
    """Record either plan request, preserving legacy direct-start identities and receipts."""
    from rcp.runs.questions import work_ask_authorized

    auto = isinstance(arguments, AutoResearchKickoffArguments)
    category = _AUTO_RESEARCH_RECEIPT if auto else _RECEIPT
    prefix = "auto_research_" if auto else "experiment_"

    def refuse(message: str, suffix: str) -> CommandResponse:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=message,
            result={"code": f"{prefix}{suffix}"},
        )

    if not (member_work_chat_turn(execution) and work_ask_authorized(execution)):
        return refuse("This turn cannot raise a plan card.", "card_unavailable")
    if mailbox is None or execution.experiment_card_raiser is None:
        return CommandResponse(
            request_id=request.request_id,
            status="unavailable",
            message="Plan cards are unavailable for this turn.",
            result={"code": f"{prefix}card_unavailable"},
        )
    assert arguments.goal_file is not None
    try:
        goal = read_workspace_text(mailbox, arguments.goal_file, "Handoff file")
        if auto and len(goal) > EPISODE_TEXT_MAX_LENGTH:
            raise ValueError(f"Handoff exceeds {EPISODE_TEXT_MAX_LENGTH} characters.")
    except ValueError as exc:
        return refuse(str(exc), "goal_invalid")
    store = execution.store
    lineage = turn_lineage(execution)
    kickoff_id = (
        turn_keyed_branch_id(lineage, "auto-research", request.idempotency_key)
        if auto
        else turn_keyed_id(lineage, "episode", request.idempotency_key)
    )
    intent = intent_sha256({**arguments.model_dump(mode="json"), "goal": goal})
    episode = store.episode(kickoff_id)
    previous = previous_receipt(
        store,
        lineage,
        category,
        **({"episode_id": kickoff_id} if episode is not None else {"kickoff_id": kickoff_id}),
    )
    if previous is not None and previous.get("intent_sha256") != intent:
        return refuse("This key already proposed a different plan; use a new key.", "key_conflict")
    subject = (
        {"node_id": arguments.node_id} if isinstance(arguments, ExperimentKickoffArguments) else {}
    )
    if episode is not None:
        return CommandResponse(
            request_id=request.request_id,
            status="ok",
            result={
                "disposition": "existing",
                "episode_id": kickoff_id,
                **subject,
                **({"branch_id": episode.graph_target.branch_id} if auto else {}),
                "status": episode.status,
                "invocation_limit": episode.invocation_ceiling,
            },
        )
    if previous is None and isinstance(arguments, ExperimentKickoffArguments):
        task = store.agent_task(execution.operation_id)
        assert task is not None
        runtime = store.experiment_loop_runtime(
            task.project_id, arguments.node_id, graph_target=task.graph_target
        )
        if runtime.episode_live:
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="This Experiment's run is live. Wait for it to end, or ask the human "
                "to stop it, before proposing another plan.",
                result={
                    "code": EXPERIMENT_RUN_LIVE,
                    "episode_id": runtime.episode_id,
                    **subject,
                },
            )
    if previous_receipt(store, (execution.operation_id,), category, kickoff_id=kickoff_id) is None:
        options = (
            {
                "code_worktree": arguments.code_worktree,
                "worktree_repository": arguments.worktree_repository,
                "branch_name": arguments.branch_name,
            }
            if isinstance(arguments, AutoResearchKickoffArguments)
            else subject
        )
        store.record_agent_task_receipt(
            execution.operation_id,
            category,
            {
                "phase": "card_pending",
                "kickoff_id": kickoff_id,
                "intent_sha256": intent,
                "goal": goal,
                "invocation_ceiling": arguments.invocation_limit,
                "provider": arguments.provider,
                "model": arguments.model,
                "reasoning": arguments.reasoning,
                **options,
            },
            tier="diagnostic",
        )
    return CommandResponse(
        request_id=request.request_id,
        status="ok",
        result={"disposition": "card_pending", **subject},
    )


def raise_plan_cards(execution: AgentTaskExecution, catalog: ProjectCatalog) -> None:
    """Raise the plan cards a finished Work turn's kickoffs asked for, from the settled graph.

    Only a succeeded turn raises them. A request whose node, or Auto-research, already
    has an open card in this chat revises that card in place, pending or parked. A card
    claimed for Start is not revised, so the request raises a new one.
    """

    store = execution.store
    task = store.agent_task(execution.operation_id)
    if task is None or task.status != "succeeded":
        return
    raised = {
        receipt.payload["kickoff_id"]
        for receipt in store.agent_task_receipts_by_category(
            execution.operation_id, PLAN_CARD_RAISED_RECEIPT
        )
    }
    requests = {
        str(receipt.payload["kickoff_id"]): (kind, receipt.payload)
        for category, kind in (
            (_RECEIPT, "experiment_start"),
            (_AUTO_RESEARCH_RECEIPT, "auto_research"),
        )
        for receipt in store.agent_task_receipts_by_category(execution.operation_id, category)
        if receipt.payload.get("phase") == "card_pending"
        and receipt.payload["kickoff_id"] not in raised
    }
    if not requests:
        return
    from rcp.runs.agent_threads import task_owner
    from rcp.runs.questions import question_origin, work_command_handler

    origin = question_origin(work_command_handler(execution, None).profile, task)
    service = task_owner(store, task, catalog=catalog)
    assert isinstance(service, ProjectService) and task.authorized_by is not None
    state = service.history.state()
    for kickoff_id, (kind, payload) in requests.items():
        subject = str(payload["node_id"]) if kind == "experiment_start" else "auto_research"
        if _newer_card_for(store, task, origin.owner_id, subject):
            store.record_agent_task_event(
                execution.operation_id,
                f"No plan card for {subject}: a later turn already proposed it.",
                level="warning",
            )
            _mark_raised(store, execution.operation_id, kickoff_id)
            continue
        if kind == "auto_research":
            action = AutoResearchAction.model_validate(
                {
                    "kind": kind,
                    "project_id": task.project_id,
                    "starting_instruction": payload.get(
                        "goal", payload.get("starting_instruction")
                    ),
                    **{
                        key: value
                        for key, value in payload.items()
                        if key not in {"phase", "kickoff_id", "intent_sha256", "goal"}
                    },
                }
            )
            title = "Auto-research"
        else:
            node = experiment_node(state, subject)
            if node is None:
                store.record_agent_task_event(
                    execution.operation_id,
                    f"No plan card for {subject}: it is not an Experiment on this chat's graph.",
                    level="warning",
                )
                _mark_raised(store, execution.operation_id, kickoff_id)
                continue
            action = ExperimentStartAction.model_validate(
                {
                    "kind": kind,
                    "project_id": task.project_id,
                    "node_id": subject,
                    "branch_id": task.graph_target.branch_id,
                    "goal": payload["goal"],
                    "invocation_ceiling": payload.get(
                        "invocation_ceiling", payload.get("invocation_limit")
                    )
                    or node.invocation_ceiling,
                    "provider": payload["provider"],
                    "model": payload["model"],
                    "reasoning": payload["reasoning"],
                    "run_truth_scope": task.request.get("run_truth_scope"),
                }
            )
            title = node.title
        card = {
            "action": action.model_dump(mode="json"),
            "pinned": approval_pins(service, store, action, task.authorized_by, state=state),
        }
        text = f"Start {title}?"
        open_cards = _open_cards(store, origin, subject)
        kept = None
        if open_cards and (
            store.revise_approval_card(
                open_cards[-1][0],
                revised_by={"kind": "agent", "name": _agent_name(store, origin.owner_id)},
                action=card["action"],
                pinned=card["pinned"],
                question=text,
                operation_id=execution.operation_id,
                raised_kickoff_id=kickoff_id,
            )
            is not None
        ):
            kept = open_cards[-1][0]
            store.record_agent_task_event(
                execution.operation_id, f"Plan card revised for {subject}."
            )
        else:
            key_prefix = "experiment-start" if kind == "experiment_start" else "auto-research"
            kept = store.create_or_get_question(
                origin=origin,
                key=f"{key_prefix}:{kickoff_id}",
                question=text,
                choices=["Approve", "Decline"],
                card=card,
            ).question_id
            _mark_raised(store, execution.operation_id, kickoff_id)
            store.record_agent_task_event(
                execution.operation_id, f"Plan card raised for {subject}."
            )
        for question_id, pinned_sha256 in open_cards:
            if question_id != kept and store.claim_approval_card(question_id, pinned_sha256):
                store.settle_approval_card(question_id, {"outcome": "stale"})


def _open_cards(store: AppStore, origin: QuestionOrigin, subject: str) -> list[tuple[str, str]]:
    """This chat's open cards for the node or Auto-research, pending or parked, oldest
    first, each with the hash it carries."""
    pending = [
        item.question_id
        for item in store.list_questions(
            project_id=origin.project_id, owner_kind="chat", owner_id=origin.owner_id
        )
        if item.state == "pending"
    ]
    cards = store.approval_cards(pending)
    return [
        (question_id, cards[question_id]["pinned_sha256"])
        for question_id in pending
        if question_id in cards
        and cards[question_id]["state"] in {"pending", "parked"}
        and _card_subject(cards[question_id]["action"]) == subject
    ]


def _agent_name(store: AppStore, chat_id: str) -> str | None:
    agent = store.agent(chat_id)
    return agent.title if agent is not None else None


def _card_subject(action: dict[str, object]) -> str | None:
    if action["kind"] == "auto_research":
        return "auto_research"
    return str(action["node_id"]) if action["kind"] == "experiment_start" else None


def _mark_raised(store: AppStore, operation_id: str, kickoff_id: str) -> None:
    store.record_agent_task_receipt(
        operation_id, PLAN_CARD_RAISED_RECEIPT, {"kickoff_id": kickoff_id}, tier="diagnostic"
    )


def _newer_card_for(store: AppStore, task: AgentTaskRecord, chat_id: str, subject: str) -> bool:
    """Whether a later turn already raised or revised a card for this node or Auto-research."""
    with store.connection() as connection:
        return (
            connection.execute(
                "SELECT 1 FROM questions q "
                "JOIN approval_cards c ON c.question_id=q.question_id "
                "JOIN graph_runs r ON r.operation_id="
                "coalesce(c.revised_operation_id,json_extract(q.origin_json,'$.operation_id')) "
                "WHERE q.project_id=? AND q.owner_kind='chat' AND q.owner_id=? "
                "AND r.created_at>? AND ("
                "(json_extract(c.action_json,'$.kind')='auto_research' AND ?='auto_research') "
                "OR (json_extract(c.action_json,'$.kind')='experiment_start' "
                "AND json_extract(c.action_json,'$.node_id')=?)) LIMIT 1",
                (task.project_id, chat_id, task.created_at, subject, subject),
            ).fetchone()
            is not None
        )


def raise_pending_experiment_cards(
    store: AppStore, execution_for: Callable[[AgentTaskRecord], AgentTaskExecution]
) -> None:
    """Raise the plan cards a crash or a failed hook left behind, oldest turn first.

    A failure leaves the card pending for the next startup and records why.
    """
    for task in store.pending_experiment_card_tasks():
        try:
            execution = execution_for(task)
            if execution.experiment_card_raiser is not None:
                execution.experiment_card_raiser(execution)
        except Exception as exc:
            logger.exception("Plan card recovery failed for %s", task.operation_id)
            store.record_agent_task_event(
                task.operation_id,
                f"plan_card_raise_failed: {exc}",
                level="warning",
            )


def work_experiment_command_usage(client: str, profile_choices: str) -> str:
    """The Experiment plan card and human-pause resume usage of a Work turn granted ``episode``."""

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
        "Experiment continue: when the human wants more turns on an ended episode (completed, "
        "stopped, failed, or needs_action) and the design stays the same, add turns to it "
        f"with `{client} {episode_control_usage(EPISODE_ADD_TURNS, required=(KICKOFF_LIMIT,))}` "
        "instead of proposing a plan card. The human's message is the authorization.\n"
        "Experiment plan card: propose a new episode for the human to review, edit, and Start. "
        "Propose one only when the human asks you "
        "to run or start an Experiment (in a chat on that Experiment, 'run it' counts) and no "
        "paused or ended episode should resume or continue instead. Otherwise suggest one in your reply. "
        "Before proposing, audit the Experiment and every node one edge away (its Hypotheses, "
        "Decisions, Blockers, and Evidence). Write or fix the Experiment and its neighbors in "
        "this same turn's Patch: graph edits land when the turn ends, and the card appears "
        "then, drawn from the settled graph. A node that is missing or not an Experiment by "
        "then gets no card and a task event.\n"
        f"`{client} episode --key <key> "
        f"{experiment_kickoff_usage(goal_required=True, child_targets=False)}`\n"
        "Write the handoff first as a direct regular UTF-8 file in this run workspace, beside "
        "the Patch, never a nested path or symlink. The handoff is what you hand to the "
        "experiment agent, and the run starts with it as that agent's first message: what the "
        "human asked to run and the specifics from this chat (settings, data, what to "
        "compare). How this episode runs (who builds what, which agent, receipts to collect) "
        "goes in the handoff, never in the "
        "Experiment's fields, which state only its scientific design. The limit may not exceed "
        "the Experiment's own invocation limit; it defaults to that limit. Pick the provider, "
        "model, and effort that fit the work, or omit them for Settings. Ready profiles:\n"
        f"{profile_choices}\n"
        "The command only proposes: it answers card_pending and starts nothing. The run starts only when a human "
        "presses Start on the card, after reviewing and possibly editing the handoff, limit, "
        "and profile. The human may instead park the card for later, which the question "
        "snapshot shows as shelved: a parked plan waits for the human, so do not raise it "
        "again unasked. While the Experiment's run is live the command is refused with "
        f"{EXPERIMENT_RUN_LIVE}: wait for the run to end, or ask the human to stop it. "
        "The same key and arguments replay the same request after a Resume or Retry. "
        "Tell the human the plan card will appear when this reply ends. The question snapshot "
        "shows the card's outcome: "
        + "; ".join(f"{outcome}: {meaning}" for outcome, meaning in APPROVAL_OUTCOMES.items())
        + f"; {PLAN_RUN_HERE}: {PLAN_RUN_HERE_MEANING}"
        + " To revise the plan after the human comments, run the command again for the same "
        "node with a new key: it revises the open card in place, pending or parked, and "
        "raises no second card. Once the card is approved, "
        "tell the human what started (node, handoff, limit, profile). Its ending reaches this "
        "chat through the episode mailbox."
    )
