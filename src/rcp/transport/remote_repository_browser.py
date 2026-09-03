"""List one bounded remote directory for the personal project setup browser."""

from __future__ import annotations

import json
import os
import sys


def browse_directory(path: str | None, *, max_entries: int) -> dict[str, object]:
    target = os.path.expanduser("~") if path is None else path
    if not os.path.isabs(target):
        raise ValueError("repository browser path must be absolute")
    current = os.path.realpath(target)
    entries: list[dict[str, object]] = []
    truncated = False
    with os.scandir(current) as iterator:
        for index, entry in enumerate(iterator):
            if index >= max_entries:
                truncated = True
                break
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
                entry_path = os.path.join(current, entry.name)
                entries.append(
                    {
                        "name": entry.name,
                        "path": entry_path,
                        "git_repository": os.path.exists(os.path.join(entry_path, ".git")),
                        "has_research": os.path.isdir(os.path.join(entry_path, ".research")),
                    }
                )
            except OSError:
                continue
    entries.sort(key=lambda item: str(item["name"]).casefold())
    parent = None if current == "/" else os.path.dirname(current)
    return {
        "path": current,
        "parent": parent,
        "entries": entries,
        "truncated": truncated,
    }


def main(argv: list[str]) -> int:
    if len(argv) not in {2, 3}:
        return 2
    try:
        maximum = int(argv[1])
        if maximum < 1:
            return 2
        result = browse_directory(argv[2] if len(argv) == 3 else None, max_entries=maximum)
    except (OSError, ValueError) as exc:
        result = {"error": str(exc)[:600]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through shipped source
    raise SystemExit(main(sys.argv))
