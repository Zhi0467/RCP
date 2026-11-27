"""Member-only approval taps reuse the owning human handlers and admission."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import ExitStack
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, TypeAdapter

from rcp.api.dependencies import (
    get_catalog,
    get_identity_access,
    get_project_display_cache,
    get_project_writer,
    get_store,
    get_watcher_delivery,
    project_write_admission,
    require_project_membership,
)
from rcp.api.identity import IdentityAccess
from rcp.approval_actions import (
    ApprovalAction,
    AutoResearchAction,
    AutoResearchStartOverrides,
    ExperimentStartAction,
    ExperimentStartOverrides,
    MergeEpisodeBody,
    PlanStartOverrides,
    ProposalAction,
)
from rcp.core.models import AuthorizedHuman, GraphState
from rcp.core.transition_models import GraphHeadRef
from rcp.core.validation.proposals import proposal_is_stale
from rcp.history import PatchRejected, ReplayHalted
from rcp.limits import AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES
from rcp.projects import ProjectCatalog
from rcp.runs.approval_cards import (
    PLAN_CARD_KINDS,
    PLAN_RUN_HERE,
    action_graph,
    approval_pins,
    approved_receipt,
    plan_card_actor,
    plan_card_episode_id,
    run_here_receipt,
    with_started,
)
from rcp.runs.experiment_admission import experiment_node, run_here_message
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.storage.approval_cards import card_sha256
from rcp.storage.question_models import QuestionRecord
from rcp.transport.state import StateUnavailable

router = APIRouter()
membership_router = APIRouter(dependencies=[Depends(require_project_membership)])
logger = logging.getLogger(__name__)


_DECISION_REASONS = {
    "approved": "Approved from an approval card",
    "rejected": "Declined from an approval card",
}


class DecideProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approved", "rejected"]


class ResolveApprovalBody(BaseModel):
    """`park`, `save` and `run_here` are plan card decisions; `run_here` is an Experiment's."""

    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "decline", "park", "save", "run_here"]
    pinned_sha256: str
    start: PlanStartOverrides | None = None


_OPEN_STATES = frozenset({"pending", "parked"})


def _sync_proposal(
    action: ProposalAction,
    request: Request,
    store: AppStore,
    catalog: ProjectCatalog,
    identity_access: IdentityAccess,
    service: ProjectService,
    pinned_revision: int,
) -> dict[str, Any]:
    """Decide the Proposal through the Inbox Sync of its graph, main or the branch it names,
    appending only at `pinned_revision`."""
    from rcp.api.sync import sync_graph
    from rcp.service import GraphSyncRequest

    try:
        return sync_graph(
            action.project_id,
            GraphSyncRequest.model_validate(
                {
                    "base_revision": pinned_revision,
                    "proposals": [action.model_dump(exclude={"kind", "project_id", "branch_id"})],
                }
            ),
            request,
            branch_id=action.branch_id,
            catalog=catalog,
            store=store,
            identity_access=identity_access,
            project_display_cache=get_project_display_cache(request),
            watcher_delivery=get_watcher_delivery(request),
        )
    except HTTPException as exc:
        graph = action_graph(service, action)
        if exc.status_code == 409 and graph.history.state().revision != pinned_revision:
            raise _NotNow(409, {"code": "graph_changed"}) from exc
        raise


def _execute(
    action: ApprovalAction,
    request_id: str,
    request: Request,
    store: AppStore,
    catalog: ProjectCatalog,
    identity_access: IdentityAccess,
    pinned: dict[str, Any],
    service: ProjectService,
    pinned_revision: int,
    human: AuthorizedHuman,
    source_operation_id: str,
    overrides: PlanStartOverrides | None,
):
    """Run the action through its human handler.

    A Proposal Sync appends only at `pinned_revision`, the graph the pins were read from,
    so its append lock rechecks the pins; a moved graph releases the card.
    """
    from rcp.api.episode_routes import (
        MergeBranchBody,
        _merge_episode_branch,
        merge_graph_branch,
    )
    from rcp.api.project_state import RepositoryWritableRequest, set_repository_writable

    project_id = action.project_id
    if action.kind == "experiment_start":
        assert isinstance(overrides, ExperimentStartOverrides)
        return _start_experiment(
            action,
            overrides,
            request_id,
            request,
            catalog,
            human,
            source_operation_id,
            tuple((decision, option) for decision, option in pinned["governing"]),
        )
    if action.kind == "proposal":
        return _sync_proposal(
            action, request, store, catalog, identity_access, service, pinned_revision
        )
    if action.kind == "truth_scope":
        return set_repository_writable(
            project_id,
            action.alias,
            RepositoryWritableRequest(writable=action.writable),
            request,
            identity_access=identity_access,
            project_display_cache=get_project_display_cache(request),
        )
    if action.kind == "auto_research":
        assert isinstance(overrides, AutoResearchStartOverrides)
        return _start_auto_research(
            action, overrides, request_id, request, catalog, human, source_operation_id
        )
    if action.kind == "merge_branch":
        return merge_graph_branch(
            project_id,
            action.branch_id,
            MergeBranchBody(request_id=UUID(request_id)),
            request,
            catalog=catalog,
            identity_access=identity_access,
            background_tasks=request.app.state.background_tasks,
        )
    return _merge_episode_branch(
        project_id,
        action.episode_id,
        request,
        MergeEpisodeBody.model_validate(
            {
                **action.model_dump(exclude={"kind", "project_id", "episode_id", "request_id"}),
                "request_id": request_id,
            }
        ),
        catalog=catalog,
        store=store,
        identity_access=identity_access,
        background_tasks=request.app.state.background_tasks,
        expected_source_head=GraphHeadRef.model_validate(pinned["source_head"]),
        # Main is not pinned; the handler's head check needs only the source.
        expected_main_head=service.history.head_ref(),
    )


def _start_experiment(
    action: ExperimentStartAction,
    overrides: ExperimentStartOverrides,
    request_id: str,
    request: Request,
    catalog: ProjectCatalog,
    human: AuthorizedHuman,
    source_operation_id: str,
    governing: tuple[tuple[str, str | None], ...],
):
    """Start the plan as the tapping member, from the raising turn, under the card's id."""
    from rcp.api.dependencies import get_background_tasks, get_experiment_operation_lock
    from rcp.api.experiments import start_work_experiment
    from rcp.runs.experiment_admission import (
        ExperimentNotReady,
        PlanChanged,
        WorkExperimentStart,
    )
    from rcp.storage import AgentTaskAdmissionConflict

    try:
        return start_work_experiment(
            catalog,
            get_background_tasks(request),
            get_experiment_operation_lock(request),
            WorkExperimentStart(
                source_operation_id=source_operation_id,
                episode_id=plan_card_episode_id(request_id, "experiment_start"),
                node_id=action.node_id,
                node_rev=overrides.node_rev,
                governing=governing,
                goal=overrides.goal,
                invocation_limit=overrides.invocation_ceiling,
                provider=overrides.provider,
                model=overrides.model,
                reasoning=overrides.reasoning,
                authorized_by=human,
            ),
        )
    except PlanChanged as exc:
        raise _NotNow(409, {"code": "plan_changed"}) from exc
    except (ExperimentNotReady, AgentTaskAdmissionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _start_auto_research(
    action: AutoResearchAction,
    overrides: AutoResearchStartOverrides,
    request_id: str,
    request: Request,
    catalog: ProjectCatalog,
    human: AuthorizedHuman,
    source_operation_id: str,
):
    from rcp.api.dependencies import get_background_tasks, get_experiment_operation_lock
    from rcp.api.work_episodes import start_work_auto_research
    from rcp.runs.work_episode_controls import EpisodeRefused, WorkAutoResearchStart

    try:
        return start_work_auto_research(
            catalog,
            get_background_tasks(request),
            get_experiment_operation_lock(request),
            WorkAutoResearchStart(
                source_operation_id=source_operation_id,
                episode_id=plan_card_episode_id(request_id, action.kind),
                goal=overrides.starting_instruction,
                invocation_ceiling=overrides.invocation_ceiling,
                code_worktree=overrides.code_worktree,
                worktree_repository=overrides.worktree_repository,
                branch_name=overrides.branch_name,
                provider=overrides.provider,
                model=overrides.model,
                reasoning=overrides.reasoning,
            ),
            authorized_by=human,
        )
    except EpisodeRefused as exc:
        detail = {"code": exc.code, "message": str(exc)}
        if exc.code == "auto_research_start_invalid":
            raise _NotNow(422, detail) from exc
        if exc.code in {
            "branch_name_invalid",
            "branch_name_taken",
            "provider_not_ready",
            "machine_not_found",
            "auto_research_profile_unavailable",
        }:
            raise _NotNow(409, detail) from exc
        raise HTTPException(status_code=422, detail=detail) from exc


def _require_seen_plan(
    state: GraphState, action: ExperimentStartAction, overrides: ExperimentStartOverrides | None
) -> None:
    """Refuse a Start the member did not see or could not mean; the card stays pending."""
    node = experiment_node(state, action.node_id)
    assert node is not None
    if overrides is None:
        raise _NotNow(422, {"code": "experiment_start_required"})
    if overrides.node_rev != node.updated_rev:
        raise _NotNow(409, {"code": "plan_changed"})
    _require_goal(overrides.goal)


def _require_goal(goal: str) -> None:
    if not goal.strip() or len(goal.encode()) > AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES:
        raise _NotNow(422, {"code": "goal_invalid"})


class _NotNow(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def _reason(detail: Any) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or "action failed")
    return str(detail)


def _live_state(
    card: dict[str, Any],
    action: ApprovalAction,
    service: ProjectService,
    store: AppStore,
    human: AuthorizedHuman,
) -> tuple[GraphState, dict[str, Any]] | None:
    """The graph state and live pins while they still match the card; None means stale.

    Stale covers a pinned target that no longer exists or no longer applies. An
    outage raises `_NotNow` before any write.
    """
    try:
        graph = (
            action_graph(service, action)
            if isinstance(action, ProposalAction | ExperimentStartAction)
            else service
        )
        state = graph.history.state()
        pinned = approval_pins(service, store, action, human, state=state)
    except (KeyError, ValueError):
        return None
    except (StateUnavailable, OSError) as exc:
        raise _NotNow(503, str(exc)) from exc
    if card_sha256(card["action"], pinned) != card["pinned_sha256"]:
        return None
    return state, pinned


def _resolve(
    card: dict[str, Any],
    question: QuestionRecord,
    body: ResolveApprovalBody,
    request: Request,
    store: AppStore,
    catalog: ProjectCatalog,
    identity_access: IdentityAccess,
    human: AuthorizedHuman,
) -> dict[str, Any]:
    """Stale only when live pins differ; outages raise `_NotNow` before any write.

    The claim already proved the presented hash is the card's. A Start runs from the
    turn that last revised the plan, which supplies its truth scope and graph target.
    """
    action = TypeAdapter(ApprovalAction).validate_python(card["action"])
    if body.decision == "decline" and action.kind != "auto_research":
        return {"outcome": "declined"}
    if body.start is not None and body.start.kind != action.kind:
        raise _NotNow(422, {"code": "start_kind_mismatch"})
    with ExitStack() as admission:
        service = _admit(admission, action, request, store, catalog)
        live = _live_state(card, action, service, store, human)
        if live is None:
            return {"outcome": "stale"}
        state, pinned = live
        if body.decision == "decline":
            return {"outcome": "declined"}
        if action.kind == "auto_research" and body.start is None:
            raise _NotNow(422, {"code": "auto_research_start_required"})
        if action.kind == "experiment_start":
            assert body.start is None or isinstance(body.start, ExperimentStartOverrides)
            _require_seen_plan(state, action, body.start)
            if body.decision == "run_here":
                assert body.start is not None
                return _run_here(
                    state, action, body.start, card["request_id"], question, request, store, human
                )
        if isinstance(body.start, AutoResearchStartOverrides):
            from rcp.api.episodes import StartEpisodeBody

            try:
                StartEpisodeBody.model_validate(
                    {"mode": "auto_research", **body.start.model_dump(exclude={"kind"})}
                )
            except ValueError as exc:
                raise _NotNow(
                    422, {"code": "auto_research_start_invalid", "message": str(exc)}
                ) from exc
        result = _run(
            lambda: _execute(
                action,
                card["request_id"],
                request,
                store,
                catalog,
                identity_access,
                pinned,
                service,
                state.revision,
                human,
                card["revised_operation_id"] or question.origin.operation_id,
                body.start,
            ),
            card["request_id"],
        )
        if result["outcome"] == "approved" and body.start is not None:
            result["result"] = with_started(store, result["result"])
        return result


def _run_here(
    state: GraphState,
    action: ExperimentStartAction,
    overrides: ExperimentStartOverrides,
    request_id: str,
    question: QuestionRecord,
    request: Request,
    store: AppStore,
    human: AuthorizedHuman,
) -> dict[str, Any]:
    """Send the plan to its own chat as the tapping member's message; start no episode.

    The message goes through the chat's ordinary queue under the card's request id, so a
    busy chat keeps it and a repeated tap names the same turn.
    """
    from rcp.api.agent_threads import QueuedMessageBody, queue_message
    from rcp.api.dependencies import get_attachment_store
    from rcp.limits import AGENT_THREAD_QUEUE_MAX_CHARS

    node = experiment_node(state, action.node_id)
    assert node is not None
    message = run_here_message(node, overrides.goal)
    if len(message) > AGENT_THREAD_QUEUE_MAX_CHARS:
        raise _NotNow(422, {"code": "run_here_too_long"})
    agent = store.agent(question.origin.owner_id)
    if agent is None or agent.project_id is None:
        raise _NotNow(409, {"code": "run_here_chat_unavailable"})
    try:
        with project_write_admission(agent.project_id, request) as project_id:
            queued = queue_message(
                store,
                get_catalog(request),
                get_attachment_store(request),
                agent,
                project_id,
                human.user_id,
                QueuedMessageBody(message=message),
                request_id,
            )
    except HTTPException as exc:
        raise _NotNow(exc.status_code, exc.detail) from exc
    return {"outcome": PLAN_RUN_HERE, "result": run_here_receipt(queued.queue_id)}


def _revise(
    card: dict[str, Any],
    question: QuestionRecord,
    body: ResolveApprovalBody,
    request: Request,
    store: AppStore,
    catalog: ProjectCatalog,
    human: AuthorizedHuman,
) -> dict[str, Any]:
    """Save the member's run fields into the plan card, and park it for `park`.

    One conditional write on the hash the member saw; it never takes the Start claim.
    Live pins that differ settle the card `stale`, as a Start would.
    """
    action = TypeAdapter(ApprovalAction).validate_python(card["action"])
    if body.start is not None and body.start.kind != action.kind:
        raise _NotNow(422, {"code": "start_kind_mismatch"})
    if body.decision == "save" and body.start is None:
        raise _NotNow(422, {"code": "start_required"})
    question_id = question.question_id
    if card["state"] not in _OPEN_STATES or card["pinned_sha256"] != body.pinned_sha256:
        raise _conflict(store, question_id, body.pinned_sha256)
    revised = card["action"]
    if body.start is not None:
        if isinstance(body.start, ExperimentStartOverrides):
            _require_goal(body.start.goal)
        revised = {
            **revised,
            **body.start.model_dump(mode="json", exclude={"kind", "node_rev"}),
        }
    with ExitStack() as admission:
        service = _admit(admission, action, request, store, catalog)
        if _live_state(card, action, service, store, human) is None:
            if not store.claim_approval_card(question_id, body.pinned_sha256):
                raise _conflict(store, question_id, body.pinned_sha256)
            return {"outcome": "stale"}
    digest = store.revise_approval_card(
        question_id,
        revised_by={"kind": "member", "user_id": human.user_id, "name": human.display_name},
        action=revised,
        expected_sha256=body.pinned_sha256,
        shelve=body.decision == "park",
    )
    if digest is None:
        raise _conflict(store, question_id, body.pinned_sha256)
    return {
        "outcome": "parked" if body.decision == "park" else "saved",
        "pinned_sha256": digest,
    }


def _conflict(store: AppStore, question_id: str, presented_sha256: str) -> HTTPException:
    """Why a tap could not act on the card: revised since the member read it, or taken."""
    card = store.approval_card(question_id)
    revised = (
        card is not None
        and card["state"] in _OPEN_STATES
        and card["pinned_sha256"] != presented_sha256
    )
    return HTTPException(
        status_code=409, detail={"code": "plan_revised" if revised else "card_taken"}
    )


def _admit(
    admission: ExitStack,
    action: ApprovalAction,
    request: Request,
    store: AppStore,
    catalog: ProjectCatalog,
) -> ProjectService:
    """Open the project's write admission and writer, or raise `_NotNow`."""
    try:
        store.require_project_accepts_new_work(action.project_id)
        # Direct calls skip FastAPI dependencies. Proposal, episode merge and
        # Experiment start handlers acquire this non-reentrant fence themselves.
        if action.kind not in {"proposal", "merge_episode", "experiment_start", "auto_research"}:
            admission.enter_context(project_write_admission(action.project_id, request))
        return get_project_writer(catalog, action.project_id)
    except ValueError as exc:
        raise _NotNow(409, str(exc)) from exc
    except HTTPException as exc:
        raise _NotNow(exc.status_code, exc.detail) from exc


def _run(execute: Callable[[], object], request_id: str) -> dict[str, Any]:
    """Map the human handler's outcome; an outage leaves the card for another tap."""
    try:
        response = execute()
    except _NotNow:
        raise
    except HTTPException as exc:
        if exc.detail == {"code": "merge_heads_changed"}:
            return {"outcome": "stale"}
        if exc.status_code == 503:
            raise _NotNow(503, exc.detail) from exc
        return {"outcome": "failed", "reason": _reason(exc.detail)}
    except (StateUnavailable, OSError) as exc:
        raise _NotNow(503, str(exc)) from exc
    except (PatchRejected, ReplayHalted) as exc:
        return {"outcome": "failed", "reason": str(exc)}
    except Exception:
        logger.exception("Approval request %s failed", request_id)
        return {"outcome": "failed", "reason": "action failed"}
    return {"outcome": "approved", "result": approved_receipt(response)}


def decide_proposal(
    request: Request,
    project_id: str,
    proposal_id: str,
    decision: Literal["approved", "rejected"],
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Decide one pending, fresh Proposal of main or `branch_id` as the member, at the
    revision it was read.

    Authority is the patch-capable member, project membership and write admission,
    the same as an Inbox decision. A Proposal's content never changes under its id
    and staleness is rechecked at the fenced revision, so no presented hash is needed.
    Raises `HTTPException` with the refusal.
    """
    store, catalog = get_store(request), get_catalog(request)
    identity_access = get_identity_access(request)
    identity_access.require_patch_capable_identity(request)
    project_id = require_project_membership(project_id, request)
    action = ProposalAction(
        kind="proposal",
        project_id=project_id,
        proposal_id=proposal_id,
        decision=decision,
        reason=_DECISION_REASONS[decision],
        branch_id=branch_id,
    )
    try:
        with ExitStack() as admission:
            service = _admit(admission, action, request, store, catalog)
            graph, revision = _decidable_revision(service, action)
            try:
                synced = _sync_proposal(
                    action, request, store, catalog, identity_access, service, revision
                )
            except StateUnavailable as exc:
                raise _NotNow(503, str(exc)) from exc
            except (PatchRejected, ReplayHalted) as exc:
                raise _NotNow(422, str(exc)) from exc
            except _NotNow as exc:
                if exc.detail == {"code": "graph_changed"} and not _still_pending(
                    graph, proposal_id
                ):
                    raise _NotNow(409, {"code": "proposal_not_pending"}) from exc
                raise
    except _NotNow as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"proposal_id": proposal_id, "status": synced["proposals"][proposal_id]["status"]}


def _decidable_revision(
    service: ProjectService, action: ProposalAction
) -> tuple[ProjectService, int]:
    """The Proposal's graph and the revision a card decision appends at, once the Proposal
    is pending and fresh there."""
    try:
        graph = action_graph(service, action)
        state = graph.history.state()
    except ValueError as exc:
        raise _NotNow(404, "Proposal not found") from exc
    except (StateUnavailable, ReplayHalted, OSError) as exc:
        raise _NotNow(503, str(exc)) from exc
    proposal = state.proposals.get(action.proposal_id)
    if proposal is None:
        raise _NotNow(404, "Proposal not found")
    if proposal.status != "pending" or proposal_is_stale(state, proposal):
        raise _NotNow(409, {"code": "proposal_not_pending"})
    return graph, state.revision


def _still_pending(service: ProjectService, proposal_id: str) -> bool:
    proposal = service.history.state().proposals.get(proposal_id)
    return proposal is not None and proposal.status == "pending"


@membership_router.post("/api/approval-cards/proposals/{project_id}/{proposal_id:path}")
def decide_proposal_card(
    project_id: str, proposal_id: str, body: DecideProposalBody, request: Request
) -> dict[str, Any]:
    return decide_proposal(request, project_id, proposal_id, body.decision)


def _declined_rejection(request: Request, card: dict[str, Any]) -> dict[str, Any]:
    """Reject the Proposal a declined approve card named, or say why it stays pending."""
    action = ProposalAction.model_validate(card["action"])
    try:
        decide_proposal(
            request, action.project_id, action.proposal_id, "rejected", action.branch_id
        )
    except HTTPException as exc:
        if exc.detail != {"code": "proposal_not_pending"}:
            return {"rejected": False, "reason": _reason(exc.detail)}
        catalog = get_catalog(request)
        graph = action_graph(catalog.open(action.project_id), action)
        if _still_pending(graph, action.proposal_id):
            return {
                "rejected": False,
                "reason": "The Proposal is out of date; decide it in the Inbox.",
            }
        return {}
    return {"rejected": True}


def _owned_card(
    store: AppStore, question_id: str, request: Request, human: AuthorizedHuman
) -> tuple[QuestionRecord, dict[str, Any]]:
    """The card this member may tap. Membership is checked on the action's project: a
    project thread's card names its own project, and a Space thread's any of the member's.

    A private thread's card is its owner's. In a shared or node chat only an episode
    plan card exists, and `plan_card_actor` decides who taps it.
    """
    question = store.get_question(question_id)
    if question is None or question.origin.owner_kind != "chat":
        raise HTTPException(status_code=404, detail="Question not found")
    if (owner := store.private_chat_owner(question.origin.owner_id)) is not None:
        allowed = owner == human.user_id
    else:
        shared = store.approval_card(question_id)
        allowed = (
            shared is not None
            and shared["kind"] in {"experiment_start", "auto_research"}
            and plan_card_actor(store, question, human)
        )
    if not allowed:
        raise HTTPException(status_code=404, detail="Question not found")
    if refusal := store.chat_turn_refusal(question.origin.project_id, question.origin.owner_id):
        raise HTTPException(status_code=409, detail={"code": refusal})
    card = store.approval_card(question_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Approval card not found")
    require_project_membership(card["action"]["project_id"], request)
    return question, card


@router.post("/api/approval-cards/{question_id}")
def resolve_approval_card(
    question_id: str,
    body: ResolveApprovalBody,
    request: Request,
    *,
    store: Annotated[AppStore, Depends(get_store)],
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
    identity_access: Annotated[IdentityAccess, Depends(get_identity_access)],
):
    human = identity_access.require_patch_capable_identity(request)
    question, card = _owned_card(store, question_id, request, human)
    if body.decision in {"park", "save", "run_here"} and card["kind"] not in (
        {"experiment_start"} if body.decision == "run_here" else PLAN_CARD_KINDS
    ):
        raise HTTPException(status_code=422, detail={"code": "decision_not_available"})
    if body.decision == "run_here" and body.start is None:
        raise HTTPException(status_code=422, detail={"code": "experiment_start_required"})
    if body.decision in {"park", "save"}:
        try:
            result = _revise(card, question, body, request, store, catalog, human)
        except _NotNow as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if result["outcome"] != "stale":
            return result
        store.settle_approval_card(question_id, result, resolved_by=human)
        _deliver(request, question)
        return result
    if not store.claim_approval_card(question_id, body.pinned_sha256):
        store.approval_card(question_id)  # Expiry may have passed since the first read.
        if store.take_expired_approval_card(question_id):
            return {"outcome": "expired"}
        raise _conflict(store, question_id, body.pinned_sha256)
    card = store.approval_card(question_id) or card
    try:
        result = _resolve(card, question, body, request, store, catalog, identity_access, human)
    except _NotNow as exc:
        # An outage or closed admission is not the member's answer; keep the card open.
        store.release_approval_card(question_id)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    store.settle_approval_card(
        question_id,
        result,
        resolved_by=human,
        received_by_operation_id=(
            result["result"]["operation_id"] if result["outcome"] == PLAN_RUN_HERE else None
        ),
    )
    if result["outcome"] == PLAN_RUN_HERE:
        request.app.state.reconcile_thread_handoffs()
    try:
        action = TypeAdapter(ApprovalAction).validate_python(card["action"])
        if (
            result["outcome"] == "declined"
            and isinstance(action, ProposalAction)
            and action.decision == "approved"
        ):
            rejection = _declined_rejection(request, card)
            if rejection:
                store.record_declined_rejection(question_id, rejection)
            result = {**result, **rejection}
    finally:
        _deliver(request, question)
    return result


def _deliver(request: Request, question: QuestionRecord) -> None:
    try:
        request.app.state.reconcile_question_answers(question.origin.project_id)
    except Exception:
        logger.exception(
            "Approval card %s settled; immediate delivery failed", question.question_id
        )
