"""Small stdlib file operations shipped to the compute execution account."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def operate(operation: str, path: str, payload: str) -> str | None:
    target = Path(path)
    if operation == "resolve":
        return str(target.expanduser())
    if operation == "prepare":
        target.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            for name, content in json.loads(payload).items():
                file = target / name
                file.write_text(content, encoding="utf-8")
                file.chmod(0o700 if name == "run.sh" else 0o600)
        except BaseException:
            shutil.rmtree(target)
            raise
        return None
    if operation == "read":
        try:
            with target.open("rb") as stream:
                size = int(payload)
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - size))
                return stream.read(size).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
    if operation == "write":
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, target)
        return None
    if operation == "remove":
        shutil.rmtree(target)
        return None
    raise ValueError("unknown compute file operation")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    print(json.dumps(operate(argv[1], argv[2], argv[3])))
    return 0


if __name__ == "__main__":  # pragma: no cover - shipped source
    raise SystemExit(main(sys.argv))
