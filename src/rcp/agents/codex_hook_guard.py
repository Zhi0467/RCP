"""Execution-account hook inventory, shipped intact to the execution host."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
import tomllib
from pathlib import Path

# Qualification requires the full per-source acceptance probe, not a version check.
QUALIFIED_CODEX_VERSIONS: frozenset[str] = frozenset()


class CodexHookGuardError(ValueError):
    def __init__(self, code: str, files: list[str] | tuple[str, ...]):
        self.code = code
        self.files = tuple(sorted(set(files)))
        super().__init__(f"{code}: {', '.join(self.files)}")


def validate_control_directory(control_dir: str | Path, writable_roots: list[str | Path]) -> None:
    """A writable directory can replace any descendant, including a symlink parent."""
    control = Path(control_dir).absolute()
    resolved = control.resolve()
    unsafe = []
    for raw_root in writable_roots:
        root = Path(raw_root).resolve()
        # Check each lexical parent as well: a symlink leading out of a writable
        # root does not protect the link itself against replacement.
        if any(
            parent.resolve().is_relative_to(root) for parent in (control, *control.parents)
        ) or root.is_relative_to(resolved):
            unsafe.append(str(root))
    if unsafe:
        raise CodexHookGuardError("codex_hook_control_writable", unsafe)


def _has_hooks(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        text = path.read_text()
        value = json.loads(text) if path.suffix == ".json" else tomllib.loads(text)
    except (OSError, ValueError) as exc:
        raise CodexHookGuardError("codex_hook_inventory_unreadable", [str(path)]) from exc
    if not isinstance(value, dict):
        raise CodexHookGuardError("codex_hook_inventory_unreadable", [str(path)])
    hooks = value.get("hooks", {})
    if not isinstance(hooks, dict):
        raise CodexHookGuardError("codex_hook_inventory_unreadable", [str(path)])
    return any(bool(entries) for name, entries in hooks.items() if name != "state")


def inspect_hook_sources(
    runtime_id: str,
    *,
    codex_home: str | Path,
    layers: list[dict],
    listed_hooks: list[dict],
    version: str,
) -> dict:
    """Inspect files from the provider's resolved layers, without trusting hook trust."""
    home = Path(codex_home).resolve()
    paths = {home / "hooks.json"}
    # These sources also exist when an empty layer was omitted from config/read.
    paths.update(
        Path("/etc/codex") / name
        for name in ("config.toml", "managed_config.toml", "hooks.json", "requirements.toml")
    )
    app_server = runtime_id == "codex.app-server-stdio.v1"
    if app_server:
        paths.add(home / "config.toml")
    for layer in layers:
        if layer.get("disabledReason"):
            continue
        name = layer.get("name", {})
        if not isinstance(name, dict):
            continue
        kind = name.get("type", "")
        if not app_server and kind not in {
            "system",
            "mdm",
            "enterpriseManaged",
            "legacyManagedConfigTomlFromFile",
            "legacyManagedConfigTomlFromMdm",
        }:
            continue
        filename = name.get("file")
        project = name.get("dotCodexFolder")
        if isinstance(project, str):
            paths.update(Path(project) / form for form in ("config.toml", "hooks.json"))
            continue
        if isinstance(filename, str):
            path = Path(filename)
            paths.update((path, path.parent / "hooks.json"))
        # MDM layers have no file: inspect the loaded value and name its source.
        elif kind not in {"sessionFlags", "commandLine"}:
            configured = layer.get("config", {}).get("hooks")
            if configured:
                raise CodexHookGuardError("codex_foreign_hooks", [f"managed:{kind}"])
    if app_server:
        for hook in listed_hooks:
            if hook.get("enabled") is not False and isinstance(hook.get("sourcePath"), str):
                paths.add(Path(hook["sourcePath"]))
    foreign = sorted(str(path) for path in paths if _has_hooks(path))
    if foreign:
        raise CodexHookGuardError("codex_foreign_hooks", foreign)
    return {
        "files": sorted(str(path) for path in paths),
        "version": version,
        "warning_codes": []
        if version in QUALIFIED_CODEX_VERSIONS
        else ["codex_hook_sources_unqualified"],
    }


def guard_codex_hooks(
    binary: str, runtime_id: str, *, cwd: str | Path, env: dict[str, str], timeout: float
) -> dict:
    """Resolve source paths with this binary/account, then inspect their contents."""
    process = subprocess.Popen(
        [binary, "app-server"],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stdin is not None
    selector.register(process.stdout, selectors.EVENT_READ)

    def request(identifier: int, method: str, params: dict) -> dict:
        process.stdin.write(
            (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
        )
        process.stdin.flush()
        while True:
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                value = json.loads(line)
                if value.get("id") == identifier:
                    if not isinstance(value.get("result"), dict):
                        raise CodexHookGuardError("codex_hook_inventory_failed", [method])
                    return value["result"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise CodexHookGuardError("codex_hook_inventory_failed", [method])
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise CodexHookGuardError("codex_hook_inventory_failed", [method])
            buffer.extend(chunk)

    try:
        initialized = request(
            1, "initialize", {"clientInfo": {"name": "rcp-hook-guard", "version": "1"}}
        )
        config = request(2, "config/read", {"includeLayers": True, "cwd": str(cwd)})
        listing = (
            request(3, "hooks/list", {"cwds": [str(cwd)]})
            if runtime_id == "codex.app-server-stdio.v1"
            else {}
        )
        hooks = []
        for item in listing.get("data", []):
            if item.get("errors"):
                raise CodexHookGuardError("codex_hook_inventory_failed", [str(cwd)])
            hooks.extend(item.get("hooks", []))
        agent = initialized.get("userAgent", "")
        version = agent.split("/", 1)[-1].split(" ", 1)[0]
        return inspect_hook_sources(
            runtime_id,
            codex_home=initialized.get("codexHome")
            or env.get("CODEX_HOME")
            or str(Path.home() / ".codex"),
            layers=config.get("layers") or [],
            listed_hooks=hooks,
            version=version,
        )
    except CodexHookGuardError:
        raise
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise CodexHookGuardError("codex_hook_inventory_failed", [binary]) from exc
    finally:
        selector.close()
        process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()
