"""Shipped state tar endpoint; host extraction needs only POSIX tar flags."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile


def main() -> int:
    operation, root, raw_excludes = sys.argv[1:]
    if operation == "pull":
        excludes = set(json.loads(raw_excludes))

        # Filter before traversal: excluded staging trees may be changing while
        # canonical state is locked, and are neither read nor sent by rsync.
        def include(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
            return None if os.path.basename(member.name) in excludes else member

        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
            archive.add(root, arcname=".", filter=include)
        return 0
    if operation == "push":
        return subprocess.run(["tar", "-xf", "-"], cwd=root, check=False).returncode
    raise ValueError("unknown state transfer operation")


if __name__ == "__main__":
    raise SystemExit(main())
