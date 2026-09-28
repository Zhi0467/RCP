"""List or check directories on one machine account, locally or as shipped SSH source.

Standard library only: setup's repository browser and the space folder picker
ship this module's source to a remote machine and also call it in-process.
"""

from __future__ import annotations

import json
import os
import sys


def browse_directory(
    path: str | None,
    *,
    name_filter: str = "",
    offset: int = 0,
    limit: int,
) -> dict[str, object]:
    """One page of the directories directly inside `path`, filtered before paging."""

    home = os.path.expanduser("~")
    target = home if path is None else path
    if not os.path.isabs(target):
        raise ValueError("directory browser path must be absolute")
    if offset < 0 or limit < 1:
        raise ValueError("directory browser page is invalid")
    current = os.path.realpath(target)
    needle = name_filter.casefold()
    names: list[str] = []
    with os.scandir(current) as iterator:
        for entry in iterator:
            if needle and needle not in entry.name.casefold():
                continue
            try:
                if entry.is_dir(follow_symlinks=True):
                    names.append(entry.name)
            except OSError:
                continue
    names.sort(key=str.casefold)
    page = names[offset : offset + limit]
    entries = []
    for name in page:
        entry_path = os.path.join(current, name)
        entries.append(
            {
                "name": name,
                "path": entry_path,
                "git_repository": os.path.exists(os.path.join(entry_path, ".git")),
                "has_research": os.path.isdir(os.path.join(entry_path, ".research")),
            }
        )
    end = offset + len(page)
    return {
        "home": home,
        "path": current,
        "parent": None if current == "/" else os.path.dirname(current),
        "entries": entries,
        "total": len(names),
        "next_offset": end if end < len(names) else None,
    }


def check_directories(paths: list[str]) -> dict[str, object]:
    """The account home, and each path's real location if it is a directory."""

    return {
        "home": os.path.expanduser("~"),
        "resolved": {
            path: os.path.realpath(path) if os.path.isdir(path) else None for path in paths
        },
    }


def handle(request: dict[str, object]) -> dict[str, object]:
    if request.get("mode") == "check":
        paths = request.get("paths")
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
            raise ValueError("directory check needs a list of paths")
        return check_directories(paths)
    path = request.get("path")
    name_filter = request.get("filter", "")
    offset = request.get("offset", 0)
    limit = request.get("limit")
    if (
        (path is not None and not isinstance(path, str))
        or not isinstance(name_filter, str)
        or not isinstance(offset, int)
        or not isinstance(limit, int)
    ):
        raise ValueError("directory browser request is invalid")
    return browse_directory(path, name_filter=name_filter, offset=offset, limit=limit)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        request = json.loads(argv[1])
        if not isinstance(request, dict):
            return 2
        result = handle(request)
    except (OSError, ValueError) as exc:
        result = {"error": str(exc)[:600]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through shipped source
    raise SystemExit(main(sys.argv))
