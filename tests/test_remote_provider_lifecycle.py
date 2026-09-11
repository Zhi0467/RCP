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
    assert result.stdout.strip() == "provider finished"
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
        launcher_module, "ssh_arguments", lambda host, command: shlex.split(command)
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
        lambda host, pid_file: confirmations.append((host, pid_file)) or True,
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

    def confirm(host, pid_file):
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
