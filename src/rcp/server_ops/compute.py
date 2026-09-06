"""Service-account compute probe using the installed service's concrete backend."""

from __future__ import annotations

import os
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


def prepare_compute_probe_command(
    request: ServerCommandRequest,
    identity: CallerIdentity,
    *,
    layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
) -> PreparedServerCommand:
    if request.command != "server compute probe":
        raise ValueError("compute probe requires the compute probe command")
    assert request.project_id is not None and request.machine_alias is not None
    pending = ServerStep(
        number=1,
        title=f"Probe compute on {request.machine_alias}",
        purpose="Verify and store the selected machine's compute backend readiness.",
        performed_by="system",
        target=MachineTarget(host=identity.host, os_account=identity.username),
        phase="compute_probe",
        state="pending",
        expected_success="A job runs outside the service lifecycle and records exit 0 and a log.",
        message="The installed service will probe the project's configured execution machine.",
    )

    def execute(emitter: ServerEventEmitter, _input_stream: BinaryIO) -> None:
        emitter.emit_step(pending.model_copy(update={"state": "running"}))
        try:
            client = ServerControlClient.from_data_dir(
                layout.data_dir, expected_server_uid=os.geteuid()
            )
            probe = client.probe_compute_backend(
                project_id=request.project_id, machine_alias=request.machine_alias
            )
        except ServerControlError as exc:
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
