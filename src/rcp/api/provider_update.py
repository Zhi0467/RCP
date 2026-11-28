"""Machine-card provider update actions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from rcp.api.dependencies import get_identity_access, get_provider_updates, get_store
from rcp.machine_cards import card
from rcp.providers import ProviderId
from rcp.runs.provider_update import ProviderUpdateRunner
from rcp.server_ops.provider_update import ProviderUpdateRefused
from rcp.storage import AppStore

router = APIRouter()
UpdatesDependency = Annotated[ProviderUpdateRunner, Depends(get_provider_updates)]
StoreDependency = Annotated[AppStore, Depends(get_store)]


def _require_machine(request: Request, store: AppStore, machine_id: str) -> None:
    get_identity_access(request).acting_user(request)
    try:
        card(store, machine_id)
    except KeyError:
        raise HTTPException(404, "Unknown machine.") from None


@router.post("/api/space/machines/{machine_id}/providers/{provider}/update", status_code=202)
def start_provider_update(
    machine_id: str,
    provider: ProviderId,
    request: Request,
    store: StoreDependency,
    updates: UpdatesDependency,
) -> None:
    _require_machine(request, store, machine_id)
    try:
        updates.start(provider, machine_id)
    except ProviderUpdateRefused as exc:
        raise HTTPException(409, str(exc)) from exc
