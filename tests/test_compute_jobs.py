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

        def start(self, root, wrapper, request, context):
            assert (Path(root) / "command.json").is_file()
            assert not (Path(root) / "launch.json").exists()
            if self.failure:
                raise RuntimeError("launch refused")
            subprocess.run(["sh", wrapper], check=True)
            return "rcp-job-test"

        def alive(self, handle, context):
            return None if self.unknown else self.is_alive

    backend = Backend()
    monkeypatch.setattr(jobs, "resolve_context", lambda *_: (context, backend))
    monkeypatch.setitem(jobs.COMPUTE_BACKENDS, "launchd", backend)

    def launch(request=None):
        return jobs.launch_compute_job(
            store,
            manifest,
            request
            or ComputeLaunchRequest(
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


def test_launch_receipt_refresh_and_log(launch_environment, tmp_path, manifest):
    store, backend, launch = launch_environment
    record = launch()
    assert record.status == "running"
    assert record.label == "Training"
    assert store.compute_job(record.job_id) == record
    root = Path(record.job_root)
    assert root.parent == tmp_path / "data" / "jobs"
    assert json.loads((root / "command.json").read_text())["origin_operation_id"] == "turn"
    assert json.loads((root / "launch.json").read_text())["backend_handle"] == record.backend_handle
    assert (root / "log").read_text() == "hello world"
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
        Path(record.job_root, "cancelled").write_text("1757000000")
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    result = store.compute_job(record.job_id)
    assert result.status == ("cancelled" if cancelled else "lost")
    assert bool(result.diagnostic) is not cancelled


@pytest.mark.parametrize(
    "receipt",
    ["", "0", "0 123 extra", "nope 123", "-1 123", "256 123", "0 nope", "0 " + "9" * 100],
    ids=[
        "empty",
        "truncated",
        "extra-field",
        "non-numeric",
        "negative",
        "too-large",
        "bad-epoch",
        "epoch-overflow",
    ],
)
def test_gone_job_with_malformed_exit_stays_lost(launch_environment, tmp_path, manifest, receipt):
    store, backend, launch = launch_environment
    record = launch()
    Path(record.exit_path).write_text(receipt)
    result = jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
    assert result.status == "lost"
    assert result.exit_status is None
    assert result.ended_at
    assert "malformed exit receipt" in result.diagnostic
    assert record.exit_path in result.diagnostic
    backend.is_alive = True
    assert (
        jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
        == result
    )


def test_unknown_backend_stays_running_even_with_exit(launch_environment, tmp_path, manifest):
    store, backend, launch = launch_environment
    record = launch()
    backend.unknown = True
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    result = store.compute_job(record.job_id)
    assert result.status == "running"
    assert "could not determine" in result.diagnostic


def test_cancel_receipt_waits_for_owner_to_disappear(launch_environment, tmp_path, manifest):
    store, backend, launch = launch_environment
    record = launch()
    Path(record.exit_path).unlink()
    Path(record.job_root, "cancelled").write_text("1757000000")
    backend.is_alive = True
    running = jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
    assert running.status == "running"
    assert running.ended_at is None
    backend.unknown = True
    unknown = jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
    assert unknown.status == "running"
    assert unknown.diagnostic
    backend.unknown = False
    backend.is_alive = False
    cancelled = jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
    assert cancelled.status == "cancelled"
    assert cancelled.ended_at == jobs.epoch_timestamp("1757000000")
    assert cancelled.exit_status is None and cancelled.diagnostic is None
    Path(record.job_root, "cancelled").unlink()
    assert (
        jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
        == cancelled
    )


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
def test_changed_machine_refresh_uses_recorded_identity(
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

    monkeypatch.setattr(backend, "alive", alive)
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
    backend.is_alive = False
    Path(record.exit_path).unlink()
    Path(record.job_root, "cancelled").write_text("1757000000")
    result = jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
    assert result.status == "cancelled"
    assert len(observed) == 2
    for context in observed:
        assert context.execution_host == record.execution_host
        assert context.execution_machine == record.execution_machine
        assert context.containment == record.containment
        assert context.compute is None


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired("launch", 10), ComputeTransportError("connection dropped")],
)
def test_launchd_keeps_completed_job_receipts_after_uncertain_bootstrap(
    launch_environment, tmp_path, manifest, monkeypatch, failure
):
    from rcp.compute_jobs.backends.launchd import LaunchdBackend

    store, _, launch = launch_environment
    context, _ = jobs.resolve_context(manifest, "laptop")
    context.uid = "501"
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[:2] == ["launchctl", "bootstrap"]:
            subprocess.run(["sh", str(Path(command[-1]).with_name("run.sh"))], check=True)
            raise failure
        assert command[:2] == ["launchctl", "bootout"]
        return subprocess.CompletedProcess(command, 0, "", "")

    context.runner = runner
    monkeypatch.setattr(jobs, "resolve_context", lambda *_: (context, LaunchdBackend()))
    with pytest.raises(ComputeLaunchUncertainError) as error:
        launch()
    assert error.value.__cause__ is failure
    assert len(calls) == 2
    (root,) = (tmp_path / "data" / "jobs").iterdir()
    assert json.loads((root / "command.json").read_text())["origin_operation_id"] == "turn"
    assert (root / "log").read_text() == "hello world"
    assert (root / "exit").read_text().split()[0] == "0"
    assert not (root / "launch.json").exists()
    assert store.running_compute_jobs() == []


def test_launch_receipt_write_failure_still_records_the_accepted_job(
    launch_environment, tmp_path, monkeypatch
):
    store, backend, launch = launch_environment
    real_write = jobs.write_job_file

    def failing_write(context, path, text):
        if path.endswith("launch.json"):
            raise OSError("connection dropped")
        return real_write(context, path, text)

    monkeypatch.setattr(jobs, "write_job_file", failing_write)
    record = launch()
    stored = store.compute_job(record.job_id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.backend_handle == record.backend_handle
    assert "Launch receipt not written" in stored.diagnostic


def test_gone_job_with_malformed_started_receipt_still_exits(
    launch_environment, tmp_path, manifest
):
    store, backend, launch = launch_environment
    record = launch()
    Path(record.job_root, "started").write_text("nope")
    Path(record.exit_path).write_text("0 1757000000")
    result = jobs.refresh_compute_job(store, manifest, record.job_id, data_dir=tmp_path / "data")
    assert result.status == "exited"
    assert result.exit_status == 0


def test_local_backend_oserror_is_not_a_host_outage(
    launch_environment, tmp_path, manifest, monkeypatch
):
    store, backend, launch = launch_environment
    records = [launch() for _ in range(3)]
    calls = []

    def alive(handle, context):
        calls.append(handle)
        if len(calls) == 1:
            raise FileNotFoundError("launchctl")
        return False

    monkeypatch.setattr(backend, "alive", alive)
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    assert len(calls) == 3
    statuses = sorted(store.compute_job(record.job_id).status for record in records)
    assert statuses == ["exited", "exited", "running"]


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

    def unavailable_project(*args, **kwargs):
        raise OSError("Project history is offline")

    monkeypatch.setattr(app.state.catalog, "open", unavailable_project)
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


@pytest.mark.parametrize("failure", ["unregistered", "unknown", "invalid handle"])
def test_reconcile_row_failure_does_not_skip_other_jobs(
    launch_environment, tmp_path, manifest, monkeypatch, failure
):
    store, backend, launch = launch_environment
    backend.id = "unregistered" if failure == "unregistered" else "launchd"
    bad = launch()
    backend.id = "launchd"
    valid = [launch() for _ in range(3)]
    calls = []

    def observe(handle, context):
        calls.append(handle)
        if failure != "unregistered" and len(calls) == 1:
            if failure == "invalid handle":
                raise ValueError("invalid handle")
            return None
        return False

    monkeypatch.setattr(backend, "alive", observe)
    assert store.running_compute_jobs()[0].job_id == bad.job_id
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    bad = store.compute_job(bad.job_id)
    assert bad.status == "running"
    assert ("could not determine" if failure == "unknown" else failure) in bad.diagnostic
    assert len(calls) == (3 if failure == "unregistered" else 4)
    for record in valid:
        refreshed = store.compute_job(record.job_id)
        assert refreshed.status == "exited"
        assert refreshed.diagnostic is None


@pytest.mark.parametrize(
    "error",
    # A local OSError is row-specific; see test_local_backend_oserror_is_not_a_host_outage.
    [ComputeTransportError("connection dropped"), subprocess.TimeoutExpired("backend status", 10)],
)
def test_reconcile_contacts_unreachable_host_once_per_pass(
    launch_environment, tmp_path, manifest, monkeypatch, error
):
    store, backend, launch = launch_environment
    records = [launch() for _ in range(4)]
    calls = []

    def unreachable(handle, context):
        calls.append(handle)
        if len(calls) > 1:
            pytest.fail("reconciliation retried an unreachable host")
        raise error

    monkeypatch.setattr(backend, "alive", unreachable)
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    assert len(calls) == 1
    refreshed = [store.compute_job(record.job_id) for record in records]
    assert all(record.status == "running" for record in refreshed)
    assert all(record.diagnostic == refreshed[0].diagnostic for record in refreshed)
    assert refreshed[0].diagnostic == str(error)

    backend.is_alive = False
    monkeypatch.setattr(backend, "alive", lambda *_: False)
    reconcile_compute_jobs(store, manifest, project_id="project", data_dir=tmp_path / "data")
    assert all(store.compute_job(record.job_id).status == "exited" for record in records)
