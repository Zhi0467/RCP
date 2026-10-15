"""Light agents: quick, human-started Work chats that recheck one thing.

A light agent runs the node-chat provider and model at that provider's lowest
effort. Project Settings keep deciding which skills are enabled; the caller
names in code the few to invoke, and one the project disabled is left out. Its
request key is the caller's, so a repeated click or a second member reuses the
first chat instead of starting another, even after its subject moved on.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import NamedTuple

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
from rcp.keyed_locks import KeyedLocks
from rcp.providers import profile_for

# One process owns the database (invariant 8), so an in-process lock serializes
# every member's lookup and admission for one key.
_LOCKS = KeyedLocks()


class LightChat(NamedTuple):
    node_id: str
    message: str
    invoked_skill_ids: list[str]


def start_light_node_chat(
    project_id: str,
    *,
    request_key: uuid.UUID,
    prepare: Callable[[], LightChat],
    http_request: Request,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
    background_tasks: BackgroundTasksDependency,
) -> dict[str, str]:
    """Return the chat already started for ``request_key``, or ``prepare`` and start one.

    ``prepare`` runs only when no chat exists yet, so its refusals guard new
    launches and never hide a chat an earlier request started.
    """

    with _LOCKS(f"{store.path.resolve()}:{project_id}:{request_key}"):
        existing = store.client_request(project_id, str(request_key))
        if existing is not None:
            operation_id = existing.operation_id or ""
        else:
            chat = prepare()
            service = get_graph_service(catalog, project_id, initialize=False)
            provider = service.resolve_agent_profile("node_chat").provider
            enabled = set(service.manifest.agent.skill_defaults.skill_ids)
            body: dict[str, object] = {
                "node_id": chat.node_id,
                "chat_id": str(uuid.uuid4()),
                "mode": "work",
                "message": chat.message,
                "invoked_skill_ids": [
                    skill for skill in chat.invoked_skill_ids if skill in enabled
                ],
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
    record = store.agent_task(operation_id)
    if record is None:
        raise RuntimeError(f"Light agent task {operation_id} is missing.")
    return {"operation_id": operation_id, "chat_id": str(record.request["chat_id"])}
