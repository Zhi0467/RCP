"""Member-only approval taps reuse the owning human handlers and admission."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
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
from rcp.core.transition_models import GraphHeadRef
from rcp.projects import ProjectCatalog
from rcp.runs.approval_cards import approval_pins
from rcp.storage import AppStore
from rcp.storage.approval_cards import card_sha256

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
        service = get_project_writer(catalog, project_id)
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
        expected_main_head=GraphHeadRef.model_validate(pinned["main_head"]),
    )


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
    if not store.claim_approval_card(question_id):
        store.approval_card(question_id)  # Expiry may have passed since the first read.
        if store.take_expired_approval_card(question_id):
            return {"outcome": "expired"}
        raise HTTPException(status_code=409, detail={"code": "card_taken"})
    try:
        if body.pinned_sha256 != card["pinned_sha256"]:
            result = {"outcome": "stale"}
        elif body.decision == "decline":
            result = {"outcome": "declined"}
        else:
            action = TypeAdapter(ApprovalAction).validate_python(card["action"])
            # Direct calls skip FastAPI dependencies. Proposal and episode merge
            # handlers acquire this non-reentrant fence themselves.
            with (
                project_write_admission(action.project_id, request)
                if action.kind not in {"proposal", "merge_episode"}
                else nullcontext()
            ):
                try:
                    pinned = approval_pins(
                        get_project_writer(catalog, action.project_id), store, action, human
                    )
                except (KeyError, ValueError):
                    pinned = None
                if pinned is None or card_sha256(card["action"], pinned) != card["pinned_sha256"]:
                    result = {"outcome": "stale"}
                else:
                    result = {
                        "outcome": "approved",
                        "result": jsonable_encoder(
                            _execute(
                                action,
                                card["request_id"],
                                request,
                                store,
                                catalog,
                                identity_access,
                                pinned,
                            )
                        ),
                    }
    except HTTPException as exc:
        result = (
            {"outcome": "stale"}
            if exc.detail == {"code": "merge_heads_changed"}
            else {"outcome": "failed", "reason": str(exc.detail)}
        )
    except Exception:
        logger.exception("Approval card %s failed", question_id)
        result = {"outcome": "failed", "reason": "action failed"}
    store.settle_approval_card(question_id, result, resolved_by=human)
    try:
        request.app.state.reconcile_question_answers(question.origin.project_id)
    except Exception:
        logger.exception("Approval card %s settled; immediate delivery failed", question_id)
    return result
