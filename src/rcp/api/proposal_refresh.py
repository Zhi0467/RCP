"""Refresh a stale pending Proposal through a light agent on main."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from rcp.api.dependencies import (
    get_graph_service,
    require_project_membership,
    require_project_write_admission,
)
from rcp.api.light_agents import LightChat, start_light_node_chat
from rcp.api.tasks import (
    AttachmentStoreDependency,
    BackgroundTasksDependency,
    CatalogDependency,
    IdentityDependency,
    StoreDependency,
)
from rcp.core.validation.proposals import proposal_is_stale
from rcp.runs.proposal_refresh import (
    proposal_refresh_key,
    proposal_refresh_message,
    proposal_refresh_node,
    proposal_refresh_skill_ids,
)

router = APIRouter(dependencies=[Depends(require_project_membership)])


def _refused(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


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
    """Offered only on a stale card, and refused unless the Sync gate would withdraw it."""

    identity_access.require_patch_capable_identity(request)
    project_id = catalog.resolve_project_id(project_id)
    state = get_graph_service(catalog, project_id, initialize=False).history.state()
    proposal = state.proposals.get(proposal_id)
    if proposal is None:
        raise _refused("proposal_not_pending", "The Proposal is not pending on main.")

    def prepare() -> LightChat:
        # Reached only before the first chat exists; a retry after the agent
        # withdrew the Proposal returns that chat instead.
        if proposal.status != "pending":
            raise _refused("proposal_not_pending", "The Proposal is not pending on main.")
        if not proposal_is_stale(state, proposal):
            raise _refused("proposal_not_stale", "The Proposal's dependencies have not moved.")
        node_id = proposal_refresh_node(state, proposal)
        if node_id is None:
            raise _refused("proposal_target_unavailable", "Every node the Proposal names is gone.")
        return LightChat(
            node_id=node_id,
            message=proposal_refresh_message(state, proposal),
            invoked_skill_ids=proposal_refresh_skill_ids(proposal),
        )

    return start_light_node_chat(
        project_id,
        request_key=proposal_refresh_key(project_id, proposal),
        prepare=prepare,
        http_request=request,
        catalog=catalog,
        store=store,
        identity_access=identity_access,
        attachment_store=attachment_store,
        background_tasks=background_tasks,
    )
