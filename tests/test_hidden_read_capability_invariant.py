from __future__ import annotations

import json
import os
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import get_args

import pytest

from rcp.agents.hidden_read import HIDDEN_READ_ENV_ALLOW_LIST
from rcp.agents.staged_hidden_read import clean_environment
from rcp.agents.write_scope import ProjectWriteScope, WritableRepositoryRoot
from rcp.core.models import HiddenReadScope, HiddenReadStatus
from rcp.providers import AgentCapability, ProviderTurnRequest, profile_for
from rcp.providers.browser_grant import BrowserGrant
from rcp.providers.codex.app_server import CodexAppServerRuntime


def _render(provider, request):
    profile = profile_for(provider.split("_")[0])
    runtime = (
        CodexAppServerRuntime()
        if provider == "codex_app_server"
        else profile.runtime(profile.legacy_runtime_id)
    )
    turn = runtime.turn(request)
    command = list(turn.command)
    environment = dict(turn.environment)
    result = {"command": command, "environment": environment}
    if provider == "claude":
        index = command.index("--settings") + 1
        result["settings"] = json.loads(command[index])
        command[index] = "<settings>"
    elif provider == "opencode":
        settings = json.loads(environment.pop("OPENCODE_CONFIG_CONTENT"))
        agent_name = command[command.index("--agent") + 1]
        assert list(settings["agent"]) == [agent_name]
        # OpenCode addresses the permission-bearing agent by its policy hash.
        # Compare its full definition, with only that derived identifier normalized.
        settings["agent"] = {"<agent>": settings["agent"][agent_name]}
        command[command.index("--agent") + 1] = "<agent>"
        result["settings"] = settings
    else:
        configs = []
        index = 0
        while index < len(command):
            if command[index] == "--config":
                configs.append(tomllib.loads(command[index + 1]))
                command[index + 1] = "<config>"
                index += 1
            index += 1
        result["configs"] = configs
        if provider == "codex_app_server":
            turn.receive_line(json.dumps({"id": 1, "result": {}}))
            step = turn.receive_line(json.dumps({"id": 2, "result": {"config": {}}}))
            thread = json.loads(step.outgoing[0])
            result["thread"] = thread
            permission = thread["params"]["permissions"]
            step = turn.receive_line(
                json.dumps(
                    {
                        "id": 3,
                        "result": {
                            "thread": {"id": "test-thread"},
                            "approvalPolicy": "never",
                            "activePermissionProfile": {"id": permission},
                        },
                    }
                )
            )
            result["turn"] = json.loads(step.outgoing[0])
    return result


def _remove_only_added_denies(provider, rendered, empty, populated):
    paths = (
        *populated.hidden_directories,
        *populated.hidden_files,
        *populated.hidden_globs,
    )
    if provider == "claude":
        denials = rendered["settings"]["permissions"]["deny"]
        for path in paths:
            suffix = "/**" if path in populated.hidden_directories else ""
            denials.remove(f"Read(/{path}{suffix})")
    elif provider == "opencode":
        read = rendered["settings"]["agent"]["<agent>"]["permission"]["read"]
        for path in (*paths, *(path + "/**" for path in populated.hidden_directories)):
            assert read.pop(path.lstrip("/")) == "deny"
    else:
        for config in rendered["configs"]:
            if "permissions" in config:
                for permission in config["permissions"].values():
                    for path in paths:
                        assert permission["filesystem"].pop(path) == "deny"
            if "shell_environment_policy" in config:
                policy = config["shell_environment_policy"]
                assert policy["include_only"] == list(populated.env_allow_list)
                policy["include_only"] = list(empty.env_allow_list)
        if provider == "codex_app_server":
            policy = rendered["thread"]["params"]["config"]["shell_environment_policy"]
            assert policy["include_only"] == list(populated.env_allow_list)
            policy["include_only"] = list(empty.env_allow_list)


@pytest.mark.parametrize("provider", ("claude", "codex", "codex_app_server", "opencode"))
@pytest.mark.parametrize("capability", get_args(AgentCapability))
@pytest.mark.parametrize("browser_enabled", (False, True))
def test_hidden_read_only_adds_denies(provider, capability, browser_enabled, tmp_path):
    # The empty baseline passes every variable in the current launch, including
    # provider authentication. The populated scope may filter tool environments.
    environment = {**os.environ, "TEST_PRIVATE_TOKEN": "secret"}
    allowed = set(HIDDEN_READ_ENV_ALLOW_LIST) | set(environment)
    identity = {
        "execution_machine": "local",
        "execution_host": "",
        "os_account": "research",
        "account_home": str(tmp_path / "home"),
        "enforcement": HiddenReadStatus(status="enforced"),
    }
    empty = HiddenReadScope(**identity, env_allow_list=tuple(sorted(allowed)))
    populated = HiddenReadScope(
        **identity,
        hidden_directories=("/secrets/folder",),
        hidden_files=("/secrets/private-key",),
        hidden_globs=("/secrets/database*",),
        env_allow_list=HIDDEN_READ_ENV_ALLOW_LIST,
    )
    assert clean_environment(empty.model_dump(), environment) == environment
    assert clean_environment(populated.model_dump(), environment) == {
        name: value for name, value in environment.items() if name in populated.env_allow_list
    }
    write_scope = None
    if capability in {"work_auto", "orchestrate"}:
        write_scope = ProjectWriteScope.create(
            project_id="project",
            execution_machine="local",
            execution_host="",
            capability=capability,
            stage_root=str(tmp_path),
            workspace_root=str(tmp_path),
            repositories=[
                WritableRepositoryRoot(alias="repo", machine="local", path="/project/repo")
            ],
            protected_write_paths=["/project/state"],
            granted_roots=["/project/outputs"],
        )
    grant = BrowserGrant(
        requested=browser_enabled,
        status="granted" if browser_enabled else "not_requested",
        session_name="browser-session" if browser_enabled else None,
        invocation_dir=str(tmp_path),
        path_prefix="/browser/bin",
        env={"PLAYWRIGHT_CLI_SESSION": "browser-session"} if browser_enabled else {},
    )
    request = ProviderTurnRequest(
        prompt="task",
        binary=provider.split("_")[0],
        cwd=tmp_path,
        model=None,
        reasoning=None,
        session_id=None,
        read_dirs=[tmp_path / "inputs"],
        write_dirs=[Path(path) for path in write_scope.repository_roots] if write_scope else [],
        write_scope=write_scope,
        capability=capability,
        provider_version={"claude": "2.1.288", "opencode": "1.18.30"}.get(provider, "0.160.0"),
        browser_grant=grant,
        hidden_read_scope=empty,
    )
    baseline = _render(provider, request)
    hidden_request = replace(request, hidden_read_scope=populated)
    # The wrapper lives in a folder named by the policy fingerprint.
    hidden = json.loads(
        json.dumps(_render(provider, hidden_request)).replace(
            populated.fingerprint, empty.fingerprint
        )
    )
    _remove_only_added_denies(provider, hidden, empty, populated)
    # Full structural equality catches removed tools, write grants, Git access,
    # network/sandbox changes, and altered browser permissions or command flags.
    assert hidden == baseline
    assert hidden_request.browser_grant == request.browser_grant == grant
