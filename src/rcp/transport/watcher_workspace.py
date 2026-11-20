"""Replace the agent's editable watcher folder locally or on an SSH stage."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path


def watcher_id(name: str) -> str:
    path = Path(name)
    if (
        path.name != name
        or path.suffix != ".json"
        or path.stem.startswith(".")
        or "\\" in name
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError("use a direct watchers/<watcher_id>.json file")
    return path.stem


def replace_watcher_folder(workspace: Path, files: dict[str, str]) -> None:
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("watcher workspace is unavailable")
    with tempfile.TemporaryDirectory(dir=workspace, prefix=".rcp-watchers-") as temp:
        staging = Path(temp) / "new"
        staging.mkdir(mode=0o700)
        for name, text in files.items():
            watcher_id(name)
            with (staging / name).open("x", encoding="utf-8") as out:
                out.write(text)
        current = workspace / "watchers"
        previous = Path(temp) / "old"
        with suppress(FileNotFoundError):
            os.replace(current, previous)
        try:
            os.replace(staging, current)
        except OSError:
            if os.path.lexists(previous):
                os.replace(previous, current)
            raise


if __name__ == "__main__":
    replace_watcher_folder(Path(sys.argv[1]), json.load(sys.stdin))
