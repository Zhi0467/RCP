"""Prepare invocation hook control on the execution account from shipped sources."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path


def prepare(payload: dict, *, env: dict[str, str] | None = None) -> dict:
    from rcp.agents.codex_hook_guard import guard_codex_hooks
    from rcp.agents.codex_turn_hooks import prepare_control

    receipt = guard_codex_hooks(
        payload["binary"],
        payload["runtime_id"],
        cwd=payload["cwd"],
        env=dict(os.environ) if env is None else env,
        timeout=payload["timeout"],
    )
    directory = Path(payload["control_dir"]).expanduser().absolute()
    writable_roots = list(payload["writable_roots"])
    if payload.get("include_temporary_roots"):
        writable_roots.extend(["/tmp", tempfile.gettempdir()])
        temporary = (os.environ if env is None else env).get("TMPDIR")
        if temporary:
            writable_roots.append(temporary)
    hooks = prepare_control(
        directory, writable_roots, source=payload["sources"].get("codex_turn_hooks")
    )
    return {"hooks": hooks, "marker": str(directory / "started"), "receipt": receipt}


def main() -> None:
    payload = json.loads(sys.argv[1])
    if "marker" in payload:
        print(json.dumps(Path(payload["marker"]).is_file()))
        return
    # The remote account needs only the standard library, never an RCP install.
    for package in ("rcp", "rcp.agents"):
        sys.modules.setdefault(package, types.ModuleType(package))
    for name, source in payload["sources"].items():
        qualified = "rcp.agents." + name
        module = types.ModuleType(qualified)
        sys.modules[qualified] = module
        exec(compile(source, name + ".py", "exec"), module.__dict__)
    from rcp.agents.codex_hook_guard import CodexHookGuardError

    try:
        print(json.dumps(prepare(payload)))
    except CodexHookGuardError as exc:
        print(json.dumps({"code": exc.code, "files": exc.files}))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
