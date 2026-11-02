"""Approval preparation reads live facts; only the human HTTP route executes them."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, get_args, get_origin

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from rcp.agents.command_protocol import AskCommandRequest
from rcp.agents.launcher import work_like_launch_problem
from rcp.approval_actions import (
    ApprovalAction,
    ApprovalOutcome,
    AutoResearchAction,
    MergeBranchAction,
    MergeEpisodeAction,
    ProposalAction,
    TruthScopeAction,
)
from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation.proposals import proposal_is_stale
from rcp.history import ReplayHalted
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.storage.question_models import QuestionOrigin
from rcp.transport.state import StateUnavailable


def private_project_thread_turn(store: AppStore, operation_id: str) -> bool:
    task = store.agent_task(operation_id)
    if task is None or task.kind != "project_chat" or task.authorized_by is None:
        return False
    with store.connection() as connection:
        return (
            connection.execute(
                """SELECT 1 FROM agent_threads WHERE thread_id=?
            AND user_id=? AND project_id=? AND kind='project' AND closed_at IS NULL""",
                (task.request.get("chat_id"), task.authorized_by.user_id, task.project_id),
            ).fetchone()
            is not None
        )


def approval_card_for_ask(
    store: AppStore,
    request: AskCommandRequest,
    origin: QuestionOrigin,
    service: ProjectService | None,
) -> dict[str, Any]:
    """The card a member's own private project thread asks for; any other asker is refused.

    A repeated key keeps the pins it first presented.
    """
    action = request.arguments.action
    assert action is not None
    member = _thread_member(store, origin)
    if service is None or member is None or action.project_id != origin.project_id:
        raise ValueError("approval_requires_own_project_thread")
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
    try:
        pinned = (
            existing["pinned"] if existing else approval_card_pins(service, store, action, member)
        )
    except KeyError as exc:
        raise ValueError("approval_target_not_found") from exc
    return {"action": action.model_dump(mode="json"), "pinned": pinned}


def _thread_member(store: AppStore, origin: QuestionOrigin) -> AuthorizedHuman | None:
    """The member whose own Work turn in their private project thread is asking."""
    task = store.agent_task(origin.operation_id)
    if task is None or task.authorized_by is None or origin.capability != "work_auto":
        return None
    owner = store.private_question_owner(origin.owner_id) if origin.owner_kind == "chat" else None
    if owner != task.authorized_by.user_id:
        return None
    return task.authorized_by if private_project_thread_turn(store, origin.operation_id) else None


def settle_interrupted_approval_cards(
    store: AppStore, *, service_for: Callable[[str], ProjectService]
) -> None:
    """Settle claimed cards left without a result, never rerunning their action.

    The action's durable effect proves it committed: a request-id receipt for
    episode start and merges, or the live Proposal status or truth membership.
    Without one the card fails as interrupted. When the graph cannot be read the
    claim is released, so a later tap rechecks the pins instead.
    """
    for card in store.interrupted_approval_cards():
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
    state = service.history.state()
    if action["kind"] == "proposal":
        proposal = state.proposals.get(action["proposal_id"])
        decided = proposal is not None and proposal.status == action["decision"]
        return {"proposal_id": action["proposal_id"]} if decided else None
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
    "declined": "the member said no; do not ask again unless they change their mind.",
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


def approval_card_usage(client: str) -> str:
    models = get_args(get_args(ApprovalAction)[0])
    outcomes = "; ".join(f"{outcome}: {meaning}" for outcome, meaning in APPROVAL_OUTCOMES.items())
    return (
        f"Protected actions in this private project thread use {client} ask --key <key> "
        "--question <reason> --action '<JSON>'. Do not supply choices or multiple; "
        "RCP creates Approve and Decline controls. Action kind is "
        + "; ".join(_action_usage(model) for model in models)
        + ". Every action includes this thread's project_id. Only the member's card tap "
        "executes the action; a plain answer grants no authority. The question snapshot "
        f"shows each card's action and result. Outcomes: {outcomes}"
    )


def approval_card_pins(
    service: ProjectService, store: AppStore, action: ApprovalAction, human: AuthorizedHuman
) -> dict[str, Any]:
    """Pins for a new card, refused at ask when its launch could not start."""
    pinned = approval_pins(service, store, action, human)
    if action.kind == "auto_research":
        profile = pinned["profile"]
        try:
            machine = service.manifest.machine_map[profile["run_on"]]
        except KeyError as exc:
            raise ValueError("machine_not_found") from exc
        readiness = service.launcher.member_readiness(
            profile["provider"],
            host=machine.host,
            binary=service.launcher.provider_binary(profile["provider"], machine.host),
            member_id=human.user_id,
            refresh=True,
        )
        if not readiness.installed or not readiness.authenticated:
            raise ValueError(f"provider_not_ready: {readiness.reason or profile['provider']}")
        if problem := work_like_launch_problem(readiness):
            raise ValueError(f"provider_not_ready: {problem}")
    return pinned


def approval_pins(
    service: ProjectService, store: AppStore, action: ApprovalAction, human: AuthorizedHuman
) -> dict[str, Any]:
    """Pin content and authority-relevant facts, excluding volatile probe timestamps."""
    if action.kind == "proposal":
        return _proposal_pins(service, action)
    if action.kind == "truth_scope":
        return _truth_scope_pins(service, action)
    if action.kind == "auto_research":
        return _auto_research_pins(service, store, action)
    return _merge_pins(service, store, action, human)


def _proposal_pins(service: ProjectService, action: ProposalAction) -> dict[str, Any]:
    state = service.history.state()
    proposal = state.proposals.get(action.proposal_id)
    if proposal is None or proposal.status != "pending" or proposal_is_stale(state, proposal):
        raise ValueError("proposal_not_pending_or_stale")
    return {"proposal": proposal.model_dump(mode="json")}


def _truth_scope_pins(service: ProjectService, action: TruthScopeAction) -> dict[str, Any]:
    repository = service.manifest.repository_map.get(action.alias)
    if repository is None:
        raise ValueError("repository_not_found")
    writable = action.alias in service.history.state().project_truth_scope
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
