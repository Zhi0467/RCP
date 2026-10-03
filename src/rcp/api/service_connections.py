"""Authenticated member service settings and raw, memory-only audio uploads."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from rcp import limits
from rcp.api.dependencies import get_identity_access, get_store
from rcp.service_connections import ConnectionError, ServiceConnections
from rcp.transcription import FORMATS, ConnectRequest, check_connection, transcribe


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
        store.save(connection, body.key.get_secret_value())
    return connection


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
        audio, mime = await read_audio(request, connection["formats"])
        # Recheck after a potentially slow upload, including Disconnect and removal.
        connection, key = store.credentials(connection_id)
        text = await transcribe(connection, key, audio, mime)
        store.credentials(connection_id)
        return {"text": text}
