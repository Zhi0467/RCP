"""RCP's invocation-scoped Codex hooks, also shipped as an executable source file."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import sys
import time
from pathlib import Path

HOOK_EVENTS = ("SessionStart", "SubagentStart", "SubagentStop", "Stop")


def hook_config(control_dir: Path, *, python: str = "python3") -> dict[str, object]:
    """Pass every path explicitly; the provider's cwd never locates control state."""
    return {
        event: [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": shlex.join(
                            [
                                python,
                                str(control_dir / "hook.py"),
                                event,
                                str(control_dir / "state.json"),
                                str(control_dir / "started"),
                            ]
                        ),
                    }
                ]
            }
        ]
        for event in HOOK_EVENTS
    }


def hook_cli_args(config: dict[str, object]) -> list[str]:
    """Encode the native TOML hook arrays without a second command definition."""
    arguments = ["--dangerously-bypass-hook-trust"]
    for event, matchers in config.items():
        entries = []
        for matcher in matchers:
            commands = ",".join(
                '{type="command",command=' + json.dumps(hook["command"]) + "}"
                for hook in matcher["hooks"]
            )
            entries.append("{hooks=[" + commands + "]}")
        arguments.extend(["-c", f"hooks.{event}=[{','.join(entries)}]"])
    return arguments


def prepare_control(
    control_dir: Path, writable_roots: list[str], *, source: str | None = None
) -> dict[str, object]:
    """Create a fresh attempt; never inherit state from a resumed provider session."""
    from rcp.agents.codex_hook_guard import validate_control_directory

    validate_control_directory(control_dir, writable_roots)
    control_dir.mkdir(parents=True, exist_ok=False)
    (control_dir / "hook.py").write_text(
        source if source is not None else Path(__file__).read_text(encoding="utf-8")
    )
    _write_state(control_dir / "state.json", {"open_agents": [], "open_work_since": None})
    return hook_config(control_dir)


def _write_state(path: Path, state: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def apply_hook(
    event: str, payload: dict[str, object], state_path: Path, marker_path: Path
) -> dict[str, object] | None:
    """Serialize child lifetimes and retain the first blocked Stop for the launcher."""
    with state_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        open_agents = set(state["open_agents"])
        if event == "SessionStart":
            marker_path.write_text("started\n", encoding="utf-8")
        elif event in {"SubagentStart", "SubagentStop"}:
            agent_id = payload.get("agent_id")
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError("codex_hook_missing_agent_id")
            if event == "SubagentStart":
                open_agents.add(agent_id)
            else:
                open_agents.discard(agent_id)
        elif event != "Stop":
            raise ValueError("codex_hook_unknown_event")
        state["open_agents"] = sorted(open_agents)
        blocked = event == "Stop" and bool(open_agents)
        if blocked and state["open_work_since"] is None:
            state["open_work_since"] = time.monotonic()
        _write_state(state_path, state)
        if blocked:
            return {
                "decision": "block",
                "reason": "Wait for these delegated agents to finish: "
                + ", ".join(sorted(open_agents)),
            }
    return None


def main() -> None:
    event, state, marker = sys.argv[1:]
    try:
        response = apply_hook(event, json.load(sys.stdin), Path(state), Path(marker))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"codex_hook_state_error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if response is not None:
        print(json.dumps(response))


if __name__ == "__main__":
    main()
