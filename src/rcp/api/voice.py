"""Member-private voice preferences, transcripts, and WebRTC session creation."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from rcp import limits
from rcp.api.dependencies import (
    get_catalog,
    get_project_service,
    get_project_writer,
    get_store,
    project_write_admission,
)
from rcp.api.service_connections import ServiceConnectionRoute, connections
from rcp.service_connections import (
    ConnectionError,
    IdleMinutes,
    VoiceSettings,
    voice_resume_input,
)
from rcp.voice import SessionRequest, create_session
from rcp.voice_threads import (
    VoiceEntries,
    append_space_entries,
    owned_thread,
    pending_records,
    read_records,
    transcript_path,
)


class VoiceRoute(ServiceConnectionRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.method in {"POST", "PUT"}:
                # A body without a declared length (chunked) would be buffered in
                # full before validation, so a bounded length is required.
                length = request.headers.get("content-length")
                if length is None:
                    raise HTTPException(411, {"code": "voice_request_length_required"})
                try:
                    size = int(length)
                except ValueError:
                    raise HTTPException(400, {"code": "voice_request_invalid_length"}) from None
                if size > limits.VOICE_TRANSCRIPT_REQUEST_MAX_BYTES:
                    raise HTTPException(413, {"code": "voice_request_too_large"})
            return await handler(request)

        return bounded


router = APIRouter(prefix="/api/voice", route_class=VoiceRoute)
thread_router = APIRouter(prefix="/api/agent-threads", route_class=VoiceRoute)


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
    thread = owned_thread(store.store, body.thread_id, store.member_id)
    connection, key = store.voice_credentials()
    settings = store.voice_settings()
    if thread["project_id"]:
        service = get_project_writer(get_catalog(request), thread["project_id"])
        with service.history.workspace.read_snapshot() as root:
            records = read_records(transcript_path(root, thread), thread)
    else:
        records = read_records(transcript_path(store.store.path.parent, thread), thread)
    session_input, truncated = voice_resume_input(
        [
            {
                "speaker": "member" if row["role"] == "user" else "agent",
                "text": row["text"],
                "source": row.get("source"),
            }
            for row in records
        ]
    )
    # Retire pre-thread records under their existing retention rule; never claim one.
    store.list_voice_sessions(0, 1)
    answer = await create_session(connection, key, settings, body, session_input)
    owned_thread(store.store, body.thread_id, store.member_id)
    return {
        "sdp_answer": answer,
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


@thread_router.post("/{thread_id}/voice-entries")
def voice_entries(request: Request, thread_id: str, body: VoiceEntries):
    private = connections(request)
    thread = owned_thread(private.store, thread_id, private.member_id)
    if thread["project_id"]:
        with project_write_admission(thread["project_id"], request):
            service = get_project_writer(get_catalog(request), thread["project_id"])
            from rcp.runs.chat import _append_chat_records

            with service.history.workspace.transaction():
                path = transcript_path(service.history.workspace.root, thread)
                records = pending_records(path, thread, body.entries)
                _append_chat_records(service, path, records)
                appended = len(records)
    else:
        appended = append_space_entries(
            private.store, transcript_path(private.store.path.parent, thread), thread, body.entries
        )
    if appended:
        private.store.bump_agent_threads_version(private.member_id)
    return {"appended": appended}


@thread_router.get("/{thread_id}/memory")
def voice_memory(request: Request, thread_id: str):
    private = connections(request)
    thread = owned_thread(private.store, thread_id, private.member_id)
    if not thread["project_id"]:
        return {"truncated": False}
    from rcp.agents.memory_files import read_project_memory_indexes
    from rcp.transport.run_stage import RemoteRunStage

    service = get_project_service(get_catalog(request), thread["project_id"])
    host = service.manifest.machine_map[service.manifest.base_machine].host
    remote = RemoteRunStage(host) if host else None
    try:
        result = read_project_memory_indexes(
            get_store(request), thread["project_id"], private.member_id, remote
        )
    except (OSError, ValueError):
        raise ConnectionError("thread_memory_unavailable", 503) from None
    owned_thread(private.store, thread_id, private.member_id)
    return result
