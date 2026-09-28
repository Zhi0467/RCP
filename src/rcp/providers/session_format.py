"""How native session files look: the shared base and RCP's own chat records.

This module runs in two places: imported normally in this process, and
**shipped as source text** to execution hosts by `rcp.providers.remote_bundle`,
where it runs under a bare `python3` with no `rcp` package. It may import only
the standard library.
"""

from __future__ import annotations

from typing import Any

TEXT_CHUNK_TYPES = frozenset({"text", "input_text", "output_text"})


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


class SessionFormat:
    """How one source writes its native session files.

    Each provider declares its subclass in its own `remote.py`, and its profile
    names an instance as `session_format`. `SESSION_FORMATS` maps source ids to
    those instances, locally and on execution hosts alike.
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


class AppChatSessionFormat(SessionFormat):
    """RCP's own chat records, which are indexed beside provider sessions."""

    default_thread_source = "user"
    default_source_kind = "app_chat"
    requires_cwd = False


#: Every indexable source by id. Registered providers are added by
#: `rcp.providers` here, and by the generated registration on a host.
SESSION_FORMATS: dict[str, SessionFormat] = {"app_chat": AppChatSessionFormat()}


def session_format(source: str) -> SessionFormat:
    try:
        return SESSION_FORMATS[source]
    except KeyError:
        raise ValueError(f"No session format is registered for {source!r}.") from None
