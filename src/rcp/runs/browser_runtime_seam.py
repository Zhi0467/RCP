"""Launch/runtime integration seam: grants resolve through the host browser runtime."""

from __future__ import annotations

import threading
from pathlib import Path

from rcp.agents.browser_grant import BrowserGrant, BrowserOwnerKey, BrowserTurnStatus
from rcp.browser import SessionLease, Unavailable, close_owner, ensure_session, release_session
from rcp.transport import RemoteRunStage

# Active leases by id. A controller restart forgets them; the runtime clears that
# controller's old leases when it next starts.
_LEASES: dict[str, SessionLease] = {}
_LEASES_LOCK = threading.Lock()


def acquire_browser_grant(
    owner: BrowserOwnerKey, *, execution: RemoteRunStage | None, workspace_dir: str, data_dir: Path
) -> BrowserGrant:
    lease = ensure_session(
        owner.token(), execution=execution, workspace_dir=workspace_dir, data_dir=data_dir
    )
    if isinstance(lease, Unavailable):
        return BrowserGrant(
            requested=True,
            status="unavailable",
            reason_code=lease.reason_code,
            detail=lease.detail,
            owner=owner,
        )
    with _LEASES_LOCK:
        _LEASES[lease.lease_id] = lease
    return BrowserGrant(
        requested=True,
        status="granted",
        owner=owner,
        session_name=lease.session_name,
        invocation_dir=lease.invocation_dir,
        path_prefix=lease.path_prefix,
        env=dict(lease.env),
        lease_id=lease.lease_id,
    )


def finish_browser_grant(grant: BrowserGrant) -> BrowserTurnStatus:
    if grant.status != "granted":
        return BrowserTurnStatus(
            status=grant.status, reason_code=grant.reason_code, detail=grant.detail
        )
    with _LEASES_LOCK:
        lease = _LEASES.pop(grant.lease_id or "", None)
    if lease is None:
        return BrowserTurnStatus(status="lost", reason_code="lease_unknown")
    check = release_session(lease)
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
) -> None:
    close_owner(
        owner.token(), execution=execution, delete_profile=delete_profile, data_dir=data_dir
    )
