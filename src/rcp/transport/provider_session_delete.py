"""Delete provider sessions RCP started, by exact id, on one execution account.

Standard library only: the provider session sweep ships this module's source over
SSH and also calls it in-process for the backend's own machine. It never lists or
globs a provider store; each id goes to the provider's own delete command, or for
Claude, which has none, to its exact transcript path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    Discover = Callable[[str, tuple[str, ...]], str | None]

SESSION_ID_SHAPES = {
    "codex": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    "claude": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    "opencode": re.compile(r"ses_[A-Za-z0-9]+"),
}
CLAUDE_PROJECT_KEY = re.compile(r"[A-Za-z0-9-]+")
GONE_MARKERS = {
    "codex": "no active or archived session found",
    "opencode": "session not found",
}


def delete_sessions(
    items: Iterable[dict], *, timeout: float, discover: Discover
) -> list[str | None]:
    """One result per item: None once the session is gone, else the failure."""

    return [_delete(item, timeout=timeout, discover=discover) for item in items]


def _delete(item: dict, *, timeout: float, discover: Discover) -> str | None:
    provider = item["provider"]
    session_id = item["session_id"]
    shape = SESSION_ID_SHAPES.get(provider)
    if shape is None or shape.fullmatch(session_id) is None:
        return "unsupported provider session id"
    if provider == "claude":
        return _delete_claude(session_id, item.get("claude_project") or "")
    binary = item.get("binary") or discover(provider, tuple(item.get("install_paths") or ()))
    if not binary:
        return f"{provider} executable not found"
    command = (
        [binary, "delete", "--force", session_id]
        if provider == "codex"
        else [binary, "session", "delete", session_id]
    )
    failure = _run(command, timeout)
    if failure is None or GONE_MARKERS[provider] in failure.casefold():
        return None
    if provider == "codex":
        probe = _run([binary, "delete", session_id], timeout)
        if probe is not None and GONE_MARKERS["codex"] in probe.casefold():
            return None
    return failure


def _run(command: list[str], timeout: float) -> str | None:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc) or type(exc).__name__
    if result.returncode == 0:
        return None
    text = re.sub(r"\x1b\[[0-9;]*m", "", f"{result.stdout}\n{result.stderr}")
    return " ".join(text.split())[:500] or f"exit {result.returncode}"


def _delete_claude(session_id: str, project_key: str) -> str | None:
    if CLAUDE_PROJECT_KEY.fullmatch(project_key) is None:
        return "unsupported Claude project key"
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    project = os.path.join(config, "projects", project_key)
    transcript = os.path.join(project, session_id + ".jsonl")
    folder = os.path.join(project, session_id)
    try:
        if os.path.lexists(transcript):
            os.unlink(transcript)
        if os.path.islink(folder):
            os.unlink(folder)
        elif os.path.isdir(folder):
            shutil.rmtree(folder)
    except OSError as exc:
        return str(exc)
    return None


def _shipped_discovery(source: str) -> Discover:
    namespace: dict[str, object] = {"__name__": "rcp_provider_discovery"}
    exec(compile(source, "provider_discovery.py", "exec"), namespace)
    return cast("Discover", namespace["discover_provider"])


def main() -> int:
    payload = json.load(sys.stdin)
    results = delete_sessions(
        payload["items"],
        timeout=float(payload["timeout"]),
        discover=_shipped_discovery(sys.argv[1]),
    )
    json.dump(results, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
