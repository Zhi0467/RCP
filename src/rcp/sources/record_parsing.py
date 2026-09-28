"""Conversation record parsing shared by local indexing and remote execution.

This module is executed in two places: imported normally in this process, and
**shipped as source text** to a remote host inside `rcp.providers.remote_bundle`,
where it runs with `python3 -c`. That host has no virtualenv and no `rcp`
package, so this module may import only the standard library. Callers pass the
source's `SessionFormat`, so it needs no provider registry.

`tests/test_sources.py` checks that the shipped program runs without `rcp` and
produces byte-identical records to the local path for the same input.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from typing import Any

KNOWN_ROLES = frozenset({"user", "assistant", "system", "tool"})


def fallback_record_id(raw: dict[str, Any], line_number: int) -> str:
    """Derive a stable id for a record whose provider gave it none."""

    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return f"line-{line_number}-{digest}"


def normalize_record(raw: dict[str, Any], source_format: Any, line_number: int) -> dict[str, Any]:
    """Normalize one provider record.

    Returns a plain dict rather than a model so the remote copy needs no
    pydantic. `timestamp` stays as the provider wrote it; callers that want a
    datetime parse it themselves.
    """

    record_id, raw_type, role, text, timestamp = source_format.record_fields(raw)
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
