"""Rules for machine writable paths, shared by save, scopes, and remote launchers.

Standard library only: remote terminal launchers ship this module's source.
"""

from __future__ import annotations

import posixpath


def check_writable_path_text(path: str) -> str:
    """Return `path` normalized, or refuse text no launcher can mount safely."""

    if not isinstance(path, str) or not path:
        raise ValueError("a writable path must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError("a writable path cannot contain control characters")
    if ":" in path or "$" in path:
        raise ValueError("a writable path cannot contain ':' or '$'")
    if not path.startswith("/"):
        raise ValueError(f"a writable path must be absolute: {path}")
    normalized = posixpath.normpath(path)
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    if normalized == "/":
        raise ValueError("the filesystem root cannot be a writable path")
    return normalized


def _within(child: str, parent: str) -> bool:
    child = posixpath.normpath(child)
    parent = posixpath.normpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def refuse_grants_inside(grants: list[str], owned: list[str]) -> None:
    """Refuse a grant equal to or inside RCP's own storage."""

    for grant in grants:
        for path in owned:
            if _within(grant, path):
                raise ValueError(f"writable path {grant} is inside RCP's own storage at {path}")


def owned_paths_covered(grants: list[str], owned: list[str]) -> list[str]:
    """RCP-owned paths strictly inside some grant; they stay read-only there."""

    return sorted(
        {
            path
            for path in owned
            if any(
                _within(path, grant) and posixpath.normpath(path) != posixpath.normpath(grant)
                for grant in grants
            )
        }
    )
