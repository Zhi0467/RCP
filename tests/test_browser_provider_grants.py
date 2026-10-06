from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
import sys
import venv
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from rcp.agents.command_mailbox import stage_command_mailbox
from rcp.agents.provider_environment import ProviderProcessEnvironment
from rcp.core.models import HiddenReadScope, HiddenReadStatus
from rcp.providers import ProviderTurnRequest, profile_for
from rcp.providers.browser_grant import BrowserGrant


def _request(
    provider: str, status: Literal["not_requested", "granted", "unavailable"]
) -> ProviderTurnRequest:
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


def _staged_discuss_mailbox(tmp_path: Path, *, broker: bool, layout: str):
    stage = tmp_path / "stage"
    workspace = stage / "workspace" if layout == "split" else stage
    workspace.mkdir(parents=True, exist_ok=True)
    return stage_command_mailbox(
        local_stage=workspace,
        local_input_stage=stage,
        remote_stage=None,
        episode_id=None,
        task_id="discuss-task",
        turn_id="discuss-turn",
        authority="broker" if broker else "validate_only",
    )


@pytest.mark.parametrize("status", ["granted", "unavailable", "not_requested"])
@pytest.mark.parametrize("broker", [False, True])
@pytest.mark.parametrize("layout", ["split", "legacy"])
@pytest.mark.parametrize(
    ("provider", "parent"),
    [
        ("claude", "data"),
        ("claude", "data[1]"),
        ("claude", "data?literal"),
        ("claude", "data*x 1"),
        ("claude", "data\\*literal"),
        ("claude", "data\\$literal[2]"),
        ("opencode", "data"),
    ],
)
def test_discuss_browser_rules_and_command_config_agree(
    status, broker, layout, provider, parent, tmp_path
):
    staged = _staged_discuss_mailbox(tmp_path / parent, broker=broker, layout=layout)
    gate = staged.invocation_gate
    assert (gate is not None) == broker
    if gate is not None:
        assert gate.client_path == staged.client_path == staged.client_argv()[3]
        assert gate.client_executable_argv() == staged.client_argv()[:4]
    assert staged.client_argv()[:3] == ("python3", "-I", "-S")
    client = shlex.join(staged.client_argv()[:4])
    inputs = Path(staged.client_path).parent
    hidden_file = str(tmp_path / "private-input")
    hidden = HiddenReadScope(
        execution_machine="local",
        execution_host="",
        os_account="test",
        hidden_files=(hidden_file,),
        enforcement=HiddenReadStatus(status="enforced"),
    )
    profile = profile_for(provider)
    request = replace(
        _request(provider, status),
        invocation_gate=gate,
        cwd=Path(staged.workspace),
        read_dirs=[inputs],
        hidden_read_scope=hidden,
    )
    for session in (None, "continued-session"):
        turn = profile.runtime(profile.legacy_runtime_id).turn(replace(request, session_id=session))
        if provider == "claude":
            expected_bash = []
            if status == "granted":
                expected_bash.append("Bash(playwright-cli:*)")
            if broker:
                expected_bash.append(f"Bash({client}:*)")
            assert [arg for arg in turn.command if arg.startswith("Bash(")] == expected_bash
            settings = json.loads(turn.command[turn.command.index("--settings") + 1])
            denied = settings["permissions"]["deny"]
            assert f"Read(/{hidden_file})" in denied
            for target in (inputs, Path(staged.client_path), inputs / "argparse.py"):
                assert _claude_edit_denied(target, denied) == broker
            if "[1]" in parent:
                assert not _claude_edit_denied(Path(str(inputs).replace("[1]", "1")), denied)
            assert not _claude_edit_denied(Path(staged.workspace) / "artifact.html", denied)
            assert "--strict-mcp-config" in turn.command
            assert json.loads(turn.command[turn.command.index("--mcp-config") + 1]) == {
                "mcpServers": {}
            }
        else:
            config = json.loads(turn.environment["OPENCODE_CONFIG_CONTENT"])
            agent = turn.command[turn.command.index("--agent") + 1]
            rules = config["agent"][agent]["permission"]
            assert rules["*"] == "deny"
            expected_bash = {"*": "deny"}
            if status == "granted":
                expected_bash["playwright-cli *"] = "allow"
            if broker:
                expected_bash[f"{client} *"] = "allow"
            assert rules.get("bash") == (expected_bash if len(expected_bash) > 1 else None)
            for target in (inputs, Path(staged.client_path), inputs / "argparse.py"):
                assert _opencode_edit_permission(target, rules["edit"]) == (
                    "deny" if broker or layout == "split" else "allow"
                )
            assert (
                _opencode_edit_permission(Path(staged.workspace) / "artifact.html", rules["edit"])
                == "allow"
            )
            assert "--pure" in turn.command
            assert "task" not in rules
    staged.cleanup()


def _claude_edit_denied(path: Path, denied: list[str]) -> bool:
    # Claude's file rules use gitignore semantics, not fnmatch character classes.
    patterns = [rule[len("Edit(/") : -1] for rule in denied if rule.startswith("Edit(")]
    result = subprocess.run(
        [
            "node",
            "-e",
            "const fs = require('node:fs'); const ignore = require('ignore');"
            "const {patterns, path} = JSON.parse(fs.readFileSync(0, 'utf8'));"
            "process.stdout.write(JSON.stringify(ignore().add(patterns).ignores(path)));",
        ],
        cwd=Path(__file__).resolve().parents[1] / "web",
        input=json.dumps({"patterns": patterns, "path": str(path).lstrip("/")}),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _opencode_edit_permission(path: Path, rules: dict[str, str]) -> str:
    result = "deny"
    for pattern, permission in rules.items():
        if fnmatch.fnmatchcase(str(path).lstrip("/"), pattern):
            result = permission
    return result


@pytest.mark.parametrize("layout", ["split", "legacy"])
def test_staged_client_and_next_broker_ignore_workspace_input_and_site_imports(tmp_path, layout):
    staged = _staged_discuss_mailbox(tmp_path, broker=True, layout=layout)
    marker = tmp_path / "outside-scratch-write"
    injected = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    inputs = Path(staged.client_path).parent
    workspace = Path(staged.workspace)
    for directory in {inputs, workspace}:
        (directory / "argparse.py").write_text(injected)
    python_home = tmp_path / "isolated-test-python"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(python_home)
    site_packages = next((python_home / "lib").glob("python*/site-packages"))
    (site_packages / "sitecustomize.py").write_text(injected)
    environment = {
        "PATH": str(python_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(workspace),
    }
    # Prove the inherited site configuration would execute without isolation.
    subprocess.run(["python3", "-c", "pass"], env=environment, check=True)
    assert marker.exists()
    marker.unlink()
    result = subprocess.run(
        staged.client_argv("--help"),
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "usage:" in result.stdout
    assert not marker.exists()
    staged.cleanup()
    # A later turn reuses the workspace; cleanup must leave arbitrary agent files.
    staged = _staged_discuss_mailbox(tmp_path, broker=True, layout=layout)
    assert (workspace / "argparse.py").exists()
    gate = staged.invocation_gate
    assert gate is not None
    result = subprocess.run(
        [*gate._broker_argv(), "--help"],
        cwd=workspace,
        env={**environment, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "usage:" in result.stdout
    assert not marker.exists()
    staged.cleanup()


def test_browser_environment_prefixes_the_execution_hosts_path(monkeypatch):
    monkeypatch.setenv("PATH", "/controller/bin")
    local = (
        ProviderProcessEnvironment()
        .with_variables({"PLAYWRIGHT_CLI_SESSION": "session"}, remote=False)
        .with_path_prefix("/tools with spaces/bin", remote=False)
    )
    assert local.local_env is not None
    assert local.local_env["PATH"] == "/tools with spaces/bin:/controller/bin"
    remote = (
        ProviderProcessEnvironment()
        .with_variables({"PLAYWRIGHT_CLI_SESSION": "session"}, remote=True)
        .with_path_prefix("/tools with spaces/bin", remote=True)
    )
    assert remote.remote_prefix is not None
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
def test_excluded_profiles_do_not_add_browser_shell(capability, tmp_path):
    staged = stage_command_mailbox(
        local_stage=tmp_path,
        remote_stage=None,
        episode_id=None,
        task_id="excluded-task",
        turn_id="excluded-turn",
        authority="broker",
    )
    for provider in ("claude", "opencode"):
        profile = profile_for(provider)
        request = replace(
            _request(provider, "granted"),
            capability=capability,
            invocation_gate=staged.invocation_gate,
        )
        turn = profile.runtime(profile.legacy_runtime_id).turn(request)
        if provider == "claude":
            assert not any(arg.startswith("Bash(") for arg in turn.command)
        else:
            config = json.loads(turn.environment["OPENCODE_CONFIG_CONTENT"])
            agent = turn.command[turn.command.index("--agent") + 1]
            assert "bash" not in config["agent"][agent]["permission"]
    staged.cleanup()


@pytest.mark.parametrize("module", ["rcp.browser", "rcp.transport", "rcp.storage", "rcp.providers"])
def test_lower_layers_import_first_in_a_fresh_interpreter(module):
    # Grant models are shared below storage; importing them from rcp.agents made
    # these packages fail with a circular import when imported first.
    subprocess.run([sys.executable, "-c", f"import {module}"], check=True)
