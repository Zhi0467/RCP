"""Provider executable discovery, shared with SSH execution hosts (stdlib only)."""

from __future__ import annotations

import os
import pwd
import shutil
import sys
from pathlib import Path


def discover_provider(provider: str, install_paths: tuple[str, ...]) -> str | None:
    discovered = shutil.which(provider)
    if discovered:
        # Keep symlinks intact: native updates may replace their versioned targets.
        return str(Path(discovered).absolute())
    try:
        account_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return None
    for relative in (f".local/bin/{provider}", *install_paths):
        candidate = account_home / relative
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def main() -> int:
    candidate = discover_provider(sys.argv[1], tuple(sys.argv[2:]))
    if candidate is None:
        return 1
    print(candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
