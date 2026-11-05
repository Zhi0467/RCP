from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

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
@pytest.mark.parametrize("attached", [False, True])
def test_coach_writes_only_its_changes_attached_artifacts_and_scratch_and_has_a_shell(
    tmp_path, provider, runtime, session_id, attached
):
    changes = tmp_path / "changes"
    roots = [changes, tmp_path / "artifacts" / "figure"] if attached else [changes]
    request = ProviderTurnRequest(
        prompt="Review",
        binary=provider,
        cwd=tmp_path / "workspace",
        model=None,
        reasoning=None,
        session_id=session_id,
        read_dirs=[tmp_path / "snapshot"],
        write_dirs=roots,
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
        assert {p for p, mode in fs.items() if mode == "write"} == {
            *map(str, roots),
            ":tmpdir",
            ":slash_tmp",
        }
        assert ":workspace_roots" not in fs
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
        assert "Bash" in settings["permissions"]["allow"]
        edits = [f"Edit(//{str(root).lstrip('/')}/**)" for root in roots]
        assert [p for p in settings["permissions"]["allow"] if p.startswith("Edit(")] == edits
        assert [p for p in turn.command if p.startswith("Edit(")] == edits
    else:
        config = json.loads(turn.environment["OPENCODE_CONFIG_CONTENT"])
        permissions = next(iter(config["agent"].values()))["permission"]
        assert permissions["edit"] == {
            "*": "deny",
            **{f"{str(root).strip('/')}/**": "allow" for root in roots},
        }
        assert permissions["bash"] == "allow"
    with pytest.raises(ValueError):
        profile.runtime(runtime).turn(replace(request, write_dirs=[]))
    with pytest.raises(ValueError):
        profile.runtime(runtime).turn(replace(request, write_dirs=[tmp_path / "changes*"]))
    for unsafe in (tmp_path / "figure*", tmp_path / "a" / ".." / "b", Path("figure"), Path("/")):
        with pytest.raises(ValueError):
            profile.runtime(runtime).turn(replace(request, write_dirs=[*roots, unsafe]))
