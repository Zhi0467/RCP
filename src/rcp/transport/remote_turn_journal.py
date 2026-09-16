"""Read one stopped provider's journal without following stage symlinks.

Shipped to the execution host from this module; only the standard library is
available there. The caller proves process absence before accepting the result.
"""

from __future__ import annotations

import json
import os
import stat
import sys


def read_journal(pid_file: str, max_bytes: int) -> dict[str, object]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        root, name = os.path.split(pid_file)
        root_fd = os.open(root, flags | os.O_DIRECTORY)
        descriptors.append(root_fd)
        info = os.fstat(root_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Provider stage ownership or permissions changed.")
        journal_fd = os.open(name + ".turn", flags | os.O_DIRECTORY, dir_fd=root_fd)
        descriptors.append(journal_fd)

        def read(name: str, limit: int) -> str:
            fd = os.open(name, flags | os.O_NONBLOCK, dir_fd=journal_fd)
            with os.fdopen(fd, "rb") as stream:
                entry = os.fstat(stream.fileno())
                if not stat.S_ISREG(entry.st_mode) or entry.st_size > limit:
                    raise ValueError("Provider journal entry is unsafe or exceeds its bound.")
                content = stream.read(limit + 1)
                if len(content) > limit:
                    raise ValueError("Provider journal entry exceeds its bound.")
                # A provider may emit a byte that is not valid UTF-8, and the
                # live pipe already decodes such a stream with a replacement
                # rather than failing. Collection must not be stricter than the
                # delivery it stands in for, so this round-trips the exact bytes
                # instead: the caller re-encodes the same way to verify the
                # writer's digest.
                return content.decode("utf-8", "surrogateescape")

        outcome = json.loads(read("outcome.json", max_bytes))
        try:
            # The provider's own diagnostic, which the live pipe also reports.
            # Its absence must never downgrade an otherwise readable journal.
            errors = read("stderr.txt", max_bytes)
        except (OSError, ValueError):
            errors = ""
        return {
            "outcome": outcome,
            "events": read("events.jsonl", max_bytes),
            "stderr": errors,
            "patch": read("patch.json", max_bytes) if outcome.get("patch_present") else None,
        }
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def main() -> int:
    try:
        result = read_journal(sys.argv[1], int(sys.argv[2]))
    except FileNotFoundError:
        print(json.dumps({"missing": True}))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
