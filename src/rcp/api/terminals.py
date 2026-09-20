"""Membership-gated transport for service-account member terminals."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import PurePosixPath
from typing import Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rcp.api.dependencies import (
    _api_services,
    get_project_service,
    require_project_membership,
    require_project_write_admission,
)
from rcp.api.identity import mutation_origin_matches
from rcp.api.terminal_projection import running_repository_work, terminal_session_payload
from rcp.limits import (
    TERMINAL_IO_CHUNK_BYTES,
    TERMINAL_MAX_DIMENSION,
    TERMINAL_SWEEP_INTERVAL_SECONDS,
)
from rcp.server_ops.layout import project_deploy_key_relative_path
from rcp.terminals.backends import machine_capability
from rcp.terminals.git_access import terminal_git_access

router = APIRouter()
_http_membership = [Depends(require_project_membership)]


class OpenTerminalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository_id: str = Field(min_length=1)


class TerminalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["input"]
    data: str = Field(max_length=TERMINAL_IO_CHUNK_BYTES)


class TerminalResize(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["resize"]
    cols: int = Field(ge=1, le=TERMINAL_MAX_DIMENSION)
    rows: int = Field(ge=1, le=TERMINAL_MAX_DIMENSION)


@router.get("/api/projects/{project_id}/terminals/repositories", dependencies=_http_membership)
async def repositories(project_id: str, request: Request) -> list[dict[str, object]]:
    services = _api_services(request)
    manifest = get_project_service(services.catalog, project_id).manifest
    work = running_repository_work(services.store, project_id, manifest)
    capabilities = {}
    for machine in manifest.machines:
        probe = services.terminals.probes.get(machine) if machine.host else None
        capabilities[machine.alias] = await asyncio.to_thread(machine_capability, machine, probe)
    result = []
    for repository in manifest.repositories:
        capability = capabilities[repository.machine]
        backend = capability.backend
        result.append(
            {
                "repository_id": repository.alias,
                "machine_id": repository.machine,
                "path": repository.path,
                "eligible": backend is not None,
                "probe_state": capability.probe_state
                if manifest.machine_map[repository.machine].host
                else None,
                "os_name": capability.os_name,
                "backend_id": backend.id if backend else None,
                "backend_name": backend.display_name if backend else None,
                "containment": capability.containment,
                "reason": capability.reason,
                "unavailable_reason": capability.reason if backend is None else None,
                "running_work": work[repository.alias],
            }
        )
    return result


@router.post("/api/projects/{project_id}/terminals/probe", dependencies=_http_membership)
async def refresh_probes(project_id: str, request: Request) -> list[dict[str, object]]:
    services = _api_services(request)
    manifest = get_project_service(services.catalog, project_id).manifest
    for machine in manifest.machines:
        if machine.host:
            services.terminals.probes.invalidate(machine)
    return await repositories(project_id, request)


@router.get("/api/projects/{project_id}/terminals", dependencies=_http_membership)
def sessions(project_id: str, request: Request) -> list[dict[str, object]]:
    services = _api_services(request)
    manifest = get_project_service(services.catalog, project_id).manifest
    work = running_repository_work(services.store, project_id, manifest)
    return [
        terminal_session_payload(session, work) for session in services.terminals.list(project_id)
    ]


@router.post(
    "/api/projects/{project_id}/terminals",
    dependencies=[
        *_http_membership,
        Depends(require_project_write_admission),
    ],
)
async def open_session(
    project_id: str, body: OpenTerminalRequest, request: Request
) -> dict[str, object]:
    services = _api_services(request)
    manifest = get_project_service(services.catalog, project_id).manifest
    member = services.identity_access.acting_user(request)
    if body.repository_id not in manifest.repository_map:
        raise HTTPException(404, "Repository not found")
    repository = manifest.repository_map[body.repository_id]
    machine = manifest.machine_map[repository.machine]
    probe = await services.terminals.probes.ensure(machine) if machine.host else None
    capability = await asyncio.to_thread(machine_capability, machine, probe)
    if capability.backend is None:
        raise HTTPException(409, capability.reason)
    try:
        try:
            inventory = await asyncio.to_thread(services.catalog.repository_ownership_inventory)
        except (OSError, ValueError) as exc:
            raise HTTPException(
                503, "Cannot establish repository ownership; a registered project is unavailable."
            ) from exc
        key = (
            request.app.state.server_layout.project_deploy_key_path(project_id, body.repository_id)
            if services.store.space_kind == "team"
            else None
        )
        paths, environment = ((), {}) if machine.host else terminal_git_access(key)
        # A remote team checkout authenticates with its provisioned deploy key,
        # which lives under the far account's home. Only that side knows the
        # home, so send the path relative to it.
        remote_key_relative = (
            str(
                PurePosixPath(".local/share/rcp/credentials")
                / project_deploy_key_relative_path(project_id, body.repository_id)
            )
            if machine.host and services.store.space_kind == "team"
            else None
        )
        session = await services.terminals.open(
            project_id=project_id,
            member_id=member.user_id,
            manifest=manifest,
            repository_alias=body.repository_id,
            repository_inventory=inventory,
            git_read_paths=paths,
            git_environment=environment,
            remote_git_key_relative=remote_key_relative,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc
    return terminal_session_payload(
        session, running_repository_work(services.store, project_id, manifest)
    )


@router.delete("/api/projects/{project_id}/terminals/{session_id}", dependencies=_http_membership)
async def end_session(project_id: str, session_id: str, request: Request) -> dict[str, bool]:
    try:
        await _api_services(request).terminals.end(project_id, session_id)
    except KeyError as exc:
        raise HTTPException(404, "Terminal not found") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ended": True}


def _socket_request(websocket: WebSocket) -> Request:
    # IdentityAccess uses cookies, headers and state; HTTP middleware never runs
    # for an upgraded connection. Fresh state prevents caching a revoked member.
    return Request(
        {
            **websocket.scope,
            "type": "http",
            "method": "GET",
            "scheme": "https" if websocket.url.scheme == "wss" else "http",
            "state": {},
        }
    )


def _admit_socket(websocket: WebSocket, project_id: str) -> str:
    request = _socket_request(websocket)
    origin = request.headers.get("origin")
    if origin is None or not mutation_origin_matches(request, origin):
        raise HTTPException(403, "Terminal origin does not match this server.")
    canonical = require_project_membership(project_id, request)
    if request.app.state.runtime_admission_gate.closed:
        raise HTTPException(503, "Server maintenance is in progress.")
    return canonical


@router.websocket("/api/projects/{project_id}/terminals/{session_id}/ws")
async def terminal_socket(websocket: WebSocket, project_id: str, session_id: str) -> None:
    manager = websocket.app.state.services.terminals
    try:
        project_id = _admit_socket(websocket, project_id)
        session = manager.get(project_id, session_id)
    except HTTPException as exc:
        await websocket.close(code=4400 + exc.status_code % 100)
        return
    except KeyError:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    try:
        queue = manager.attach(project_id, session_id)
    except KeyError:
        await websocket.close(code=4404)
        return

    async def receive_input() -> None:
        while True:
            payload = await websocket.receive_json()
            _admit_socket(websocket, project_id)
            if isinstance(payload, dict) and payload.get("type") == "input":
                message = TerminalInput.model_validate(payload)
                await manager.write(project_id, session_id, message.data.encode("utf-8"))
            else:
                dimensions = TerminalResize.model_validate(payload)
                manager.resize(project_id, session_id, dimensions.cols, dimensions.rows)

    async def send_output() -> None:
        while True:
            data = await queue.get()
            _admit_socket(websocket, project_id)
            if data is None:
                await websocket.send_json(
                    {
                        "type": "ended",
                        "reason": (
                            "SSH link dropped; terminal session ended."
                            if session.termination_reason == "link_dropped"
                            else "Terminal session ended."
                        ),
                    }
                )
                return
            await websocket.send_bytes(data)

    async def watch_admission() -> None:
        while True:
            await asyncio.sleep(TERMINAL_SWEEP_INTERVAL_SECONDS)
            _admit_socket(websocket, project_id)

    tasks = [
        asyncio.create_task(action()) for action in (receive_input, send_output, watch_admission)
    ]
    close_code = 1000
    try:
        completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            task.result()
    except HTTPException as exc:
        close_code = 4400 + exc.status_code % 100
    except (ValueError, ValidationError, KeyError, OSError):
        close_code = 1008
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        manager.detach(project_id, session_id, queue)
        for task in tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=close_code)
