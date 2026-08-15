"""Read one kept result view out of a repository on the execution machine.

RCP ships this module's *own source* to the execution machine and runs it with
``python -c``. Unlike its siblings this module is also imported in-process, for
the exit-status constants below — the caller and the script must agree on them,
and importing is the only way to guarantee they cannot drift apart.

Protocol. ``argv`` is ``(repository, name, max_bytes)``. The view's bytes go to
stdout on success; otherwise the exit status is one of the constants below.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

MISSING = 44
TOO_LARGE = 45
UNSAFE = 46


def main() -> None:
    repository = Path(sys.argv[1])
    name = sys.argv[2]
    max_bytes = int(sys.argv[3])
    if (
        not repository.is_absolute()
        or str(repository) == "/"
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,238})[.]html", name)
        or max_bytes < 1
        or max_bytes > 16 * 1024 * 1024
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise SystemExit(UNSAFE)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        repository_fd = os.open(repository, directory_flags)
    except FileNotFoundError:
        raise SystemExit(MISSING) from None
    except OSError:
        raise SystemExit(UNSAFE) from None
    try:
        try:
            views_fd = os.open("views", directory_flags, dir_fd=repository_fd)
        except FileNotFoundError:
            raise SystemExit(MISSING) from None
        except OSError:
            raise SystemExit(UNSAFE) from None
        try:
            try:
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=views_fd)
            except FileNotFoundError:
                raise SystemExit(MISSING) from None
            except OSError:
                raise SystemExit(UNSAFE) from None
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise SystemExit(UNSAFE)
                if info.st_size > max_bytes:
                    raise SystemExit(TOO_LARGE)
                remaining = max_bytes + 1
                chunks = []
                while remaining > 0:
                    chunk = os.read(file_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                if len(data) > max_bytes:
                    raise SystemExit(TOO_LARGE)
                sys.stdout.buffer.write(data)
            finally:
                os.close(file_fd)
        finally:
            os.close(views_fd)
    finally:
        os.close(repository_fd)


if __name__ == "__main__":
    main()
