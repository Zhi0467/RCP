from __future__ import annotations

import os
import shlex
import subprocess

import pytest

from rcp.compute_jobs import jobs
from rcp.compute_jobs.backend_context import BackendContext
from rcp.compute_jobs.files import prepare_job_root
from rcp.compute_jobs.models import ComputeJobRecord, ComputeLaunchRequest


@pytest.mark.parametrize("backend_id", ["systemd_user", "launchd"])
def test_shipped_helper_shell_watcher_checks_and_cancels_without_launch_receipt(
    tmp_path, backend_id
):
    root = tmp_path / "job with spaces"
    state = tmp_path / "owner-state"
    state.write_text("active")
    binary = tmp_path / "bin"
    binary.mkdir()
    systemctl = binary / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = stop ]; then echo inactive > "$OWNER_TEST_STATE"; exit 0; fi\n'
        'state=$(cat "$OWNER_TEST_STATE")\n'
        'if [ "$state" = unknown ]; then echo disconnected >&2; exit 1; fi\n'
        'echo "ActiveState=$state"\n'
    )
    systemctl.chmod(0o700)
    launchctl = binary / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = bootout ]; then echo inactive > "$OWNER_TEST_STATE"; exit 0; fi\n'
        'state=$(cat "$OWNER_TEST_STATE")\n'
        'if [ "$state" = unknown ]; then echo disconnected >&2; exit 1; fi\n'
        'if [ "$state" = active ]; then echo "state = running"; '
        'else echo "state = not running"; fi\n'
    )
    launchctl.chmod(0o700)
    request = ComputeLaunchRequest(argv=["true"], cwd=str(tmp_path))
    prepare_job_root(
        BackendContext(execution_host="", execution_machine="local", compute=None),
        str(root),
        request,
        project_id="project",
        origin_operation_id="turn",
        episode_id=None,
    )
    record = ComputeJobRecord(
        job_id="job",
        project_id="project",
        origin_operation_id="turn",
        execution_machine="local",
        backend_id=backend_id,
        backend_handle="rcp-job-test",
        job_root=str(root),
        cwd=str(tmp_path),
        argv=["true"],
        log_path=str(root / "log"),
        exit_path=str(root / "exit"),
        created_at="2026-09-06T00:00:00Z",
    )
    spec = jobs.helper_watch_spec(record)
    assert spec["cwd"] == str(root)
    environment = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "OWNER_TEST_STATE": str(state),
    }

    def run(field):
        return subprocess.run(
            shlex.split(spec[field]), env=environment, capture_output=True, text=True, timeout=15
        )

    assert not (root / "launch.json").exists()
    assert run("check_command").returncode == 1
    state.write_text("unknown")
    assert run("check_command").returncode == 2
    state.write_text("inactive")
    missing = run("check_command")
    assert missing.returncode == 2 and "without a valid exit receipt" in missing.stderr
    (root / "exit").write_text("7 100")
    assert run("check_command").returncode == 0
    (root / "exit").unlink()
    state.write_text("active")
    assert run("cancel_command").returncode == 0
    assert (root / "cancelled").is_file()
    assert run("check_command").returncode == 0
