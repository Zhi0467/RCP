"""Approval preparation reads live facts; only the human HTTP route executes them."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, get_args, get_origin

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from rcp.agents.command_protocol import AskCommandRequest
from rcp.approval_actions import (
    ApprovalAction,
    ApprovalOutcome,
    AutoResearchAction,
    MergeBranchAction,
    MergeEpisodeAction,
    ProposalAction,
    TruthScopeAction,
)
from rcp.core.attention import project_graph_attention
from rcp.core.models import AuthorizedHuman, GraphState, Proposal
from rcp.core.transition_models import GraphTargetRef, ProposalActionLine
from rcp.core.validation.proposals import proposal_is_stale
from rcp.history import ReplayHalted
from rcp.limits import ASK_MAX_ATTEMPTS, COMMAND_CLIENT_WAIT_SECONDS
from rcp.runs.shared import turn_first_attempt
from rcp.service import ProjectService
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.agent_threads import AgentThreadRecord, bound_thread
from rcp.storage.models import ProjectRecord
from rcp.storage.question_models import QuestionOrigin
from rcp.transport.state import StateUnavailable

PROPOSAL_CARD_SHOWN = "proposal_card_shown"
AUTO_RESEARCH_USES_EPISODE = "auto_research_uses_episode_verb"
"""A new Auto-research ask's refusal; stored Auto-research cards still render and settle."""


class ProposalCardView(BaseModel):
    """A pending Proposal a turn of the member's own project thread raised, drawn live."""

    project_id: str
    project_name: str
    thread_id: str
    turn_id: str
    turn_created_at: str
    proposal: dict[str, Any]
    stale: bool
    changes: list[ProposalActionLine]


def raising_turn(
    store: AppStore, proposal: Proposal
) -> tuple[AgentTaskRecord, AgentThreadRecord] | None:
    """The task that raised a Proposal and the member thread it is a turn of, if any."""
    operation_id = proposal.created_by_operation_id
    task = store.agent_task(operation_id) if operation_id else None
    thread = bound_thread(store, task) if task is not None else None
    return (task, thread) if task is not None and thread is not None else None


def proposal_cards(
    store: AppStore, project: ProjectRecord, state: GraphState, threads: list[AgentThreadRecord]
) -> list[ProposalCardView]:
    """Cards for the pending Proposals that turns of `threads` raised; writes nothing."""
    attention = project_graph_attention(state)
    wanted = {thread.thread_id for thread in threads}
    cards = []
    for proposal_id in attention.pending_proposal_ids:
        proposal = state.proposals[proposal_id]
        raised = raising_turn(store, proposal)
        if raised is None or raised[1].thread_id not in wanted:
            continue
        turn = turn_first_attempt(store, raised[0])
        cards.append(
            ProposalCardView(
                project_id=project.project_id,
                project_name=project.name,
                thread_id=raised[1].thread_id,
                turn_id=turn.operation_id,
                turn_created_at=turn.created_at,
                proposal=proposal.model_dump(mode="json"),
                stale=proposal_id in attention.stale_proposal_ids,
                changes=attention.proposal_actions.get(proposal_id, []),
            )
        )
    return cards


def approval_card_member(store: AppStore, task: AgentTaskRecord) -> AuthorizedHuman | None:
    """The member whose own Work turn in their open private thread, project or Space, may ask
    a card.

    Work and the Space handler resolve it once with their verbs; the prompt and the ask
    handler read that value.
    """
    thread = bound_thread(store, task)
    return task.authorized_by if thread is not None and thread.closed_at is None else None


_ACTION_MODELS: tuple[type[BaseModel], ...] = get_args(get_args(ApprovalAction)[0])


def _action_kind(model: type[BaseModel]) -> str:
    return get_args(model.model_fields["kind"].annotation)[0]


THREAD_CARD_KINDS: dict[str, frozenset[str]] = {
    "project": frozenset(_action_kind(model) for model in _ACTION_MODELS) - {"auto_research"},
    "space": frozenset({"proposal", "truth_scope", "merge_branch", "merge_episode"}),
}


def card_project_allowed(store: AppStore, thread: AgentThreadRecord, project_id: str) -> bool:
    """A project thread's card names its own project; a Space thread's any of its member's."""
    if thread.kind == "project":
        return project_id == thread.project_id
    return store.is_project_member(project_id, thread.user_id)


def draws_proposal_cards(store: AppStore, thread: AgentThreadRecord) -> bool:
    """Whether the member's docks draw this thread's Proposal cards: an open, shown project
    thread."""
    return (
        thread.kind == "project"
        and thread.project_id is not None
        and thread.closed_at is None
        and not store.chat_archived(thread.project_id, thread.thread_id)
    )


def _proposal_card_drawn(
    store: AppStore, asker: AgentThreadRecord, raiser: AgentThreadRecord
) -> bool:
    """A project thread's dock draws its own raised Proposals; Pip's dock draws every one."""
    if asker.kind == "project":
        return raiser.thread_id == asker.thread_id
    return raiser.user_id == asker.user_id and draws_proposal_cards(store, raiser)


def proposal_graph(service: ProjectService, action: ProposalAction) -> ProjectService:
    """The graph a Proposal action decides on: live main, or the graph branch it names."""
    if action.branch_id is None:
        return service
    try:
        return service.for_graph_target(
            GraphTargetRef(kind="branch", branch_id=action.branch_id), initialize=False
        )
    except (KeyError, ValueError, FileNotFoundError) as exc:
        raise ValueError("branch_not_found") from exc


def _asking_thread(
    store: AppStore,
    origin: QuestionOrigin,
    member: AuthorizedHuman | None,
    action: ApprovalAction,
    *,
    replay: bool,
) -> AgentThreadRecord | None:
    """The member's open thread that asks, when its grant covers this action's kind and project."""
    thread = store.agent_thread(origin.owner_id) if origin.owner_kind == "chat" else None
    if (
        member is None
        or thread is None
        or thread.closed_at is not None
        or thread.user_id != member.user_id
    ):
        return None
    allowed = replay or action.kind in THREAD_CARD_KINDS[thread.kind]
    return thread if allowed and card_project_allowed(store, thread, action.project_id) else None


def approval_card_for_ask(
    store: AppStore,
    request: AskCommandRequest,
    origin: QuestionOrigin,
    service: ProjectService | None,
    member: AuthorizedHuman | None,
) -> dict[str, Any]:
    """The card a member's own open private thread asks for; any other asker is refused.

    `member` is the turn's resolved `approval_card_member` and `service` the action's
    project. A repeated key keeps the pins it first presented.
    """
    action = request.arguments.action
    assert action is not None
    previous = next(
        (
            item
            for item in store.list_questions(
                project_id=origin.project_id, owner_kind="chat", owner_id=origin.owner_id
            )
            if item.key == request.idempotency_key
        ),
        None,
    )
    existing = store.approval_card(previous.question_id) if previous else None
    if existing is None and action.kind == "auto_research":
        raise ValueError(AUTO_RESEARCH_USES_EPISODE)
    thread = _asking_thread(store, origin, member, action, replay=existing is not None)
    if service is None or member is None or thread is None:
        raise ValueError("approval_requires_own_thread")
    if existing is None and action.kind == "proposal" and action.branch_id is None:
        proposal = service.history.state().proposals.get(action.proposal_id)
        raised = raising_turn(store, proposal) if proposal is not None else None
        if raised is not None and _proposal_card_drawn(store, thread, raised[1]):
            raise ValueError(PROPOSAL_CARD_SHOWN)
    try:
        pinned = existing["pinned"] if existing else approval_pins(service, store, action, member)
    except KeyError as exc:
        raise ValueError("approval_target_not_found") from exc
    return {"action": action.model_dump(mode="json"), "pinned": pinned}


def settle_interrupted_approval_cards(
    store: AppStore, *, service_for: Callable[[str], ProjectService], claimed_before: str
) -> None:
    """Settle cards an earlier process claimed and left without a result, never rerunning them.

    Only claims older than `claimed_before`, this process's start, count: this
    process may already serve taps, and a live tap's claim is not interrupted.

    The action's durable effect proves it committed: a request-id receipt for
    episode start and merges, or the live Proposal status or truth membership.
    Without one the card fails as interrupted. When the graph cannot be read the
    claim is released, so a later tap rechecks the pins instead.
    """
    for card in store.interrupted_approval_cards(claimed_before):
        try:
            receipt = committed_receipt(store, service_for, card)
        except (StateUnavailable, ReplayHalted, OSError, KeyError):
            store.release_approval_card(card["question_id"])
            continue
        store.settle_approval_card(
            card["question_id"],
            {"outcome": "approved", "result": receipt}
            if receipt is not None
            else {"outcome": "failed", "reason": "interrupted"},
        )


def committed_receipt(
    store: AppStore, service_for: Callable[[str], ProjectService], card: dict[str, Any]
) -> dict[str, Any] | None:
    """The receipt an action's durable effect leaves, or None when it never committed."""
    action = card["action"]
    if action["kind"] in {"proposal", "truth_scope"}:
        return _graph_receipt(service_for(action["project_id"]), action)
    return _request_receipt(store, card)


def _graph_receipt(service: ProjectService, action: dict[str, Any]) -> dict[str, Any] | None:
    if action["kind"] == "proposal":
        decision = ProposalAction.model_validate(action)
        try:
            graph = proposal_graph(service, decision)
        except ValueError:
            return None
        proposal = graph.history.state().proposals.get(decision.proposal_id)
        decided = proposal is not None and proposal.status == decision.decision
        return {"proposal_id": decision.proposal_id} if decided else None
    state = service.history.state()
    return {} if (action["alias"] in state.project_truth_scope) == action["writable"] else None


def _request_receipt(store: AppStore, card: dict[str, Any]) -> dict[str, Any] | None:
    from rcp.runs.branch_merge_admission import member_merge_operation_id

    action, request_id = card["action"], card["request_id"]
    project_id = action["project_id"]
    if action["kind"] == "auto_research":
        admitted = store.client_request(project_id, request_id)
        return {"episode_id": admitted.episode_id} if admitted and admitted.episode_id else None
    if action["kind"] == "merge_branch":
        if card["user_id"] is None:
            return None
        operation_id = member_merge_operation_id(project_id, card["user_id"], request_id)
        return {"operation_id": operation_id} if store.agent_task(operation_id) else None
    with store.connection() as connection:
        row = connection.execute(
            """SELECT accepted_task_id FROM episode_merge_request_receipts
            WHERE project_id=? AND request_id=? AND accepted_task_id IS NOT NULL""",
            (project_id, request_id),
        ).fetchone()
    return {"operation_id": row["accepted_task_id"]} if row else None


def approved_receipt(response: object) -> dict[str, Any]:
    """The ids an approved action created, from its human handler's response."""
    data = jsonable_encoder(response)
    if not isinstance(data, dict):
        return {}
    receipt = {key: data[key] for key in ("episode_id", "operation_id") if data.get(key)}
    if isinstance(task := data.get("accepted_merge_task"), dict) and task.get("operation_id"):
        receipt["operation_id"] = task["operation_id"]
    return receipt


#: What each card outcome means to the asking agent, keyed by `ApprovalOutcome`.
APPROVAL_OUTCOMES: dict[ApprovalOutcome, str] = {
    "approved": "RCP ran the action as the member; result names any episode_id or "
    "operation_id it created.",
    "declined": "the member said no; do not ask again unless they change their mind. "
    "Declining a card that approves a Proposal also tries to reject that Proposal; read "
    "its status from graph state.",
    "stale": "the pinned facts changed before the tap; recheck and ask again with a new key.",
    "expired": "nobody tapped in time or the thread closed; ask again with a new key if "
    "it still matters.",
    "failed": "the action was refused or interrupted; the answer carries the reason. "
    "Fix the cause and ask again with a new key.",
}


def _action_usage(model: type[BaseModel]) -> str:
    fields = []
    for name, field in model.model_fields.items():
        if name in {"kind", "project_id"}:
            continue
        choices = get_args(field.annotation) if get_origin(field.annotation) is Literal else ()
        text = f"{name} ({'/'.join(map(str, choices))})" if choices else name
        fields.append(text if field.is_required() else f"optional {text}")
    kind = get_args(model.model_fields["kind"].annotation)[0]
    return f"{kind} ({', '.join(fields)})"


def _card_contract(thread_kind: str) -> str:
    """The action kinds this thread kind's grant accepts and what a card's tap means."""
    kinds = THREAD_CARD_KINDS[thread_kind]
    outcomes = "; ".join(f"{outcome}: {meaning}" for outcome, meaning in APPROVAL_OUTCOMES.items())
    return (
        "Do not supply choices or multiple; RCP creates Approve and Decline controls. "
        "Action kind is "
        + "; ".join(
            _action_usage(model) for model in _ACTION_MODELS if _action_kind(model) in kinds
        )
        + ". A proposal pending on a graph branch names that branch_id; one on main omits it. "
        "Only the member's card tap executes the action; a plain answer grants no authority. "
        f"Outcomes: {outcomes}"
    )


def approval_card_usage(client: str) -> str:
    return (
        f"Protected actions in this private project thread use {client} ask --key <key> "
        "--question <reason> --action '<JSON>'. "
        + _card_contract("project")
        + " Every action includes this thread's project_id. The member already gets a card "
        "for every Proposal this thread's turns raise once it is pending on main, so never "
        "ask for one; that ask is refused with "
        f"{PROPOSAL_CARD_SHOWN}. Ask a proposal card only when the member names a pending "
        "Proposal this thread did not raise. Start Auto-research with the episode command, "
        f"never a card; that ask is refused with {AUTO_RESEARCH_USES_EPISODE}. "
        "The question snapshot shows each card's action and result."
    )


def space_card_usage() -> str:
    """The Space thread's ask, rendered from the grant its handler enforces."""
    return (
        "ask --key <key> --question <reason> --action '<JSON>' (raises an approval card in "
        "this thread's dock. When the member wants to decide, approve or merge something that "
        "would otherwise need a project's Inbox, branch view or Settings, raise its card here "
        "and never send them into the project. Every action names the project_id of one of "
        "the member's projects. "
        + _card_contract("space")
        + f" A main Proposal one of the member's project threads raised already has its card "
        f"in this dock; that ask is refused with {PROPOSAL_CARD_SHOWN}, so point the member to "
        "it. A decision no action kind covers is handed off to the project thread, or say "
        "plainly that it cannot be done here. Never say a card exists unless ask returned its "
        f"question_id. A call waits up to {COMMAND_CLIENT_WAIT_SECONDS:g} seconds; repeat the "
        f"exact same call to keep waiting. After attempt {ASK_MAX_ATTEMPTS} with no answer, end "
        "your turn: the card stays in the dock and the tap still runs it, and the same call in "
        "a later turn reads its outcome.)"
    )


def approval_pins(
    service: ProjectService,
    store: AppStore,
    action: ApprovalAction,
    human: AuthorizedHuman,
    *,
    state: GraphState | None = None,
) -> dict[str, Any]:
    """Pin content and authority-relevant facts, excluding volatile probe timestamps.

    Graph pins read `state` when given, so a caller pairs them with that state's revision;
    a Proposal's state is its `proposal_graph`.
    """
    if action.kind == "proposal":
        return _proposal_pins(state or proposal_graph(service, action).history.state(), action)
    if action.kind == "truth_scope":
        return _truth_scope_pins(service, state or service.history.state(), action)
    if action.kind == "auto_research":
        return _auto_research_pins(service, store, action)
    return _merge_pins(service, store, action, human)


def _proposal_pins(state: GraphState, action: ProposalAction) -> dict[str, Any]:
    proposal = state.proposals.get(action.proposal_id)
    if proposal is None or proposal.status != "pending" or proposal_is_stale(state, proposal):
        raise ValueError("proposal_not_pending_or_stale")
    return {"proposal": proposal.model_dump(mode="json")}


def _truth_scope_pins(
    service: ProjectService, state: GraphState, action: TruthScopeAction
) -> dict[str, Any]:
    repository = service.manifest.repository_map.get(action.alias)
    if repository is None:
        raise ValueError("repository_not_found")
    writable = action.alias in state.project_truth_scope
    if writable == action.writable:
        raise ValueError("truth_scope_unchanged")
    return {"repository": repository.model_dump(mode="json"), "writable": writable}


def _auto_research_pins(
    service: ProjectService, store: AppStore, action: AutoResearchAction
) -> dict[str, Any]:
    from rcp.api.episode_routes import _resolved_auto_research_start_request
    from rcp.api.episodes import StartEpisodeBody

    body = StartEpisodeBody.model_validate(
        {"mode": "auto_research", **action.model_dump(exclude={"kind", "project_id"})}
    )
    profile = _resolved_auto_research_start_request(
        service, body, store=store, project_id=action.project_id
    )
    if profile.provider is None or profile.run_on is None:
        raise ValueError("auto_research_profile_unavailable")
    # Readiness is checked at ask and again by the launch itself, never pinned.
    return {"profile": profile.model_dump(mode="json")}


def _merge_pins(
    service: ProjectService,
    store: AppStore,
    action: MergeBranchAction | MergeEpisodeAction,
    human: AuthorizedHuman,
) -> dict[str, Any]:
    from rcp.runs.branch_merge_context import merge_history
    from rcp.runs.episodes.merge import branch_merge_preview, merge_owner, merge_preview

    if isinstance(action, MergeBranchAction):
        try:
            source = merge_history(
                service, GraphTargetRef(kind="branch", branch_id=action.branch_id)
            )
            preview = branch_merge_preview(service, store, action.branch_id, authorized_by=human)
        except KeyError as exc:
            raise ValueError("branch_not_found") from exc
    else:
        episode = store.episode(action.episode_id)
        if episode is None or episode.project_id != action.project_id:
            raise ValueError("episode_not_found")
        owner = merge_owner(store, episode)
        source = merge_history(service, owner.graph_target)
        preview = merge_preview(
            service, store, owner, authorized_by=human, target_branch=action.target_branch
        )
    # Main is not pinned: the merge plans against live main, and the preview
    # already pins main's value of every field the source changed.
    return {
        "source_head": source.head_ref().model_dump(mode="json"),
        "preview": preview.model_dump(mode="json"),
    }
