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
def transported_launcher(monkeypatch):
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
            supervise_remote=True,
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
