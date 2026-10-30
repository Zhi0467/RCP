"""Prepare isolated Codex homes; this module also runs unchanged over SSH."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SHARED_FILES = ("config.toml", "rules", "skills", "AGENTS.md")


def prepare_home(home: Path, machine_home: Path | None = None) -> Path:
    """Refresh shared configuration links, refusing to replace independent files."""
    home = home.expanduser().absolute()
    machine_home = (machine_home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser()
    machine_home = machine_home.absolute()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    for name in SHARED_FILES:
        link = home / name
        if link.exists() and not link.is_symlink():
            raise FileExistsError(f"Codex home link became a regular file or directory: {link}")
    for name in SHARED_FILES:
        link = home / name
        target = machine_home / name
        if link.is_symlink():
            if link.readlink() == target:
                continue
            link.unlink()
        link.symlink_to(target)
    return home


if __name__ == "__main__":
    print(prepare_home(Path(sys.argv[1])))
