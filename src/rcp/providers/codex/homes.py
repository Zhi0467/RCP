"""Codex home policy and legacy migration, backed by shared overlay machinery."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or "HomeLayout" not in globals():
    from rcp.providers.login_homes import MIGRATION_LOCK as MIGRATION_LOCK
    from rcp.providers.login_homes import (
        HomeLayout,
        run_home,
    )
    from rcp.providers.login_homes import (
        home_lease as _home_lease,
    )
    from rcp.providers.login_homes import (
        prepare_home as _prepare_home,
    )

CREDENTIAL_FILES = ("auth.json",)
SHARED_FILES = ("config.toml", "rules", "skills", "AGENTS.md")
SHARED_SESSIONS = ("sessions", "archived_sessions", "session_index.jsonl")
SHARED_DIRECTORIES = (
    "cache",
    "plugins",
    "shell_snapshots",
    "thread-writer-locks",
    "tmp",
)
SHARED_STATE = (
    "installation_id",
    "history.jsonl",
    "models_cache.json",
    "cloud-config-bundle-cache.json",
    ".sandbox_migration",
    "state_5.sqlite",
    "memories_1.sqlite",
    "logs_2.sqlite",
    "queue_1.sqlite",
    "goals_1.sqlite",
    "thread_history_1.sqlite",
)


_LAYOUT = HomeLayout(
    independent_paths=CREDENTIAL_FILES,
    shared_files=SHARED_FILES + SHARED_STATE,
    shared_directories=SHARED_DIRECTORIES,
    session_paths=SHARED_SESSIONS,
    retain_private=True,
)


def prepare_home(home: Path, machine_home: Path | None = None, *, migrate: bool = True) -> Path:
    return _prepare_home(
        home,
        machine_home or Path(os.environ.get("CODEX_HOME", "~/.codex")),
        _LAYOUT,
        migrate=migrate,
        legacy_idle=_legacy_home_idle,
    )


def relocate_home(legacy: Path, home: Path) -> Path:
    """Move a home out of the hidden credential namespace; return the home to use.

    Codex starts its sandbox helper from inside its home, so the home must stay
    readable. The old path always links to the home, so a rolled-back release
    uses the same home and credential instead of starting a second one.
    A home a process may still use stays in place until a later preparation.
    """
    if legacy.is_symlink():
        return home
    if legacy.exists():
        with _home_lease(legacy) as (_, exclusive):
            if not exclusive or not _legacy_home_idle(legacy):
                print("Codex home move deferred: a process may still use it.", file=sys.stderr)
                return legacy
            home.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            retained = legacy.with_name(legacy.name + "-retained")
            if retained.exists():
                retained.rename(home.with_name(home.name + "-retained"))
            legacy.rename(home)
    legacy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    legacy.symlink_to(home)
    return home


def _legacy_home_idle(home: Path) -> bool:
    """Fail closed for migration when execution-host process inspection is unavailable."""
    try:
        processes = subprocess.run(
            ["ps", "eww", "-U", str(os.getuid()), "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
            env={key: value for key, value in os.environ.items() if key != "CODEX_HOME"},
        )
        if not processes.stdout.strip():
            return False
        pattern = r"(?:^|\s)CODEX_HOME=" + re.escape(str(home)) + r"(?:\s|$)"
        for line in processes.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or int(fields[0]) == os.getpid():
                continue
            if re.search(pattern, fields[1]):
                return False
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _run(home: Path, command: list[str]) -> None:
    machine_home = Path(os.environ["RCP_CODEX_MACHINE_HOME"])
    with run_home(home, machine_home, _LAYOUT, legacy_idle=_legacy_home_idle):
        os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    if sys.argv[1] == "--run":
        _run(Path(sys.argv[2]), sys.argv[3:])
    else:
        print(prepare_home(Path(sys.argv[1]), migrate="--links-only" not in sys.argv[2:]))
