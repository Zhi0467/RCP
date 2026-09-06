from __future__ import annotations

import subprocess
from dataclasses import replace

from rcp.compute_jobs.backend_context import BackendContext, ComputeBackendProfile
from rcp.compute_jobs.backends import resolve_backend
from rcp.compute_jobs.models import ComputeJobRecord
from rcp.config import MachineComputeConfig, Manifest


def resolve_context(
    manifest: Manifest,
    machine_alias: str,
    runner=subprocess.run,
) -> tuple[BackendContext, ComputeBackendProfile | None]:
    """Resolve identity and OS on the execution machine, never on its client."""
    machine = manifest.machine_map[machine_alias]
    context = BackendContext(
        execution_machine=machine_alias,
        execution_host=machine.host,
        compute=machine.compute or MachineComputeConfig(),
        runner=runner,
    )
    os_result = context.run(["uname", "-s"])
    uid_result = context.run(["id", "-u"])
    if os_result.returncode or uid_result.returncode:
        raise RuntimeError(
            os_result.stderr or uid_result.stderr or "Cannot resolve compute machine identity."
        )
    uid = uid_result.stdout.strip()
    if not uid.isdecimal():
        raise RuntimeError("The compute machine returned an invalid user id.")
    context = replace(context, os_name=os_result.stdout.strip(), uid=uid)
    has_user_manager = False
    if context.os_name == "Linux" and context.compute.backend is None:
        try:
            manager = context.run(
                [
                    "env",
                    f"XDG_RUNTIME_DIR=/run/user/{context.uid}",
                    "systemctl",
                    "--user",
                    "show-environment",
                ]
            )
        except FileNotFoundError:
            pass
        else:
            has_user_manager = manager.returncode == 0
    profile = resolve_backend(
        context.compute,
        context.os_name,
        bool(context.execution_host),
        has_user_manager,
    )
    return context, profile


def recorded_job_context(manifest: Manifest, record: ComputeJobRecord) -> BackendContext:
    machine = manifest.machine_map.get(record.execution_machine)
    if machine is None or machine.host != record.execution_host:
        raise ValueError("recorded compute execution machine is no longer configured")
    return BackendContext(
        execution_host=record.execution_host,
        execution_machine=record.execution_machine,
        compute=machine.compute,
        containment=record.containment,
    )
