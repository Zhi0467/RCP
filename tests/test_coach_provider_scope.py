from __future__ import annotations

import json
import tomllib
from dataclasses import replace

import pytest

from rcp.providers import ProviderTurnRequest, profile_for


@pytest.mark.parametrize(
    "provider,runtime",
    [
        ("codex", "codex.exec-json.v1"),
        ("codex", "codex.app-server-stdio.v1"),
        ("claude", "claude.stream-json.v1"),
        ("opencode", "opencode.run-json.v1"),
    ],
)
@pytest.mark.parametrize("session_id", [None, "native-session"])
def test_coach_changes_are_the_only_write_grant(tmp_path, provider, runtime, session_id):
    changes = tmp_path / "changes"
    request = ProviderTurnRequest(
        prompt="Review",
        binary=provider,
        cwd=tmp_path / "workspace",
        model=None,
        reasoning=None,
        session_id=session_id,
        read_dirs=[tmp_path / "snapshot"],
        write_dirs=[changes],
        write_scope=None,
        capability="paper_suggest",
        provider_version="2.200.0",
    )
    profile = profile_for(provider)
    if provider == "claude":
        runtime = profile.legacy_runtime_id
    turn = profile.runtime(runtime).turn(request)
    if provider == "codex":
        config = {}
        for index, arg in enumerate(turn.command):
            if arg == "--config":
                config.update(tomllib.loads(turn.command[index + 1]))
        assert config["default_permissions"] == "rcp_paper"
        fs = config["permissions"]["rcp_paper"]["filesystem"]
        assert fs[":root"] == "read"
        assert {p for p, mode in fs.items() if mode == "write"} == {str(changes)}
        assert ":workspace_roots" not in fs and ":tmpdir" not in fs and ":slash_tmp" not in fs
        if runtime.endswith("stdio.v1"):
            turn.receive_line(json.dumps({"id": 1, "result": {}}))
            step = turn.receive_line(json.dumps({"id": 2, "result": {"config": {}}}))
            message = json.loads(step.outgoing[0])
            assert message["method"] == ("thread/resume" if session_id else "thread/start")
            assert message["params"]["permissions"] == "rcp_paper"
            thread_id = session_id or "new-session"
            step = turn.receive_line(
                json.dumps(
                    {
                        "id": 3,
                        "result": {
                            "thread": {"id": thread_id},
                            "approvalPolicy": "never",
                            "activePermissionProfile": {"id": "rcp_paper"},
                        },
                    }
                )
            )
            message = json.loads(step.outgoing[0])
            assert message["method"] == "turn/start"
            assert "sandboxPolicy" not in message["params"]
    elif provider == "claude":
        assert turn.command[turn.command.index("--permission-mode") + 1] == "dontAsk"
        assert turn.command[turn.command.index("--setting-sources") + 1] == ""
        settings = json.loads(turn.command[turn.command.index("--settings") + 1])
        assert "Bash" not in settings["permissions"]["allow"]
        assert [p for p in settings["permissions"]["allow"] if p.startswith("Edit(")] == [
            f"Edit(//{str(changes).lstrip('/')}/**)"
        ]
    else:
        config = json.loads(turn.environment["OPENCODE_CONFIG_CONTENT"])
        permissions = next(iter(config["agent"].values()))["permission"]
        assert permissions["edit"] == {"*": "deny", f"{str(changes).strip('/')}/**": "allow"}
        assert "bash" not in permissions
    with pytest.raises(ValueError, match="exactly one absolute changes"):
        profile.runtime(runtime).turn(replace(request, write_dirs=[]))
    with pytest.raises(ValueError, match="permission wildcards"):
        profile.runtime(runtime).turn(replace(request, write_dirs=[tmp_path / "changes*"]))
