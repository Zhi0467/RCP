"""Launch/runtime integration seam: grants resolve through the host browser runtime."""

from __future__ import annotations

from pathlib import Path

from rcp.browser import Unavailable, close_owner, ensure_session, release_session
from rcp.core.models import HiddenReadScope
from rcp.providers.browser_grant import BrowserGrant, BrowserOwnerKey, BrowserTurnStatus
from rcp.transport import RemoteRunStage


def acquire_browser_grant(
    owner: BrowserOwnerKey,
    *,
    execution: RemoteRunStage | None,
    workspace_dir: str,
    data_dir: Path,
    retained_lease_ids: list[str],
    hidden_read_scope: HiddenReadScope | None = None,
) -> BrowserGrant:
    lease = ensure_session(
        owner.token(),
        execution=execution,
        workspace_dir=workspace_dir,
        data_dir=data_dir,
        retained_lease_ids=retained_lease_ids,
        hidden_read_scope=hidden_read_scope,
    )
    if isinstance(lease, Unavailable):
        return BrowserGrant(
            requested=True,
            status="unavailable",
            reason_code=lease.reason_code,
            detail=lease.detail,
            owner=owner,
        )
    return BrowserGrant(
        requested=True,
        status="granted",
        hidden_read_scope=hidden_read_scope,
        hidden_read_enforcement=lease.hidden_read_enforcement,
        owner=owner,
        session_name=lease.session_name,
        invocation_dir=lease.invocation_dir,
        path_prefix=lease.path_prefix,
        env=dict(lease.env),
        lease_id=lease.lease_id,
    )


def finish_browser_grant(
    grant: BrowserGrant, *, execution: RemoteRunStage | None, data_dir: Path
) -> BrowserTurnStatus:
    if grant.status != "granted":
        return BrowserTurnStatus(
            status=grant.status, reason_code=grant.reason_code, detail=grant.detail
        )
    if grant.owner is None or grant.lease_id is None:
        return BrowserTurnStatus(status="lost", reason_code="lease_unknown")
    check = release_session(
        grant.owner.token(), lease_id=grant.lease_id, execution=execution, data_dir=data_dir
    )
    if check.alive:
        return BrowserTurnStatus(status="granted")
    return BrowserTurnStatus(
        status="lost", reason_code=check.reason_code or "lost", detail=check.detail
    )


def close_browser_owner(
    owner: BrowserOwnerKey,
    *,
    execution: RemoteRunStage | None,
    delete_profile: bool,
    data_dir: Path,
    retained_lease_ids: list[str],
) -> None:
    close_owner(
        owner.token(),
        execution=execution,
        delete_profile=delete_profile,
        data_dir=data_dir,
        retained_lease_ids=retained_lease_ids,
    )
