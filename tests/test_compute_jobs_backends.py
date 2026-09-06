from __future__ import annotations

import plistlib
import shlex
import subprocess
from pathlib import Path
from typing import get_args

import pytest

from rcp.compute_jobs.backend_context import (
    BackendContext,
    ComputeLaunchUncertainError,
    ComputeTransportError,
)
from rcp.compute_jobs.backends import COMPUTE_BACKENDS, ComputeBackendId, resolve_backend
from rcp.compute_jobs.models import ComputeLaunchRequest
from rcp.config import MachineComputeConfig
from rcp.limits import COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS, COMPUTE_JOB_STATUS_TIMEOUT_SECONDS


class Runner:
    def __init__(self, *responses: tuple[int, str, str]):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        code, stdout, stderr = self.responses.pop(0) if self.responses else (0, "", "")
        return subprocess.CompletedProcess(command, code, stdout, stderr)


def context(runner, **kwargs):
    return BackendContext("", "local", None, runner=runner, uid="501", **kwargs)


def request():
    return ComputeLaunchRequest(argv=["echo", "hello"], cwd="/work")


def test_registry_and_resolution():
    assert set(get_args(ComputeBackendId)) == set(COMPUTE_BACKENDS) == {"systemd_user", "launchd"}
    for os_name, remote, manager, expected in [
        ("Linux", False, True, "systemd_user"),
        ("Linux", True, True, "systemd_user"),
        ("Linux", True, False, None),
        ("Linux", False, False, None),
        ("Darwin", False, False, "launchd"),
        ("Darwin", True, False, "launchd"),
        ("FreeBSD", True, False, None),
    ]:
        backend = resolve_backend(None, os_name, remote, manager)
        assert (backend.id if backend else None) == expected
    # An opted-in scheduler is used directly; it never resolves to a launch wrapper.
    assert resolve_backend(MachineComputeConfig(job_manager="slurm"), "Linux", False, True) is None
    with pytest.raises(ValueError):
        MachineComputeConfig(backend="ssh_session")


def test_runner_remote_quotes_and_resolves_target_uid():
    runner = Runner((0, "1234\n", ""), (0, "ok", ""))
    ctx = BackendContext("worker", "remote", None, runner=runner)
    assert ctx.target_uid() == "1234"
    assert ctx.target_uid() == "1234"
    ctx.run(["printf", "%s", "a b; echo bad"], input="payload", check=True)
    assert runner.calls[0][0][-2:] == ["worker", "id -u"]
    assert runner.calls[1][0][-1] == "printf %s 'a b; echo bad'"
    assert runner.calls[1][1] == dict(
        capture_output=True, text=True, timeout=COMPUTE_JOB_STATUS_TIMEOUT_SECONDS, input="payload"
    )


def test_systemd_commands_and_mirrored_roots():
    backend = COMPUTE_BACKENDS["systemd_user"]
    runner = Runner((0, "", ""), (0, "ActiveState=active\n", ""), (0, "", ""))
    ctx = context(
        runner,
        containment="mirrored",
        writable_roots=("/work", "/space dir"),
        protected_paths=("/work/.research", '/space dir/protected "state"\\path'),
    )
    assert backend.start("/jobs/abc", "/jobs/abc/run.sh", request(), ctx) == "rcp-job-abc"
    prefix = ["env", "XDG_RUNTIME_DIR=/run/user/501"]
    assert runner.calls[0][0] == [
        *prefix,
        "systemd-run",
        "--user",
        "--unit",
        "rcp-job-abc",
        "--collect",
        "-p",
        "StandardOutput=file:/jobs/abc/log",
        "-p",
        "StandardError=file:/jobs/abc/log",
        "-p",
        "PrivateUsers=yes",
        "-p",
        "ProtectSystem=strict",
        "-p",
        "ProtectHome=read-only",
        "-p",
        'ReadWritePaths="/work"',
        "-p",
        'ReadWritePaths="/space dir"',
        "-p",
        'ReadWritePaths="/jobs/abc"',
        "-p",
        'ReadOnlyPaths="/work/.research"',
        "-p",
        'ReadOnlyPaths="/space dir/protected \\"state\\"\\\\path"',
        "--",
        "sh",
        "/jobs/abc/run.sh",
    ]
    assert runner.calls[0][1]["timeout"] == COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS
    assert backend.alive("rcp-job-abc", ctx) is True
    assert runner.calls[1][0] == [
        *prefix,
        "systemctl",
        "--user",
        "show",
        "-p",
        "ActiveState",
        "rcp-job-abc",
    ]
    backend.cancel("rcp-job-abc", ctx)
    assert runner.calls[2][0] == [*prefix, "systemctl", "--user", "stop", "rcp-job-abc"]


def test_systemd_cooperative_start_omits_protected_paths():
    runner = Runner()
    ctx = context(
        runner,
        containment="cooperative",
        writable_roots=("/work",),
        protected_paths=("/work/.research",),
    )
    COMPUTE_BACKENDS["systemd_user"].start("/jobs/abc", "/jobs/abc/run.sh", request(), ctx)
    assert not any(arg.startswith("ReadOnlyPaths=") for arg in runner.calls[0][0])


@pytest.mark.parametrize(
    "backend_id, response, expected",
    [
        ("systemd_user", (0, "ActiveState=inactive", ""), False),
        ("systemd_user", (1, "", "Unit rcp-job-x.service could not be found."), False),
        ("systemd_user", (1, "", "Failed to connect to bus"), None),
        ("launchd", (0, "\tstate = running\n", ""), True),
        ("launchd", (0, "\tstate = not running\n", ""), False),
        ("launchd", (1, "", "Could not find service rcp-job-x"), False),
        ("launchd", (1, "", "Operation not permitted"), None),
    ],
)
def test_backend_observation_states(backend_id, response, expected):
    assert COMPUTE_BACKENDS[backend_id].alive("42", context(Runner(response))) is expected


def test_launchd_plist_and_commands(tmp_path):
    backend = COMPUTE_BACKENDS["launchd"]
    runner = Runner()
    ctx = context(runner)
    root = str(tmp_path / "abc")
    Path(root).mkdir()
    assert backend.start(root, f"{root}/run.sh", request(), ctx) == "rcp-job-abc"
    assert plistlib.loads((Path(root) / "job.plist").read_bytes()) == {
        "Label": "rcp-job-abc",
        "ProgramArguments": ["/bin/sh", f"{root}/run.sh"],
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": f"{root}/log",
        "StandardErrorPath": f"{root}/log",
    }
    assert runner.calls[0][0] == ["launchctl", "bootstrap", "gui/501", f"{root}/job.plist"]
    backend.cancel("rcp-job-abc", ctx)
    assert runner.calls[1][0] == ["launchctl", "bootout", "gui/501/rcp-job-abc"]


@pytest.mark.parametrize(
    "backend_id,response",
    [
        (
            "systemd_user",
            (5, "", "Failed to stop rcp-job-x.service: Unit rcp-job-x.service not loaded."),
        ),
        ("launchd", (3, "", "Boot-out failed: 3: No such process")),
    ],
)
def test_cancellation_is_idempotent_for_gone_jobs(backend_id, response):
    COMPUTE_BACKENDS[backend_id].cancel("42", context(Runner(response)))


@pytest.mark.parametrize("backend_id", list(COMPUTE_BACKENDS))
@pytest.mark.parametrize("returncode", [1, 255])
def test_facility_probes_redact_and_report_failure(backend_id, returncode):
    runner = Runner((returncode, "", "token=secret-value\nbackend denied access"))
    ctx = context(runner)
    ctx.execution_host = "worker"
    probe = COMPUTE_BACKENDS[backend_id].probe(ctx)
    assert probe.state == "failed"
    assert not probe.ready
    assert probe.required_action
    assert "secret-value" not in probe.diagnostic
    assert "\n" not in probe.diagnostic


@pytest.mark.parametrize("backend_id", list(COMPUTE_BACKENDS))
@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("status", 10), OSError("no ssh")])
def test_backend_transport_failure_propagates(backend_id, error):
    def unavailable(command, **kwargs):
        raise error

    ctx = context(unavailable)
    ctx.execution_host = "worker"
    with pytest.raises(type(error)):
        COMPUTE_BACKENDS[backend_id].alive("42", ctx)


@pytest.mark.parametrize("backend_id", list(COMPUTE_BACKENDS))
def test_backend_ssh_exit_255_raises_transport_error(backend_id):
    ctx = context(Runner((255, "", "connection dropped")))
    ctx.execution_host = "worker"
    with pytest.raises(ComputeTransportError, match="connection dropped"):
        COMPUTE_BACKENDS[backend_id].alive("42", ctx)


@pytest.mark.parametrize("backend_id", list(COMPUTE_BACKENDS))
def test_backend_nontransport_failure_is_unknown(backend_id):
    ctx = context(Runner((1, "", "observation failed")))
    ctx.execution_host = "worker"
    assert COMPUTE_BACKENDS[backend_id].alive("42", ctx) is None


@pytest.mark.parametrize("backend_id", list(COMPUTE_BACKENDS))
def test_cancel_ssh_exit_255_raises_transport_error(backend_id):
    ctx = context(Runner((255, "", "connection dropped")))
    ctx.execution_host = "worker"
    with pytest.raises(ComputeTransportError, match="connection dropped"):
        COMPUTE_BACKENDS[backend_id].cancel("42", ctx)


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("remote", [False, True])
def test_runner_exit_255_is_transport_failure_only_over_ssh(check, remote):
    ctx = context(Runner((255, "", "command failed")))
    ctx.execution_host = "worker" if remote else ""
    if remote or check:
        with pytest.raises(ComputeTransportError if remote else RuntimeError) as error:
            ctx.run(["command"], check=check)
        assert isinstance(error.value, ComputeTransportError) is remote
    else:
        assert ctx.run(["command"], check=check).returncode == 255


@pytest.mark.parametrize("backend_id", ["systemd_user", "launchd"])
@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired("launch", 10), ComputeTransportError("connection dropped")],
)
def test_uncertain_start_stops_the_stable_unit(backend_id, failure, tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "systemd-run" in command or "bootstrap" in command:
            raise failure
        return subprocess.CompletedProcess(command, 0, "", "")

    root = tmp_path / "abc"
    root.mkdir()
    # The manager may have already run the job, even when cleanup succeeds.
    with pytest.raises(ComputeLaunchUncertainError) as error:
        COMPUTE_BACKENDS[backend_id].start(
            str(root), str(root / "run.sh"), request(), context(runner)
        )
    assert error.value.__cause__ is failure
    assert calls[-1] == (
        ["env", "XDG_RUNTIME_DIR=/run/user/501", "systemctl", "--user", "stop", "rcp-job-abc"]
        if backend_id == "systemd_user"
        else ["launchctl", "bootout", "gui/501/rcp-job-abc"]
    )


@pytest.mark.parametrize("cancel_status", [0, 255])
def test_systemd_transport_failure_attempts_cancel(cancel_status):
    runner = Runner((255, "", "connection dropped"), (cancel_status, "", "connection dropped"))
    ctx = BackendContext("worker", "remote", None, runner=runner, uid="501")
    # Contact was lost after the manager may have accepted and collected the unit, so the
    # launch is uncertain whether or not the stop attempt reached the host.
    with pytest.raises(ComputeLaunchUncertainError):
        COMPUTE_BACKENDS["systemd_user"].start("/jobs/abc", "/jobs/abc/run.sh", request(), ctx)
    assert len(runner.calls) == 2
    assert shlex.split(runner.calls[-1][0][-1]) == [
        "env",
        "XDG_RUNTIME_DIR=/run/user/501",
        "systemctl",
        "--user",
        "stop",
        "rcp-job-abc",
    ]


@pytest.mark.parametrize("backend_id", list(COMPUTE_BACKENDS))
def test_unconfirmed_launch_reports_uncertain_acceptance(backend_id, tmp_path):
    from rcp.compute_jobs.backend_context import ComputeLaunchUncertainError

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    ctx = context(runner)
    root = tmp_path / "abc"
    root.mkdir()
    with pytest.raises(ComputeLaunchUncertainError):
        COMPUTE_BACKENDS[backend_id].start(str(root), str(root / "run.sh"), request(), ctx)
