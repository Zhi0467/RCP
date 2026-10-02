"""Personal-space controls for this backend machine's power policy."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from rcp.machine_power import MachinePowerController
from rcp.machine_power_install import InstallError

router = APIRouter()


def _controller(request: Request) -> MachinePowerController:
    controller = request.app.state.machine_power
    if controller is None:
        raise HTTPException(status_code=404, detail="Not found")
    return controller


Controller = Annotated[MachinePowerController, Depends(_controller)]


class PowerPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idle_hold: StrictBool | None = None
    lid_mode: StrictBool | None = None


@router.get("/api/machine-power")
def machine_power(controller: Controller) -> dict[str, object]:
    return controller.status()


@router.put("/api/machine-power")
def update_machine_power(body: PowerPreferences, controller: Controller) -> dict[str, object]:
    return controller.update(body.model_dump(exclude_none=True))


@router.post("/api/machine-power/install")
def install_machine_power(controller: Controller) -> dict[str, object]:
    try:
        return controller.install()
    except InstallError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc


@router.post("/api/machine-power/uninstall")
def uninstall_machine_power(controller: Controller) -> dict[str, object]:
    try:
        return controller.uninstall()
    except InstallError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
