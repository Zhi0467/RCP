"""Conversation record parsing shared by local indexing and remote execution.

This module is executed in two places: imported normally in this process, and
**shipped as source text** to a remote host by `indexer.py`, where it is
prepended to a small driver and run with `python3 -c`. That remote host has no
virtualenv and no `rcp` package, so this module may import **only** the standard
library and must not import anything else from `rcp`.

`tests/test_sources.py` enforces both halves of that contract: the import
restriction, and that the shipped program produces byte-identical records to the
local path for the same input.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from typing import Any

TEXT_CHUNK_TYPES = frozenset({"text", "input_text", "output_text"})
KNOWN_ROLES = frozenset({"user", "assistant", "system", "tool"})


def extract_text(content: Any) -> str:
    """Flatten a provider content field into plain text."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict) and item.get("type") in TEXT_CHUNK_TYPES:
            value = item.get("text")
            if isinstance(value, str):
                chunks.append(value)
    return "\n".join(chunks)


def fallback_record_id(raw: dict[str, Any], line_number: int) -> str:
    """Derive a stable id for a record whose provider gave it none."""

    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return f"line-{line_number}-{digest}"


class SessionFormat:
    """How one source writes its native session files.

    Every provider-specific fact about session files lives in one subclass
    here, because this module is shipped to execution hosts that cannot import
    the provider registry. `SESSION_FORMATS` is keyed by the same ids.
    """

    #: Path components that mark files belonging to another session.
    skipped_path_parts: frozenset[str] = frozenset()
    #: Metadata a file starts with before any of its records is read.
    default_thread_source: str | None = None
    default_source_kind: str | None = None
    #: Whether a file without a working directory is malformed.
    requires_cwd = True

    def skip_file(self, parts: tuple[str, ...]) -> bool:
        return any(part in self.skipped_path_parts for part in parts)

    def initial_metadata(self) -> dict[str, Any]:
        return {
            "cwd": "",
            "session_id": "",
            "thread_source": self.default_thread_source,
            "parent_session_id": None,
            "originator": None,
            "source_kind": self.default_source_kind,
        }

    def read_metadata(self, raw: dict[str, Any], metadata: dict[str, Any]) -> None:
        """Fold one raw record's session facts into `metadata`."""
        metadata["cwd"] = raw.get("cwd", metadata["cwd"])
        metadata["session_id"] = raw.get("sessionId", metadata["session_id"])

    def record_fields(self, raw: dict[str, Any]) -> tuple[Any, str, Any, str, Any]:
        """Return `(record_id, raw_type, role, text, timestamp)` for one record."""
        return (
            raw.get("uuid") or raw.get("id"),
            str(raw.get("type", "")),
            raw.get("role", "unknown"),
            str(raw.get("text", raw.get("content", ""))),
            raw.get("timestamp"),
        )


class CodexSessionFormat(SessionFormat):
    def read_metadata(self, raw: dict[str, Any], metadata: dict[str, Any]) -> None:
        if raw.get("type") != "session_meta":
            super().read_metadata(raw, metadata)
            return
        payload = raw.get("payload", {})
        metadata["cwd"] = payload.get("cwd", metadata["cwd"])
        metadata["session_id"] = (
            payload.get("id") or payload.get("session_id") or metadata["session_id"]
        )
        metadata["thread_source"] = payload.get("thread_source") or metadata["thread_source"]
        metadata["originator"] = payload.get("originator") or metadata["originator"]
        source = payload.get("source")
        if isinstance(source, str):
            metadata["source_kind"] = source
        elif isinstance(source, dict):
            if "subagent" in source:
                metadata["source_kind"] = "subagent"
            subagent = source.get("subagent")
            spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            if isinstance(spawn, dict):
                metadata["parent_session_id"] = (
                    spawn.get("parent_thread_id") or metadata["parent_session_id"]
                )

    def record_fields(self, raw: dict[str, Any]) -> tuple[Any, str, Any, str, Any]:
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        record_id = payload.get("id") or raw.get("id")
        raw_type = f"{raw.get('type', '')}:{payload.get('type', '')}".rstrip(":")
        role = payload.get("role", "unknown")
        text = extract_text(payload.get("content"))
        if not text and payload.get("type") in {"user_message", "agent_message"}:
            text = str(payload.get("message", ""))
            role = "user" if payload.get("type") == "user_message" else "assistant"
        if not text and payload.get("type") == "custom_tool_call":
            tool_input = payload.get("input")
            if isinstance(tool_input, str):
                text = tool_input
                role = "assistant"
        return record_id, raw_type, role, text, payload.get("timestamp") or raw.get("timestamp")


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


class AppChatSessionFormat(SessionFormat):
    """RCP's own chat records, which are indexed beside provider sessions."""

    default_thread_source = "user"
    default_source_kind = "app_chat"
    requires_cwd = False


SESSION_FORMATS: dict[str, SessionFormat] = {
    "codex": CodexSessionFormat(),
    "claude": ClaudeSessionFormat(),
    "app_chat": AppChatSessionFormat(),
}


def session_format(source: str) -> SessionFormat:
    return SESSION_FORMATS.get(source) or SessionFormat()


def normalize_record(raw: dict[str, Any], provider: str, line_number: int) -> dict[str, Any]:
    """Normalize one provider record.

    Returns a plain dict rather than a model so the remote copy needs no
    pydantic. `timestamp` stays as the provider wrote it; callers that want a
    datetime parse it themselves.
    """

    record_id, raw_type, role, text, timestamp = session_format(provider).record_fields(raw)
    if role not in KNOWN_ROLES:
        role = "unknown"
    return {
        "uuid": str(record_id or fallback_record_id(raw, line_number)),
        "timestamp": timestamp,
        "role": role,
        "text": text,
        "raw_type": raw_type,
    }


def normalize_path(value: str) -> str:
    if not value:
        return ""
    return posixpath.normpath(value.replace("\\", "/"))


def path_matches_roots(cwd: str, roots: list[str]) -> bool:
    """True when `cwd` is one of `roots` or sits inside one of them."""

    normalized = normalize_path(cwd)
    for root in roots:
        normalized_root = normalize_path(root)
        if normalized == normalized_root or normalized.startswith(
            normalized_root.rstrip("/") + "/"
        ):
            return True
    return False
