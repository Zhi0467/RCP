"""Light agents: quick, human-started Work chats that recheck one thing.

A light agent runs the node-chat provider and model at that provider's lowest
effort. Project Settings keep deciding which skills are enabled; the caller
names in code the few to invoke, and one the project disabled is left out. Its
request key is the caller's, so a repeated click or a second member reuses the
first chat instead of starting another.
"""

from __future__ import annotations

import uuid

from fastapi import Request

from rcp.api.dependencies import get_graph_service
from rcp.api.tasks import (
    AttachmentStoreDependency,
    BackgroundTasksDependency,
    CatalogDependency,
    IdentityDependency,
    StoreDependency,
    start_agent_task,
)
from rcp.providers import profile_for


def start_light_node_chat(
    project_id: str,
    *,
    node_id: str,
    message: str,
    invoked_skill_ids: list[str],
    request_key: uuid.UUID,
    http_request: Request,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
    background_tasks: BackgroundTasksDependency,
) -> dict[str, str]:
    """Start (or return) the main-graph Work chat for ``request_key``."""

    existing = store.client_request(project_id, str(request_key))
    if existing is None:
        service = get_graph_service(catalog, project_id, initialize=False)
        provider = service.resolve_agent_profile("node_chat").provider
        enabled = set(service.manifest.agent.skill_defaults.skill_ids)
        body: dict[str, object] = {
            "node_id": node_id,
            "chat_id": str(uuid.uuid4()),
            "mode": "work",
            "message": message,
            "invoked_skill_ids": [skill for skill in invoked_skill_ids if skill in enabled],
        }
        lowest = profile_for(provider).lowest_reasoning
        if lowest is not None:
            body["reasoning"] = lowest
        response = start_agent_task(
            project_id,
            "node_chat",
            body,
            http_request,
            catalog=catalog,
            store=store,
            identity_access=identity_access,
            attachment_store=attachment_store,
            background_tasks=background_tasks,
            idempotency_key=request_key,
        )
        operation_id = str(response["operation_id"])
    else:
        operation_id = existing.operation_id or ""
    record = store.agent_task(operation_id)
    if record is None:
        raise RuntimeError(f"Light agent task {operation_id} is missing.")
    return {"operation_id": operation_id, "chat_id": str(record.request["chat_id"])}
