"""Execution-host staging, browser suppression, and Antigravity turn boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or "TurnFence" not in globals():
    from rcp.providers.turn_fence import TurnFence


if TYPE_CHECKING or "HomeLayout" not in globals():
    from rcp.providers.login_homes import HomeLayout, run_home


AGY_HOME_LAYOUT = HomeLayout(
    independent_paths=(".gemini/antigravity-cli/antigravity-oauth-token",),
    shared_directories=(
        ".gemini/antigravity-cli/conversations",
        ".gemini/antigravity-cli/brain",
        ".gemini/config",
    ),
)


class AgyStreamTurnFence(TurnFence):
    """Match each queued user input to its FIFO result."""

    def __init__(self, runtime_id: str):
        super().__init__(runtime_id)
        self.results = 0

    @property
    def prompt_started(self) -> bool:
        return bool(self.message_ids)

    def _input(self, value: dict) -> None:
        if value.get("event") == "user":
            self.message_ids[str(len(self.message_ids))] = None

    def _output(self, value: dict) -> None:
        if value.get("event") != "result":
            return
        self.results += 1
        result = value.get("result")
        if not isinstance(result, dict):
            self.terminal = True
            return
        self.terminal = (
            result.get("status") != "SUCCESS"
            or bool(result.get("denied_actions"))
            or self.results >= len(self.message_ids)
        )


def agy_browser_environment(directory: Path) -> dict[str, str]:
    """Install failing browser launchers in an RCP-owned process directory."""
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    for name in ("open", "xdg-open", "sensible-browser"):
        path = directory / name
        contents = "#!/bin/sh\nexit 1\n"
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != contents:
            descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(contents)
                os.chmod(temporary, 0o700)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        path.chmod(0o700)
    return {
        "PATH": str(directory) + os.pathsep + os.environ.get("PATH", ""),
        "BROWSER": str(directory / "open"),
    }


def agy_agent_name(policy: dict[str, object], tools: tuple[str, ...]) -> str:
    digest = hashlib.sha256(json.dumps([policy, tools], sort_keys=True).encode()).hexdigest()
    return "rcp-" + digest[:16]


def _agy_stage_agent(command: list[str], tools: list[str]) -> None:
    name = command[command.index("--agent") + 1]
    directory = Path.cwd() / ".agents" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (name + ".md")).write_text(
        "---\nname: "
        + name
        + "\ndescription: RCP launch policy\ntools: "
        + json.dumps(tools)
        + "\n---\n",
        encoding="utf-8",
    )


def _agy_host_policy(
    command: list[str], tools: tuple[str, ...], *, probe: bool, data_dir: Path | None = None
) -> None:
    index = command.index("--settings") + 1
    policy = json.loads(command[index])
    permissions = policy["permissions"]
    roots = [
        str(Path.cwd()),
        *(
            rule[len("write_file(") : -1]
            for rule in permissions["allow"]
            if rule.startswith("write_file(")
        ),
    ]
    if probe:
        permissions["deny"] = [f"write_file({Path.cwd()}/.agents)"]
    for directory in dict.fromkeys(
        ["/tmp", str(Path("/tmp").resolve()), str(Path(os.environ.get("TMPDIR", "/tmp")).resolve())]
    ):
        temporary = Path(directory).resolve()
        rule = f"write_file({directory})"
        resolved = [Path(root).resolve() for root in roots]
        granted = any(root == temporary or root in temporary.parents for root in resolved)
        nested = any(temporary in root.parents for root in resolved)
        if granted or nested:
            if rule in permissions["deny"]:
                permissions["deny"].remove(rule)
            if nested and not granted:
                print(
                    "RCP Antigravity containment gap: writable root inside temporary directory",
                    file=sys.stderr,
                )
        elif rule not in permissions["deny"]:
            permissions["deny"].append(rule)
    directories = [Path.home() / ".rcp" / "provider-homes"]
    if data_dir is not None:
        directories.append(data_dir / "provider-homes")
    permissions["deny"].extend(f"read_file({path.resolve()})" for path in directories)
    permissions["deny"].append(
        f"write_file({(Path.home() / '.rcp' / 'agy-browser-shims').resolve()})"
    )
    permissions["deny"] = list(dict.fromkeys(permissions["deny"]))
    command[index] = json.dumps(policy, separators=(",", ":"))
    command[command.index("--agent") + 1] = agy_agent_name(policy, tools)


def _agy_arguments(
    arguments: list[str],
) -> tuple[list[str], tuple[str, ...], bool, Path | None, Path | None]:
    boundary = arguments.index("--")
    options = arguments[:boundary]
    data_dir = None
    home = None
    if len(options) % 2:
        raise ValueError("Invalid Antigravity wrapper options")
    for key, value in zip(options[::2], options[1::2]):
        if key == "--data-dir":
            data_dir = Path(value)
        elif key == "--home":
            home = Path(value).expanduser().absolute()
        else:
            raise ValueError("Invalid Antigravity wrapper options")
    command = list(arguments[boundary + 1 :])
    if not command:
        raise ValueError("Missing Antigravity executable")
    tools: tuple[str, ...] = ()
    if command[1:2] == ["--rcp-agy-tools"]:
        value = json.loads(command[2])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("Invalid Antigravity tool list")
        tools = tuple(value)
        del command[1:3]
    probe = command[1:2] == ["--rcp-agy-probe"]
    if probe:
        del command[1]
    if command.count("--settings") > 1 or command.count("--agent") > 1:
        raise ValueError("Antigravity launch repeats a policy option")
    return command, tools, probe, data_dir, home


def agy_run(arguments: list[str]) -> None:
    """Stage host-owned policy and replace this process with the provider."""
    command, tools, probe, data_dir, home = _agy_arguments(arguments)
    environment = dict(os.environ)
    environment.update(agy_browser_environment(Path.home() / ".rcp" / "agy-browser-shims"))
    if probe:
        workspace = Path.home() / ".rcp" / "agy-probe"
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chdir(workspace)
    if tools:
        _agy_host_policy(command, tools, probe=probe, data_dir=data_dir)
        _agy_stage_agent(command, list(tools))
    with run_home(home, Path.home(), AGY_HOME_LAYOUT) if home is not None else nullcontext():
        if home is not None:
            environment["HOME"] = str(home)
        os.execvpe(command[0], command, environment)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--agy-run":
    agy_run(sys.argv[2:])
