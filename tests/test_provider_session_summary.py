from __future__ import annotations

import json
import tomllib
from dataclasses import replace

import pytest

from rcp.core.models import HiddenReadScope, HiddenReadStatus
from rcp.providers import ProviderTurnRequest, profile_for


@pytest.mark.parametrize(
    "provider,runtime",
    [
        ("codex", "codex.exec-json.v1"),
        ("codex", "codex.app-server-stdio.v1"),
        ("claude", "claude.stream-json.v1"),
    ],
)
@pytest.mark.parametrize("fork", [False, True])
def test_summary_resume_has_no_write_grants(tmp_path, provider, runtime, fork):
    request = ProviderTurnRequest(
        prompt="Summarize",
        binary=provider,
        cwd=tmp_path,
        model=None,
        reasoning=None,
        session_id=None if fork else "old-session",
        fork_session_id="old-session" if fork else None,
        read_dirs=[],
        write_dirs=[],
        write_scope=None,
        capability="session_summary",
        provider_version="2.200.0",
        hidden_read_scope=HiddenReadScope(
            execution_machine="local",
            execution_host="",
            os_account="member",
            enforcement=HiddenReadStatus(status="enforced"),
            hidden_directories=("/private/other",),
        ),
    )
    turn = profile_for(provider).runtime(runtime).turn(request)
    if provider == "codex":
        config = {}
        for index, arg in enumerate(turn.command):
            if arg == "--config":
                config.update(tomllib.loads(turn.command[index + 1]))
        assert config["default_permissions"] == "rcp_summary"
        policy = config["permissions"]["rcp_summary"]
        assert policy["filesystem"] == {":root": "read", "/private/other": "deny"}
        assert policy["network"] == {"enabled": False}
        if runtime.endswith("stdio.v1"):
            turn.receive_line(json.dumps({"id": 1, "result": {}}))
            step = turn.receive_line(json.dumps({"id": 2, "result": {"config": {}}}))
            message = json.loads(step.outgoing[0])
            assert message["method"] == ("thread/fork" if fork else "thread/resume")
            assert message["params"]["permissions"] == "rcp_summary"
            assert message["params"]["config"]["agents"]["enabled"] is False
            step = turn.receive_line(
                json.dumps(
                    {
                        "id": 3,
                        "result": {
                            "thread": {"id": "summary-fork" if fork else "old-session"},
                            "approvalPolicy": "never",
                            "activePermissionProfile": {"id": "rcp_summary"},
                        },
                    }
                )
            )
            assert json.loads(step.outgoing[0])["method"] == "turn/start"
        else:
            assert turn.command[1:3] == ["exec", "fork" if fork else "resume"]
            assert "old-session" in turn.command
    else:
        assert turn.command[turn.command.index("--tools") + 1] == ""
        assert turn.command[turn.command.index("--setting-sources") + 1] == ""
        assert turn.command[turn.command.index("--resume") + 1] == "old-session"
        settings = json.loads(turn.command[turn.command.index("--settings") + 1])
        assert settings["disableAllHooks"] is True
        assert settings["permissions"]["deny"] == ["Read(//private/other/**)"]
        assert ("--fork-session" in turn.command) == fork
        assert "--add-dir" not in turn.command
    with pytest.raises(ValueError):
        profile_for(provider).runtime(runtime).turn(replace(request, write_dirs=[tmp_path]))
    with pytest.raises(ValueError):
        profile_for("opencode").runtime("opencode.run-json.v1").turn(request)
