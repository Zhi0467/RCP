"""Member voice preferences and stateless WebRTC session creation."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from rcp import limits
from rcp.api.service_connections import ServiceConnectionRoute, connections
from rcp.service_connections import IdleMinutes, VoiceSettings
from rcp.voice import SessionRequest, create_session

router = APIRouter(prefix="/api/voice", route_class=ServiceConnectionRoute)


@router.get("/settings")
def settings(request: Request):
    return connections(request).voice_settings()


class VoicePreferences(BaseModel):
    """What this route may change; voice models go through the checked connection update."""

    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: Literal["tap", "none"] | None = None
    idle_minutes: IdleMinutes | None = None


@router.put("/settings")
def update_settings(request: Request, body: VoicePreferences):
    changed = body.model_dump(exclude_none=True)
    if not changed:
        raise HTTPException(422, "Send confirm or idle_minutes.")
    # Only the sent fields count as set, so the others keep their saved value.
    return connections(request).voice_settings(VoiceSettings(**changed))


@router.post("/sessions")
async def session(request: Request, body: SessionRequest):
    store = connections(request)
    connection, key = store.voice_credentials()
    settings = store.voice_settings()
    answer = await create_session(connection, key, settings, body)
    store.require_member()
    return {
        "sdp_answer": answer,
        "limits": {
            "idle_seconds": settings["idle_minutes"] * 60,
            "hard_cap_seconds": limits.VOICE_HARD_CAP_SECONDS,
            "confirm_timeout_seconds": limits.VOICE_CONFIRM_TIMEOUT_SECONDS,
            "commentary_max_chars": limits.VOICE_COMMENTARY_MAX_CHARS,
        },
    }
