from __future__ import annotations

from typing import get_args

import pytest

from rcp.compute_jobs.backends import COMPUTE_BACKENDS, ComputeBackendId
from rcp.compute_jobs.models import ComputeBackendProbe, ComputeJobRecord, ComputeLaunchRequest
from rcp.config import MachineComputeConfig
from rcp.limits import COMPUTE_JOB_DIAGNOSTIC_MAX_CHARS, COMPUTE_JOB_LABEL_MAX_CHARS


def test_compute_backend_type_is_registry_derived() -> None:
    assert set(get_args(ComputeBackendId)) == set(COMPUTE_BACKENDS)
    assert MachineComputeConfig().job_manager is None
    assert MachineComputeConfig(job_manager="slurm").job_manager == "slurm"


@pytest.mark.parametrize(
    "values",
    [
        {"job_manager": "unknown"},
        {"backend": "subprocess"},
        {"backend": "systemd_user", "slurm_account": "lab"},
        {"slurm_partition": "gpu"},
        {"slurm_submit_args": ["--time=1"]},
        {"jobs_root": "relative/jobs"},
        {"jobs_root": "~/.rcp/jobs"},
        {"backend": "slurm", "slurm_account": "lab\naccount"},
        {"backend": "slurm", "slurm_submit_args": ["--token=secret"]},
        {"jobs_root": "/home/user/.ssh/jobs"},
        {"other": True},
    ],
)
def test_machine_compute_rejects_invalid_metadata(values) -> None:
    with pytest.raises(ValueError):
        MachineComputeConfig.model_validate(values)


@pytest.mark.parametrize(
    "values",
    [
        {"argv": []},
        {"argv": [""]},
        {"argv": ["echo", "a\x00b"]},
        {"cwd": "relative"},
        {"label": "line\nbreak"},
        {"label": " "},
        {"label": "x" * (COMPUTE_JOB_LABEL_MAX_CHARS + 1)},
        {"label": "token=secret"},
        {"label": "identity_file=/secret"},
        {"backend": "slurm"},
    ],
)
def test_launch_request_is_strict(values) -> None:
    with pytest.raises(ValueError):
        ComputeLaunchRequest.model_validate({"argv": ["echo"], "cwd": "/tmp", **values})


def test_probe_and_record_redact_diagnostics() -> None:
    probe = ComputeBackendProbe(
        machine_id="laptop",
        backend_id="systemd_user",
        state="failed",
        ready=False,
        diagnostic="token=secret\nfailed",
        required_action="password=hidden\nrepair",
        containment="cooperative",
        status_label="Failed",
        status_tone="error",
    )
    assert "secret" not in probe.diagnostic
    assert "hidden" not in probe.required_action
    assert "\n" not in probe.diagnostic + probe.required_action
    record = ComputeJobRecord(
        job_id="job",
        project_id="project",
        origin_operation_id="operation",
        machine_id="laptop",
        backend_id="systemd_user",
        backend_handle="label",
        job_root="/jobs/job",
        cwd="/project",
        argv=["true"],
        log_path="/jobs/job/log",
        exit_path="/jobs/job/exit",
        created_at="2026-09-06T00:00:00Z",
        diagnostic="x" * (COMPUTE_JOB_DIAGNOSTIC_MAX_CHARS + 1),
    )
    assert len(record.diagnostic) == COMPUTE_JOB_DIAGNOSTIC_MAX_CHARS
    with pytest.raises(ValueError):
        ComputeJobRecord.model_validate({**record.model_dump(), "extra": True})
