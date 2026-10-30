"""Prepare isolated Codex homes; this module also runs unchanged over SSH."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SHARED_FILES = ("config.toml", "rules", "skills", "AGENTS.md")
# One session store for every login, so any usable login can resume any session.
SHARED_SESSIONS = ("sessions", "archived_sessions", "session_index.jsonl")


def _move_sessions(source: Path, target: Path) -> None:
    """Move a pre-link session store into the machine store, never overwriting."""
    if source.is_file():
        known = set(target.read_text().splitlines()) if target.exists() else set()
        missing = [line for line in source.read_text().splitlines() if line not in known]
        if missing:
            with target.open("a") as index:
                index.writelines(line + "\n" for line in missing)
        source.unlink()
        return
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        destination = target / path.relative_to(source)
        if destination.exists():
            if destination.read_bytes() != path.read_bytes():
                raise FileExistsError(f"Codex session differs from the shared copy: {path}")
            path.unlink()
            continue
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(path, destination)
    for path in sorted(source.rglob("*"), reverse=True):
        path.rmdir()
    source.rmdir()


def prepare_home(home: Path, machine_home: Path | None = None) -> Path:
    """Refresh shared links, refusing to replace independent configuration files."""
    home = home.expanduser().absolute()
    machine_home = (machine_home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser()
    machine_home = machine_home.absolute()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    for name in SHARED_FILES:
        link = home / name
        if link.exists() and not link.is_symlink():
            raise FileExistsError(f"Codex home link became a regular file or directory: {link}")
    for name in SHARED_SESSIONS:
        target = machine_home / name
        if name.endswith(".jsonl"):
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.touch(mode=0o600, exist_ok=True)
        else:
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        link = home / name
        if link.exists() and not link.is_symlink():
            _move_sessions(link, target)
    for name in SHARED_FILES + SHARED_SESSIONS:
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
