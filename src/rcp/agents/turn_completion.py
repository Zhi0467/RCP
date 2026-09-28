"""Stateless Claude result attribution, shared with the execution-host fence."""

from __future__ import annotations


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
