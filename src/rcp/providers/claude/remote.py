"""Claude Code's session files and turn boundaries, as an execution host reads them.

Shipped as source text to execution hosts by `rcp.providers.remote_bundle`,
after the shared bases, so it may import only the standard library. The guarded
imports serve the local copy.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

if "SessionFormat" not in globals():
    from rcp.providers.session_format import SessionFormat, extract_text
if "TurnFence" not in globals():
    from rcp.providers.turn_fence import TurnFence


def attributed_ids(value: dict) -> set[str]:
    identifiers = value.get("user_message_uuids")
    result = (
        {item for item in identifiers if isinstance(item, str) and item.strip()}
        if isinstance(identifiers, list)
        else set()
    )
    identifier = value.get("user_message_uuid")
    if not result and isinstance(identifier, str) and identifier.strip():
        result.add(identifier)
    return result


def is_claude_task_notice(value: dict, sent_ids: set[str], finished_ids: set[str]) -> bool:
    origin = value.get("origin")
    return (
        value.get("subtype") == "success"
        and value.get("is_error") is not True
        and isinstance(origin, dict)
        and origin.get("kind") == "task-notification"
        and not finished_ids & sent_ids
    )


def claude_project_directory(cwd: Path) -> str:
    """Claude Code's project key, including its long-path hash (2.1.307)."""
    encoded = str(cwd).encode("utf-16-le")
    units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
    sanitized = "".join(re.sub(r"[^a-zA-Z0-9]", "-", chr(unit)) for unit in units)
    if len(sanitized) <= 200:
        return sanitized
    value = 0
    for unit in units:
        value = (31 * value + unit) & 0xFFFFFFFF
    value = abs(value if value < 0x80000000 else value - 0x100000000)
    suffix = ""
    while value:
        value, remainder = divmod(value, 36)
        suffix = "0123456789abcdefghijklmnopqrstuvwxyz"[remainder] + suffix
    return sanitized[:200] + "-" + (suffix or "0")


class ClaudeSessionFormat(SessionFormat):
    # Subagent transcripts sit beside their parent and are not sessions.
    skipped_path_parts = frozenset({"subagents"})
    default_thread_source = "user"
    default_source_kind = "claude"

    def prepare_session_fork(self, *, source_session_id: str, source_cwd: Path, cwd: Path) -> None:
        # Claude resolves --resume within the current project directory. Copy
        # the transcript, never move it or launch from the source stage.
        uuid.UUID(source_session_id)
        source_cwd = source_cwd.resolve()
        cwd = cwd.resolve()
        if cwd == source_cwd:
            raise ValueError("A Claude session fork requires a separate working directory.")
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        projects = config / "projects"
        source = projects / claude_project_directory(source_cwd) / f"{source_session_id}.jsonl"
        target = projects / claude_project_directory(cwd) / source.name
        if target == source:
            raise ValueError("Claude source and fork directories share a session namespace.")
        if target.exists():
            # Retry keeps the prepared snapshot when the source chat advances.
            identifiers = {
                record.get("sessionId")
                for line in target.read_text(encoding="utf-8").splitlines()
                if line.strip()
                and isinstance(record := json.loads(line), dict)
                and record.get("sessionId")
            }
            if identifiers != {source_session_id}:
                raise ValueError(
                    "The fork directory already contains a different Claude transcript."
                )
            return
        if not source.is_file():
            raise FileNotFoundError(
                "The source Claude session transcript is unavailable on this machine."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def record_fields(self, raw: dict[str, Any]) -> tuple[Any, str, Any, str, Any]:
        raw_type = str(raw.get("type", ""))
        role = raw_type if raw_type in {"user", "assistant", "system"} else "unknown"
        message = raw.get("message", {})
        text = extract_text(message.get("content") if isinstance(message, dict) else message)
        return raw.get("uuid"), raw_type, role, text, raw.get("timestamp")


class ClaudeStreamTurnFence(TurnFence):
    """Claude's stream-json session, which reports each prompt it finishes."""

    @property
    def prompt_started(self) -> bool:
        return bool(self.message_ids)

    def _input(self, value: dict) -> None:
        if value.get("type") == "user" and isinstance(value.get("uuid"), str):
            self.message_ids.setdefault(value["uuid"], None)

    def _output(self, value: dict) -> None:
        kind = value.get("type")
        identifier = value.get("command_uuid")
        if kind == "command_lifecycle" and identifier in self.message_ids:
            if value.get("state") in {"queued", "started"}:
                self.outstanding.add(identifier)
            elif value.get("state") == "completed":
                self.outstanding.discard(identifier)
        if kind == "error":
            self.terminal = True
            return
        if kind != "result":
            return
        finished = attributed_ids(value)
        if is_claude_task_notice(value, set(self.message_ids), finished):
            return
        self.outstanding.difference_update(finished)
        # A failure ends the turn even with inputs still out: nothing is coming
        # back for them. This is the one place a boundary needs to know that a
        # result went badly, and it still says nothing about what to report.
        failed = (
            value.get("is_error") is True or "error" in str(value.get("subtype") or "").casefold()
        )
        if finished and self.outstanding and not failed:
            return
        self.terminal = True
