"""Shipped state tar endpoint; host extraction needs only POSIX tar flags."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile


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
        # Extract into a private folder, then rename complete files into place:
        # an abandoned earlier attempt can then never leave a truncated file.
        incoming = tempfile.mkdtemp(prefix=".incoming-", dir=root)
        try:
            code = subprocess.run(["tar", "-xpf", "-"], cwd=incoming, check=False).returncode
            if code:
                return code
            for folder, _directories, files in os.walk(incoming):
                target = os.path.join(root, os.path.relpath(folder, incoming))
                os.makedirs(target, exist_ok=True)
                for name in files:
                    os.replace(os.path.join(folder, name), os.path.join(target, name))
            return 0
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
    raise ValueError("unknown state transfer operation")


if __name__ == "__main__":
    raise SystemExit(main())
