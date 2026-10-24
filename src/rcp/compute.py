from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import (
    AGENT_EXECUTION_PROFILES,
    Manifest,
    ResolvedComputeContext,
    ResolvedComputeProfile,
)
from rcp.transport.remote_compute_probe import probe_connection
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script

#: Each execution machine with the routes it probes; hints never change reachability.
ComputeProbeCacheKey = tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...]


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
    if state == "authentication_failed":
        location = f'agent machine "{execution_machine}"'
        if execution_host:
            location += f" ({execution_host})"
        required_action = (
            f"Add SSH credentials for {connection.name} on {location}, then probe again. "
            "RCP does not collect keys or passwords."
        )
    elif state == "host_key_failed":
        required_action = (
            f"Verify and add the SSH host key for {connection.name} on agent machine "
            f'"{execution_machine}", then probe again.'
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


def _probe_targets(manifest: Manifest) -> dict[str, list[ResolvedComputeProfile]]:
    """Every other project machine an agent profile's machine can route to."""

    execution_aliases = sorted(
        {manifest.agent_profile(profile).run_on for profile in AGENT_EXECUTION_PROFILES}
    )
    return {
        alias: [
            profile
            for profile in machine_compute_context(manifest, execution_machine=alias).active
            if profile.kind == "ssh"
        ]
        for alias in execution_aliases
    }


def probe_compute_connections(
    manifest: Manifest,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, dict[str, dict[str, object]]]:
    """Probe every other project machine from each machine an agent profile runs on."""

    by_execution = _probe_targets(manifest)
    targets = [(alias, target) for alias, profiles in by_execution.items() for target in profiles]
    result: dict[str, dict[str, dict[str, object]]] = {alias: {} for alias in by_execution}
    if not targets:
        return result
    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as executor:
        futures = [
            (
                alias,
                target.id,
                executor.submit(
                    _probe_one,
                    target,
                    execution_machine=alias,
                    execution_host=manifest.machine_map[alias].host,
                    runner=runner,
                ),
            )
            for alias, target in targets
        ]
        for alias, compute_id, future in futures:
            result[alias][compute_id] = future.result().model_dump(mode="json")
    return result


def compute_probe_cache_key(manifest: Manifest) -> ComputeProbeCacheKey:
    return tuple(
        (
            alias,
            manifest.machine_map[alias].host,
            tuple((target.id, target.ssh_target) for target in targets),
        )
        for alias, targets in _probe_targets(manifest).items()
    )


def machine_compute_context(
    manifest: Manifest,
    *,
    execution_machine: str,
) -> ResolvedComputeContext:
    """Every project machine as seen from the agent's own machine.

    The machine the agent runs on is local; any other machine is reached over its
    SSH route from there. A host-less (server-local) machine has no route from an
    agent running elsewhere, so that agent is not told about it.
    """

    machines = manifest.machine_map
    if execution_machine not in machines:
        raise ValueError(f"unknown execution machine: {execution_machine}")
    profiles = []
    for machine in manifest.machines:
        hint = machine.access_hint
        if machine.alias == execution_machine:
            profiles.append(
                ResolvedComputeProfile(
                    id=machine.alias, name=machine.alias, kind="local", access_hint=hint
                )
            )
        elif machine.host:
            route = (
                f"{machine.os_account}@{machine.host}"
                if machine.os_account and "@" not in machine.host
                else machine.host
            )
            profiles.append(
                ResolvedComputeProfile(
                    id=machine.alias,
                    name=machine.alias,
                    kind="ssh",
                    ssh_target=route,
                    access_hint=hint,
                )
            )
    return ResolvedComputeContext(active=tuple(profiles))
