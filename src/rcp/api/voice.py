"""Member-private voice preferences, transcripts, and WebRTC session creation."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from rcp import limits
from rcp.api.service_connections import ServiceConnectionRoute, connections
from rcp.service_connections import (
    IdleMinutes,
    VoiceSettings,
    VoiceTranscriptSave,
    voice_resume_input,
)
from rcp.voice import SessionRequest, create_session


class VoiceRoute(ServiceConnectionRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.method in {"POST", "PUT"}:
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > limits.VOICE_TRANSCRIPT_REQUEST_MAX_BYTES:
                        raise HTTPException(413, {"code": "voice_request_too_large"})
                    body.extend(chunk)
                # Cache the bounded body for FastAPI's normal Pydantic validation.
                request._body = bytes(body)
            return await handler(request)

        return bounded


router = APIRouter(prefix="/api/voice", route_class=VoiceRoute)


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
    record = store.claim_voice_session(body.resume_id)
    session_input, truncated = voice_resume_input(record["entries"])
    answer = await create_session(connection, key, settings, body, session_input)
    store.require_voice_generation(record["id"], record["generation"])
    return {
        "sdp_answer": answer,
        "session": record,
        "input_truncated": truncated,
        "limits": {
            "idle_seconds": settings["idle_minutes"] * 60,
            "hard_cap_seconds": limits.VOICE_HARD_CAP_SECONDS,
            "confirm_timeout_seconds": limits.VOICE_CONFIRM_TIMEOUT_SECONDS,
            "commentary_max_chars": limits.VOICE_COMMENTARY_MAX_CHARS,
            "transcript_entry_max_bytes": limits.VOICE_TRANSCRIPT_ENTRY_MAX_BYTES,
            "transcript_session_max_bytes": limits.VOICE_TRANSCRIPT_SESSION_MAX_BYTES,
            "transcript_max_entries": limits.VOICE_TRANSCRIPT_MAX_ENTRIES,
            "transcript_max_receipts": limits.VOICE_TRANSCRIPT_MAX_RECEIPTS,
        },
    }


@router.get("/sessions")
def list_sessions(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(5, ge=1, le=limits.VOICE_TRANSCRIPT_MAX_SESSIONS),
):
    return connections(request).list_voice_sessions(offset, limit)


@router.put("/sessions/{session_id}")
def save_session(request: Request, session_id: str, body: VoiceTranscriptSave):
    return connections(request).save_voice_session(session_id, body)


@router.delete("/sessions/{session_id}")
def delete_session(request: Request, session_id: str):
    connections(request).delete_voice_session(session_id)
    return {"deleted": True}
