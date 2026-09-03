from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.config import AGENT_EXECUTION_PROFILES, ComputeConnectionConfig, Manifest
from rcp.limits import ACTIVE_COMPUTE_ID_MAX_COUNT
from rcp.server_ops.models import redact_server_text
from rcp.transport.remote_compute_probe import probe_connection
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script

ComputeProbeCacheKey = tuple[
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str, str, str, str], ...],
]


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
    redacted = redact_server_text(str(value or "Compute probe failed."))
    return " ".join(redacted.split())[:600] or "Compute probe failed."


def _probe_one(
    connection: ComputeConnectionConfig,
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


def probe_compute_connections(
    manifest: Manifest,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, dict[str, dict[str, object]]]:
    """Probe each resource from every machine currently used by an agent profile."""

    execution_aliases = sorted(
        {manifest.agent_profile(profile).run_on for profile in AGENT_EXECUTION_PROFILES}
    )
    targets = [
        (alias, connection)
        for alias in execution_aliases
        for connection in manifest.compute_connections
    ]
    result: dict[str, dict[str, dict[str, object]]] = {alias: {} for alias in execution_aliases}
    if not targets:
        return result
    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as executor:
        futures = [
            (
                alias,
                connection.id,
                executor.submit(
                    _probe_one,
                    connection,
                    execution_machine=alias,
                    execution_host=manifest.machine_map[alias].host,
                    runner=runner,
                ),
            )
            for alias, connection in targets
        ]
        for alias, compute_id, future in futures:
            result[alias][compute_id] = future.result().model_dump(mode="json")
    return result


def compute_probe_cache_key(manifest: Manifest) -> ComputeProbeCacheKey:
    execution_aliases = sorted(
        {manifest.agent_profile(profile).run_on for profile in AGENT_EXECUTION_PROFILES}
    )
    machines = tuple((alias, manifest.machine_map[alias].host) for alias in execution_aliases)
    connections = tuple(
        (
            connection.id,
            connection.name,
            connection.kind,
            connection.ssh_target,
            connection.access_hint,
        )
        for connection in manifest.compute_connections
    )
    return machines, connections


def selected_compute_connections(
    manifest: Manifest,
    ids: list[str],
) -> list[ComputeConnectionConfig]:
    if len(ids) > ACTIVE_COMPUTE_ID_MAX_COUNT:
        raise ValueError(
            f"active compute connections exceed the limit of {ACTIVE_COMPUTE_ID_MAX_COUNT}"
        )
    by_id = {connection.id: connection for connection in manifest.compute_connections}
    unknown = sorted(set(ids) - set(by_id))
    if unknown:
        raise ValueError(f"unknown compute connections: {unknown}")
    return [by_id[compute_id] for compute_id in ids]
