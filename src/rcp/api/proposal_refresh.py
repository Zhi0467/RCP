"""Refresh a stale pending Proposal through a light agent on main.

The light agent is an ordinary node Work chat. It runs the node-chat provider
and model at the lowest effort that model's probed catalog lists; with no
cached catalog it keeps the configured effort. Project Settings keep deciding
which skills are enabled; a refresh skill the project disabled is left out.
The request key is per Proposal, so a repeated click or a second member reuses
the first chat instead of starting another, even after the Proposal moved on.
"""

from __future__ import annotations

import uuid

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
from rcp.core.validation.proposals import proposal_refresh_refusal, proposal_subject_node
from rcp.keyed_locks import KeyedLocks
from rcp.runs.proposal_refresh import (
    proposal_refresh_key,
    proposal_refresh_message,
    proposal_refresh_skill_ids,
)
from rcp.storage.models import MACHINE_LOGIN_ID, LoginKey

router = APIRouter(dependencies=[Depends(require_project_membership)])

# One process owns the database (invariant 8), so an in-process lock serializes
# every member's lookup and admission for one Proposal.
_LOCKS = KeyedLocks()

_REFUSALS = {
    "proposal_not_pending": "The Proposal is not pending on main.",
    "proposal_not_stale": "The Proposal's dependencies have not moved.",
    "proposal_not_refileable": "Ordinary Work cannot file this Proposal's form.",
    "proposal_target_unavailable": "Every node the Proposal names is gone.",
}


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

    identity_access.require_patch_capable_identity(request)
    project_id = catalog.resolve_project_id(project_id)
    service = get_graph_service(catalog, project_id, initialize=False, write=True)
    request_key = proposal_refresh_key(proposal_id)
    with _LOCKS(f"{store.path.resolve()}:{project_id}:{request_key}"):
        existing = store.client_request(project_id, str(request_key))
        if existing is not None:
            # A retry after the agent withdrew the Proposal returns that chat.
            operation_id = existing.operation_id or ""
        else:
            state = service.history.state()
            proposal = state.proposals.get(proposal_id)
            refusal = proposal_refresh_refusal(state, proposal)
            if refusal is not None:
                raise HTTPException(
                    status_code=409, detail={"code": refusal, "message": _REFUSALS[refusal]}
                )
            assert proposal is not None
            profile = service.resolve_agent_profile("node_chat")
            enabled = set(service.manifest.agent.skill_defaults.skill_ids)
            body: dict[str, object] = {
                "node_id": proposal_subject_node(state, proposal),
                "chat_id": str(uuid.uuid4()),
                "mode": "work",
                "message": proposal_refresh_message(state, proposal),
                "invoked_skill_ids": [
                    skill for skill in proposal_refresh_skill_ids(proposal) if skill in enabled
                ],
            }
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
            if efforts:
                body["reasoning"] = efforts[0]
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
                idempotency_key=request_key,
            )
            operation_id = str(response["operation_id"])
    record = store.agent_task(operation_id)
    if record is None:
        raise RuntimeError(f"Proposal refresh task {operation_id} is missing.")
    return {"operation_id": operation_id, "chat_id": str(record.request["chat_id"])}
