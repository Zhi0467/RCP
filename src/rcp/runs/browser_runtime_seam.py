"""Launch/runtime integration seam. The integrator replaces only these stubs."""

from __future__ import annotations

from rcp.agents.browser_grant import BrowserGrant, BrowserOwnerKey, BrowserTurnStatus
from rcp.transport import RemoteRunStage


def acquire_browser_grant(
    owner: BrowserOwnerKey, *, execution: RemoteRunStage | None, workspace_dir: str
) -> BrowserGrant:
    return BrowserGrant(
        requested=True,
        status="unavailable",
        reason_code="runtime_not_wired",
        owner=owner,
    )


def finish_browser_grant(grant: BrowserGrant) -> BrowserTurnStatus:
    return BrowserTurnStatus(
        status=grant.status, reason_code=grant.reason_code, detail=grant.detail
    )


def close_browser_owner(
    owner: BrowserOwnerKey, *, execution: RemoteRunStage | None, delete_profile: bool
) -> None:
    pass
