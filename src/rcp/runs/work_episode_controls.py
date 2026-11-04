"""A member's Work chat runs the episode controls Runs gives that member.

Each action reaches the same function as the member's own control, through
`WorkEpisodeControls`, which the app provides. Only an episode this chat
started is the chat's to control.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from rcp.agents.command_protocol import (
    AutoResearchKickoffArguments,
    CommandResponse,
    EpisodeCommandRequest,
    WorkEpisodeControlArguments,
)
from rcp.agents.staged_command_client import (
    EPISODE_ADD_TURNS,
    EPISODE_BODY,
    EPISODE_MESSAGE,
    EPISODE_PAUSE,
    EPISODE_RESUME,
    EPISODE_STOP,
    KICKOFF_LIMIT,
    auto_research_kickoff_usage,
    episode_control_usage,
)
from rcp.limits import AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.storage import AgentTaskRecord, AppStore, AutoResearchMessageRecord, EpisodeRecord
    from rcp.transport.workspace_mailbox import RunStageMailbox

_AUTO_RESEARCH_RECEIPT = "work_auto_research_kickoff"
_TURN_CONTROL_RECEIPT = "work_episode_turn_control"


class EpisodeRefused(ValueError):
    """A refused episode action, with the code the member's own control answers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkEpisodeAct:
    """One member Work turn acting on one episode its chat started."""

    source_operation_id: str
    episode_id: str


@dataclass(frozen=True)
class WorkAutoResearchStart:
    """A Work turn's Auto-research kickoff, resolved by its command handler.

    ``episode_id`` derives from the turn and the command key, so a replay names
    the same episode.
    """

    source_operation_id: str
    episode_id: str
    goal: str
    invocation_ceiling: int
    code_worktree: bool | None
    worktree_repository: str | None
    branch_name: str | None
    provider: str | None
    model: str | None
    reasoning: str | None


class WorkEpisodeControls(Protocol):
    """The member's episode controls, each raising `EpisodeRefused` or ValueError."""

    def start_auto_research(self, start: WorkAutoResearchStart) -> EpisodeRecord: ...

    def stop(self, act: WorkEpisodeAct) -> None: ...

    def pause(self, act: WorkEpisodeAct) -> AgentTaskRecord: ...

    def resume_turn(self, act: WorkEpisodeAct) -> AgentTaskRecord: ...

    def add_turns(
        self, act: WorkEpisodeAct, *, invocation_ceiling: int, request_id: str
    ) -> EpisodeRecord: ...

    def message(
        self,
        act: WorkEpisodeAct,
        *,
        body: str,
        invocation_ceiling: int | None,
        request_id: str,
    ) -> AutoResearchMessageRecord: ...


def chat_started_episode(store: AppStore, task: AgentTaskRecord, episode_id: str) -> bool:
    """Whether a Work turn of ``task``'s chat started this episode or the one it continues.

    A continuation, whether this chat's or the member's Add N turns, runs the
    same episode the chat started, so it stays the chat's to control.
    """

    chat_id = task.request.get("chat_id")
    current = store.episode(episode_id)
    seen: set[str] = set()
    while current is not None and current.episode_id not in seen:
        if current.project_id != task.project_id:
            return False
        seen.add(current.episode_id)
        if current.continues_episode_id is None:
            started = store.episode_started_from_chat(current.episode_id)
            return bool(chat_id) and started is not None and started.chat_id == chat_id
        current = store.episode(current.continues_episode_id)
    return False


def turn_lineage(execution: AgentTaskExecution) -> tuple[str, ...]:
    """This turn's attempts, newest first; the oldest names the turn across retries."""

    from rcp.runs.tasks.compute_commands import compute_recovery_lineage

    return compute_recovery_lineage(
        execution.store, execution.operation_id, continuation=execution.continuation
    )


def turn_keyed_id(lineage: tuple[str, ...], scope: str, key: str | None) -> str:
    """An id a Resume or Retry of this turn names again for the same command key."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:work:{lineage[-1]}:{scope}:{key}"))


def turn_keyed_branch_id(lineage: tuple[str, ...], scope: str, key: str | None) -> str:
    """`turn_keyed_id` in the canonical UUIDv4 form a graph branch id requires."""

    digest = hashlib.sha256(f"rcp:work:{lineage[-1]}:{scope}:{key}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def read_workspace_text(mailbox: RunStageMailbox, filename: str, label: str) -> str:
    """One nonblank command file from this turn's workspace; ValueError says why not."""

    try:
        text = mailbox.read_text(filename, max_bytes=AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES)
    except FileNotFoundError as exc:
        raise ValueError(f"{label} {filename!r} does not exist in this turn's workspace.") from exc
    if not text.strip():
        raise ValueError(f"{label} {filename!r} must not be blank.")
    return text


def intent_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def previous_receipt(
    store: AppStore, lineage: tuple[str, ...], category: str, **match: str
) -> dict[str, object] | None:
    """The payload an earlier attempt of this turn recorded for the same command."""

    return next(
        (
            receipt.payload
            for operation_id in lineage
            for receipt in store.agent_task_receipts_by_category(operation_id, category)
            if all(receipt.payload.get(name) == value for name, value in match.items())
        ),
        None,
    )


def _refuse(request: EpisodeCommandRequest, message: str, code: str | None) -> CommandResponse:
    return CommandResponse(
        request_id=request.request_id,
        status="invalid",
        message=message,
        result={"code": code} if code else {},
    )


def _unavailable(request: EpisodeCommandRequest, message: str) -> CommandResponse:
    return CommandResponse(request_id=request.request_id, status="unavailable", message=message)


def _ok(request: EpisodeCommandRequest, **result: object) -> CommandResponse:
    return CommandResponse(request_id=request.request_id, status="ok", result=result)


def _attempt(
    request: EpisodeCommandRequest,
    default_code: str,
    action: Callable[[], CommandResponse],
) -> CommandResponse:
    try:
        return action()
    except EpisodeRefused as exc:
        return _refuse(request, str(exc), exc.code)
    except ValueError as exc:
        return _refuse(request, str(exc), default_code)


def own_episode(
    execution: AgentTaskExecution, request: EpisodeCommandRequest, episode_id: str
) -> tuple[WorkEpisodeControls, WorkEpisodeAct] | CommandResponse:
    """The controls and act for an episode this chat started, or the refusal."""

    task = execution.store.agent_task(execution.operation_id)
    controls = execution.episode_controls
    if task is None or controls is None:
        return _unavailable(request, "Episode controls are unavailable for this turn.")
    if not chat_started_episode(execution.store, task, episode_id):
        return _refuse(
            request,
            "This chat may control only an episode it started; tell the human to use Runs.",
            "episode_not_started_here",
        )
    return controls, WorkEpisodeAct(
        source_operation_id=execution.operation_id, episode_id=episode_id
    )


def stop_own_episode(
    execution: AgentTaskExecution, request: EpisodeCommandRequest, episode_id: str
) -> CommandResponse:
    """Stop an episode this chat started, now, as the member's Stop would.

    Stop is a graceful fence that changes no graph, so it does not wait for this
    turn's Patch: the running turn finishes and nothing continues it.
    """

    owned = own_episode(execution, request, episode_id)
    if isinstance(owned, CommandResponse):
        return owned
    controls, act = owned
    store = execution.store
    episode = store.episode(episode_id)
    assert episode is not None

    def stop() -> CommandResponse:
        if episode.stop_requested_at is None:
            controls.stop(act)
            store.record_agent_task_event(
                execution.operation_id, f"Episode {episode_id} is stopping."
            )
        current = store.episode(episode_id)
        assert current is not None
        return _ok(
            request,
            disposition="stopped" if current.status == "stopped" else "stopping",
            episode_id=episode_id,
            status=current.status,
        )

    return _attempt(request, "episode_not_stoppable", stop)


def control_own_turn(
    execution: AgentTaskExecution,
    request: EpisodeCommandRequest,
    episode_id: str,
    action: str,
) -> CommandResponse:
    """Pause or resume the episode's current turn, as its Runs card button does.

    A replay of the same key answers with the turn it already controlled.
    """

    owned = own_episode(execution, request, episode_id)
    if isinstance(owned, CommandResponse):
        return owned
    controls, act = owned
    store = execution.store
    lineage = turn_lineage(execution)
    previous = previous_receipt(
        store,
        lineage,
        _TURN_CONTROL_RECEIPT,
        action=action,
        episode_id=episode_id,
        key=str(request.idempotency_key),
    )
    if previous is not None:
        return _ok(
            request,
            disposition="existing",
            episode_id=episode_id,
            operation_id=previous["operation_id"],
        )

    def control() -> CommandResponse:
        task = controls.pause(act) if action == "pause" else controls.resume_turn(act)
        store.record_agent_task_receipt(
            execution.operation_id,
            _TURN_CONTROL_RECEIPT,
            {
                "action": action,
                "episode_id": episode_id,
                "key": str(request.idempotency_key),
                "operation_id": task.operation_id,
            },
            tier="diagnostic",
        )
        store.record_agent_task_event(
            execution.operation_id, f"Episode {episode_id} turn {task.operation_id}: {action}."
        )
        return _ok(
            request,
            disposition="paused" if action == "pause" else "resumed",
            episode_id=episode_id,
            operation_id=task.operation_id,
            status=task.status,
        )

    return _attempt(request, f"episode_not_{action}able", control)


def handle_work_episode_controls(
    execution: AgentTaskExecution,
    mailbox: RunStageMailbox | None,
    request: EpisodeCommandRequest,
    arguments: WorkEpisodeControlArguments,
) -> CommandResponse:
    """Pause, add turns to, or message an episode this chat started."""

    if arguments.action == "pause":
        return control_own_turn(execution, request, arguments.episode_id, "pause")
    owned = own_episode(execution, request, arguments.episode_id)
    if isinstance(owned, CommandResponse):
        return owned
    controls, act = owned
    lineage = turn_lineage(execution)
    request_id = turn_keyed_id(lineage, arguments.action, request.idempotency_key)
    if arguments.action == "add_turns":
        assert arguments.invocation_limit is not None
        limit = arguments.invocation_limit

        def add_turns() -> CommandResponse:
            continuation = controls.add_turns(act, invocation_ceiling=limit, request_id=request_id)
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Episode {arguments.episode_id} continues as {continuation.episode_id}.",
            )
            return _ok(
                request,
                disposition="continued",
                episode_id=continuation.episode_id,
                continues_episode_id=arguments.episode_id,
                status=continuation.status,
                invocation_limit=continuation.invocation_ceiling,
            )

        return _attempt(request, "episode_not_continuable", add_turns)
    assert arguments.body_file is not None
    if mailbox is None:
        return _unavailable(request, "Episode messages are unavailable for this turn.")
    try:
        body = read_workspace_text(mailbox, arguments.body_file, "Body file")
    except ValueError as exc:
        return _refuse(request, str(exc), None)

    def message() -> CommandResponse:
        sent = controls.message(
            act,
            body=body,
            invocation_ceiling=arguments.invocation_limit,
            request_id=request_id,
        )
        return _ok(
            request,
            disposition="sent",
            message_id=sent.message_id,
            episode_id=sent.episode_id,
            delivered=sent.delivered_at is not None,
        )

    return _attempt(request, "episode_message_refused", message)


def handle_work_auto_research_kickoff(
    execution: AgentTaskExecution,
    mailbox: RunStageMailbox | None,
    request: EpisodeCommandRequest,
    arguments: AutoResearchKickoffArguments,
) -> CommandResponse:
    """Start one Auto-research episode on a new branch, or answer a replay of the same key."""

    controls = execution.episode_controls
    if mailbox is None or controls is None:
        return _unavailable(request, "Auto-research kickoff is unavailable for this turn.")
    try:
        goal = read_workspace_text(mailbox, arguments.goal_file, "Goal file")
    except ValueError as exc:
        return _refuse(request, str(exc), None)
    store = execution.store
    lineage = turn_lineage(execution)
    episode_id = turn_keyed_branch_id(lineage, "auto-research", request.idempotency_key)
    intent = intent_sha256({**arguments.model_dump(mode="json"), "goal": goal})
    previous = previous_receipt(store, lineage, _AUTO_RESEARCH_RECEIPT, episode_id=episode_id)
    if previous is not None and previous.get("intent_sha256") != intent:
        return _refuse(
            request, "This key already started a different Auto-research; use a new key.", None
        )
    episode = store.episode(episode_id)
    disposition = "existing"
    if episode is None:
        if previous is None:
            store.record_agent_task_receipt(
                execution.operation_id,
                _AUTO_RESEARCH_RECEIPT,
                {"episode_id": episode_id, "intent_sha256": intent},
                tier="diagnostic",
            )
        start = WorkAutoResearchStart(
            source_operation_id=execution.operation_id,
            episode_id=episode_id,
            goal=goal,
            invocation_ceiling=arguments.invocation_limit,
            code_worktree=arguments.code_worktree,
            worktree_repository=arguments.worktree_repository,
            branch_name=arguments.branch_name,
            provider=arguments.provider,
            model=arguments.model,
            reasoning=arguments.reasoning,
        )
        try:
            controls.start_auto_research(start)
        except EpisodeRefused as exc:
            return _refuse(request, str(exc), exc.code)
        except ValueError as exc:
            return _refuse(request, str(exc), "auto_research_not_started")
        episode = store.episode(episode_id)
        assert episode is not None
        disposition = "created"
    store.record_agent_task_event(
        execution.operation_id, f"Auto-research {disposition}: episode {episode_id}."
    )
    return _ok(
        request,
        disposition=disposition,
        episode_id=episode_id,
        branch_id=episode.graph_target.branch_id,
        status=episode.status,
        invocation_limit=episode.invocation_ceiling,
    )


def work_episode_control_usage(client: str, orchestrator_profiles: str) -> str:
    """The Auto-research kickoff and shared episode-control usage of a Work turn."""

    add_turns = episode_control_usage(EPISODE_ADD_TURNS, required=(KICKOFF_LIMIT,))
    message = episode_control_usage(
        EPISODE_MESSAGE, required=(EPISODE_BODY,), optional=(KICKOFF_LIMIT,)
    )
    return (
        "Auto-research kickoff starts a new episode on a new graph branch, only when the "
        "human asks you to start Auto-research: "
        f"`{client} {auto_research_kickoff_usage()}`. Write the goal file first, as for an "
        "Experiment goal: what the human asked the orchestrator to pursue, with the specifics "
        "from this chat. The limit is the orchestrator's turn budget. --code-worktree on gives "
        "it a code worktree, --worktree-repository names its one writable repository, and "
        "--branch-name names the branch. Omit any of these, and the provider, model, and "
        "effort, for Settings. Ready orchestrator profiles:\n"
        f"{orchestrator_profiles}\n"
        "Afterwards tell the human what you started (goal, limit, branch).\n"
        "Episode controls act only on episodes this chat started, as the human's Runs card "
        "would; tell the human to use Runs for any other.\n"
        f"- `{client} {episode_control_usage(EPISODE_STOP)}` stops it gracefully: the running "
        "turn finishes, then nothing continues it.\n"
        f"- `{client} {episode_control_usage(EPISODE_PAUSE)}` pauses its running turn; "
        f"`{client} {episode_control_usage(EPISODE_RESUME)}` resumes a paused turn in its "
        "session.\n"
        f"- `{client} {add_turns}` adds turns to an ended episode. It continues on the same "
        "branch, code worktree, and session, so this is how you restart one there.\n"
        f"- `{client} {message}` sends the body file to an Auto-research orchestrator. An "
        "ended orchestrator needs the limit, which continues it with that message.\n"
        "To steer a running episode, message it or post to the Bulletin. To change its course, "
        "pause it, make the change (a message for an orchestrator; your Patch for an "
        "Experiment on this chat's graph), then resume it or add turns in a later turn, once "
        "the change has landed. The same key replays the same action after a Resume or Retry."
    )
