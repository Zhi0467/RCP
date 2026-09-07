from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from rcp.compute_jobs.backend_context import resolve_context
from rcp.compute_jobs.probe import (
    _cgroup_isolated,
    probe_compute_backend,
)
from rcp.config import MachineComputeConfig
from rcp.limits import COMPUTE_JOB_STATUS_TIMEOUT_SECONDS


class ProbeRunner:
    """A fake OS owner; real job files still cross the production file boundary."""

    def __init__(self, *, os_name="Linux", marker=True, exit_status=0, observable=True):
        self.os_name = os_name
        self.marker = marker
        self.exit_status = exit_status
        self.observable = observable
        self.commands = []
        self.root = None
        self.polls = 0
        self.reject_mirrored = False
        self.reject_cancel = False

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        code, out, err = 0, "", ""
        if command == ["uname", "-s"]:
            out = self.os_name
        elif command == ["id", "-u"]:
            out = "501"
        elif command[:2] == ["launchctl", "bootstrap"]:
            self.root = Path(command[-1]).parent
            self.polls = 0
        elif "systemd-run" in command:
            if self.reject_mirrored and "PrivateUsers=yes" in command:
                return subprocess.CompletedProcess(command, 1, "", "Unsupported PrivateUsers")
            self.root = Path(command[-1]).parent
            self.polls = 0
        elif (
            command[:2] == ["launchctl", "print"] and "/rcp-job-" in command[-1]
        ) or "ActiveState" in command:
            self.polls += 1
            running = self.polls == 1
            if not self.observable:
                code, err = 1, "password=hunter2\nunobservable"
            elif "ActiveState" in command:
                out = "ActiveState=active" if running else "ActiveState=inactive"
            else:
                out = "state = running" if running else "state = exited"
            if not running:
                (self.root / "exit").write_text(f"{self.exit_status} 12345\n")
                (self.root / "log").write_text("rcp-probe\n" if self.marker else "wrong marker\n")
                (self.root / "cgroup").write_text("0::/user.slice/compute-probe\n")
        elif ("bootout" in command or "stop" in command) and self.reject_cancel:
            code, err = 1, "Cancellation transport failed"
        return subprocess.CompletedProcess(command, code, out, err)


@pytest.fixture
def fake_linux_cgroup(monkeypatch):
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kw: (
            "0::/system.slice/rcp.service\n"
            if str(path) == "/proc/self/cgroup"
            else original_read(path, *args, **kw)
        ),
    )


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_probe_runs_backend_and_requires_liveness_exit_and_log(
    manifest, tmp_path, fake_linux_cgroup, os_name
):
    runner = ProbeRunner(os_name=os_name)
    result = probe_compute_backend(manifest, "laptop", runner, data_dir=tmp_path)
    assert result.ready
    if os_name == "Linux":
        assert result.backend_id == "systemd_user"
        assert result.containment == "mirrored"
        assert result.cgroup_isolated is True
        assert any("systemd-run" in command for command in runner.commands)
        assert any("stop" in command for command in runner.commands)
    else:
        assert result.backend_id == "launchd"
        assert result.containment == "cooperative"
        assert result.cgroup_isolated is None
        assert any(command[:2] == ["launchctl", "bootstrap"] for command in runner.commands)
        assert any(command[:2] == ["launchctl", "bootout"] for command in runner.commands)
    assert list((tmp_path / "jobs").iterdir()) == []


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
@pytest.mark.parametrize("options", [{"marker": False}, {"exit_status": 1}, {"observable": False}])
def test_probe_fails_when_backend_contract_is_not_proved(manifest, tmp_path, os_name, options):
    result = probe_compute_backend(
        manifest, "laptop", ProbeRunner(os_name=os_name, **options), data_dir=tmp_path
    )
    assert result.state == "failed"
    assert result.required_action
    assert result.status_tone == "error"
    assert not result.ready


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_probe_retains_root_when_cancellation_cannot_be_confirmed(manifest, tmp_path, os_name):
    runner = ProbeRunner(os_name=os_name)
    runner.reject_cancel = True
    result = probe_compute_backend(manifest, "laptop", runner, data_dir=tmp_path)
    assert result.state == "failed"
    assert "Cancellation transport failed" in result.diagnostic
    assert runner.root.is_dir()


def test_probe_redacts_machine_resolution_failure(manifest, tmp_path):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, "", "password=hunter2\nBearer abcdefghijklmnop"
        )

    result = probe_compute_backend(manifest, "laptop", runner, data_dir=tmp_path)
    assert result.state == "failed"
    assert "hunter2" not in result.diagnostic
    assert "abcdefghijklmnop" not in result.diagnostic
    assert "\n" not in result.diagnostic


@pytest.mark.parametrize("failed_command", ["uname -s", "id -u", "show-environment"])
def test_probe_reports_ssh_resolution_failure(manifest, tmp_path, failed_command):
    manifest.machines[0].host = "compute.example"

    def runner(command, **kwargs):
        remote = command[-1]
        if failed_command in remote:
            return subprocess.CompletedProcess(command, 255, "", "connection dropped")
        return subprocess.CompletedProcess(
            command, 0, "Linux" if remote == "uname -s" else "501", ""
        )

    result = probe_compute_backend(manifest, "laptop", runner, data_dir=tmp_path)
    assert result.state == "failed"
    assert result.diagnostic == "connection dropped"
    assert not (tmp_path / "jobs").exists()


def test_probe_without_resolvable_backend_is_unavailable(manifest, tmp_path):
    result = probe_compute_backend(
        manifest, "laptop", ProbeRunner(os_name="FreeBSD"), data_dir=tmp_path
    )
    assert result.state == "unavailable"
    assert "Linux" in result.required_action
    assert "macOS" in result.required_action
    assert "Slurm" in result.required_action
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_probe_returns_fresh_observation(manifest, tmp_path, fake_linux_cgroup, os_name):
    first = probe_compute_backend(
        manifest, "laptop", ProbeRunner(os_name=os_name), data_dir=tmp_path
    )
    second = probe_compute_backend(
        manifest, "laptop", ProbeRunner(os_name=os_name, exit_status=1), data_dir=tmp_path
    )
    assert first.ready
    assert not second.ready


@pytest.mark.parametrize("execution_host", ["", "rcp@cluster.example"])
@pytest.mark.parametrize("failure", [None, "sbatch", "squeue", "scancel", "queue"])
def test_slurm_readiness_uses_watcher_shell_without_submitting_a_job(
    manifest, tmp_path, monkeypatch, execution_host, failure
):
    machine = manifest.machines[0]
    machine.compute = MachineComputeConfig(job_manager="slurm")
    machine.host = execution_host
    binaries = tmp_path / "bin"
    binaries.mkdir()
    submission = tmp_path / "unexpected-submission"
    for tool in ("sbatch", "squeue", "scancel"):
        if tool == failure:
            continue
        body = (
            ("echo 'Queue unavailable' >&2; exit 1" if failure == "queue" else "exit 0")
            if tool == "squeue"
            else f"echo invoked > {shlex.quote(str(submission))}; exit 99"
        )
        path = binaries / tool
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)
    calls = []

    def execute(command, cwd, host, timeout):
        calls.append((cwd, host))
        return subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            env={"PATH": str(binaries)},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def unexpected_helper(*_args, **_kwargs):
        pytest.fail("A selected scheduler must not probe or launch an OS helper")

    monkeypatch.setattr("rcp.watchers._run_watcher_command", execute)
    result = probe_compute_backend(manifest, "laptop", unexpected_helper, data_dir=tmp_path)
    assert result.backend_id == "slurm"
    assert result.ready is (failure is None)
    assert calls == [("/", execution_host)]
    assert not submission.exists()
    assert not (tmp_path / "jobs").exists()
    if failure:
        assert result.required_action
        assert "administrator" in result.required_action
        assert (
            "Queue unavailable" if failure == "queue" else f"Missing Slurm tool: {failure}"
        ) in result.diagnostic
    else:
        assert result.required_action is None
        assert "when the agent submits" in result.diagnostic


def test_systemd_probe_records_explicit_cooperative_fallback(manifest, tmp_path, monkeypatch):
    runner = ProbeRunner(os_name="Linux")
    runner.reject_mirrored = True
    monkeypatch.setattr("rcp.compute_jobs.probe._cgroup_isolated", lambda *_: True)
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kw: (
            "0::/rcp.service\n"
            if str(path) == "/proc/self/cgroup"
            else original_read(path, *args, **kw)
        ),
    )
    result = probe_compute_backend(manifest, "laptop", runner, data_dir=tmp_path)
    assert result.ready
    assert result.containment == "cooperative"
    assert "Mirrored containment probe failed" in result.diagnostic
    assert "Unsupported PrivateUsers" in result.diagnostic
    assert result.cgroup_isolated is True


@pytest.mark.parametrize("isolated", [True, False])
def test_systemd_probe_requires_independent_cgroup(manifest, tmp_path, monkeypatch, isolated):
    runner = ProbeRunner(os_name="Linux")
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kw: (
            ("0::/system.slice/rcp.service\n" if isolated else "0::/user.slice/compute-probe\n")
            if str(path) == "/proc/self/cgroup"
            else original_read(path, *args, **kw)
        ),
    )
    result = probe_compute_backend(manifest, "laptop", runner, data_dir=tmp_path)
    assert result.ready is isolated
    if isolated:
        assert result.containment == "mirrored"
        assert result.cgroup_isolated is True
    else:
        assert "shares the RCP service cgroup" in result.diagnostic
        assert result.cgroup_isolated is False


def test_cgroup_comparison_rejects_same_service_and_descendants():
    assert _cgroup_isolated("0::/user.slice/job\n", "0::/system.slice/rcp.service\n")
    assert not _cgroup_isolated("0::/system.slice/rcp.service\n", "0::/system.slice/rcp.service\n")
    assert not _cgroup_isolated(
        "0::/system.slice/rcp.service/child\n", "0::/system.slice/rcp.service\n"
    )


def test_cgroup_comparison_ignores_hierarchies_where_both_sit_at_the_root():
    # Observed on a cgroup2 host with leftover v1 controllers (Ubuntu 22.04, 5.15).
    own = "3:devices:/\n2:freezer:/\n0::/user.slice/user-1002.slice/session-8.scope\n"
    job = "3:devices:/\n2:freezer:/\n0::/user.slice/user-1014.slice/user@1014.service/app.slice/rcp-job-x.service\n"
    assert _cgroup_isolated(job, own)
    assert not _cgroup_isolated(
        "3:devices:/\n0::/system.slice/rcp.service\n",
        own.replace("session-8.scope", "x") and "3:devices:/\n0::/system.slice/rcp.service\n",
    )
    with pytest.raises(RuntimeError, match="could not compare"):
        _cgroup_isolated("3:devices:/\n", "3:devices:/\n")


def test_remote_linux_without_user_manager_refuses_compute(manifest, tmp_path):
    manifest.machines[0].host = "compute.example"
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        remote = command[-1]
        if remote == "uname -s":
            return subprocess.CompletedProcess(command, 0, "Linux\n", "")
        if remote == "id -u":
            return subprocess.CompletedProcess(command, 0, "1234\n", "")
        assert "XDG_RUNTIME_DIR=/run/user/1234" in remote
        return subprocess.CompletedProcess(command, 1, "", "No user manager")

    context, profile = resolve_context(manifest, "laptop", runner)
    assert context.uid == "1234"
    assert profile is None
    assert all(command[0] == "ssh" and "compute.example" in command for command in commands)
    commands.clear()
    result = probe_compute_backend(manifest, "laptop", runner, data_dir=tmp_path)
    assert result.state == "unavailable"
    assert not result.ready
    assert result.required_action
    assert len(commands) == 3
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("backend_id", ["systemd_user", "launchd"])
def test_real_compute_owner_when_facility_available(manifest, tmp_path, backend_id):
    executable = "systemd-run" if backend_id == "systemd_user" else "launchctl"
    if shutil.which(executable) is None:
        pytest.skip(f"{executable} facility is not installed")
    uid = str(os.getuid())
    check = (
        ["env", f"XDG_RUNTIME_DIR=/run/user/{uid}", "systemctl", "--user", "show-environment"]
        if backend_id == "systemd_user"
        else ["launchctl", "print", f"gui/{uid}"]
    )
    facility = subprocess.run(
        check,
        capture_output=True,
        text=True,
        timeout=COMPUTE_JOB_STATUS_TIMEOUT_SECONDS,
        check=False,
    )
    if facility.returncode:
        pytest.skip(f"{backend_id} facility unavailable: {facility.stderr.strip()}")
    if backend_id == "launchd":
        # Probe OS admission independently of the implementation's generated plist.
        label = f"rcp-compute-facility-{uuid.uuid4().hex}"
        plist = tmp_path / "facility.plist"
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": ["/bin/sh", "-c", "exit 0"],
                    "RunAtLoad": True,
                    "KeepAlive": False,
                }
            )
        )
        try:
            admission = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                capture_output=True,
                text=True,
                timeout=COMPUTE_JOB_STATUS_TIMEOUT_SECONDS,
                check=False,
            )
            if admission.returncode:
                pytest.skip(
                    f"launchd facility cannot bootstrap a minimal job: {admission.stderr.strip()}"
                )
        finally:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=COMPUTE_JOB_STATUS_TIMEOUT_SECONDS,
                check=False,
            )
    manifest.machines[0].compute = None
    if (backend_id == "launchd") != (os.uname().sysname == "Darwin"):
        pytest.skip("The automatic backend uses the execution machine's operating system")
    units = []

    def runner(command, **kwargs):
        if "systemd-run" in command:
            units.append(command[command.index("--unit") + 1])
        return subprocess.run(command, **kwargs)

    try:
        result = probe_compute_backend(manifest, "laptop", runner, data_dir=tmp_path)
    finally:
        for unit in units:
            for action in ("stop", "reset-failed"):
                subprocess.run(
                    [
                        "env",
                        f"XDG_RUNTIME_DIR=/run/user/{uid}",
                        "systemctl",
                        "--user",
                        action,
                        unit,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=COMPUTE_JOB_STATUS_TIMEOUT_SECONDS,
                    check=False,
                )
    if not result.ready and any(
        marker in result.diagnostic.casefold()
        for marker in ("operation not permitted", "permission denied")
    ):
        pytest.skip(f"{backend_id} facility blocked by execution sandbox: {result.diagnostic}")
    assert result.ready, result.diagnostic
    assert list((tmp_path / "jobs").iterdir()) == []
