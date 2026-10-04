from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from rcp.agents.browser_grant import BrowserGrant
from rcp.agents.provider_environment import ProviderProcessEnvironment
from rcp.providers import ProviderTurnRequest, profile_for


def _request(provider: str, status: str) -> ProviderTurnRequest:
    return ProviderTurnRequest(
        prompt="Check the page",
        binary=provider,
        cwd=Path("/tmp/rcp-browser-test"),
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[],
        write_dirs=[],
        write_scope=None,
        capability="discuss",
        provider_version="1.18.30",
        browser_grant=BrowserGrant(requested=True, status=status),
    )


@pytest.mark.parametrize("status", ["granted", "unavailable", "not_requested"])
def test_discuss_browser_rules_and_command_config_agree(status):
    for provider in ("claude", "opencode"):
        profile = profile_for(provider)
        request = _request(provider, status)
        for session in (None, "continued-session"):
            turn = profile.runtime(profile.legacy_runtime_id).turn(
                replace(request, session_id=session)
            )
            if provider == "claude":
                assert ("Bash(playwright-cli:*)" in turn.command) == (status == "granted")
                assert "--strict-mcp-config" in turn.command
                assert json.loads(turn.command[turn.command.index("--mcp-config") + 1]) == {
                    "mcpServers": {}
                }
            else:
                config = json.loads(turn.environment["OPENCODE_CONFIG_CONTENT"])
                agent = turn.command[turn.command.index("--agent") + 1]
                rules = config["agent"][agent]["permission"]
                assert rules["*"] == "deny"
                assert rules.get("bash") == (
                    {"*": "deny", "playwright-cli *": "allow"} if status == "granted" else None
                )
                assert "--pure" in turn.command
                assert "task" not in rules


def test_browser_environment_prefixes_the_execution_hosts_path(monkeypatch):
    monkeypatch.setenv("PATH", "/controller/bin")
    local = (
        ProviderProcessEnvironment()
        .with_variables({"PLAYWRIGHT_CLI_SESSION": "session"}, remote=False)
        .with_path_prefix("/tools with spaces/bin", remote=False)
    )
    assert local.local_env["PATH"] == "/tools with spaces/bin:/controller/bin"
    remote = (
        ProviderProcessEnvironment()
        .with_variables({"PLAYWRIGHT_CLI_SESSION": "session"}, remote=True)
        .with_path_prefix("/tools with spaces/bin", remote=True)
    )
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            remote.remote_prefix + '; printf "%s\\n%s" "$PATH" "$PLAYWRIGHT_CLI_SESSION"',
        ],
        env={"PATH": "/remote/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == ["/tools with spaces/bin:/remote/bin", "session"]


@pytest.mark.asyncio
async def test_launcher_delivers_grant_environment(tmp_path, monkeypatch):
    import sys

    from rcp.agents.launcher import AgentLauncher, ProviderReadiness

    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda *args, **kwargs: ProviderReadiness(
            provider="opencode", label="test", installed=True, authenticated=True, version="1.18.30"
        ),
    )
    script = 'import json,os; print(json.dumps({"type":"text","part":{"text":json.dumps([os.environ["PLAYWRIGHT_CLI_SESSION"],os.environ["PATH"]])}}))'
    monkeypatch.setattr(
        launcher, "_command", lambda *args, browser_grant, **kwargs: [sys.executable, "-c", script]
    )
    grant = BrowserGrant(
        requested=True,
        status="granted",
        path_prefix="/browser/bin",
        env={"PLAYWRIGHT_CLI_SESSION": "session"},
    )
    events = [
        event
        async for event in launcher.stream(
            "opencode", "probe", cwd=tmp_path, capability="discuss", browser_grant=grant
        )
    ]
    answer = next(event for event in events if event.event == "answer")
    session, path = json.loads(answer.text)
    assert session == "session"
    assert path.startswith("/browser/bin:")


@pytest.mark.parametrize("capability", ["paper_readonly", "scratch_patch"])
def test_excluded_profiles_do_not_add_browser_shell(capability):
    for provider in ("claude", "opencode"):
        profile = profile_for(provider)
        request = replace(_request(provider, "granted"), capability=capability)
        turn = profile.runtime(profile.legacy_runtime_id).turn(request)
        if provider == "claude":
            assert "Bash(playwright-cli:*)" not in turn.command
        else:
            config = json.loads(turn.environment["OPENCODE_CONFIG_CONTENT"])
            agent = turn.command[turn.command.index("--agent") + 1]
            assert "bash" not in config["agent"][agent]["permission"]
