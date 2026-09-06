"""Compute readiness admission for human-authorized episode starts."""

from __future__ import annotations

from rcp.compute_jobs.probe import probe_compute_backend
from rcp.config import Manifest
from rcp.storage import AppStore


class ComputeBackendNotReady(ValueError):
    """The resolved execution machine cannot own episode computation."""


def require_episode_compute_backend(
    store: AppStore,
    project_id: str,
    manifest: Manifest,
    machine_alias: str,
) -> None:
    probe = store.compute_backend_probe(project_id, machine_alias)
    if probe is None:
        probe = probe_compute_backend(manifest, machine_alias, data_dir=store.path.parent)
        store.record_compute_backend_probe(project_id, probe)
    if not probe.ready:
        raise ComputeBackendNotReady(
            f"Compute backend on machine {machine_alias} is not ready: {probe.diagnostic} "
            f"{probe.required_action or 'Repair the compute backend and probe again.'}"
        )
