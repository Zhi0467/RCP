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
from rcp.approval_actions import ApprovalAction, MergeEpisodeBody
from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphHeadRef
from rcp.history import PatchRejected, ReplayHalted
from rcp.projects import ProjectCatalog
from rcp.runs.approval_cards import approval_pins, approved_receipt
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.storage.approval_cards import card_sha256
from rcp.storage.question_models import QuestionRecord
from rcp.transport.state import StateUnavailable

router = APIRouter()
logger = logging.getLogger(__name__)


class ResolveApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "decline"]
    pinned_sha256: str


def _execute(
    action: ApprovalAction,
    request_id: str,
    request: Request,
    store: AppStore,
    catalog: ProjectCatalog,
    identity_access: IdentityAccess,
    pinned: dict[str, Any],
    service: ProjectService,
):
    from rcp.api.episode_routes import (
        MergeBranchBody,
        _merge_episode_branch,
        merge_graph_branch,
        start_episode,
    )
    from rcp.api.episodes import StartEpisodeBody
    from rcp.api.project_state import RepositoryWritableRequest, set_repository_writable
    from rcp.api.sync import sync_graph
    from rcp.service import GraphSyncRequest

    project_id = action.project_id
    if action.kind == "proposal":
        return sync_graph(
            project_id,
            GraphSyncRequest.model_validate(
                {
                    "base_revision": service.history.state().revision,
                    "proposals": [action.model_dump(exclude={"kind", "project_id"})],
                }
            ),
            request,
            catalog=catalog,
            store=store,
            identity_access=identity_access,
            project_display_cache=get_project_display_cache(request),
            watcher_delivery=get_watcher_delivery(request),
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
    with ExitStack() as admission:
        service = _admit(admission, action, request, store, catalog)
        try:
            pinned = approval_pins(service, store, action, human)
        except (KeyError, ValueError):
            # The pinned target no longer exists or no longer applies.
            return {"outcome": "stale"}
        except (StateUnavailable, OSError) as exc:
            raise _NotNow(503, str(exc)) from exc
        if card_sha256(card["action"], pinned) != card["pinned_sha256"]:
            return {"outcome": "stale"}
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
        # Direct calls skip FastAPI dependencies. Proposal and episode merge
        # handlers acquire this non-reentrant fence themselves.
        if action.kind not in {"proposal", "merge_episode"}:
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
    except HTTPException as exc:
        if exc.detail == {"code": "merge_heads_changed"}:
            return {"outcome": "stale"}
        if exc.status_code == 503:
            raise _NotNow(503, exc.detail) from exc
        return {"outcome": "failed", "reason": _reason(exc.detail)}
    except StateUnavailable as exc:
        raise _NotNow(503, str(exc)) from exc
    except (PatchRejected, ReplayHalted) as exc:
        return {"outcome": "failed", "reason": str(exc)}
    except Exception:
        logger.exception("Approval request %s failed", request_id)
        return {"outcome": "failed", "reason": "action failed"}
    return {"outcome": "approved", "result": approved_receipt(response)}


def _owned_card(
    store: AppStore, question_id: str, request: Request, human: AuthorizedHuman
) -> tuple[QuestionRecord, dict[str, Any]]:
    question = store.get_question(question_id)
    if (
        question is None
        or question.origin.owner_kind != "chat"
        or store.private_question_owner(question.origin.owner_id) != human.user_id
    ):
        raise HTTPException(status_code=404, detail="Question not found")
    require_project_membership(question.origin.project_id, request)
    if refusal := store.chat_turn_refusal(question.origin.project_id, question.origin.owner_id):
        raise HTTPException(status_code=409, detail={"code": refusal})
    card = store.approval_card(question_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Approval card not found")
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
        result = _resolve(card, body, request, store, catalog, identity_access, human)
    except _NotNow as exc:
        # An outage or closed admission is not the member's answer; keep the card open.
        store.release_approval_card(question_id)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    store.settle_approval_card(question_id, result, resolved_by=human)
    try:
        request.app.state.reconcile_question_answers(question.origin.project_id)
    except Exception:
        logger.exception("Approval card %s settled; immediate delivery failed", question_id)
    return result
