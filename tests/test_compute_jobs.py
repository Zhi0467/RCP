from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from rcp.compute_jobs import jobs
from rcp.compute_jobs.backend_context import (
    BackendContext,
    ComputeLaunchUncertainError,
    ComputeTransportError,
)
from rcp.compute_jobs.models import ComputeLaunchRequest
from rcp.compute_jobs.reconcile import reconcile_compute_jobs
from rcp.compute_jobs.wrapper import render_wrapper
from rcp.config import MachineComputeConfig
from rcp.storage import AppStore
from tests.helpers import wait_until


@pytest.mark.parametrize("status", [0, 7])
def test_wrapper_quotes_arguments_and_records_exit(tmp_path, status):
    cwd = tmp_path / "working ' directory"
    cwd.mkdir()
    root = tmp_path / "job ' root"
    root.mkdir()
    text = "literal $(touch injected); ' spaces"
    request = ComputeLaunchRequest(
        argv=[
            "sh",
            "-c",
            'printf "%s\\n" "$1"; printf err >&2; exit "$2"',
            "sh",
            text,
            str(status),
        ],
        cwd=str(cwd),
    )
    wrapper = root / "run.sh"
    wrapper.write_text(render_wrapper(str(root), request))
    result = subprocess.run(["sh", str(wrapper)], check=False)
    assert result.returncode == status
    assert (root / "log").read_text() == text + "\nerr"
    assert not (cwd / "injected").exists()
    exit_status, ended = (root / "exit").read_text().split()
    assert int(exit_status) == status
    assert int(ended) >= int((root / "started").read_text())


def test_wrapper_records_failed_cd(tmp_path):
    request = ComputeLaunchRequest(argv=["touch", "should-not-exist"], cwd=str(tmp_path / "absent"))
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(render_wrapper(str(tmp_path), request))
    result = subprocess.run(["sh", str(wrapper)], check=False)
    assert result.returncode != 0
    assert int((tmp_path / "exit").read_text().split()[0]) == result.returncode
    assert not (tmp_path / "should-not-exist").exists()
    assert "absent" in (tmp_path / "log").read_text()


@pytest.fixture
def launch_environment(tmp_path, manifest, monkeypatch):
    store = AppStore(tmp_path / "database" / "rcp.sqlite3")
    machine = next(machine for machine in manifest.machines if not machine.host)
    context = BackendContext(execution_host="", execution_machine=machine.alias, compute=None)

    class Backend:
        id = "launchd"
        is_alive = False
        failure = False
        unknown = False
        cancelled = 0

        def start(self, root, wrapper, request, context):
            assert (Path(root) / "command.json").is_file()
            assert not (Path(root) / "launch.json").exists()
            if self.failure:
                raise RuntimeError("launch refused")
            subprocess.run(["sh", wrapper], check=True)
            return "rcp-job-test"

        def alive(self, handle, context):
            return None if self.unknown else self.is_alive

        def cancel(self, handle, context):
            self.cancelled += 1
            self.is_alive = False

    backend = Backend()
    monkeypatch.setattr(jobs, "resolve_context", lambda *_: (context, backend))
    monkeypatch.setitem(jobs.COMPUTE_BACKENDS, "launchd", backend)

    def launch():
        return jobs.launch_compute_job(
            store,
            manifest,
            ComputeLaunchRequest(
                argv=["printf", "hello world"], cwd=str(tmp_path), label="Training"
            ),
            data_dir=tmp_path / "data",
            project_id="project",
            origin_operation_id="turn",
            episode_id=None,
            execution_machine=machine.alias,
            writable_roots=[str(tmp_path)],
        )

    return store, backend, launch


def test_launch_receipt_refresh_and_bounded_log(launch_environment, tmp_path, manifest):
    store, backend, launch = launch_environment
    record = launch()
    assert record.status == "running"
    assert record.label == "Training"
    assert store.compute_job(record.job_id) == record
    root = Path(record.job_root)
    assert root.parent == tmp_path / "data" / "jobs"
    assert json.loads((root / "command.json").read_text())["origin_operation_id"] == "turn"
    assert json.loads((root / "launch.json").read_text())["backend_handle"] == record.backend_handle
    assert (
        jobs.read_job_log_tail(
            store, manifest, record.job_id, data_dir=tmp_path / "data", max_bytes=5
        )
        == "world"
    )
    result = jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
    assert result.status == "exited" and result.exit_status == 0
    assert result.started_at and result.ended_at
    assert (
        jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
        == result
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_reconcile_missing_exit(launch_environment, tmp_path, manifest, cancelled):
    store, backend, launch = launch_environment
    record = launch()
    Path(record.exit_path).unlink()
    if cancelled:
        store.request_compute_job_cancel(record.job_id, "human")
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    result = store.compute_job(record.job_id)
    assert result.status == ("cancelled" if cancelled else "lost")
    assert bool(result.diagnostic) is not cancelled


def test_unknown_backend_stays_running_even_with_exit(launch_environment, tmp_path, manifest):
    store, backend, launch = launch_environment
    record = launch()
    backend.unknown = True
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    result = store.compute_job(record.job_id)
    assert result.status == "running"
    assert "could not determine" in result.diagnostic


def test_cancel_records_first_requester_and_is_idempotent(launch_environment, tmp_path, manifest):
    store, backend, launch = launch_environment
    record = launch()
    backend.is_alive = True
    result = jobs.cancel_compute_job(
        store, manifest, record.job_id, "human-one", data_dir=tmp_path / "data"
    )
    assert result.status == "cancelled"
    again = jobs.cancel_compute_job(
        store, manifest, record.job_id, "human-two", data_dir=tmp_path / "data"
    )
    assert again.cancel_requested_by == "human-one"
    assert backend.cancelled == 1


def test_launch_without_backend_names_machine_and_setup_action(
    launch_environment, tmp_path, monkeypatch
):
    store, backend, launch = launch_environment
    monkeypatch.setattr(jobs, "resolve_context", lambda *_: (None, None))
    with pytest.raises(RuntimeError, match="laptop.*configure a compute backend"):
        launch()
    assert not (tmp_path / "data" / "jobs").exists()
    assert store.running_compute_jobs() == []


def test_failed_start_removes_root(launch_environment, tmp_path):
    store, backend, launch = launch_environment
    backend.failure = True
    with pytest.raises(RuntimeError, match="launch refused"):
        launch()
    assert list((tmp_path / "data" / "jobs").iterdir()) == []
    assert store.running_compute_jobs() == []


def test_insert_failure_retains_backend_receipt(launch_environment, tmp_path, monkeypatch):
    store, backend, launch = launch_environment

    def fail(record):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "create_compute_job", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        launch()
    roots = list((tmp_path / "data" / "jobs").iterdir())
    assert len(roots) == 1
    assert json.loads((roots[0] / "launch.json").read_text())["backend_handle"] == "rcp-job-test"


@pytest.mark.parametrize("change", ["removed", "repointed"])
@pytest.mark.parametrize("reconcile", [False, True])
def test_changed_machine_refresh_and_cancel_use_recorded_identity(
    launch_environment, tmp_path, manifest, monkeypatch, change, reconcile
):
    store, backend, launch = launch_environment
    context, _ = jobs.resolve_context(manifest, "laptop")
    context.execution_host = "recorded-host"
    context.compute = MachineComputeConfig(jobs_root=str(tmp_path / "remote-jobs"))
    context.containment = "mirrored"
    machine = manifest.machine_map[context.execution_machine]
    machine.host = context.execution_host
    machine.compute = context.compute

    def ssh_bridge(host, command):
        assert host == "recorded-host"
        return [sys.executable, *shlex.split(command)[1:]]

    monkeypatch.setattr("rcp.transport.ssh.ssh_arguments", ssh_bridge)
    record = launch()
    if change == "removed":
        manifest.machines.remove(machine)
    else:
        machine.host = "other-host"

    observed = []

    def alive(handle, context):
        observed.append(context)
        assert handle == record.backend_handle
        return backend.is_alive

    def cancel(handle, context):
        observed.append(context)
        assert handle == record.backend_handle
        backend.is_alive = False

    monkeypatch.setattr(backend, "alive", alive)
    monkeypatch.setattr(backend, "cancel", cancel)
    backend.is_alive = True
    if reconcile:
        reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
        result = store.compute_job(record.job_id)
    else:
        result = jobs.refresh_compute_job(
            store, manifest, record.job_id, data_dir=tmp_path / "data"
        )
    assert result.status == "running"
    assert result.diagnostic is None
    result = jobs.cancel_compute_job(
        store, manifest, record.job_id, "human", data_dir=tmp_path / "data"
    )
    assert result.status == "cancelled"
    assert len(observed) == 3
    for context in observed:
        assert context.execution_host == record.execution_host
        assert context.execution_machine == record.execution_machine
        assert context.containment == record.containment
        assert context.compute is None


@pytest.mark.parametrize(
    "backend_id, remote", [("ssh_session", True), ("slurm", True), ("slurm", False)]
)
def test_launch_exit_255_retains_root_only_for_remote_transport_failure(
    launch_environment, tmp_path, manifest, monkeypatch, backend_id, remote
):
    from rcp.transport.state import _remote_script

    store, _, launch = launch_environment
    context, _ = jobs.resolve_context(manifest, "laptop")
    context.execution_host = "worker" if remote else ""
    context.compute = MachineComputeConfig(jobs_root=str(tmp_path / "remote-jobs"))
    calls = []

    def runner(command, **kwargs):
        if remote:
            assert command[-2] == "worker"
            command = shlex.split(command[-1])
            if command[:3] == ["python3", "-c", _remote_script("remote_job_files.py")]:
                return subprocess.run([sys.executable, *command[1:]], **kwargs)
        calls.append(command)
        return subprocess.CompletedProcess(command, 255, "", "connection dropped")

    context.runner = runner
    monkeypatch.setattr(
        jobs, "resolve_context", lambda *_: (context, jobs.COMPUTE_BACKENDS[backend_id])
    )
    expected_error = ComputeLaunchUncertainError if remote else RuntimeError
    with pytest.raises(expected_error) as error:
        launch()
    assert len(calls) == 1
    roots = list((tmp_path / "remote-jobs" if remote else tmp_path / "data" / "jobs").iterdir())
    if remote:
        assert isinstance(error.value.__cause__, ComputeTransportError)
        assert len(roots) == 1
        assert (roots[0] / "run.sh").is_file()
        assert json.loads((roots[0] / "command.json").read_text())["origin_operation_id"] == "turn"
        assert not (roots[0] / "launch.json").exists()
    else:
        assert type(error.value) is RuntimeError
        assert roots == []
    assert store.running_compute_jobs() == []


def test_uncertain_launch_retains_intent(launch_environment, tmp_path):
    from rcp.compute_jobs.backend_context import ComputeLaunchUncertainError

    store, backend, launch = launch_environment

    def uncertain(*args):
        raise ComputeLaunchUncertainError("submission timed out")

    backend.start = uncertain
    with pytest.raises(ComputeLaunchUncertainError):
        launch()
    roots = list((tmp_path / "data" / "jobs").iterdir())
    assert len(roots) == 1
    assert json.loads((roots[0] / "command.json").read_text())["origin_operation_id"] == "turn"
    assert not (roots[0] / "launch.json").exists()


@pytest.mark.parametrize("fenced", [True, False])
def test_startup_reconciliation_does_not_block_health_or_watchers_and_respects_fence(
    tmp_path,
    fenced,
    manifest,
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from rcp.api.app import create_app
    from rcp.background import StartupEffectFence
    from rcp.compute_jobs.models import ComputeJobRecord

    backend = jobs.COMPUTE_BACKENDS["launchd"]
    observed = []
    entered = threading.Event()
    release = threading.Event()

    def alive(handle, context):
        observed.append(handle)
        entered.set()
        assert release.wait(10), "startup waited for compute reconciliation"
        return False

    monkeypatch.setattr(backend, "alive", alive)
    root = tmp_path / "jobs" / "startup"
    root.mkdir(parents=True)
    (root / "started").write_text("100")
    (root / "exit").write_text("0 101")
    app = create_app(
        str(manifest.path),
        data_dir=tmp_path / f"app-{fenced}",
        startup_effect_fence=StartupEffectFence("test") if fenced else None,
    )
    store = app.state.catalog.store
    record = ComputeJobRecord(
        job_id="startup",
        project_id=app.state.default_project_id,
        origin_operation_id="turn",
        execution_machine="laptop",
        backend_id="launchd",
        backend_handle="rcp-job-startup",
        job_root=str(root),
        cwd=str(tmp_path),
        argv=["true"],
        log_path=str(root / "log"),
        exit_path=str(root / "exit"),
        created_at=store.now(),
    )
    store.create_compute_job(record)
    with TestClient(app) as client:
        try:
            assert client.get("/api/health").status_code == 200
            assert store.compute_job("startup").status == "running"
            if not fenced:
                assert entered.wait(5)
                assert app.state.watcher_poller.is_running()
        finally:
            release.set()
        if not fenced:
            wait_until(lambda: store.compute_job("startup").status == "exited")
    assert observed == ([] if fenced else ["rcp-job-startup"])


def test_shipped_file_operations_use_execution_machine(tmp_path, monkeypatch):
    import shlex
    import sys

    from rcp.compute_jobs.files import (
        prepare_job_root,
        read_job_file,
        remove_job_root,
        resolve_jobs_root,
        write_job_file,
    )
    from rcp.config import MachineComputeConfig

    calls = []

    def ssh_bridge(host, command):
        assert host == "test-execution-account"
        calls.append(shlex.split(command))
        return [sys.executable, *shlex.split(command)[1:]]

    monkeypatch.setattr("rcp.transport.ssh.ssh_arguments", ssh_bridge)
    context = BackendContext(
        execution_host="test-execution-account",
        execution_machine="remote",
        compute=MachineComputeConfig(jobs_root=str(tmp_path / "remote-jobs")),
    )
    root = Path(resolve_jobs_root(context, tmp_path / "unused-local-data")) / "one"
    request = ComputeLaunchRequest(argv=["printf", "remote"], cwd=str(tmp_path))
    prepare_job_root(
        context, str(root), request, project_id="p", origin_operation_id="t", episode_id=None
    )
    assert json.loads((root / "command.json").read_text())["argv"] == request.argv
    assert read_job_file(context, str(root / "missing")) is None
    write_job_file(context, str(root / "log"), "abcdef")
    assert read_job_file(context, str(root / "log"), 3) == "def"
    with pytest.raises(ValueError, match="limit"):
        read_job_file(context, str(root / "log"), 0)
    remove_job_root(context, str(root))
    assert not root.exists()
    assert all(command[1] == "-c" for command in calls)


def test_reconcile_contacts_unreachable_host_once_per_pass(
    launch_environment, tmp_path, manifest, monkeypatch
):
    store, backend, launch = launch_environment
    records = [launch() for _ in range(4)]
    calls = []

    def unreachable(handle, context):
        calls.append(handle)
        if len(calls) > 1:
            pytest.fail("reconciliation retried an unreachable host")
        raise subprocess.TimeoutExpired("backend status", 10)

    monkeypatch.setattr(backend, "alive", unreachable)
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    assert len(calls) == 1
    refreshed = [store.compute_job(record.job_id) for record in records]
    assert all(record.status == "running" for record in refreshed)
    assert all(record.diagnostic == refreshed[0].diagnostic for record in refreshed)
    assert "timed out" in refreshed[0].diagnostic

    backend.is_alive = False
    monkeypatch.setattr(backend, "alive", lambda *_: False)
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    assert all(store.compute_job(record.job_id).status == "exited" for record in records)
