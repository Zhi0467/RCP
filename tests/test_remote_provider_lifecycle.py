from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import subprocess
import sys
from contextlib import aclosing
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
def transported_launcher(monkeypatch):
    from rcp.agents import launcher as launcher_module

    # These tests isolate launch/fallback ownership; the execution-host wrapper
    # has its own real subprocess transport tests in test_turn_journal.py.
    monkeypatch.setattr(launcher_module, "journal_command", lambda command, **kwargs: command)
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

    def confirm(host, pid_file, started_at):
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
        assert events[-1].event == "error"
        assert "fallback is blocked" in events[-1].text


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


@pytest.mark.asyncio
async def test_transport_loss_terminates_a_turn_no_route_will_collect(
    tmp_path, monkeypatch, transported_launcher
):
    """A journalled launch nothing can adopt keeps the pre-collection ending.

    Discuss, seed and refresh, and episode reports all run journalled on a
    remote stage, so preservation would otherwise follow from the journal alone.
    Their result is never collected, so a survivor would only hold the stage
    fence against recovery -- alive, uncollectable, and with no Stop control.
    """

    from rcp.agents import AgentProcessControl

    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); sys.exit(255)",
        ],
    )
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda host, pid: False)
    terminated = []
    monkeypatch.setattr(
        AgentProcessControl,
        "_confirm_remote_stopped",
        lambda host, pid, started_at: terminated.append((host, pid)),
    )
    pid_file = str(tmp_path / "provider.pid")
    async for _event in transported_launcher.stream(
        "codex",
        "prompt",
        cwd=tmp_path,
        capability="scratch_patch",
        host="fixture",
        remote_pid_file=pid_file,
    ):
        pass

    assert terminated == [("fixture", pid_file)]


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_state", [False, None])
@pytest.mark.parametrize("result", ["pending", "complete", "error"])
async def test_remote_exit_distinguishes_transport_loss_from_provider_error(
    tmp_path, monkeypatch, transported_launcher, remote_state, result
):
    from rcp.agents import AgentProcessControl

    script = "import sys; sys.stdin.read(); print('{}'); "
    if result == "complete":
        script += "print(" + repr(json.dumps({"type": "turn.completed"})) + "); "
    elif result == "error":
        script += (
            "print(" + repr(json.dumps({"type": "error", "message": "Provider failed."})) + "); "
        )
    script += "sys.exit(255)"
    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [sys.executable, "-c", script],
    )
    probes = []
    monkeypatch.setattr(
        AgentProcessControl,
        "remote_stopped",
        lambda host, pid: probes.append((host, pid)) or remote_state,
    )

    def unexpected_termination(*args):
        if result == "error":
            return True
        pytest.fail("Losing SSH must not terminate the authorized remote turn")

    monkeypatch.setattr(AgentProcessControl, "_confirm_remote_stopped", unexpected_termination)
    pid_file = str(tmp_path / "provider.pid")
    events = [
        event
        async for event in transported_launcher.stream(
            "codex",
            "prompt",
            cwd=tmp_path,
            capability="scratch_patch",
            host="fixture",
            remote_pid_file=pid_file,
            preserve_on_transport_loss=True,
        )
    ]
    assert probes == [("fixture", pid_file)]
    receipt = json.loads(next(event.text for event in events if event.event == "provider_exit"))
    assert receipt["remote_process_stopped"] is remote_state
    assert receipt["return_code"] == 255
    # Recorded only when the link, not the provider, ended the turn.
    assert receipt.get("delivery_lost", False) is (result != "error")
    assert not any(event.event == "runtime_fallback" for event in events)


@pytest.mark.asyncio
async def test_a_machine_without_python_names_the_prerequisite_not_the_shell(
    tmp_path, monkeypatch, transported_launcher
):
    """RCP's one unchecked prerequisite should read as a one-line fix.

    A machine is configured by naming its provider binary, so that CLI was set up
    on purpose. `python3` was not: RCP needs it for the stage, journal, and
    process helpers, and a host without it answers with the shell's own
    `command not found` and nothing a human can act on.
    """

    from rcp.agents import AgentProcessControl

    script = (
        "import sys; sys.stderr.write('bash: line 1: python3: command not found\\n'); sys.exit(127)"
    )
    monkeypatch.setattr(
        transported_launcher, "_command", lambda *args, **kwargs: [sys.executable, "-c", script]
    )
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args: True)

    events = [
        event
        async for event in transported_launcher.stream(
            "codex",
            "prompt",
            cwd=tmp_path,
            capability="scratch_patch",
            host="fixture",
            remote_pid_file=str(tmp_path / "provider.pid"),
        )
    ]

    failure = next(event for event in events if event.event == "error")
    assert "no python3 on PATH" in failure.text
    assert "fixture" in failure.text
    assert "command not found" not in failure.text


@pytest.mark.asyncio
async def test_the_python_prerequisite_is_named_before_a_probe_that_needs_python(
    tmp_path, monkeypatch, transported_launcher
):
    """The probe that would report the missing interpreter is itself that interpreter.

    Asked on a host with no `python3`, it can only answer that the process state
    is unknown, and an unknown state reports as blocked recovery -- the one
    message that hides the prerequisite from the human who can install it. The
    shell already said what was missing, so nothing needs to be asked.
    """

    from rcp.agents import AgentProcessControl

    script = (
        "import sys; sys.stderr.write('bash: line 1: python3: command not found\\n'); sys.exit(127)"
    )
    monkeypatch.setattr(
        transported_launcher, "_command", lambda *args, **kwargs: [sys.executable, "-c", script]
    )
    for probe in ("remote_stopped", "_confirm_remote_stopped"):
        monkeypatch.setattr(
            AgentProcessControl,
            probe,
            lambda *_args, **_kwargs: pytest.fail("A host with no python3 cannot answer a probe"),
        )

    events = [
        event
        async for event in transported_launcher.stream(
            "codex",
            "prompt",
            cwd=tmp_path,
            capability="scratch_patch",
            host="fixture",
            remote_pid_file=str(tmp_path / "provider.pid"),
        )
    ]

    failure = next(event for event in events if event.event == "error")
    assert "no python3 on PATH" in failure.text
    assert "Recovery is blocked" not in failure.text
    # The pass must settle too, or the stage stays fenced against the retry that
    # follows the human installing the interpreter.
    exit_event = next(event for event in events if event.event == "provider_exit")
    assert json.loads(exit_event.text)["remote_process_stopped"] is True


@pytest.mark.asyncio
async def test_a_consumer_that_stops_reading_does_not_leave_the_provider_running(
    tmp_path, monkeypatch, transported_launcher
):
    """Preservation is for a link that ended the turn, not for any early exit.

    A consumer can stop reading for reasons of its own -- a continuation that
    refused the session it was handed, an error on the way -- and the turn then
    fails as something other than a lost link. Nothing will offer to collect it
    and nothing will offer to stop it, so a survivor would hold the stage fence
    against every later recovery with no control left to end it.
    """

    from rcp.agents import AgentProcessControl

    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [
            sys.executable,
            "-c",
            # Speaks once the prompt has landed, then holds the group open.
            "import sys, json, time; sys.stdin.read(); "
            + "print(json.dumps({'type': 'thread.started', 'thread_id': 'native'}), flush=True); "
            + "time.sleep(30)",
        ],
    )
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args: None)
    terminated = []
    monkeypatch.setattr(
        AgentProcessControl,
        "_confirm_remote_stopped",
        lambda host, pid, _started: terminated.append((host, pid)) or True,
    )
    pid_file = str(tmp_path / "provider.pid")

    stream = transported_launcher.stream(
        "codex",
        "prompt",
        cwd=tmp_path,
        capability="scratch_patch",
        host="fixture",
        remote_pid_file=pid_file,
        preserve_on_transport_loss=True,
    )
    async with aclosing(stream):
        async for event in stream:
            # Past the prompt, which is what the old condition took as reason
            # enough on its own to leave the group running.
            if event.event == "session":
                break

    assert terminated == [("fixture", pid_file)]


@pytest.mark.asyncio
async def test_a_pass_is_not_called_delivered_before_its_prompt_is_written(
    tmp_path, monkeypatch, transported_launcher
):
    """Scheduling the write is not the write, and a stop can land between them.

    The delivery event is what a consumer persists as "there is a turn on the
    host to come back for". Creating the feeder task only queues the write, so
    announcing delivery there would let a stop in that window record a pass
    whose wrapper was handed nothing, and send recovery to collect it.
    """

    from rcp.agents import AgentProcessControl
    from rcp.agents import launcher as launcher_module

    monkeypatch.setattr(
        transported_launcher,
        "_command",
        lambda *args, **kwargs: [sys.executable, "-c", "import sys; sys.stdin.read()"],
    )
    monkeypatch.setattr(AgentProcessControl, "_confirm_remote_stopped", lambda *_args: True)
    order: list[str] = []
    write_stdin = launcher_module._write_stdin

    async def recorded(stream, data, **kwargs):
        order.append("write")
        await write_stdin(stream, data, **kwargs)

    monkeypatch.setattr(launcher_module, "_write_stdin", recorded)
    pid_file = str(tmp_path / "provider.pid")

    stream = transported_launcher.stream(
        "codex",
        "prompt",
        cwd=tmp_path,
        capability="scratch_patch",
        host="fixture",
        remote_pid_file=pid_file,
    )
    async with aclosing(stream):
        async for event in stream:
            if event.event == "remote_prompt_delivered":
                order.append("delivered")
                break

    assert order == ["write", "delivered"]


@pytest.mark.asyncio
async def test_a_pipe_that_was_already_gone_delivers_no_pass(tmp_path, monkeypatch):
    """A swallowed write is not a delivery, however far the launch otherwise got."""

    from rcp.agents import AgentProcessControl
    from rcp.agents import launcher as launcher_module

    launcher = AgentLauncher()
    monkeypatch.setattr(launcher_module, "journal_command", lambda command, **kwargs: command)
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
    monkeypatch.setattr(
        launcher,
        "_command",
        lambda *args, **kwargs: [sys.executable, "-c", "import sys; sys.stdin.read()"],
    )
    monkeypatch.setattr(AgentProcessControl, "_confirm_remote_stopped", lambda *_args: True)

    write_stdin = launcher_module._write_stdin

    async def refused(stream, data, **kwargs):
        class _Closed:
            def write(self, _payload):
                raise BrokenPipeError

        await write_stdin(_Closed(), data, **kwargs)

    monkeypatch.setattr(launcher_module, "_write_stdin", refused)

    events = [
        event.event
        async for event in launcher.stream(
            "codex",
            "prompt",
            cwd=tmp_path,
            capability="scratch_patch",
            host="fixture",
            remote_pid_file=str(tmp_path / "provider.pid"),
        )
    ]

    assert "remote_process_start" in events
    assert "remote_prompt_delivered" not in events
