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

        def read(name: str, limit: int, directory_fd: int | None = None) -> str:
            fd = os.open(
                name,
                flags | os.O_NONBLOCK,
                dir_fd=journal_fd if directory_fd is None else directory_fd,
            )
            with os.fdopen(fd, "rb") as stream:
                entry = os.fstat(stream.fileno())
                if not stat.S_ISREG(entry.st_mode) or entry.st_size > limit:
                    raise ValueError("Provider journal entry is unsafe or exceeds its bound.")
                content = stream.read(limit + 1)
                if len(content) > limit:
                    raise ValueError("Provider journal entry exceeds its bound.")
                # A provider may emit a byte that is not valid UTF-8. Round-trip
                # the exact bytes so the caller can re-encode them the same way
                # and verify the writer's digest. The caller then re-decodes each
                # field the way the live path decodes that same channel; nothing
                # here decides that, because nothing here knows which channel the
                # field stands in for.
                return content.decode("utf-8", "surrogateescape")

        outcome = json.loads(read("outcome.json", max_bytes))
        try:
            # Written before the prompt moved and never rewritten. Its absence is
            # the host saying this pass never took the work, which is the only
            # answer that makes a replacement safe.
            accepted = json.loads(read("accepted.json", max_bytes))
        except (OSError, ValueError):
            accepted = None
        try:
            # The provider's own diagnostic, which the live pipe also reports.
            # Its absence must never downgrade an otherwise readable journal.
            errors = read("stderr.txt", max_bytes)
        except (OSError, ValueError):
            errors = ""
        experiment_watch: dict[str, str] = {}
        recorded_names = outcome.get("experiment_watch_sha256")
        if isinstance(recorded_names, dict) and recorded_names:
            # One directory of per-resource outputs. The outcome names them, so
            # nothing here lists a directory the agent could have added to.
            resource_fd = os.open("experiment-watch", flags | os.O_DIRECTORY, dir_fd=journal_fd)
            descriptors.append(resource_fd)
            for name in sorted(recorded_names):
                experiment_watch[name] = read(name, max_bytes, resource_fd)
        return {
            "accepted": accepted,
            "outcome": outcome,
            "events": read("events.jsonl", max_bytes),
            "delegation": read("delegation.jsonl", max_bytes)
            if outcome.get("delegation_sha256")
            else None,
            "stderr": errors,
            "patch": read("patch.json", max_bytes) if outcome.get("patch_present") else None,
            "watch": read("watch.json", max_bytes) if outcome.get("watch_present") else None,
            "experiment_watch": experiment_watch,
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
        # A distinct status, because the caller must tell "this host cannot be
        # asked" from "this host answered and its evidence is bad". The first
        # waits; the second is a turn that will never be recoverable and has to
        # say so to a human.
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
