from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path

from rcp.compute_jobs.backend_context import (
    BackendContext,
    ComputeBackendProfile,
    ComputeLaunchUncertainError,
)
from rcp.compute_jobs.files import (
    prepare_job_root,
    read_job_file,
    remove_job_root,
    resolve_jobs_root,
)
from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest
from rcp.compute_jobs.resolution import resolve_context
from rcp.config import Manifest
from rcp.limits import (
    COMPUTE_JOB_POLL_INTERVAL_SECONDS,
    COMPUTE_PROBE_JOB_SECONDS,
    COMPUTE_PROBE_TIMEOUT_SECONDS,
)

# A machine never inherits a result recorded for another host or configuration.
_PROBES: dict[str, tuple[tuple[str, str], ComputeBackendProbe]] = {}


class _CgroupIsolationError(RuntimeError):
    """The job was observed inside the server lifecycle boundary."""


class _ProbeCleanupError(RuntimeError):
    """A failed cancellation must not launch a second probe."""


def _data_dir(data_dir: Path | None) -> Path:
    if data_dir is None:
        from rcp.api.app import default_data_dir

        data_dir = default_data_dir()
    return data_dir.resolve()


def _cache_key(manifest: Manifest, alias: str, data_dir: Path) -> tuple[str, str]:
    machine = manifest.machine_map[alias]
    return machine.model_dump_json(), str(data_dir) if not machine.host else ""


def cached_compute_backend_probe(
    manifest: Manifest,
    machine_alias: str,
    *,
    data_dir: Path | None = None,
) -> ComputeBackendProbe | None:
    cached = _PROBES.get(machine_alias)
    if cached is None or cached[0] != _cache_key(manifest, machine_alias, _data_dir(data_dir)):
        return None
    return cached[1].model_copy(deep=True)


def _result(
    machine_alias: str,
    backend_id: str,
    state: str,
    diagnostic: str,
    *,
    containment: str = "cooperative",
    cgroup_isolated: bool | None = None,
) -> ComputeBackendProbe:
    return ComputeBackendProbe(
        execution_machine=machine_alias,
        backend_id=backend_id,
        state=state,
        ready=state == "ready",
        diagnostic=diagnostic,
        required_action=(
            None
            if state == "ready"
            else "configure a compute backend for this machine"
            if state == "unavailable"
            else "Repair the compute backend on this machine and probe again."
        ),
        containment=containment,
        cgroup_isolated=cgroup_isolated,
        status_label={"ready": "Ready", "unavailable": "Unavailable", "failed": "Failed"}[state],
        status_tone="ready" if state == "ready" else "error",
    )


def _cgroup_isolated(job_cgroup: str, own_cgroup: str) -> bool:
    def paths(value: str) -> dict[str, str]:
        result = {}
        for line in value.splitlines():
            hierarchy, controllers, path = line.split(":", 2)
            result[f"{hierarchy}:{controllers}"] = path
        return result

    job_paths, own_paths = paths(job_cgroup), paths(own_cgroup)
    common = job_paths.keys() & own_paths.keys()
    if not common:
        raise RuntimeError("The probe could not compare compute and RCP cgroups.")
    return all(
        job_paths[key] != own_paths[key]
        and (own_paths[key] == "/" or not job_paths[key].startswith(own_paths[key] + "/"))
        for key in common
    )


def _run_probe_job(
    context: BackendContext,
    profile: ComputeBackendProfile,
    data_dir: Path,
) -> bool | None:
    root = f"{resolve_jobs_root(context, data_dir).rstrip('/')}/probe-{uuid.uuid4().hex}"
    request = ComputeLaunchRequest(
        argv=["sh", "-c", f"echo rcp-probe; sleep {COMPUTE_PROBE_JOB_SECONDS}"],
        cwd=root,
        label="Compute backend probe",
    )
    context = replace(context, writable_roots=(root,))
    handle = None
    prepared = False
    try:
        prepare_job_root(
            context,
            root,
            request,
            project_id="compute-probe",
            origin_operation_id="compute-probe",
            episode_id=None,
        )
        prepared = True
        handle = profile.start(root, f"{root}/run.sh", request, context)
        observed_alive = False
        deadline = time.monotonic() + COMPUTE_PROBE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            alive = profile.alive(handle, context)
            if alive is None:
                raise RuntimeError("The compute backend could not report probe liveness.")
            observed_alive |= alive
            exit_text = read_job_file(context, f"{root}/exit")
            if exit_text is not None and not alive:
                break
            time.sleep(COMPUTE_JOB_POLL_INTERVAL_SECONDS)
        else:
            raise RuntimeError("The compute probe did not finish before its deadline.")
        if not observed_alive:
            raise RuntimeError("The compute probe was never observed alive.")
        fields = exit_text.split()
        if len(fields) != 2 or int(fields[0]) != 0 or int(fields[1]) < 0:
            raise RuntimeError("The compute probe did not record a successful exit.")
        log = read_job_file(context, f"{root}/log")
        if log is None or "rcp-probe" not in log.splitlines():
            raise RuntimeError("The compute probe log is missing its marker.")
        isolated = None
        if context.os_name == "Linux" and not context.execution_host:
            job_cgroup = read_job_file(context, f"{root}/cgroup")
            if job_cgroup is None:
                raise RuntimeError("The compute probe did not record its cgroup.")
            isolated = _cgroup_isolated(job_cgroup, Path("/proc/self/cgroup").read_text())
            if not isolated:
                raise _CgroupIsolationError("The compute job shares the RCP service cgroup.")
        return isolated
    except ComputeLaunchUncertainError as exc:
        prepared = False
        raise _ProbeCleanupError(f"Probe launch outcome unknown; retained {root}: {exc}") from exc
    finally:
        # Never remove provenance under a process whose cancellation could not be proved.
        if handle is not None:
            try:
                profile.cancel(handle, context)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                raise _ProbeCleanupError(f"Probe cleanup failed; retained {root}: {exc}") from exc
        if prepared:
            remove_job_root(context, root)


def probe_compute_backend(
    manifest: Manifest,
    machine_alias: str,
    runner=subprocess.run,
    *,
    data_dir: Path | None = None,
) -> ComputeBackendProbe:
    """Exercise the actual owner, wrapper, liveness, exit file, and job log."""
    resolved_data_dir = _data_dir(data_dir)
    backend_id = ""
    try:
        context, profile = resolve_context(manifest, machine_alias, runner)
        if profile is None:
            result = _result(machine_alias, "", "unavailable", "No compute backend is available.")
        else:
            backend_id = profile.id
            if not profile.supports(context.os_name, bool(context.execution_host)):
                raise ValueError(
                    f"Compute backend {profile.id} does not support this execution machine."
                )
            facility = profile.probe(context)
            if not facility.ready:
                result = facility
            else:
                diagnostic = "Probe observed a running job, exit 0, and its log marker."
                containment = "cooperative"
                if profile.id == "systemd_user":
                    try:
                        isolated = _run_probe_job(
                            replace(context, containment="mirrored"), profile, resolved_data_dir
                        )
                    except _ProbeCleanupError:
                        raise
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        diagnostic = f"Mirrored containment probe failed: {exc}. " + diagnostic
                        isolated = _run_probe_job(context, profile, resolved_data_dir)
                        diagnostic += " Cooperative containment only."
                    else:
                        containment = "mirrored"
                else:
                    isolated = _run_probe_job(context, profile, resolved_data_dir)
                result = _result(
                    machine_alias,
                    backend_id,
                    "ready",
                    diagnostic,
                    containment=containment,
                    cgroup_isolated=isolated,
                )
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result = _result(
            machine_alias,
            backend_id,
            "failed",
            str(exc),
            cgroup_isolated=False if isinstance(exc, _CgroupIsolationError) else None,
        )
    if machine_alias in manifest.machine_map:
        _PROBES[machine_alias] = (_cache_key(manifest, machine_alias, resolved_data_dir), result)
    return result.model_copy(deep=True)
