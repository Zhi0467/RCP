from __future__ import annotations

import subprocess
import sys
from contextlib import aclosing
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher, ProviderReadiness

_SANDBOX_REASON = "sandbox required but unavailable: bubblewrap (bwrap) not installed"


def _claude_binary(tmp_path: Path, *, authenticated: bool = True, sandbox: bool = False) -> Path:
    binary = tmp_path / "claude"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('2.1.266 (Claude Code)')\n"
        "elif args == ['auth', 'status']:\n"
        f"    print(json.dumps({{'loggedIn': {authenticated!r}}}))\n"
        "elif '--settings' in args:\n"
        "    settings = json.loads(args[args.index('--settings') + 1])\n"
        "    assert settings['sandbox']['enabled']\n"
        "    assert settings['sandbox']['failIfUnavailable']\n"
        "    assert args[args.index('--input-format') + 1] == 'stream-json'\n"
        "    assert args[args.index('--permission-mode') + 1] == 'dontAsk'\n"
        "    assert sys.stdin.read() == ''\n"
        f"    with pathlib.Path({str(tmp_path / 'probes')!r}).open('a') as f: f.write('probe\\n')\n"
        f"    if not {sandbox!r}:\n"
        "        print('bash: no job control in this shell', file=sys.stderr)\n"
        f"        print({_SANDBOX_REASON!r}, file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "else:\n"
        "    command = json.loads(sys.stdin.readline())\n"
        "    print(json.dumps({'type':'result', 'result':'Discuss works',"
        "'user_message_uuids':[command['uuid']]}), flush=True)\n"
    )
    binary.chmod(0o755)
    return binary


@pytest.mark.parametrize("sandbox", [False, True])
def test_work_probe_is_empty_stdin_cached_and_refreshable(tmp_path: Path, sandbox: bool) -> None:
    binary = _claude_binary(tmp_path, sandbox=sandbox)
    launcher = AgentLauncher()

    readiness = launcher.readiness("claude", binary=str(binary))
    assert readiness.authenticated
    assert readiness.work_like_available is sandbox
    # A Work-only fault never becomes the general readiness reason: `reason` also
    # carries benign notes, so folding them together makes a usable Discuss
    # provider read as broken.
    assert readiness.reason is None
    if sandbox:
        assert readiness.work_like_reason is None
    else:
        assert _SANDBOX_REASON in readiness.work_like_reason
        assert "job control" not in readiness.work_like_reason
    assert launcher.readiness("claude", binary=str(binary)) == readiness
    assert (tmp_path / "probes").read_text().splitlines() == ["probe"]
    launcher.readiness("claude", binary=str(binary), refresh=True)
    assert (tmp_path / "probes").read_text().splitlines() == ["probe", "probe"]


def test_unauthenticated_provider_does_not_probe_sandbox(tmp_path: Path) -> None:
    binary = _claude_binary(tmp_path, authenticated=False)
    readiness = AgentLauncher().readiness("claude", binary=str(binary))
    assert not readiness.authenticated
    assert readiness.work_like_available is None
    assert readiness.work_like_reason is None
    assert not (tmp_path / "probes").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["work_auto", "orchestrate"])
async def test_failed_work_readiness_refuses_before_launch_but_discuss_runs(
    tmp_path: Path, capability: str
) -> None:
    binary = _claude_binary(tmp_path)
    launcher = AgentLauncher()
    errors = [
        event
        async for event in launcher.stream(
            "claude", "Do work", binary=str(binary), cwd=tmp_path, capability=capability
        )
    ]
    assert len(errors) == 1
    assert errors[0].event == "error"
    assert _SANDBOX_REASON in errors[0].text

    events = [
        event
        async for event in launcher.stream(
            "claude", "Discuss", binary=str(binary), cwd=tmp_path, capability="discuss"
        )
    ]
    assert [event.text for event in events if event.event == "answer"] == ["Discuss works"]
    assert not any(event.event == "error" for event in events)
    assert (tmp_path / "probes").read_text().splitlines() == ["probe"]


@pytest.mark.parametrize("host", ["", "compute.example"])
def test_probe_transport_failure_is_not_a_missing_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    binary = _claude_binary(tmp_path)
    launcher = AgentLauncher()

    def probe(_host, command):
        if "--input-format" in command:
            return subprocess.CompletedProcess(command, 255, "", "connection failed")
        if command[-2:] == ["auth", "status"]:
            return subprocess.CompletedProcess(command, 0, '{"loggedIn":true}', "")
        return subprocess.CompletedProcess(command, 0, "2.1.266", "")

    monkeypatch.setattr(launcher, "_probe", probe)
    readiness = launcher.readiness("claude", binary=str(binary), host=host)
    assert readiness.work_like_available is None
    assert "could not be checked" in readiness.work_like_reason
    assert "connection failed" in readiness.work_like_reason
    assert "sandbox" not in readiness.work_like_reason


@pytest.mark.asyncio
async def test_first_provider_error_contains_stderr_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "failing-claude"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "sys.stdin.readline()\n"
        "print('bash: no job control in this shell', file=sys.stderr, flush=True)\n"
        f"print({_SANDBOX_REASON!r}, file=sys.stderr, flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'error_during_execution',"
        "'is_error':True,'result':None,'error':None,'message':None}),flush=True)\n"
        "time.sleep(30)\n"
    )
    binary.chmod(0o755)
    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda provider, **kwargs: ProviderReadiness(
            provider=provider, installed=True, authenticated=True, binary_path=str(binary)
        ),
    )
    # The task engine stops consuming on the first error. Exercise that exact edge.
    async with aclosing(
        launcher.stream("claude", "Run", cwd=tmp_path, capability="discuss")
    ) as stream:
        async for event in stream:
            if event.event == "error":
                assert _SANDBOX_REASON in event.text
                assert "job control" not in event.text
                break
        else:
            pytest.fail("provider failure was not reported")


def test_remote_probe_closes_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {}

    def run(arguments, **kwargs):
        observed.update(kwargs)
        assert arguments[0] == "ssh"
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    AgentLauncher._probe("compute.example", ["claude", "--input-format", "stream-json"])
    assert observed["input"] == ""
