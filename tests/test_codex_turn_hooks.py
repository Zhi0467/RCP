from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from rcp.agents.codex_turn_hooks import hook_cli_args, prepare_control


def test_hooks_track_invocation_children_and_block_stop(tmp_path: Path) -> None:
    control = tmp_path / "control" / "operation" / "attempt"
    config = prepare_control(control, [str(tmp_path / "workspace")])
    arguments = hook_cli_args(config)
    assert arguments[0] == "--dangerously-bypass-hook-trust"
    for value in arguments[2::2]:
        assert tomllib.loads(value)["hooks"]

    def invoke(event: str, **payload: object) -> dict | None:
        result = subprocess.run(
            [
                sys.executable,
                str(control / "hook.py"),
                event,
                str(control / "state.json"),
                str(control / "started"),
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            cwd=tmp_path,
        )
        return json.loads(result.stdout) if result.stdout else None

    assert invoke("SessionStart") is None
    assert (control / "started").is_file()
    invoke("SubagentStart", agent_id="child")
    invoke("SubagentStart", agent_id="nested")
    assert invoke("Stop", stop_hook_active=False)["decision"] == "block"
    first = json.loads((control / "state.json").read_text())
    assert set(first["open_agents"]) == {"child", "nested"}
    assert isinstance(first["open_work_since"], float)
    assert invoke("Stop", stop_hook_active=True)["decision"] == "block"
    assert (
        json.loads((control / "state.json").read_text())["open_work_since"]
        == first["open_work_since"]
    )
    invoke("SubagentStop", agent_id="nested")
    invoke("SubagentStop", agent_id="child")
    assert invoke("Stop") is None
    assert json.loads((control / "state.json").read_text())["open_agents"] == []
