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
    ExperimentStartAction,
    ExperimentStartOverrides,
    MergeEpisodeBody,
    ProposalAction,
)
from rcp.core.models import AuthorizedHuman, GraphState
from rcp.core.transition_models import GraphHeadRef
from rcp.core.validation.proposals import proposal_is_stale
from rcp.history import PatchRejected, ReplayHalted
from rcp.limits import AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES
from rcp.projects import ProjectCatalog
from rcp.runs.approval_cards import (
    action_graph,
    approval_pins,
    approved_receipt,
    experiment_card_actor,
    experiment_card_episode_id,
)
from rcp.runs.experiment_admission import experiment_node
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
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "decline"]
    pinned_sha256: str
    experiment_start: ExperimentStartOverrides | None = None


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
    overrides: ExperimentStartOverrides | None,
):
    """Run the action through its human handler.

    A Proposal Sync appends only at `pinned_revision`, the graph the pins were read from,
    so its append lock rechecks the pins; a moved graph releases the card.
    """
    from rcp.api.episode_routes import (
        MergeBranchBody,
        _merge_episode_branch,
        merge_graph_branch,
        start_episode,
    )
    from rcp.api.episodes import StartEpisodeBody
    from rcp.api.project_state import RepositoryWritableRequest, set_repository_writable

    project_id = action.project_id
    if action.kind == "experiment_start":
        assert overrides is not None
        return _start_experiment(
            action, overrides, request_id, request, catalog, human, source_operation_id
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
        return start_episode(
            project_id,
            StartEpisodeBody.model_validate(
                {"mode": "auto_research", **action.model_dump(exclude={"kind", "project_id"})}
            ),
            request,
            catalog=catalog,
            store=store,
            identity_access=identity_access,
            background_tasks=request.app.state.background_tasks,
            idempotency_key=UUID(request_id),
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
                episode_id=experiment_card_episode_id(request_id),
                node_id=action.node_id,
                node_rev=overrides.node_rev,
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
    if overrides.invocation_ceiling > node.invocation_ceiling:
        raise _NotNow(422, {"code": "invocation_ceiling_above_node"})
    goal = overrides.goal
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
    """Stale only when live pins differ; outages raise `_NotNow` before any write."""
    if body.pinned_sha256 != card["pinned_sha256"]:
        return {"outcome": "stale"}
    if body.decision == "decline":
        return {"outcome": "declined"}
    action = TypeAdapter(ApprovalAction).validate_python(card["action"])
    if body.experiment_start is not None and action.kind != "experiment_start":
        raise _NotNow(422, {"code": "experiment_start_not_accepted"})
    with ExitStack() as admission:
        service = _admit(admission, action, request, store, catalog)
        try:
            graph = (
                action_graph(service, action)
                if action.kind in {"proposal", "experiment_start"}
                else service
            )
            state = graph.history.state()
            pinned_revision = state.revision
            pinned = approval_pins(service, store, action, human, state=state)
        except (KeyError, ValueError):
            # The pinned target no longer exists or no longer applies.
            return {"outcome": "stale"}
        except (StateUnavailable, OSError) as exc:
            raise _NotNow(503, str(exc)) from exc
        if card_sha256(card["action"], pinned) != card["pinned_sha256"]:
            return {"outcome": "stale"}
        if action.kind == "experiment_start":
            _require_seen_plan(state, action, body.experiment_start)
        return _run(
            lambda: _execute(
                action,
                card["request_id"],
                request,
                store,
                catalog,
                identity_access,
                pinned,
                service,
                pinned_revision,
                human,
                question.origin.operation_id,
                body.experiment_start,
            ),
            card["request_id"],
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
        if action.kind not in {"proposal", "merge_episode", "experiment_start"}:
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

    A private thread's card is its owner's. In a shared or node chat only an Experiment
    plan card exists, and `experiment_card_actor` decides who taps it.
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
            and shared["kind"] == "experiment_start"
            and experiment_card_actor(store, question, human)
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
    if not store.claim_approval_card(question_id):
        store.approval_card(question_id)  # Expiry may have passed since the first read.
        if store.take_expired_approval_card(question_id):
            return {"outcome": "expired"}
        raise HTTPException(status_code=409, detail={"code": "card_taken"})
    try:
        result = _resolve(card, question, body, request, store, catalog, identity_access, human)
    except _NotNow as exc:
        # An outage or closed admission is not the member's answer; keep the card open.
        store.release_approval_card(question_id)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    store.settle_approval_card(question_id, result, resolved_by=human)
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
        try:
            request.app.state.reconcile_question_answers(question.origin.project_id)
        except Exception:
            logger.exception("Approval card %s settled; immediate delivery failed", question_id)
    return result
