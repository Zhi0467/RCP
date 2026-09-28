"""Claude Code's session files and turn boundaries, as an execution host reads them.

Shipped as source text to execution hosts by `rcp.providers.remote_bundle`,
after the shared bases, so it may import only the standard library. The guarded
imports serve the local copy.
"""

from __future__ import annotations

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


class ClaudeSessionFormat(SessionFormat):
    # Subagent transcripts sit beside their parent and are not sessions.
    skipped_path_parts = frozenset({"subagents"})
    default_thread_source = "user"
    default_source_kind = "claude"

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
