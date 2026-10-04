"""Credential-verifying personal sign-in and owner session management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from rcp.api.dependencies import get_identity_access, get_store
from rcp.api.identity import OWNER_SESSION_COOKIE, IdentityAccess, mutation_origin_matches
from rcp.limits import TEAM_DEVICE_PAIRING_CODE_MAX_LENGTH
from rcp.storage import AppStore

router = APIRouter()
Store = Annotated[AppStore, Depends(get_store)]
Identity = Annotated[IdentityAccess, Depends(get_identity_access)]


class OwnerExchange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    secret: str = Field(min_length=43, max_length=43)


class OwnerRedemption(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=TEAM_DEVICE_PAIRING_CODE_MAX_LENGTH)
    secret: str | None = Field(default=None, min_length=43, max_length=43)


def _admit_exchange(request: Request, store: AppStore) -> None:
    if store.space_kind != "personal":
        raise HTTPException(404, "Owner sign-in is unavailable.")
    origin = request.headers.get("origin")
    if origin is not None and not mutation_origin_matches(request, origin):
        raise HTTPException(403, detail={"code": "owner_origin_invalid"})


@router.post("/api/owner/exchange")
def exchange(
    body: OwnerExchange, request: Request, response: Response, store: Store, identity: Identity
) -> dict[str, object]:
    _admit_exchange(request, store)
    session, owner = store.create_owner_session(body.secret)
    identity.set_owner_session_cookie(response, session, secure=request.url.scheme == "https")
    response.headers["Cache-Control"] = "no-store"
    return identity.identity_payload(owner)


@router.post("/api/owner/redeem")
def redeem(
    body: OwnerRedemption, request: Request, response: Response, store: Store, identity: Identity
) -> dict[str, object]:
    _admit_exchange(request, store)
    session, owner = store.redeem_owner_sign_in_code(body.code, secret=body.secret)
    identity.set_owner_session_cookie(response, session, secure=request.url.scheme == "https")
    response.headers["Cache-Control"] = "no-store"
    return identity.identity_payload(owner)


@router.post("/api/owner/logout")
def logout(
    request: Request, response: Response, store: Store, identity: Identity
) -> dict[str, bool]:
    _admit_exchange(request, store)
    identity.acting_user(request)
    store.delete_team_session(request.cookies.get(OWNER_SESSION_COOKIE))
    response.delete_cookie(
        OWNER_SESSION_COOKIE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return {"ok": True}


@router.get("/api/owner/sessions")
def sessions(request: Request, store: Store, identity: Identity) -> dict[str, object]:
    _admit_exchange(request, store)
    owner = identity.acting_user(request)
    return {
        "sessions": [
            item.model_dump(mode="json")
            for item in store.team_sessions(
                owner.user_id, authenticating_session=request.cookies.get(OWNER_SESSION_COOKIE)
            )
        ]
    }


@router.delete("/api/owner/sessions/{session_id}")
def revoke(session_id: str, request: Request, store: Store, identity: Identity) -> dict[str, bool]:
    _admit_exchange(request, store)
    owner = identity.acting_user(request)
    try:
        store.revoke_team_session(
            session_id,
            owner.user_id,
            authenticating_session=request.cookies.get(OWNER_SESSION_COOKIE),
        )
    except KeyError as exc:
        raise HTTPException(404, "Session not found.") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}
