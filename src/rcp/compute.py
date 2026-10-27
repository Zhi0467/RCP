from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import (
    Manifest,
    ResolvedComputeContext,
    ResolvedComputeProfile,
)
from rcp.operator_commands import operator_route
from rcp.storage.models import SpaceMachineRecord
from rcp.transport.remote_compute_probe import probe_connection
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script


class ComputeConnectionProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compute_id: str
    execution_machine: str
    state: Literal[
        "reachable",
        "unreachable",
        "authentication_failed",
        "host_key_failed",
    ]
    reachable: bool
    diagnostic: str
    required_action: str | None = None
    status_label: str
    status_tone: Literal["ready", "error"]


_PROBE_PRESENTATION: dict[str, tuple[str, Literal["ready", "error"]]] = {
    "reachable": ("Reachable", "ready"),
    "unreachable": ("Unreachable", "error"),
    "authentication_failed": ("Authentication failed", "error"),
    "host_key_failed": ("Host key failed", "error"),
}


def _safe_probe_diagnostic(value: object) -> str:
    return safe_compute_diagnostic(str(value or "Compute probe failed.")) or "Compute probe failed."


def _probe_one(
    connection: ResolvedComputeProfile,
    *,
    execution_machine: str,
    execution_host: str,
    space_kind: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> ComputeConnectionProbe:
    if execution_host:
        command = shlex.join(
            [
                "python3",
                "-c",
                _remote_script("remote_compute_probe.py"),
                connection.kind,
                connection.ssh_target,
            ]
        )
        try:
            completed = runner(
                ssh_arguments(execution_host, command),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            payload: dict[str, object] = {
                "state": "unreachable",
                "diagnostic": str(exc),
            }
        else:
            if completed.returncode:
                payload = {
                    "state": "unreachable",
                    "diagnostic": (
                        completed.stderr.strip()
                        or f'Agent machine "{execution_machine}" could not run the compute probe.'
                    ),
                }
            else:
                try:
                    decoded = json.loads(completed.stdout)
                except json.JSONDecodeError:
                    decoded = None
                payload = (
                    decoded
                    if isinstance(decoded, dict)
                    else {
                        "state": "unreachable",
                        "diagnostic": "The agent machine returned an invalid compute probe.",
                    }
                )
    else:
        try:
            payload = probe_connection(
                connection.kind,
                connection.ssh_target,
                runner=runner,
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            payload = {"state": "unreachable", "diagnostic": str(exc)}

    raw_state = payload.get("state")
    state = (
        raw_state
        if isinstance(raw_state, str) and raw_state in _PROBE_PRESENTATION
        else "unreachable"
    )
    diagnostic = _safe_probe_diagnostic(payload.get("diagnostic"))
    required_action = None
    # The check runs as the account RCP probes with, typed from the human's own prompt.
    check = operator_route(space_kind, execution_host).command(["ssh", connection.ssh_target])
    if state == "authentication_failed":
        location = f'agent machine "{execution_machine}"'
        if execution_host:
            location += f" ({execution_host})"
        required_action = (
            f"Add SSH credentials for {connection.name} on {location}, check with `{check}`, "
            "then probe again. RCP does not collect keys or passwords."
        )
    elif state == "host_key_failed":
        required_action = (
            f"Verify and add the SSH host key for {connection.name} on agent machine "
            f'"{execution_machine}" by running `{check}`, then probe again.'
        )
    status_label, status_tone = _PROBE_PRESENTATION[state]
    return ComputeConnectionProbe(
        compute_id=connection.id,
        execution_machine=execution_machine,
        state=state,
        reachable=state == "reachable",
        diagnostic=diagnostic,
        required_action=required_action,
        status_label=status_label,
        status_tone=status_tone,
    )


def _machine_route(host: str, os_account: str) -> str:
    return f"{os_account}@{host}" if os_account and "@" not in host else host


def probe_machine_reach(
    target: SpaceMachineRecord,
    sources: list[SpaceMachineRecord],
    *,
    space_kind: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[tuple[SpaceMachineRecord, ComputeConnectionProbe]]:
    """Probe `target` from each machine an agent runs on.

    A host-less (server-local) machine has no route from any other machine,
    and a machine is local to itself, so neither is probed.
    """

    if not target.host:
        return []
    profile = ResolvedComputeProfile(
        id=target.machine_id,
        name=target.name,
        kind="ssh",
        ssh_target=_machine_route(target.host, target.os_account),
    )
    others = [source for source in sources if source.host != target.host]
    if not others:
        return []
    with ThreadPoolExecutor(max_workers=min(len(others), 8)) as executor:
        futures = [
            (
                source,
                executor.submit(
                    _probe_one,
                    profile,
                    execution_machine=source.name,
                    execution_host=source.host,
                    space_kind=space_kind,
                    runner=runner,
                ),
            )
            for source in others
        ]
        return [(source, future.result()) for source, future in futures]


def machine_compute_context(
    manifest: Manifest,
    cards: Mapping[str, SpaceMachineRecord],
    *,
    execution_machine: str,
) -> ResolvedComputeContext:
    """Every project machine as seen from the agent's own machine.

    The machine the agent runs on is local; any other machine is reached over its
    SSH route from there. A host-less (server-local) machine has no route from an
    agent running elsewhere, so that agent is not told about it. Each machine's
    access hint and job manager come from its space card, keyed by host.
    """

    machines = manifest.machine_map
    if execution_machine not in machines:
        raise ValueError(f"unknown execution machine: {execution_machine}")
    profiles = []
    for machine in manifest.machines:
        card = cards.get(machine.host)
        hint = card.access_hint if card is not None else ""
        manager = "slurm" if card is not None and card.job_manager == "slurm" else ""
        if machine.alias == execution_machine:
            profiles.append(
                ResolvedComputeProfile(
                    id=machine.alias,
                    name=machine.alias,
                    kind="local",
                    access_hint=hint,
                    job_manager=manager,
                )
            )
        elif machine.host:
            profiles.append(
                ResolvedComputeProfile(
                    id=machine.alias,
                    name=machine.alias,
                    kind="ssh",
                    ssh_target=_machine_route(machine.host, machine.os_account),
                    access_hint=hint,
                    job_manager=manager,
                )
            )
    return ResolvedComputeContext(active=tuple(profiles))
