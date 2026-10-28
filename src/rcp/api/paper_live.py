"""Bounded Yjs transport; admission spans validation and the durable commit."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Any, TypeVar

import anyio
from anyio.to_thread import run_sync
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pycrdt import Decoder, Encoder, create_awareness_message, create_sync_message

from rcp.api.dependencies import _api_services, project_write_admission, require_project_membership
from rcp.api.identity import mutation_origin_matches
from rcp.limits import (
    PAPER_ADMISSION_RECHECK_SECONDS,
    PAPER_AWARENESS_MAX_CLIENTS,
    PAPER_CONNECTION_UPDATES_PER_SECOND,
    PAPER_MAX_AWARENESS_BYTES,
    PAPER_MAX_MESSAGE_BYTES,
    PAPER_SOCKET_QUEUE_SIZE,
)
from rcp.paper import PaperLive
from rcp.paper.models import PaperLimit, PaperRateLimit
from rcp.server_ops.maintenance import MaintenanceAdmissionClosed

router = APIRouter()
_Result = TypeVar("_Result")


def get_paper_live(request: Request) -> PaperLive:
    return request.app.state.paper_live


def _socket_request(socket: WebSocket) -> Request:
    return Request(
        {
            **socket.scope,
            "type": "http",
            "method": "GET",
            "scheme": "https" if socket.url.scheme == "wss" else "http",
            "state": {},
        }
    )


def _admit_socket(socket: WebSocket, project_id: str) -> Request:
    request = _socket_request(socket)
    origin = request.headers.get("origin")
    if origin is None or not mutation_origin_matches(request, origin):
        raise HTTPException(403, "Paper origin does not match this server.")
    require_project_membership(project_id, request)
    request.app.state.runtime_admission_gate.require_open("Paper live connection")
    return request


async def paper_mutation(
    request: Request, project_id: str, action: Callable[[], Coroutine[Any, Any, _Result]]
) -> _Result:
    """Keep blocking admission off the room loop, including its release."""
    loop = asyncio.get_running_loop()

    def admitted() -> _Result:
        with (
            request.app.state.runtime_admission_gate.mutation("Paper edit"),
            project_write_admission(project_id, request),
        ):
            require_project_membership(project_id, request)
            return asyncio.run_coroutine_threadsafe(action(), loop).result()

    try:
        return await run_sync(admitted)
    except KeyError as exc:
        raise HTTPException(404, "Paper file or version not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except MaintenanceAdmissionClosed as exc:
        raise HTTPException(503, str(exc)) from exc


def _awareness_states(
    message: bytes, current: dict[int, tuple[int, str]]
) -> dict[int, tuple[int, str]]:
    if len(message) > PAPER_MAX_AWARENESS_BYTES:
        raise OverflowError("Paper awareness exceeds its limit")
    frame = Decoder(message[1:])
    body = frame.read_message()
    if body is None or frame.length != 0:
        raise ValueError("Malformed Paper awareness frame")
    decoder = Decoder(body)
    count = decoder.read_var_uint()
    if count > PAPER_AWARENESS_MAX_CLIENTS:
        raise OverflowError("Paper awareness client limit exceeded")
    clocks = dict(current)
    for _ in range(count):
        client_id, clock = decoder.read_var_uint(), decoder.read_var_uint()
        encoded_state = decoder.read_var_string()
        state = json.loads(encoded_state)
        if state is not None and not isinstance(state, dict):
            raise ValueError("Malformed Paper awareness state")
        if clock >= clocks.get(client_id, (0, "null"))[0]:
            clocks[client_id] = (clock, json.dumps(state, ensure_ascii=True, separators=(",", ":")))
    if decoder.length != 0:
        raise ValueError("Malformed Paper awareness payload")
    if len(clocks) > PAPER_AWARENESS_MAX_CLIENTS:
        raise OverflowError("Paper awareness client limit exceeded")
    return clocks


def _awareness_message(clocks: dict[int, tuple[int, str]]) -> bytes:
    encoder = Encoder()
    encoder.write_var_uint(len(clocks))
    for client_id, (clock, state) in clocks.items():
        encoder.write_var_uint(client_id)
        encoder.write_var_uint(clock)
        encoder.write_var_string(state)
    return create_awareness_message(encoder.to_bytes())


@router.websocket("/api/projects/{project_id}/paper/files/{file_id}/live")
async def paper_socket(socket: WebSocket, project_id: str, file_id: str) -> None:
    live: PaperLive = socket.app.state.paper_live
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=PAPER_SOCKET_QUEUE_SIZE)
    try:
        request = _admit_socket(socket, project_id)
        project_id = require_project_membership(project_id, request)
        room = await paper_mutation(
            request, project_id, lambda: live.connect(project_id, file_id, queue)
        )
    except (HTTPException, MaintenanceAdmissionClosed, KeyError, ValueError):
        await socket.close(code=4403)
        return
    try:
        await socket.accept()
    except BaseException:
        room.subscribers.discard(queue)
        raise
    queue.put_nowait(create_sync_message(room.doc))
    if room.awareness:
        queue.put_nowait(_awareness_message(room.awareness))
    awareness: dict[int, tuple[int, str]] = {}

    async def receive() -> None:
        nonlocal awareness
        count = 0
        window = asyncio.get_running_loop().time()
        while True:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            payload = message.get("bytes")
            if payload is None:
                raise ValueError("Paper live messages must be binary")
            if len(payload) > PAPER_MAX_MESSAGE_BYTES:
                raise OverflowError("Paper message exceeds its limit")
            now = asyncio.get_running_loop().time()
            if now - window >= 1:
                count, window = 0, now
            count += 1
            if count > PAPER_CONNECTION_UPDATES_PER_SECOND:
                raise HTTPException(429, "Paper connection update rate exceeded")
            pending_awareness = (
                _awareness_states(payload, awareness) if payload[:1] == b"\x01" else awareness
            )
            fresh = _admit_socket(socket, project_id)
            actor = _api_services(fresh).identity_access.acting_user(fresh).user_id

            async def accept(message=payload, owner=actor):
                states = (
                    _awareness_states(message, room.awareness) if message[:1] == b"\x01" else None
                )
                if (
                    states is not None
                    and len(_awareness_message(states)) > PAPER_MAX_AWARENESS_BYTES
                ):
                    raise OverflowError("Paper room awareness exceeds its limit")
                replies = await live.receive(project_id, file_id, message, owner)
                if states is not None:
                    room.awareness = states
                return replies

            replies = await paper_mutation(fresh, project_id, accept)
            awareness = pending_awareness
            for reply in replies:
                queue.put_nowait(reply)

    async def send() -> None:
        while True:
            payload = await queue.get()
            _admit_socket(socket, project_id)
            # A deleted file must close even while another member still has its room.
            if not live.file_exists(project_id, file_id):
                raise HTTPException(403, "Paper file was deleted")
            if not payload:
                raise asyncio.QueueFull("Paper client fell behind")
            await socket.send_bytes(payload)

    async def watch_admission() -> None:
        while True:
            await asyncio.sleep(PAPER_ADMISSION_RECHECK_SECONDS)
            _admit_socket(socket, project_id)
            if not live.file_exists(project_id, file_id):
                raise HTTPException(403, "Paper file was deleted")

    tasks = [asyncio.create_task(action()) for action in (receive, send, watch_admission)]
    close_code = 1000
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except HTTPException as exc:
        if isinstance(exc.__cause__, PaperLimit):
            # The shared mutation adapter preserves the live owner's budget error.
            close_code = 4529 if isinstance(exc.__cause__, PaperRateLimit) else 4413
        else:
            close_code = {409: 4400, 429: 4529, 503: 4503}.get(exc.status_code, 4403)
    except MaintenanceAdmissionClosed:
        close_code = 4503
    except KeyError:
        close_code = 4403
    except asyncio.QueueFull:
        close_code = 1013
    except OverflowError:
        close_code = 4413
    except (ValueError, RuntimeError, IndexError):
        close_code = 4400
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        room.subscribers.discard(queue)
        if awareness:
            removed = {
                client_id: (state[0], "null")
                for client_id, state in awareness.items()
                if room.awareness.get(client_id) == state
            }
            for client_id in removed:
                room.awareness.pop(client_id, None)
            disconnected = _awareness_message(removed)
            for subscriber in tuple(room.subscribers):
                with suppress(asyncio.QueueFull):
                    subscriber.put_nowait(disconnected)
        for task in tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close(code=close_code)
