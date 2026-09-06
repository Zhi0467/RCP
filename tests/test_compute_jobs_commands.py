from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import validate_command_request
from rcp.agents.write_scope import ProjectWriteScope
from rcp.background import AgentTaskExecution
from rcp.compute_jobs import jobs
from rcp.compute_jobs.backend_context import BackendContext, ComputeProbeStaleError
from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest
from rcp.runs.tasks import compute_commands
from rcp.runs.tasks.compute_commands import WorkComputeCommands
from rcp.storage import AgentTaskRecord, AppStore


def _request(verb, key, **arguments):
    return validate_command_request(
        json.dumps(
            {
                "mailbox_id": "a" * 32,
                "request_id": uuid.uuid4().hex,
                "credential": "b" * 64,
                "verb": verb,
                "idempotency_key": key,
                "arguments": arguments,
            }
        )
    )


@pytest.fixture
def commands(tmp_path, manifest, monkeypatch):
    store = AppStore(tmp_path / "data" / "rcp.sqlite3")
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="work-turn",
            project_id="project",
            kind="project_chat",
            status="running",
            request={},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Running",
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = workspace / "protected"
    protected.mkdir()
    scope = ProjectWriteScope.create(
        project_id="project",
        execution_machine="laptop",
        execution_host="",
        capability="work_auto",
        stage_root=str(workspace),
        workspace_root=str(workspace),
        repositories=[],
        protected_write_paths=[str(protected)],
    )
    context = BackendContext(execution_host="", execution_machine="laptop", compute=None)

    class Backend:
        id = "launchd"

        def __init__(self):
            self.starts = []
            self.alive_handles = set()
            self.cancels = []

        def start(self, root, wrapper, request, context):
            handle = Path(root).name
            self.starts.append((request, tuple(context.writable_roots), context.containment))
            self.protected_paths = context.protected_paths
            self.alive_handles.add(handle)
            (Path(root) / "started").write_text("100")
            (Path(root) / "log").write_text("compute output\n")
            return handle

        def alive(self, handle, context):
            return handle in self.alive_handles

        def cancel(self, handle, context):
            self.cancels.append(handle)
            self.alive_handles.discard(handle)

    backend = Backend()
    monkeypatch.setattr(jobs, "resolve_context", lambda *_: (context, backend))
    monkeypatch.setitem(jobs.COMPUTE_BACKENDS, "launchd", backend)
    probe = ComputeBackendProbe(
        execution_machine="laptop",
        backend_id="launchd",
        state="ready",
        ready=True,
        diagnostic="Ready",
        containment="mirrored",
        status_label="Ready",
        status_tone="ready",
    )
    probe_calls = []

    def probe_backend(*args, **kwargs):
        probe_calls.append((args, kwargs))
        return probe

    monkeypatch.setattr(compute_commands, "probe_compute_backend", probe_backend)
    execution = AgentTaskExecution(
        operation_id="work-turn",
        store=store,
        control=AgentProcessControl(),
    )
    handler = WorkComputeCommands(execution, manifest, scope, None, None)
    identity = CommandTurnIdentity(None, execution.operation_id, "turn", "broker")

    def launch(key="launch-once", **overrides):
        return handler(
            _request(
                "launch",
                key,
                **{
                    "cwd": str(workspace),
                    "argv": ["true"],
                    **overrides,
                },
            ),
            identity,
        )

    return SimpleNamespace(
        store=store,
        backend=backend,
        handler=handler,
        identity=identity,
        launch=launch,
        probe=probe,
        probe_calls=probe_calls,
        workspace=workspace,
        protected=protected,
    )


def test_compute_command_launch_cancel_idempotency_survives_store_reopen(commands):
    first = commands.launch()
    assert first.status == "ok", first.message
    job_id = first.result["job_id"]
    job = commands.store.compute_job(job_id)
    assert first.result == {
        "job_id": job_id,
        "log_path": job.log_path,
        "backend_id": "launchd",
    }
    assert job.origin_operation_id == "work-turn"
    assert job.containment == "mirrored"
    assert commands.backend.starts[0][1] == (str(commands.workspace), job.job_root)
    assert commands.backend.protected_paths == tuple(
        commands.handler.write_scope.protected_write_paths
    )
    assert str(commands.protected) in commands.backend.protected_paths
    assert len(commands.probe_calls) == 1

    reopened = AppStore(commands.store.path)
    handler = replace(
        commands.handler, execution=replace(commands.handler.execution, store=reopened)
    )
    retry = handler(
        _request("launch", "launch-once", cwd=str(commands.workspace), argv=["true"]),
        commands.identity,
    )
    assert retry.result == first.result
    assert len(commands.backend.starts) == 1
    assert commands.launch(argv=["false"]).status == "invalid"
    for _ in range(2):
        cancelled = handler(_request("cancel", "cancel-once", job_id=job_id), commands.identity)
        assert cancelled.status == "ok"
        assert cancelled.result["status"] == "cancelled"
    assert commands.backend.cancels == [job_id]
    assert reopened.compute_job(job_id).cancel_requested_by == "work-turn"
    with reopened.connection() as connection:
        events = connection.execute(
            "SELECT message FROM graph_run_events WHERE operation_id = ?", ("work-turn",)
        ).fetchall()
        receipts = connection.execute(
            "SELECT category FROM graph_run_receipts "
            "WHERE operation_id = ? AND category LIKE 'compute%'",
            ("work-turn",),
        ).fetchall()
    assert len(events) == 5
    assert sum("Replayed: True" in row[0] for row in events) == 3
    assert sorted(row[0] for row in receipts) == [
        "compute_command_result",
        "compute_command_result",
        "compute_command_started",
        "compute_command_started",
    ]


def test_compute_launch_key_remains_durable_after_diagnostic_retention(commands):
    first = commands.launch()
    from rcp.limits import AGENT_TASK_RECEIPT_RETENTION_COUNTS

    for _ in range(AGENT_TASK_RECEIPT_RETENTION_COUNTS["diagnostic"] + 1):
        commands.store.record_agent_task_receipt(
            "work-turn", "other-diagnostic", {}, tier="diagnostic"
        )
    assert commands.launch().result == first.result
    assert len(commands.backend.starts) == 1


def test_compute_status_refreshes_exit_and_rejects_other_project(commands):
    job_id = commands.launch().result["job_id"]
    job = commands.store.compute_job(job_id)
    Path(job.exit_path).write_text("7 103")
    commands.backend.alive_handles.remove(job_id)
    result = commands.handler(_request("job_status", "read-once", job_id=job_id), commands.identity)
    assert result.status == "ok"
    assert result.result == {
        "status": "exited",
        "exit_status": 7,
        "started_at": "1970-01-01T00:01:40+00:00",
        "ended_at": "1970-01-01T00:01:43+00:00",
        "log_tail": "compute output\n",
    }
    foreign = job.model_copy(update={"job_id": "other-project-job", "project_id": "other"})
    commands.store.create_compute_job(foreign)
    for verb in ("job_status", "cancel"):
        for target in (foreign.job_id, "missing"):
            refused = commands.handler(
                _request(verb, f"{verb}-{target}", job_id=target), commands.identity
            )
            assert refused.status == "invalid"
            assert "this project" in refused.message
    assert not commands.backend.cancels


@pytest.mark.parametrize("stored", [False, True])
def test_compute_not_ready_probe_is_rerun_at_launch_until_ready(commands, monkeypatch, stored):
    unavailable = commands.probe.model_copy(
        update={
            "ready": False,
            "state": "unavailable",
            "diagnostic": "No user manager",
            "required_action": "Enable the user manager",
            "status_tone": "error",
        }
    )
    if stored:
        commands.store.record_compute_backend_probe("project", unavailable)
    results = [unavailable, commands.probe]
    calls = []

    def probe(*_args, **_kwargs):
        calls.append(1)
        return results[len(calls) - 1]

    monkeypatch.setattr(compute_commands, "probe_compute_backend", probe)
    first = commands.launch("first")
    assert first.status == "unavailable"
    assert first.message == "No user manager"
    assert first.result["required_action"] == "Enable the user manager"
    assert AppStore(commands.store.path).compute_backend_probe("project", "laptop") == unavailable
    assert not commands.backend.starts
    # The manager became available: the stored failure is re-run, not trusted forever.
    second = commands.launch("second")
    assert second.status == "ok"
    assert calls == [1, 1]
    assert (
        AppStore(commands.store.path).compute_backend_probe("project", "laptop") == commands.probe
    )
    assert len(commands.backend.starts) == 1


def test_compute_launch_reprobes_stale_backend_once(commands):
    commands.store.record_compute_backend_probe(
        "project", commands.probe.model_copy(update={"backend_id": "ssh_session"})
    )
    response = commands.launch()
    assert response.status == "ok", response.message
    assert response.result["backend_id"] == "launchd"
    assert len(commands.backend.starts) == 1
    assert len(commands.probe_calls) == 1
    assert (
        AppStore(commands.store.path).compute_backend_probe("project", "laptop") == commands.probe
    )
    assert commands.launch().result == response.result
    assert len(commands.probe_calls) == 1
    assert len(commands.backend.starts) == 1


def test_compute_launch_does_not_loop_when_reprobe_is_also_stale(commands, monkeypatch):
    commands.store.record_compute_backend_probe(
        "project", commands.probe.model_copy(update={"backend_id": "ssh_session"})
    )
    monkeypatch.setattr(commands.backend, "id", "systemd_user")
    response = commands.launch()
    assert response.status == "invalid"
    assert "does not match" in response.message
    assert len(commands.probe_calls) == 1
    assert not commands.backend.starts


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"backend_id": "ssh_session"}, ComputeProbeStaleError),
        ({"execution_machine": "other"}, ComputeProbeStaleError),
        ({"ready": False}, ValueError),
    ],
)
def test_compute_launch_distinguishes_stale_probe_from_not_ready(commands, updates, error):
    with pytest.raises(error) as raised:
        jobs.launch_compute_job(
            commands.store,
            commands.handler.manifest,
            ComputeLaunchRequest(cwd=str(commands.workspace), argv=["true"]),
            data_dir=commands.handler.data_dir,
            project_id="project",
            origin_operation_id="work-turn",
            episode_id=None,
            execution_machine="laptop",
            writable_roots=commands.handler.write_scope.writable_roots,
            probe=commands.probe.model_copy(update=updates),
        )
    assert type(raised.value) is error
    assert not commands.backend.starts
    assert not commands.store.running_compute_jobs()


@pytest.mark.parametrize("target", ["outside", "symlink", "protected", "missing"])
def test_compute_launch_rejects_cwd_outside_effective_writable_scope(commands, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = commands.workspace / "escape"
    link.symlink_to(outside, target_is_directory=True)
    cwd = {
        "outside": outside,
        "symlink": link,
        "protected": commands.protected,
        "missing": commands.workspace / "missing",
    }[target]
    response = commands.launch(cwd=str(cwd))
    assert response.status == "invalid", response.message
    assert not commands.probe_calls
    assert not commands.backend.starts


def test_compute_handoff_requires_only_this_turns_still_running_jobs(commands):
    commands.handler.validate_handoff(set())
    job_id = commands.launch().result["job_id"]
    with pytest.raises(ValueError, match=job_id):
        commands.handler.validate_handoff(set())
    commands.handler.validate_handoff({job_id})
    job = commands.store.compute_job(job_id)
    commands.store.create_compute_job(
        job.model_copy(
            update={
                "job_id": "other-turn-job",
                "origin_operation_id": "another-turn",
            }
        )
    )
    Path(job.exit_path).write_text("0 101")
    commands.backend.alive_handles.remove(job_id)
    commands.handler.validate_handoff(set())
    assert commands.store.compute_job(job_id).status == "exited"


@pytest.mark.parametrize(
    "identity",
    [
        CommandTurnIdentity(None, "work-turn", "turn", "validate_only"),
        CommandTurnIdentity(None, "other-task", "turn", "broker"),
    ],
)
def test_compute_commands_require_the_owning_work_broker(commands, identity):
    response = commands.handler(
        _request("launch", "key", cwd=str(commands.workspace), argv=["true"]), identity
    )
    assert response.status == "invalid"
    assert not commands.backend.starts


def test_compute_receipts_replay_large_argv_and_escaped_log_tail(commands):
    from rcp.limits import COMPUTE_COMMAND_LOG_TAIL_MAX_BYTES

    argv = ["printf", "argument" * 2_000]
    first = commands.launch(argv=argv)
    assert first.status == "ok", first.message
    job = commands.store.compute_job(first.result["job_id"])
    Path(job.log_path).write_bytes(b"\x01" * (COMPUTE_COMMAND_LOG_TAIL_MAX_BYTES * 2))
    request = _request("job_status", "status-with-escaped-log", job_id=job.job_id)
    status = commands.handler(request, commands.identity)
    assert status.status == "ok"
    assert status.result["log_tail"] == "\x01" * COMPUTE_COMMAND_LOG_TAIL_MAX_BYTES
    reopened = replace(
        commands.handler,
        execution=replace(commands.handler.execution, store=AppStore(commands.store.path)),
    )
    Path(job.log_path).write_text("later output")
    assert reopened(request, commands.identity).result == status.result
    assert commands.launch(argv=argv).result == first.result
    assert len(commands.backend.starts) == 1


def test_compute_uncertain_launch_never_repeats_backend_submission(commands, monkeypatch):
    from rcp.compute_jobs.backend_context import ComputeLaunchUncertainError

    submitted = []

    def start(*args):
        submitted.append(args)
        raise ComputeLaunchUncertainError("Submission outcome unknown")

    monkeypatch.setattr(commands.backend, "start", start)
    first = commands.launch()
    assert first.status == "unavailable"
    assert commands.launch().model_dump(exclude={"request_id"}) == first.model_dump(
        exclude={"request_id"}
    )
    assert len(submitted) == 1
    roots = list((commands.store.path.parent / "jobs").iterdir())
    assert len(roots) == 1
    assert (roots[0] / "command.json").is_file()


def test_compute_launch_uses_experiment_turn_lineage_and_existing_probe(commands, monkeypatch):
    commands.store.record_compute_backend_probe("project", commands.probe)

    def unexpected_probe(*args, **kwargs):
        raise AssertionError("The saved ready probe should be reused")

    monkeypatch.setattr(compute_commands, "probe_compute_backend", unexpected_probe)
    handler = replace(commands.handler, episode_id="experiment-episode")
    response = handler(
        _request("launch", "experiment-launch", cwd=str(commands.workspace), argv=["true"]),
        replace(commands.identity, episode_id="experiment-episode"),
    )
    assert response.status == "ok", response.message
    job = commands.store.compute_job(response.result["job_id"])
    assert job.project_id == "project"
    assert job.episode_id == "experiment-episode"
    assert job.origin_operation_id == "work-turn"
    assert job.execution_machine == "laptop"
    assert job.execution_host == ""
