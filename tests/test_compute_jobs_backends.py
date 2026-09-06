from __future__ import annotations

import os
import plistlib
import shlex
import signal
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
from rcp.transport import remote_job_launch
from rcp.transport.remote_job_launch import alive, process_identity
from rcp.transport.state import _remote_script


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
    assert (
        set(get_args(ComputeBackendId))
        == set(COMPUTE_BACKENDS)
        == {"systemd_user", "launchd", "ssh_session", "slurm"}
    )
    for os_name, remote, manager, expected in [
        ("Linux", False, True, "systemd_user"),
        ("Linux", True, True, "systemd_user"),
        ("Linux", True, False, "ssh_session"),
        ("Linux", False, False, None),
        ("Darwin", False, False, "launchd"),
        ("Darwin", True, False, "launchd"),
        ("FreeBSD", True, False, None),
    ]:
        backend = resolve_backend(None, os_name, remote, manager)
        assert (backend.id if backend else None) == expected
    assert (
        resolve_backend(MachineComputeConfig(backend="slurm"), "Linux", False, True).id == "slurm"
    )
    assert not COMPUTE_BACKENDS["ssh_session"].supports("Linux", False)
    assert not COMPUTE_BACKENDS["ssh_session"].supports("Darwin", True)


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
    ctx = context(runner, containment="mirrored", writable_roots=("/work", "/space dir"))
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
        ("slurm", (0, "42\n43\n", ""), True),
        ("slurm", (0, "43\n", ""), False),
        ("slurm", (1, "", "Unable to contact controller"), None),
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


def test_slurm_exact_commands():
    backend = COMPUTE_BACKENDS["slurm"]
    runner = Runner((0, "42;cluster\n", ""), (0, "42\n", ""))
    ctx = context(runner)
    ctx.compute = MachineComputeConfig(
        backend="slurm", slurm_account="lab", slurm_partition="gpu", slurm_submit_args=["--time=1"]
    )
    assert backend.start("/jobs/abc", "/jobs/abc/run.sh", request(), ctx) == "42"
    assert runner.calls[0][0] == [
        "sbatch",
        "--parsable",
        "--job-name",
        "rcp-job-abc",
        "--output",
        "/jobs/abc/log",
        "--error",
        "/jobs/abc/log",
        "--account",
        "lab",
        "--partition",
        "gpu",
        "--time=1",
        "/jobs/abc/run.sh",
    ]
    assert backend.alive("42", ctx) is True
    assert runner.calls[1][0] == ["squeue", "-h", "-o", "%A"]
    backend.cancel("42", ctx)
    assert runner.calls[2][0] == ["scancel", "42"]


def test_slurm_shell_stubs(tmp_path, monkeypatch):
    marker = tmp_path / "cancelled"
    for name, body in {
        "sbatch": 'printf "42;cluster\\n"',
        "squeue": 'test "$*" = "-h -o %A" || exit 2\nprintf "42\\n"',
        "scancel": f'printf "%s" "$1" > {shlex.quote(str(marker))}',
    }.items():
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    backend = COMPUTE_BACKENDS["slurm"]
    ctx = context(subprocess.run)
    handle = backend.start(str(tmp_path), str(tmp_path / "run.sh"), request(), ctx)
    assert backend.alive(handle, ctx) is True
    backend.cancel(handle, ctx)
    assert marker.read_text() == handle


def test_ssh_session_ships_source_and_checks_identity():
    backend = COMPUTE_BACKENDS["ssh_session"]
    runner = Runner(
        (0, "123456:789\n", ""), (0, "alive\n", ""), (0, "gone\n", ""), (1, "", "SSH failed")
    )
    ctx = BackendContext("worker", "remote", None, runner=runner)
    handle = backend.start("/jobs/abc", "/jobs/abc/run.sh", request(), ctx)
    assert shlex.split(runner.calls[0][0][-1]) == [
        "python3",
        "-c",
        _remote_script("remote_job_launch.py"),
        "start",
        "/jobs/abc",
        "/jobs/abc/run.sh",
    ]
    assert backend.alive(handle, ctx) is True
    assert backend.alive(handle, ctx) is False
    assert backend.alive(handle, ctx) is None
    backend.cancel(handle, ctx)
    assert shlex.split(runner.calls[-1][0][-1])[-4:] == ["cancel", handle, "1.0", "0.05"]
    with pytest.raises(ValueError, match="remote"):
        backend.start("/jobs/abc", "/jobs/abc/run.sh", request(), context(runner))


def test_process_identity_handles_parentheses_and_pid_reuse(monkeypatch):
    stat = "123 (worker (a b)) S 1 123 " + " ".join(["0"] * 16) + " 987 0\n"
    monkeypatch.setattr(Path, "read_text", lambda path: stat)
    monkeypatch.setattr(remote_job_launch, "_group_alive", lambda pid: False)
    assert process_identity(123) == ("987", "S")
    assert alive("123:987") is True
    assert alive("123:986") is False


def test_session_liveness_reports_gone_when_leader_identity_mismatches(monkeypatch):
    if not Path("/proc/self/stat").is_file():
        pytest.skip("SSH session process groups require Linux /proc")
    with subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as leader:
        try:
            child = int(leader.stdout.readline())
            leader.wait(timeout=5)
            assert process_identity(child)[1] not in {"Z", "X"}
            monkeypatch.setattr(remote_job_launch, "process_identity", lambda pid: ("988", "S"))
            # A live group under a recycled leader pid is not this job and is never signalled.
            assert alive(f"{leader.pid}:987") is False
        finally:
            os.killpg(leader.pid, signal.SIGKILL)


@pytest.mark.parametrize("leader_state", ["missing", "Z", "X"])
def test_session_liveness_tracks_group_after_leader_exits(monkeypatch, leader_state):
    def identity(pid):
        if leader_state == "missing":
            raise FileNotFoundError
        return "987", leader_state

    monkeypatch.setattr(remote_job_launch, "process_identity", identity)
    members = []
    monkeypatch.setattr(remote_job_launch, "_group_alive", lambda pid: bool(members))
    assert not alive("123:987")
    members.append(456)
    assert alive("123:987")


@pytest.mark.parametrize("already_gone", [False, True])
def test_session_cancel_signals_group_without_leader(monkeypatch, already_gone):
    def identity(pid):
        raise FileNotFoundError

    signals = []

    def killpg(pid, requested_signal):
        assert pid == 123
        signals.append(requested_signal)
        if already_gone:
            raise ProcessLookupError

    monkeypatch.setattr(remote_job_launch, "process_identity", identity)
    monkeypatch.setattr(
        remote_job_launch, "_group_alive", lambda pid: signal.SIGKILL not in signals
    )
    monkeypatch.setattr(os, "killpg", killpg)
    clock = iter(range(10))
    monkeypatch.setattr(remote_job_launch.time, "monotonic", lambda: next(clock))
    remote_job_launch.cancel("123:987", grace=0.5, poll_interval=0.1)
    assert signals == ([signal.SIGTERM] if already_gone else [signal.SIGTERM, signal.SIGKILL])


@pytest.mark.parametrize(
    "backend_id,response",
    [
        (
            "systemd_user",
            (5, "", "Failed to stop rcp-job-x.service: Unit rcp-job-x.service not loaded."),
        ),
        ("launchd", (3, "", "Boot-out failed: 3: No such process")),
        ("slurm", (1, "", "scancel: error: Kill job error on job id 42: Invalid job id specified")),
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
def test_uncertain_start_stops_the_stable_unit(backend_id, tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "systemd-run" in command or "bootstrap" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "", "")

    root = tmp_path / "abc"
    root.mkdir()
    # A collected systemd unit may already have run, so its receipts are retained.
    expected = (
        ComputeLaunchUncertainError if backend_id == "systemd_user" else subprocess.TimeoutExpired
    )
    with pytest.raises(expected):
        COMPUTE_BACKENDS[backend_id].start(
            str(root), str(root / "run.sh"), request(), context(runner)
        )
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
    if backend_id == "ssh_session":
        ctx.execution_host = "worker"
    root = tmp_path / "abc"
    root.mkdir()
    with pytest.raises(ComputeLaunchUncertainError):
        COMPUTE_BACKENDS[backend_id].start(str(root), str(root / "run.sh"), request(), ctx)
