"""Authenticated member service settings and raw, memory-only audio uploads."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager, suppress

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from rcp import limits
from rcp.api.dependencies import get_identity_access, get_store
from rcp.service_connections import ConnectionError, ModelId, PurposesRequest, ServiceConnections
from rcp.transcription import (
    FORMATS,
    ConnectRequest,
    ServiceAddress,
    check_connection,
    check_purposes,
    list_models,
    transcribe,
)
from rcp.voice import check_delegation_model


class ServiceConnectionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, {"code": "invalid_service_connection_request"}) from None
            except ConnectionError as exc:
                raise HTTPException(
                    exc.status, {"code": exc.code, "message": exc.message}
                ) from None

        return handle


router = APIRouter(prefix="/api/service-connections", route_class=ServiceConnectionRoute)
_ACTIVE: dict[tuple[str, str], int] = {}
_ACTIVE_LOCK = threading.Lock()


def connections(request: Request) -> ServiceConnections:
    member = get_identity_access(request).acting_user(request)
    return ServiceConnections(get_store(request), member.user_id)


@contextmanager
def transcription_slot(store: ServiceConnections):
    key = (str(store.store.path.resolve()), store.member_id)
    with _ACTIVE_LOCK:
        if _ACTIVE.get(key, 0) >= limits.TRANSCRIPTION_CONCURRENT_PER_MEMBER:
            raise ConnectionError("transcription_busy", 429)
        _ACTIVE[key] = _ACTIVE.get(key, 0) + 1
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE[key] -= 1
            if not _ACTIVE[key]:
                del _ACTIVE[key]


@router.get("")
def list_connections(request: Request):
    return connections(request).summary()


@router.post("")
async def connect(request: Request, body: ConnectRequest):
    store = connections(request)
    store.require_member()
    with transcription_slot(store):
        connection = await check_connection(body)
        key = body.key.get_secret_value()
        changed, saved = await checked_delegation(
            store, connection, key, "voice" in body.purposes, body.delegation_model
        )
        store.save(connection, key, changed, saved)
    return connection


async def checked_delegation(
    store: ServiceConnections, connection: dict, key: str, voice_added: bool, requested: str | None
) -> tuple[str | None, str]:
    """Check the thinking model where it runs; return (new value or None, value checked against).

    It is one member setting used by whichever connection runs voice: a new
    voice connection must reach the current value, and a new value is checked
    with the voice connection's key, or with this one when none runs voice.
    """
    saved = store.voice_settings()["delegation_model"]
    changed = requested if requested not in (None, saved) else None
    if voice_added:
        await check_delegation_model(connection, key, changed or saved)
    elif changed is not None:
        with suppress(ConnectionError):
            connection, key = store.voice_credentials()
        await check_delegation_model(connection, key, changed)
    return changed, saved


@router.post("/models")
async def models_for_key(request: Request, body: ServiceAddress):
    store = connections(request)
    store.require_member()
    with transcription_slot(store):
        return await list_models(body.address(), body.key.get_secret_value())


@router.get("/{connection_id}/models")
async def models_for_connection(request: Request, connection_id: str):
    store = connections(request)
    with transcription_slot(store):
        connection, key = store.credentials(connection_id)
        return await list_models(connection, key)


@router.delete("/{connection_id}", status_code=204)
def disconnect(request: Request, connection_id: str):
    connections(request).disconnect(connection_id)
    return Response(status_code=204)


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dictation: str


@router.put("/selection")
def select(request: Request, body: SelectionRequest):
    return connections(request).select(body.dictation)


class ConnectionUpdate(PurposesRequest):
    model: ModelId | None = None
    delegation_model: ModelId | None = None


@router.put("/{connection_id}")
async def update_connection(request: Request, connection_id: str, body: ConnectionUpdate):
    """Change uses and models; RCP checks only what changed, with the stored key."""
    store = connections(request)
    with transcription_slot(store):
        previous, key = store.credentials(connection_id)
        current = {**previous, "purposes": body.purposes}
        added = [purpose for purpose in body.purposes if purpose not in previous["purposes"]]
        if "transcription" in body.purposes and body.model not in (None, previous["model"]):
            current["model"] = body.model
            added = list(dict.fromkeys([*added, "transcription"]))
        checked = await check_purposes(current, key, added)
        changed, saved = await checked_delegation(
            store, current, key, "voice" in added, body.delegation_model
        )
        return store.update_connection(previous, checked, changed, saved)


async def read_audio(request: Request, formats: list[str]) -> tuple[bytes, str]:
    mime = request.headers.get("content-type", "")
    if mime not in FORMATS or mime not in formats:
        raise ConnectionError("audio_type_unsupported", 415)
    raw_length = request.headers.get("content-length", "")
    if not raw_length.isascii() or not raw_length.isdecimal():
        raise ConnectionError("audio_too_large", 413)
    try:
        length = int(raw_length)
    except ValueError:
        raise ConnectionError("audio_too_large", 413) from None
    if length > limits.TRANSCRIPTION_AUDIO_MAX_BYTES:
        raise ConnectionError("audio_too_large", 413)
    data = bytearray()
    try:
        async with asyncio.timeout(limits.TRANSCRIPTION_UPLOAD_SECONDS):
            async for chunk in request.stream():
                if len(data) + len(chunk) > min(length, limits.TRANSCRIPTION_AUDIO_MAX_BYTES):
                    raise ConnectionError("audio_too_large", 413)
                data.extend(chunk)
    except TimeoutError:
        raise ConnectionError("audio_too_large", 413) from None
    if len(data) != length:
        raise ConnectionError("audio_too_large", 413)
    return bytes(data), mime


@router.post("/{connection_id}/transcribe")
async def transcribe_audio(request: Request, connection_id: str):
    store = connections(request)
    with transcription_slot(store):
        connection, key = store.credentials(connection_id)
        if "transcription" not in connection["purposes"]:
            raise ConnectionError("transcription_not_enabled", 409)
        audio, mime = await read_audio(request, connection.get("formats", []))
        # Recheck after a potentially slow upload, including Disconnect and removal.
        connection, key = store.credentials(connection_id)
        if "transcription" not in connection["purposes"]:
            raise ConnectionError("transcription_not_enabled", 409)
        text = await transcribe(connection, key, audio, mime)
        store.credentials(connection_id)
        return {"text": text}
