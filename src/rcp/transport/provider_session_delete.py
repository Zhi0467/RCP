"""Delete provider sessions RCP started, by exact id, on one execution account.

The provider session sweep ships this module's source after the provider remote
bundle, which supplies `claude_project_directory`, through the account's login
shell over SSH, and also calls it in-process for the backend's own machine. It
never lists a provider store; each id goes to the provider's own delete command,
or for Claude, which has none, to its exact transcript path under the host's
resolved workspace. The stores are the account's own, so a session used or
forked outside RCP within the retention window is kept instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING or "claude_project_directory" not in globals():
    from rcp.providers.claude.remote import claude_project_directory
if TYPE_CHECKING or "locate_codex_transcript" not in globals():
    from rcp.providers.codex.remote import locate_codex_transcript

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    Discover = Callable[[str, tuple[str, ...]], str | None]
    Outcome = tuple[str, str | None]

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


def delete_sessions(items: Iterable[dict], *, timeout: float, discover: Discover) -> list[Outcome]:
    """One `(state, error)` per item; state is "deleted", "absent", "kept" or "failed".

    "kept" means the provider shows use within the item's `retention_seconds`.
    """

    return [_delete(item, timeout=timeout, discover=discover) for item in items]


def _delete(item: dict, *, timeout: float, discover: Discover) -> Outcome:
    provider = item["provider"]
    session_id = item["session_id"]
    shape = SESSION_ID_SHAPES.get(provider)
    if shape is None or shape.fullmatch(session_id) is None:
        return ("failed", "unsupported provider session id")
    since = time.time() - float(item["retention_seconds"])
    if provider == "claude":
        return _delete_claude(session_id, item.get("claude_cwd") or "", since)
    binary = item.get("binary") or discover(provider, tuple(item.get("install_paths") or ()))
    if not binary:
        return ("failed", f"{provider} executable not found")
    if provider == "codex":
        try:
            rollout = locate_codex_transcript(session_id)
        except (OSError, ValueError) as exc:
            return ("failed", str(exc))
        if rollout is not None and _modified_since(rollout, since):
            return ("kept", None)
        command = [binary, "delete", "--force", session_id]
    else:
        activity = _opencode_activity(binary, session_id, since, timeout)
        if activity is not None:
            return activity
        command = [binary, "session", "delete", session_id]
    failure, _output = _run(command, timeout)
    if failure is None or GONE_MARKERS[provider] in failure.casefold():
        return ("deleted", None)
    if provider == "codex":
        probe, _output = _run([binary, "delete", session_id], timeout)
        if probe is not None and GONE_MARKERS["codex"] in probe.casefold():
            return ("deleted", None)
    return ("failed", failure)


def _opencode_activity(
    binary: str, session_id: str, since: float, timeout: float
) -> Outcome | None:
    query = (
        "SELECT time_updated, (SELECT COUNT(*) FROM session AS child "
        "WHERE child.parent_id = session.id) AS children "
        f"FROM session WHERE id = '{session_id}'"
    )
    failure, output = _run([binary, "db", query, "--format", "json"], timeout)
    if failure is not None:
        return ("failed", failure)
    try:
        rows = json.loads(output)
        row = rows[0] if rows else None
        active = row is not None and (
            float(row["time_updated"]) / 1000 > since or int(row["children"]) > 0
        )
    except (ValueError, TypeError, KeyError, IndexError):
        return ("failed", "unexpected OpenCode session query result")
    return ("kept", None) if active else None


def _modified_since(path: str, since: float) -> bool:
    return os.stat(path).st_mtime > since


def _run(command: list[str], timeout: float) -> tuple[str | None, str]:
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
        return (str(exc) or type(exc).__name__, "")
    if result.returncode == 0:
        return (None, result.stdout)
    text = re.sub(r"\x1b\[[0-9;]*m", "", f"{result.stdout}\n{result.stderr}")
    return (" ".join(text.split())[:500] or f"exit {result.returncode}", result.stdout)


def _delete_claude(session_id: str, cwd: str, since: float) -> Outcome:
    if not os.path.isabs(cwd):
        return ("failed", "unsupported Claude workspace")
    project_key = claude_project_directory(Path(os.path.realpath(cwd)))
    if CLAUDE_PROJECT_KEY.fullmatch(project_key) is None:
        return ("failed", "unsupported Claude project key")
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    project = os.path.join(config, "projects", project_key)
    transcript = os.path.join(project, session_id + ".jsonl")
    folder = os.path.join(project, session_id)
    if not os.path.lexists(transcript) and not os.path.lexists(folder):
        return ("absent", None)
    try:
        if os.path.isfile(transcript) and _modified_since(transcript, since):
            return ("kept", None)
        if os.path.lexists(transcript):
            os.unlink(transcript)
        if os.path.islink(folder):
            os.unlink(folder)
        elif os.path.isdir(folder):
            shutil.rmtree(folder)
    except OSError as exc:
        return ("failed", str(exc))
    return ("deleted", None)


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
    sys.stdout.write("\n" + json.dumps(results) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
