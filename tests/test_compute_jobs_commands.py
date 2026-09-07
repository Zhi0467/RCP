from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import time
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
from rcp.compute_jobs.backend_context import BackendContext
from rcp.compute_jobs.models import ComputeBackendProbe
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
        id = "systemd_user"

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
    monkeypatch.setitem(jobs.COMPUTE_BACKENDS, "systemd_user", backend)
    probe = ComputeBackendProbe(
        execution_machine="laptop",
        backend_id="systemd_user",
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

    def check_runner(spec, execution_host="", timeout_seconds=10):
        from rcp.watchers import WatcherCheckResult

        parts = shlex.split(spec.check_command)
        handle = Path(parts[1]).parent.name
        return WatcherCheckResult(
            state="active" if backend.alive(handle, context) else "complete",
            checked_at=store.now(),
            exit_code=1 if backend.alive(handle, context) else 0,
        )

    def job_for(response):
        return next(
            job
            for job in store.running_compute_jobs()
            if job.log_path == response.result["watcher"]["log_path"]
        )

    return SimpleNamespace(
        check_runner=check_runner,
        job_for=job_for,
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


def test_helper_launch_replays_its_shell_handoff_after_store_reopen(commands):
    first = commands.launch()
    assert first.status == "ok", first.message
    job = commands.job_for(first)
    assert first.result == {"watcher": jobs.helper_watch_spec(job)}
    assert set(first.result["watcher"]) == {"check_command", "cancel_command", "cwd", "log_path"}
    assert job.origin_operation_id == "work-turn"
    assert commands.backend.starts[0][1] == (str(commands.workspace), job.job_root)
    assert commands.backend.protected_paths == tuple(
        commands.handler.write_scope.protected_write_paths
    )
    handler = replace(
        commands.handler,
        execution=replace(commands.handler.execution, store=AppStore(commands.store.path)),
    )
    retry = handler(
        _request("launch", "launch-once", cwd=str(commands.workspace), argv=["true"]),
        commands.identity,
    )
    assert retry.result == first.result
    assert len(commands.backend.starts) == len(commands.probe_calls) == 1
    assert commands.launch(argv=["false"]).status == "invalid"


@pytest.mark.asyncio
async def test_slow_remote_launch_returns_before_client_deadline_and_replays(commands, monkeypatch):
    from rcp import limits
    from rcp.compute_jobs import backend_context
    from rcp.compute_jobs import probe as probe_module
    from rcp.compute_jobs.backends.systemd_user import SystemdUserBackend
    from rcp.config import MachineComputeConfig
    from rcp.runs import patch_validator
    from rcp.transport.remote_job_files import operate
    from rcp.transport.workspace_mailbox import RunStageMailbox

    # Exercise the real sequential helper and staged-client path at 50x speed.
    # Only SSH latency/OS responses are simulated; job files and receipts are real.
    speed = 50
    elapsed = 0.0
    calls = []
    launches = []
    probe_roots = {}
    original_monotonic = time.monotonic

    def delay(seconds):
        nonlocal elapsed
        elapsed += seconds
        time.sleep(seconds / speed)

    def runner(command, *, timeout, **_kwargs):
        argv = shlex.split(command[-1])
        calls.append(argv)
        polling = "ActiveState" in argv or (
            argv[:2] == ["python3", "-c"] and argv[-3] == "read" and argv[-2].endswith("/exit")
        )
        delay(min(1, timeout * 0.8) if polling else timeout * 0.8)
        code, output, error = 0, "", ""
        if argv == ["uname", "-s"]:
            output = "Linux"
        elif argv == ["id", "-u"]:
            output = "501"
        elif argv[:2] == ["python3", "-c"]:
            output = json.dumps(operate(*argv[-3:]))
        elif "systemd-run" in argv:
            root = Path(argv[-1]).parent
            if root.name.startswith("probe-"):
                probe_roots["rcp-job-" + root.name] = (root, 0)
                if "PrivateUsers=yes" in argv:
                    code, error = 1, "Unsupported PrivateUsers"
            else:
                launches.append(root)
                (root / "log").write_text("launched exactly once\n")
        elif "ActiveState" in argv:
            root, polls = probe_roots[argv[-1]]
            probe_roots[argv[-1]] = (root, polls + 1)
            output = "ActiveState=active" if polls == 0 else "ActiveState=inactive"
            if polls:
                (root / "exit").write_text("0 12345\n")
                (root / "log").write_text("rcp-probe\n")
        return subprocess.CompletedProcess(command, code, output, error)

    machine = commands.handler.manifest.machines[0]
    machine.host = "rcp@slow-compute.example"
    machine.compute = MachineComputeConfig(jobs_root=str(commands.workspace / "jobs"))
    monkeypatch.setattr(
        probe_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: original_monotonic() * speed,
            sleep=lambda seconds: time.sleep(seconds / speed),
        ),
    )
    monkeypatch.setattr(
        compute_commands,
        "probe_compute_backend",
        lambda manifest, alias, **kwargs: probe_module.probe_compute_backend(
            manifest, alias, runner=runner, **kwargs
        ),
    )
    resolutions = 0

    def resolve_launch_context(manifest, alias):
        nonlocal resolutions
        resolutions += 1
        context, backend = backend_context.resolve_context(manifest, alias, runner)
        # Exercise the existing fresh-probe retry before any real launch is attempted.
        return context, SimpleNamespace(id="changed-owner") if resolutions == 1 else backend

    monkeypatch.setattr(jobs, "resolve_context", resolve_launch_context)
    monkeypatch.setitem(jobs.COMPUTE_BACKENDS, "systemd_user", SystemdUserBackend())

    def canonical_directories(paths, *, require_writable):
        assert require_writable
        delay(limits.REMOTE_RUN_STAGE_COMMAND_TIMEOUT_SECONDS * 0.8)
        return {path: str(Path(path).resolve()) for path in paths}, str(commands.workspace)

    handler = replace(
        commands.handler,
        remote_stage=SimpleNamespace(canonical_directories=canonical_directories),
    )
    monkeypatch.setattr(
        patch_validator,
        "COMPUTE_COMMAND_TIMEOUT_SECONDS",
        limits.COMPUTE_COMMAND_TIMEOUT_SECONDS / speed,
    )
    staged = patch_validator.stage_patch_validation_mailbox(
        local_stage=commands.workspace,
        remote_stage=None,
        task_id=handler.execution.operation_id,
        turn_id="slow-remote-launch",
        timeout_seconds=0.1,
        authority="broker",
    )
    assert staged.invocation_gate is not None
    assert staged.invocation_gate.response_timeout_seconds > staged.timeout_seconds
    # Remote delivery includes a workspace listing and request/response transfers.
    for name in ("entry_names", "read_text", "write_text"):
        original = getattr(RunStageMailbox, name)

        def remote_io(mailbox, *args, _original=original, **kwargs):
            if mailbox is staged.mailbox:
                delay(limits.REMOTE_RUN_STAGE_COMMAND_TIMEOUT_SECONDS * 0.8)
            return _original(mailbox, *args, **kwargs)

        monkeypatch.setattr(RunStageMailbox, name, remote_io)

    stop = asyncio.Event()
    async with staged.invocation_gate.serve_current_session():
        server = asyncio.create_task(
            patch_validator.serve_patch_validation_mailbox(
                staged=staged,
                execution=handler.execution,
                validate=lambda _document: None,
                stop=stop,
                budget=patch_validator.PatchValidationBudget(),
                command_handler=handler,
            )
        )
        try:
            responses = []
            for _attempt in range(2):
                process = await asyncio.create_subprocess_exec(
                    *staged.client_argv(
                        "launch",
                        "--key",
                        "slow-launch",
                        "--cwd",
                        str(commands.workspace),
                        "--",
                        "true",
                    ),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                output, _ = await process.communicate()
                assert process.returncode == 0, output.decode()
                responses.append(json.loads(output))
                if len(responses) == 1:
                    assert elapsed > 120
                    assert elapsed < limits.COMPUTE_COMMAND_TIMEOUT_SECONDS
                    first_call_count = len(calls)
            assert responses[0]["result"] == responses[1]["result"]
            assert len(calls) == first_call_count
            assert len(launches) == 1
            assert resolutions == 2
            assert len(probe_roots) == 4
        finally:
            stop.set()
            await server
    staged.cleanup()


def test_compute_launch_key_survives_diagnostic_retention(commands):
    from rcp.limits import AGENT_TASK_RECEIPT_RETENTION_COUNTS

    first = commands.launch()
    for _ in range(AGENT_TASK_RECEIPT_RETENTION_COUNTS["diagnostic"] + 1):
        commands.store.record_agent_task_receipt(
            "work-turn", "other-diagnostic", {}, tier="diagnostic"
        )
    assert commands.launch().result == first.result
    assert len(commands.backend.starts) == 1


def test_each_new_helper_launch_checks_current_readiness(commands, monkeypatch):
    commands.store.record_compute_backend_probe("project", commands.probe)
    unavailable = commands.probe.model_copy(
        update={
            "ready": False,
            "state": "failed",
            "diagnostic": "No user manager",
            "required_action": "Enable user manager",
            "status_tone": "error",
        }
    )
    monkeypatch.setattr(compute_commands, "probe_compute_backend", lambda *a, **kw: unavailable)
    first = commands.launch("first")
    assert first.status == "unavailable"
    assert first.result["required_action"] == "Enable user manager"
    assert not commands.backend.starts
    monkeypatch.setattr(compute_commands, "probe_compute_backend", lambda *a, **kw: commands.probe)
    assert commands.launch("second").status == "ok"
    assert commands.store.compute_backend_probe("project", "laptop").ready


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
    assert not commands.probe_calls and not commands.backend.starts


def test_helper_handoff_required_only_while_this_turns_job_is_running(commands):
    commands.handler.validate_handoff(set())
    response = commands.launch()
    job = commands.job_for(response)
    check = response.result["watcher"]["check_command"]
    with pytest.raises(ValueError, match="shell watchers"):
        commands.handler.validate_handoff(set())
    commands.handler.validate_handoff({check})
    commands.store.create_compute_job(
        job.model_copy(update={"job_id": "other-turn-job", "origin_operation_id": "another-turn"})
    )
    Path(job.exit_path).write_text("0 101")
    commands.backend.alive_handles.remove(job.job_id)
    commands.handler.validate_handoff(set())
    assert commands.store.compute_job(job.job_id).status == "exited"


def test_retry_cannot_abandon_a_running_helper_from_its_failed_parent(commands):
    response = commands.launch()
    parent = commands.store.agent_task("work-turn")
    commands.store.create_agent_task(
        parent.model_copy(
            update={"operation_id": "retry", "parent_operation_id": "work-turn", "attempt": 2}
        )
    )
    handler = replace(
        commands.handler,
        execution=replace(commands.handler.execution, operation_id="retry", continuation="retry"),
    )
    with pytest.raises(ValueError, match="shell watchers"):
        handler.validate_handoff(set())
    handler.validate_handoff({response.result["watcher"]["check_command"]})


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
    assert len(roots) == 1 and (roots[0] / "command.json").is_file()


def test_slurm_instructions_authorize_direct_submission_without_helper(commands):
    from rcp.config import MachineComputeConfig

    manifest = commands.handler.manifest.model_copy(deep=True)
    manifest.machine_map["laptop"].compute = MachineComputeConfig(job_manager="slurm")
    handler = replace(commands.handler, manifest=manifest)
    assert not handler.allowed_verbs
    prose = handler.execution_instructions("secret-helper-command")
    assert "Submit directly" in prose and "10 minutes" in prose
    assert "secret-helper-command" not in prose
    response = handler(
        _request("launch", "key", cwd=str(commands.workspace), argv=["true"]), commands.identity
    )
    assert response.status == "invalid"
    assert not commands.probe_calls and not commands.backend.starts


def test_inline_short_work_needs_no_watcher(commands):
    commands.handler.validate_handoff(set())
    prose = commands.handler.execution_instructions("helper launch")
    assert "Short compute jobs can run normally without a watcher" in prose
    assert "not an enforced time limit" in prose
    assert "`helper launch`" in prose
