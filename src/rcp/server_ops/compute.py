"""Service-account compute probe using the installed service's concrete backend."""

from __future__ import annotations

import os
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from typing import BinaryIO

from rcp.server_ops.cli import CallerIdentity, PreparedServerCommand, ServerEventEmitter
from rcp.server_ops.control import ServerControlClient, ServerControlError
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout
from rcp.server_ops.models import (
    MachineTarget,
    NonsecretField,
    ServerCommandRequest,
    ServerPlanEvent,
    ServerStep,
)
from rcp.storage import AppStore
from rcp.storage.models import SpaceMachineRecord


def resolve_machine_selector(selector: str, cards: Iterable[SpaceMachineRecord]) -> str:
    """The card id an operator named: an id, or a name exactly one card has."""

    cards = list(cards)
    if any(card.machine_id == selector for card in cards):
        return selector
    named = [card.machine_id for card in cards if card.name == selector]
    if len(named) != 1:
        raise ValueError(
            f"No machine is named {selector!r}."
            if not named
            else f"Several machines are named {selector!r}; use the machine id."
        )
    return named[0]


def prepare_compute_probe_command(
    request: ServerCommandRequest,
    identity: CallerIdentity,
    *,
    layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
) -> PreparedServerCommand:
    if request.command != "server compute probe":
        raise ValueError("compute probe requires the compute probe command")
    project_id, selector = request.project_id, request.machine_id
    assert project_id is not None and selector is not None
    assert request.compute_route is not None
    pending = ServerStep(
        number=1,
        title=f"Probe {request.compute_route} on {request.machine_id}",
        purpose="Verify and store the selected machine's compute backend readiness.",
        performed_by="system",
        target=MachineTarget(host=identity.host, os_account=identity.username),
        phase="compute_probe",
        state="pending",
        expected_success=(
            "The selected route is ready: a helper job ran outside the service lifecycle, "
            "or the Slurm tools and queue are reachable under the execution account."
        ),
        message="The installed service will probe the project's configured execution machine.",
    )

    def execute(emitter: ServerEventEmitter, _input_stream: BinaryIO) -> None:
        emitter.emit_step(pending.model_copy(update={"state": "running"}))
        try:
            with closing(AppStore.open_read_only(layout.data_dir / "rcp.sqlite3")) as store:
                machine_id = resolve_machine_selector(selector, store.space_machines())
            client = ServerControlClient.from_data_dir(
                layout.data_dir, expected_server_uid=os.geteuid()
            )
            probe = client.probe_compute_backend(
                project_id=project_id,
                machine_id=machine_id,
                route=request.compute_route,
            )
        except (ServerControlError, ValueError) as exc:
            emitter.emit_step(pending.model_copy(update={"state": "failed", "message": str(exc)}))
            return
        emitter.emit_step(
            pending.model_copy(
                update={
                    "state": "succeeded" if probe.ready else "failed",
                    "message": probe.diagnostic,
                    "fields": tuple(
                        NonsecretField(name=name, value=value)
                        for name, value in (
                            ("route", request.compute_route),
                            ("status_label", probe.status_label),
                            ("backend_id", probe.backend_id or "unavailable"),
                            ("containment", probe.containment),
                            ("required_action", probe.required_action or "none"),
                        )
                    ),
                }
            )
        )

    return PreparedServerCommand(
        plan=ServerPlanEvent(
            command=request.command, timestamp=datetime.now(UTC), steps=(pending,)
        ),
        execute=execute,
    )
