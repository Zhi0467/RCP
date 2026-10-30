"""Refresh a stale pending Proposal or outcome edge through a light agent on main.

The light agent is an ordinary node Work chat. It runs the node-chat provider
and model at the lowest effort that model's probed catalog lists; with no
cached catalog it keeps the configured effort. Project Settings keep deciding
which skills are enabled; a refresh skill the project disabled is left out.
The request key is per Proposal, or per edge and Evidence revision, so a
repeated click or a second member reuses the first chat instead of starting
another, even after the agent moved the Proposal or edge on.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from rcp.api.dependencies import (
    get_graph_service,
    require_project_membership,
    require_project_write_admission,
)
from rcp.api.tasks import (
    AttachmentStoreDependency,
    BackgroundTasksDependency,
    CatalogDependency,
    IdentityDependency,
    StoreDependency,
    start_agent_task,
)
from rcp.core.models import GraphState
from rcp.core.validation.outcome_edges import edge_is_stale
from rcp.core.validation.proposals import proposal_refresh_refusal, proposal_subject_node
from rcp.keyed_locks import KeyedLocks
from rcp.runs.proposal_refresh import (
    EDGE_REFRESH_SKILL_IDS,
    edge_refresh_key,
    edge_refresh_message,
    proposal_refresh_key,
    proposal_refresh_message,
    proposal_refresh_skill_ids,
)
from rcp.service import ProjectService
from rcp.storage.models import MACHINE_LOGIN_ID, LoginKey

router = APIRouter(dependencies=[Depends(require_project_membership)])

# One process owns the database (invariant 8), so an in-process lock serializes
# every member's lookup and admission for one refresh key.
_LOCKS = KeyedLocks()

_REFUSALS = {
    "proposal_not_pending": "The Proposal is not pending on main.",
    "proposal_not_stale": "The Proposal's dependencies have not moved.",
    "proposal_not_refileable": "Ordinary Work cannot file this Proposal's form.",
    "proposal_target_unavailable": "Every node the Proposal names is gone.",
    "edge_not_stale": "The relation's Evidence has not moved since it was judged.",
}

# The node the chat opens on, its message, and the skills it asks for.
LightChat = tuple[str | None, str, list[str]]


def _refused(code: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": _REFUSALS[code]})


# ``{proposal_id:path}`` because Proposal ids contain slashes.
@router.post(
    "/api/projects/{project_id}/proposals/{proposal_id:path}/refresh",
    dependencies=[Depends(require_project_write_admission)],
)
def refresh_proposal(
    project_id: str,
    proposal_id: str,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
    background_tasks: BackgroundTasksDependency,
) -> dict[str, str]:
    """Return the Proposal's refresh chat, starting it only when the card offers Refresh."""

    def chat(state: GraphState) -> LightChat:
        proposal = state.proposals.get(proposal_id)
        refusal = proposal_refresh_refusal(state, proposal)
        if refusal is not None:
            raise _refused(refusal)
        assert proposal is not None
        return (
            proposal_subject_node(state, proposal),
            proposal_refresh_message(state, proposal),
            proposal_refresh_skill_ids(proposal),
        )

    return _light_refresh(
        project_id,
        request,
        lambda _state: proposal_refresh_key(proposal_id),
        chat,
        store=store,
        catalog=catalog,
        identity_access=identity_access,
        attachment_store=attachment_store,
        background_tasks=background_tasks,
    )


# ``{edge_id:path}`` because edge ids contain node ids, which contain slashes.
@router.post(
    "/api/projects/{project_id}/edges/{edge_id:path}/refresh",
    dependencies=[Depends(require_project_write_admission)],
)
def refresh_edge(
    project_id: str,
    edge_id: str,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
    background_tasks: BackgroundTasksDependency,
) -> dict[str, str]:
    """Return the stale edge's re-judging chat, one per Evidence revision."""

    def key(state: GraphState) -> uuid.UUID:
        edge = state.edges.get(edge_id)
        if edge is None or edge.source not in state.nodes:
            raise _refused("edge_not_stale")
        return edge_refresh_key(edge_id, state.nodes[edge.source].updated_rev)

    def chat(state: GraphState) -> LightChat:
        edge = state.edges.get(edge_id)
        if edge is None or not edge_is_stale(state, edge):
            raise _refused("edge_not_stale")
        return edge.source, edge_refresh_message(state, edge), list(EDGE_REFRESH_SKILL_IDS)

    return _light_refresh(
        project_id,
        request,
        key,
        chat,
        store=store,
        catalog=catalog,
        identity_access=identity_access,
        attachment_store=attachment_store,
        background_tasks=background_tasks,
    )


def _light_refresh(
    project_id: str,
    request: Request,
    request_key: Callable[[GraphState], uuid.UUID],
    chat: Callable[[GraphState], LightChat],
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
    background_tasks: BackgroundTasksDependency,
) -> dict[str, str]:
    identity_access.require_patch_capable_identity(request)
    project_id = catalog.resolve_project_id(project_id)
    service = get_graph_service(catalog, project_id, initialize=False, write=True)
    key = request_key(service.history.state())
    with _LOCKS(f"{store.path.resolve()}:{project_id}:{key}"):
        existing = store.client_request(project_id, str(key))
        if existing is not None:
            # A retry after the agent moved the subject on returns that chat.
            operation_id = existing.operation_id or ""
        else:
            node_id, message, skills = chat(service.history.state())
            enabled = set(service.manifest.agent.skill_defaults.skill_ids)
            body: dict[str, object] = {
                "node_id": node_id,
                "chat_id": str(uuid.uuid4()),
                "mode": "work",
                "message": message,
                "invoked_skill_ids": [skill for skill in skills if skill in enabled],
            }
            effort = _lowest_effort(service)
            if effort is not None:
                body["reasoning"] = effort
            response = start_agent_task(
                project_id,
                "node_chat",
                body,
                request,
                catalog=catalog,
                store=store,
                identity_access=identity_access,
                attachment_store=attachment_store,
                background_tasks=background_tasks,
                idempotency_key=key,
            )
            operation_id = str(response["operation_id"])
    record = store.agent_task(operation_id)
    if record is None:
        raise RuntimeError(f"Refresh task {operation_id} is missing.")
    return {"operation_id": operation_id, "chat_id": str(record.request["chat_id"])}


def _lowest_effort(service: ProjectService) -> str | None:
    """The first effort the node-chat model's probed catalog lists, if one is cached."""
    profile = service.resolve_agent_profile("node_chat")
    machine = service.manifest.machine_map[profile.run_on]
    readiness = service.launcher.cached_readiness(
        LoginKey(profile.provider, machine.host, MACHINE_LOGIN_ID),
        binary=service.launcher.provider_binary(profile.provider, machine.host),
    )
    efforts = next(
        (
            model.reasoning
            for model in (readiness.models if readiness else [])
            if model.id == profile.model
        ),
        [],
    )
    return efforts[0] if efforts else None
