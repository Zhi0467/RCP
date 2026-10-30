"""Share Codex state, isolating credentials; also executed unchanged over SSH."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

CREDENTIAL_FILES = ("auth.json",)
SHARED_FILES = ("config.toml", "rules", "skills", "AGENTS.md")
SHARED_SESSIONS = ("sessions", "archived_sessions", "session_index.jsonl")
# Prelink stores Codex 0.165 creates even when the machine home is still empty.
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


def _retain(source: Path, retained: Path) -> None:
    """Keep displaced bytes together, including SQLite journals, across interruption."""
    target = retained / source.name
    retained.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    retained.mkdir(mode=0o700, exist_ok=True)
    # A pending snapshot is reused after interruption. Already moved names are
    # absent from the source; a newly recreated name starts another snapshot.
    if target.exists() or target.is_symlink():
        retained.rename(retained.with_name(str(uuid.uuid4())))
        retained.mkdir(mode=0o700)
    source.rename(target)


def _move_sessions(source: Path, target: Path) -> None:
    """Merge a pre-link session store without overwriting differing rollouts."""
    if source.is_file():
        known = set(target.read_text().splitlines()) if target.exists() else set()
        missing = [line for line in source.read_text().splitlines() if line not in known]
        if missing:
            with target.open("a") as index:
                index.writelines(line + "\n" for line in missing)
                index.flush()
                os.fsync(index.fileno())
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
        _move_rollout(path, destination)
    for path in sorted(source.rglob("*"), reverse=True):
        path.rmdir()
    source.rmdir()


def _move_rollout(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    print("Codex home migration: copying rollout across filesystems.", file=sys.stderr)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".rcp-migration-", delete=False
    ) as output:
        temporary = Path(output.name)
        try:
            with source.open("rb") as original:
                shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, destination)
            source.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()


def prepare_home(home: Path, machine_home: Path | None = None, *, migrate: bool = True) -> Path:
    """Link noncredential state; callers permit migration only with no active turn.

    The credential startup gate serializes callers for one login. This machine
    lock additionally serializes merges from different added logins. It does not
    lock Codex's own writers; live homes must use migrate=False.
    """
    home = home.expanduser().absolute()
    machine_home = (machine_home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser()
    machine_home = machine_home.absolute()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    if home.resolve() == machine_home.resolve():
        return home
    machine_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _home_lease(home) as (_, exclusive):
        _prepare_locked(home, machine_home, migrate=migrate and exclusive)
    return home


@contextmanager
def _home_lease(home):
    """An execution-host lease survives exec and an SSH supervisor reconnect."""
    path = home.with_name(home.name + ".use.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as lease:
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            exclusive = True
        except BlockingIOError:
            fcntl.flock(lease.fileno(), fcntl.LOCK_SH)
            exclusive = False
        yield lease, exclusive


def _prepare_locked(home: Path, machine_home: Path, *, migrate: bool) -> None:
    if migrate and any(
        path.name not in CREDENTIAL_FILES and not path.is_symlink() for path in home.iterdir()
    ):
        # Releases before the home lease can leave a supervised SSH turn alive.
        # Inspect the execution account's process environments, never emit them.
        migrate = _legacy_home_idle(home)
        if not migrate:
            print(
                "Codex home migration deferred: an older process may still use it.", file=sys.stderr
            )
    with (machine_home / ".rcp-home-migration.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _prepare_links(home, machine_home, migrate=migrate)


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
    home = home.expanduser().absolute()
    machine_home = Path(os.environ["RCP_CODEX_MACHINE_HOME"]).expanduser().absolute()
    with _home_lease(home) as (lease, exclusive):
        _prepare_locked(home, machine_home, migrate=exclusive)
        fcntl.flock(lease.fileno(), fcntl.LOCK_SH)
        os.set_inheritable(lease.fileno(), True)
        os.execvp(command[0], command)


def _prepare_links(home: Path, machine_home: Path, *, migrate: bool) -> None:
    retained = home.with_name(home.name + "-retained") / "pending"
    names: set[str] = set(SHARED_FILES + SHARED_SESSIONS + SHARED_DIRECTORIES + SHARED_STATE)
    names.update(path.name for path in machine_home.iterdir())
    names.update(path.name for path in home.iterdir())
    if retained.exists():
        names.update(path.name for path in retained.iterdir())
    for name in sorted(names - set(CREDENTIAL_FILES)):
        link, target = home / name, machine_home / name
        if not migrate and (retained / name).exists():
            continue
        if link.is_symlink() and os.readlink(str(link)) == str(target):
            continue
        was_directory = link.is_dir() or (retained / name).is_dir()
        if link.exists() or link.is_symlink():
            if not migrate:
                continue
            if name in SHARED_SESSIONS and not link.is_symlink():
                if name.endswith(".jsonl"):
                    target.touch(mode=0o600, exist_ok=True)
                else:
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                _move_sessions(link, target)
            else:
                _retain(link, retained)
        if was_directory or name in SHARED_DIRECTORIES or name in ("sessions", "archived_sessions"):
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif name == "session_index.jsonl":
            target.touch(mode=0o600, exist_ok=True)
        link.symlink_to(target)
    if migrate and retained.exists():
        retained.rename(retained.with_name(str(uuid.uuid4())))


if __name__ == "__main__":
    if sys.argv[1] == "--run":
        _run(Path(sys.argv[2]), sys.argv[3:])
    else:
        print(prepare_home(Path(sys.argv[1]), migrate="--links-only" not in sys.argv[2:]))
