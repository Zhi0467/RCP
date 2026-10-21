from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher, ProviderReadiness
from rcp.agents.launcher import REMOTE_PROVIDER_START_LINE


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("setsid") is None,
    reason="Requires Linux util-linux setsid process-group semantics",
)
@pytest.mark.parametrize("exit_code", [0, 7])
def test_remote_wrapper_waits_for_forked_provider_and_preserves_exit(
    tmp_path: Path, exit_code: int
) -> None:
    pid_file = tmp_path / "agent.pid"
    command = AgentLauncher._remote_login_command(
        [sys.executable, "-c", f"print('provider finished'); raise SystemExit({exit_code})"],
        pid_file=str(pid_file),
        cwd=tmp_path,
    )
    # Interactive job control makes setsid a process-group leader, forcing it
    # to fork. Exercise that same condition without reading login startup files.
    payload = shlex.split(command)[2]
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-m", "-c", payload],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == exit_code
    # The wrapper announces the provider's start before the provider speaks.
    assert result.stdout.split() == [REMOTE_PROVIDER_START_LINE, "provider", "finished"]
    assert int(pid_file.read_text()) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "turn.completed", "turn.failed"])
async def test_codex_exec_requires_terminal_protocol_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str | None
) -> None:
    wire_events = [
        {"type": "thread.started", "thread_id": "session-test"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "I will inspect the task contract."},
        },
    ]
    if terminal is not None:
        wire_events.append({"type": terminal})
    script = "import sys\nsys.stdin.read()\n" + "\n".join(
        f"print({json.dumps(event)!r}, flush=True)" for event in wire_events
    )
    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda *args, **kwargs: ProviderReadiness(
            provider="codex", installed=True, authenticated=True
        ),
    )
    monkeypatch.setattr(
        launcher, "_command", lambda *args, **kwargs: [sys.executable, "-c", script]
    )

    async def collect():
        return [
            event
            async for event in launcher.stream(
                "codex", "inspect the task", cwd=tmp_path, capability="scratch_patch"
            )
        ]

    events = await asyncio.wait_for(collect(), timeout=10)
    exit_evidence = json.loads(
        next(event.text for event in events if event.event == "provider_exit")
    )
    assert exit_evidence["return_code"] == 0
    assert exit_evidence["explicit_terminal_event"] is (terminal is not None)
    if terminal == "turn.completed":
        assert events[-1].event == "done"
        assert not any(event.event == "error" for event in events)
    else:
        assert not any(event.event == "done" for event in events)
        assert any(event.event == "error" for event in events)
        if terminal is None:
            assert "before the turn completed" in events[-1].text


@pytest.fixture
def transported_launcher(monkeypatch, tmp_path):
    from rcp.agents import launcher as launcher_module

    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda *args, **kwargs: ProviderReadiness(
            provider="codex", installed=True, authenticated=True
        ),
    )
    monkeypatch.setattr(
        launcher, "_remote_login_command", lambda command, **kwargs: shlex.join(command)
    )
    monkeypatch.setattr(
        launcher_module, "ssh_arguments", lambda host, command, **kwargs: shlex.split(command)
    )
    from rcp.transport.state import _remote_turn_supervisor_script

    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(_remote_turn_supervisor_script(), encoding="utf-8")
    original_stream = launcher.stream

    def stream(*args, **kwargs):
        kwargs.setdefault("supervisor_path", str(supervisor))
        return original_stream(*args, **kwargs)

    monkeypatch.setattr(launcher, "stream", stream)
    return launcher


@pytest.mark.asyncio
async def test_closing_at_runtime_checkpoint_awaits_remote_and_local_cleanup(
    tmp_path, monkeypatch, transported_launcher
):
    from rcp.agents import AgentProcessControl

    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    confirmations = []
    monkeypatch.setattr(
        AgentProcessControl,
        "_confirm_remote_stopped",
        lambda host, pid_file, started_at: confirmations.append((host, pid_file)) or True,
    )
    control = AgentProcessControl()
    pid_file = str(tmp_path / "agent.pid")
    stream = transported_launcher.stream(
        "codex",
        "prompt",
        cwd=tmp_path,
        capability="scratch_patch",
        runtime_id="exec",
        host="test-host",
        remote_pid_file=pid_file,
        control=control,
    )
    assert (await anext(stream)).event == "remote_process_start"
    assert (await anext(stream)).event == "runtime"
    process = control._process
    assert process is not None
    await stream.aclose()
    assert confirmations == [("test-host", pid_file)]
    assert process.returncode is not None
    assert control._process is None


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_preprompt_fallback_requires_remote_exit_confirmation(
    tmp_path, monkeypatch, transported_launcher, confirmed
):
    from types import SimpleNamespace

    from rcp.agents import AgentProcessControl
    from rcp.providers import ProviderRuntimeStep, ProviderStreamEvent, ProviderTurn, profile_for

    # This test supplies a synthetic runtime without a real wire protocol. The
    # supervisor's actual wire fence is exercised by the journal journey below.
    monkeypatch.setattr(
        "rcp.agents.launcher._supervised_remote_turn_command", lambda command, **_kwargs: command
    )

    actions = []

    class TestTurn(ProviderTurn):
        requires_protocol_completion = True

        def __init__(self):
            self.initial_input_delivers_prompt = bool(actions)
            actions.append("launch")
            self.command = [sys.executable, "-c", "import sys; sys.stdin.read(); print('{}')"]

        def initial_input(self):
            return b""

        def receive_line(self, line):
            return ProviderRuntimeStep(
                events=(ProviderStreamEvent(event="raw", text=line),),
                complete=True,
            )

    runtime = SimpleNamespace(id="test-runtime", turn=lambda request: TestTurn())
    profile = profile_for("codex")
    monkeypatch.setattr(profile, "runtime", lambda runtime_id: runtime)
    monkeypatch.setattr(profile, "runtime_candidates", lambda configured: (runtime, runtime))

    confirm_flags: list[bool] = []

    def confirm(host, pid_file, started_at, *, launch_ended=False):
        confirm_flags.append(launch_ended)
        actions.append("confirm")
        return confirmed

    monkeypatch.setattr(AgentProcessControl, "_confirm_remote_stopped", confirm)
    events = [
        event
        async for event in transported_launcher.stream(
            "codex",
            "prompt",
            cwd=tmp_path,
            capability="scratch_patch",
            host="test-host",
            remote_pid_file=str(tmp_path / "agent.pid"),
        )
    ]
    # Only a confirmation taken after the launch exited may read a missing
    # pidfile as a pass that never started.
    assert confirm_flags[0] is True
    starts = [event.text for event in events if event.event == "remote_process_start"]
    stops = [event.text for event in events if event.event == "remote_process_stop"]
    if confirmed:
        assert starts == [str(tmp_path / "agent.pid"), str(tmp_path / "agent.pid.1")]
        assert stops == starts[:1]
        assert actions == ["launch", "confirm", "launch", "confirm"]
        assert any(event.event == "runtime_fallback" for event in events)
        assert events[-1].event == "done"
    else:
        assert len(starts) == 1
        assert stops == []
        assert actions.count("launch") == 1
        assert not any(event.event == "runtime_fallback" for event in events)
        # The cleanup confirmation races a live SSH process, so it stays
        # conservative: absence there is not a pass that never started.
        assert confirm_flags[-1] is False
        assert events[-1].event == "error"
        assert "fallback is blocked" in events[-1].text
        assert "closed its provider runtime before accepting the turn" in events[-1].text


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_provider_error_settles_confirmed_remote_pass_before_early_consumer_close(
    tmp_path, monkeypatch, transported_launcher, confirmed
):
    from rcp.agents import AgentProcessControl

    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [
            sys.executable,
            "-c",
            'import sys; sys.stdin.read(); print(\'{"type":"turn.failed"}\')',
        ],
    )
    confirmations = []
    monkeypatch.setattr(
        AgentProcessControl,
        "_confirm_remote_stopped",
        lambda *args: confirmations.append(confirmed) or confirmed,
    )
    stream = transported_launcher.stream(
        "codex",
        "prompt",
        cwd=tmp_path,
        capability="scratch_patch",
        runtime_id="exec",
        host="test-host",
        remote_pid_file=str(tmp_path / "agent.pid"),
    )
    received = []
    try:
        async for event in stream:
            received.append(event)
            if event.event == "error":
                assert confirmations
                stopped = [item.text for item in received if item.event == "remote_process_stop"]
                assert stopped == ([str(tmp_path / "agent.pid")] if confirmed else [])
                assert not any(item.event == "provider_exit" for item in received)
                break
        else:
            pytest.fail("Expected the provider error")
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_remote_process_reservation_precedes_spawn_and_spawn_failure_settles_it(
    tmp_path, monkeypatch, transported_launcher
):
    launches = []

    async def fail_spawn(*args, **kwargs):
        launches.append(args)
        raise OSError("fixture process could not be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    pid_file = str(tmp_path / "agent.pid")
    stream = transported_launcher.stream(
        "codex",
        "prompt",
        cwd=tmp_path,
        capability="scratch_patch",
        runtime_id="exec",
        host="test-host",
        remote_pid_file=pid_file,
    )
    try:
        started = await anext(stream)
        assert started.event == "remote_process_start"
        assert started.text == pid_file
        assert launches == []
        stopped = await anext(stream)
        assert len(launches) == 1
        assert stopped.event == "remote_process_stop"
        assert stopped.text == pid_file
        failed = await anext(stream)
        assert failed.event == "error"
        assert "could not be spawned" in failed.text
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_supervised_transport_loss_leaves_the_remote_pass_for_reconciliation(
    tmp_path, monkeypatch, transported_launcher
):
    """A lost SSH process is not permission to kill or repeat accepted work."""

    from rcp.providers import profile_for
    from rcp.transport.state import _remote_turn_supervisor_script

    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); sys.exit(255)",
        ],
    )
    pid_file = str(tmp_path / "agent.pid")
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(_remote_turn_supervisor_script(), encoding="utf-8")
    events = [
        event
        async for event in transported_launcher.stream(
            "codex",
            "prompt",
            cwd=tmp_path,
            capability="scratch_patch",
            host="test-host",
            remote_pid_file=pid_file,
            runtime_id=profile_for("codex").legacy_runtime_id,
            supervisor_path=str(supervisor),
        )
    ]

    assert [event.event for event in events][-2:] == ["provider_exit", "remote_result_pending"]
    evidence = json.loads(events[-2].text)
    assert evidence["delivery_lost"] is True
    assert evidence["remote_process_stopped"] is None
    assert not any(event.event in {"error", "remote_process_stop"} for event in events)
    assert (Path(pid_file + ".turn") / "accepted.json").is_file()


@pytest.mark.asyncio
async def test_pause_of_supervised_remote_turn_does_not_wait_for_reconciliation(
    tmp_path, monkeypatch, transported_launcher
):
    from rcp.agents import AgentProcessControl
    from rcp.agents import launcher as launcher_module

    # Stand in for the local SSH client: deliver a provider event, then remain
    # connected until Pause signals it. The remote stop is observed separately.
    monkeypatch.setattr(
        launcher_module, "_supervised_remote_turn_command", lambda command, **kwargs: command
    )
    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(); "
            'print(\'{"type":"thread.started","thread_id":"paused-session"}\', flush=True); '
            "time.sleep(30)",
        ],
    )
    stops = []
    monkeypatch.setattr(
        AgentProcessControl,
        "_terminate_remote_after_hold",
        lambda *args, **kwargs: stops.append(args[:2]) or True,
    )
    monkeypatch.setattr(AgentProcessControl, "_confirm_remote_stopped", lambda *a, **kw: True)
    control = AgentProcessControl()

    async def collect():
        events = []
        async for event in transported_launcher.stream(
            "codex",
            "prompt",
            cwd=tmp_path,
            capability="scratch_patch",
            host="test-host",
            remote_pid_file=str(tmp_path / "agent.pid"),
            runtime_id="exec",
            supervisor_path="/stage/supervisor.py",
            control=control,
        ):
            events.append(event)
            if event.event == "session":
                control.request_pause()
        return events

    events = await asyncio.wait_for(collect(), timeout=10)
    evidence = json.loads(next(event.text for event in events if event.event == "provider_exit"))
    assert evidence["return_code"] < 0
    assert stops
    assert events[-1].event == "paused"
    assert evidence["remote_process_stopped"] is True
    assert not any(event.event in {"remote_result_pending", "error"} for event in events)


@pytest.mark.asyncio
async def test_provider_turn_rides_the_master_of_its_own_run(
    tmp_path, monkeypatch, transported_launcher
):
    """The longest call of a run must not inherit a stranger's connection."""

    from rcp.agents import launcher as launcher_module

    partitions: list[str | None] = []

    def capture(host, command, **kwargs):
        partitions.append(kwargs.get("partition"))
        return shlex.split(command)

    monkeypatch.setattr(launcher_module, "ssh_arguments", capture)
    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [sys.executable, "-c", "print('{}')"],
    )

    async for _event in transported_launcher.stream(
        "codex",
        "prompt",
        cwd=tmp_path,
        capability="scratch_patch",
        runtime_id="exec",
        host="test-host",
        remote_pid_file=str(tmp_path / "agent.pid"),
        transport_partition="test-host:/tmp/rcp-run.op-one",
    ):
        pass

    # The turn opens the run's own master. Everything after it is cleanup that
    # has to reach the host precisely when that master may already be gone, so
    # it deliberately stays on the shared one.
    assert partitions[0] == "test-host:/tmp/rcp-run.op-one"
    assert set(partitions[1:]) <= {None}


@pytest.mark.parametrize(
    ("host", "delivered", "human_pause"),
    [
        ("test-host", True, False),
        ("", True, False),
        ("test-host", False, False),
        ("test-host", True, True),
    ],
)
def test_shutdown_and_pause_settle_the_right_pass(
    tmp_path, monkeypatch, transported_launcher, host, delivered, human_pause
):
    import hashlib
    from unittest.mock import Mock

    from rcp.agents import AgentEvent, AgentProcessControl
    from rcp.background import BackgroundAgentTasks
    from rcp.runs import remote_finalization, shared
    from rcp.runs.tasks.work import WORK_FINALIZATION_CONTEXT_ROLE
    from rcp.transport import RemoteRunStage

    from .helpers import wait_until
    from .test_background import _admitted_launch_task, _sse, _store
    from .test_remote_reconciliation import _journal

    store = _store(tmp_path)
    pause = Mock(wraps=store.request_agent_task_pause)
    monkeypatch.setattr(store, "request_agent_task_pause", pause)
    monkeypatch.setattr(
        transported_launcher,
        "readiness",
        lambda *a, **kw: ProviderReadiness(
            provider="codex", installed=True, authenticated=True, version="0.153.4"
        ),
    )
    stage = Mock(spec=RemoteRunStage, root=tmp_path)
    stage.list_workspace_files.return_value = []
    monkeypatch.setattr(shared, "_stage_or_reuse_task_input", lambda *a: "/stage/supervisor.py")
    monkeypatch.setattr(shared, "require_remote_provider_quiescence", lambda *a: None)
    # A real local process stands in for SSH; host stop and journal reads are
    # independent inputs, so killing the client cannot imply provider death.
    ready = tmp_path / "client-ready"
    spawn = asyncio.create_subprocess_exec

    async def spawn_client(*args, **kwargs):
        return await spawn(
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path; sys.stdin.buffer.readline(); "
            f"Path({str(ready)!r}).touch(); "
            'print(\'{"type":"thread.started","thread_id":"kept-session"}\', flush=True); '
            "time.sleep(30)",
            **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_client)
    stop = Mock(return_value=True)
    confirm = Mock(return_value=True)
    monkeypatch.setattr(AgentProcessControl, "_terminate_remote_after_hold", stop)
    monkeypatch.setattr(AgentProcessControl, "_confirm_remote_stopped", confirm)
    mailbox = Mock()

    async def stream(_project_id, _kind, request, execution):
        execution.checkpoint_stage(host, str(tmp_path))
        execution.command_mailbox = mailbox
        mailbox.execution = execution
        async for frame in shared._stream_agent_events(
            transported_launcher,
            request,
            "prompt",
            workspace=tmp_path,
            session_id=None,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            execution_host=host,
            execution=execution,
            remote_stage=stage if host else None,
            capability="scratch_patch",
            outcome=shared._ProviderOutcome(),
            binary=None,
        ):
            yield frame

    tasks = BackgroundAgentTasks(store, stream)
    task = _admitted_launch_task(
        store,
        operation_id="quit-turn",
        record_updates={
            "runtime_id": "codex.exec-json.v1" if delivered else "codex.app-server-stdio.v1"
        },
    )
    store.record_agent_task_contract(
        task.operation_id, WORK_FINALIZATION_CONTEXT_ROLE, "{}", hashlib.sha256(b"{}").hexdigest()
    )
    tasks.launch_admitted(task.operation_id)
    try:
        wait_until(
            lambda: (
                (current := store.agent_task(task.operation_id)) is not None
                and current.native_session_id == "kept-session"
                if delivered
                else ready.exists()
            )
        )
        if human_pause:
            tasks.pause(task.operation_id)
            wait_until(tasks.runtime_is_idle)
        tasks.shutdown()
        assert tasks.runtime_is_idle()
        record = store.agent_task(task.operation_id)
        assert record is not None
        detached = bool(host and delivered and not human_pause)
        if not detached:
            assert record.status == "paused"
            assert record.phase != "awaiting_remote_result"
            if host:
                assert stop.called
                assert confirm.called
                assert store.unresolved_remote_provider_passes(host, str(tmp_path)) == []
            return

        assert record.status == "running"
        assert record.phase == "awaiting_remote_result"
        stop.assert_not_called()
        confirm.assert_not_called()
        mailbox.detach.assert_called_once()
        mailbox.suspend.assert_called()
        passes = store.unresolved_remote_provider_passes(host, str(tmp_path))
        assert len(passes) == 1
        operation_id, pid_file = passes[0]
        assert operation_id == task.operation_id
        pause.assert_not_called()

        # Drive the real planner and journal validation across a new manager's
        # startup. Only remote observations and the owner's effects are fixtures.
        finished = False
        journal = _journal(events=json.dumps({"type": "turn.completed"}) + "\n")
        journal["outcome"]["pid_file"] = pid_file
        journal["accepted"]["pid_file"] = pid_file
        plan = remote_finalization.plan_remote_reconciliation
        monkeypatch.setattr(
            remote_finalization,
            "plan_remote_reconciliation",
            lambda store: plan(store, stopped=lambda *a: finished, read_journal=lambda *a: journal),
        )
        finalized = []

        async def recorded_stream(_project_id, _kind, _request, execution, recorded):
            finalized.append((execution.operation_id, recorded.pid_file))
            yield _sse(AgentEvent(event="answer", text="Recovered answer."))
            yield _sse(AgentEvent(event="done"))

        restarted = BackgroundAgentTasks(store, stream, recorded_stream=recorded_stream)
        monkeypatch.setattr(restarted, "_schedule_remote_reconciliation", lambda **kw: None)
        restarted.recover_at_startup()
        recovered = store.agent_task(operation_id)
        assert recovered is not None and recovered.phase == "awaiting_remote_result"
        assert restarted._reconcile_remote_results() is True
        assert finalized == []
        finished = True
        assert restarted._reconcile_remote_results() is False
        assert finalized == [(operation_id, pid_file)]
        settled = store.agent_task(operation_id)
        assert settled is not None and settled.status == "succeeded"
        assert settled.result == {"messages": ["Recovered answer."]}
        assert store.unresolved_remote_provider_passes(host, str(tmp_path)) == []
        assert len(store.agent_tasks("project", include_hidden=True)) == 1
    finally:
        tasks.shutdown()
