"""Shared execution-host overlay homes; shipped as stdlib-only source."""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, NamedTuple

MIGRATION_LOCK = ".rcp-home-migration.lock"


class HomeLayout(NamedTuple):
    independent_paths: tuple[str, ...]
    shared_files: tuple[str, ...] = ()
    shared_directories: tuple[str, ...] = ()
    session_paths: tuple[str, ...] = ()
    retain_private: bool = False


def _retain(source: Path, retained: Path) -> None:
    """Keep displaced bytes together, including SQLite journals, across interruption."""
    target = retained / source.name
    retained.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    retained.mkdir(mode=0o700, exist_ok=True)
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
                raise FileExistsError(f"Provider session differs from the shared copy: {path}")
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
    print("Provider home migration: copying rollout across filesystems.", file=sys.stderr)
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


def prepare_home(
    home: Path,
    machine_home: Path,
    layout: HomeLayout,
    *,
    migrate: bool = True,
    legacy_idle: Callable[[Path], bool] | None = None,
) -> Path:
    """Prepare shared links, migrating private entries only without an active lease."""
    home = home.expanduser().absolute()
    machine_home = machine_home.expanduser().absolute()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    if home.resolve() == machine_home.resolve():
        return home
    machine_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with home_lease(home) as (_, exclusive):
        _prepare_locked(
            home, machine_home, layout, migrate=migrate and exclusive, legacy_idle=legacy_idle
        )
    return home


@contextmanager
def home_lease(home: Path) -> Iterator[tuple[IO[str], bool]]:
    """Hold an execution-host lease that can survive exec and supervisor reconnect."""
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


@contextmanager
def run_home(
    home: Path,
    machine_home: Path,
    layout: HomeLayout,
    *,
    legacy_idle: Callable[[Path], bool] | None = None,
) -> Iterator[Path]:
    """Prepare once and keep an inheritable shared lease around the caller's exec."""
    home = home.expanduser().absolute()
    machine_home = machine_home.expanduser().absolute()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    machine_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with home_lease(home) as (lease, exclusive):
        _prepare_locked(home, machine_home, layout, migrate=exclusive, legacy_idle=legacy_idle)
        fcntl.flock(lease.fileno(), fcntl.LOCK_SH)
        os.set_inheritable(lease.fileno(), True)
        yield home


def _prepare_locked(
    home: Path,
    machine_home: Path,
    layout: HomeLayout,
    *,
    migrate: bool,
    legacy_idle: Callable[[Path], bool] | None,
) -> None:
    if (
        migrate
        and legacy_idle is not None
        and any(
            path.name not in layout.independent_paths and not path.is_symlink()
            for path in home.iterdir()
        )
    ):
        migrate = legacy_idle(home)
        if not migrate:
            print(
                "Provider home migration deferred: an older process may still use it.",
                file=sys.stderr,
            )
    with (machine_home / MIGRATION_LOCK).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        resolved_home, resolved_machine = home.resolve(), machine_home.resolve()
        excluded = (
            resolved_home.relative_to(resolved_machine)
            if resolved_home.is_relative_to(resolved_machine)
            else None
        )
        _prepare_links(
            home,
            machine_home,
            layout,
            Path(),
            excluded,
            home.with_name(home.name + "-retained") / "pending",
            migrate=migrate,
        )


def _prepare_links(
    home: Path,
    machine_home: Path,
    layout: HomeLayout,
    relative: Path,
    excluded: Path | None,
    retained: Path,
    *,
    migrate: bool,
) -> None:
    configured = (
        layout.shared_files
        + layout.shared_directories
        + layout.session_paths
        + layout.independent_paths
    )
    names = {
        Path(name).relative_to(relative).parts[0]
        for name in configured
        if Path(name).is_relative_to(relative) and Path(name) != relative
    }
    names.update(path.name for path in machine_home.iterdir())
    names.update(path.name for path in home.iterdir())
    if retained.exists():
        names.update(path.name for path in retained.iterdir())
    excluded_names = {MIGRATION_LOCK}
    if excluded is not None and relative == excluded.parent:
        siblings = {path.name.removesuffix(".use.lock") for path in machine_home.glob("*.use.lock")}
        excluded_names.update(siblings)
        excluded_names.update(name + ".use.lock" for name in siblings)
        excluded_names.update(name + "-retained" for name in siblings)
    if migrate:
        for name in excluded_names:
            link = home / name
            if link.is_symlink():
                link.unlink()
    for name in sorted(names - excluded_names):
        path = relative / name
        if str(path) in layout.independent_paths or path == excluded:
            continue
        link, target = home / name, machine_home / name
        split = any(Path(item) != path and Path(item).is_relative_to(path) for item in configured)
        split = split or (excluded is not None and excluded.is_relative_to(path))
        if split:
            if link.is_symlink():
                if not migrate:
                    continue
                link.unlink()
            link.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            _prepare_links(link, target, layout, path, excluded, retained / name, migrate=migrate)
        else:
            _prepare_link(link, target, layout, str(path), retained, migrate=migrate)
    if migrate and retained.exists():
        retained.rename(retained.with_name(str(uuid.uuid4())))


def _prepare_link(
    link: Path, target: Path, layout: HomeLayout, relative: str, retained: Path, *, migrate: bool
) -> None:
    if not migrate and (retained / link.name).exists():
        return
    if link.is_symlink() and os.readlink(str(link)) == str(target):
        return
    was_directory = link.is_dir() or (retained / link.name).is_dir()
    if link.exists() or link.is_symlink():
        if not migrate:
            return
        if relative in layout.session_paths and not link.is_symlink():
            if relative.endswith(".jsonl"):
                target.touch(mode=0o600, exist_ok=True)
            else:
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            _move_sessions(link, target)
        elif layout.retain_private:
            _retain(link, retained)
        else:
            _migrate_private(link, target)
    if (
        was_directory
        or relative in layout.shared_directories
        or (relative in layout.session_paths and not relative.endswith(".jsonl"))
    ):
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif relative in layout.session_paths:
        target.touch(mode=0o600, exist_ok=True)
    link.symlink_to(target)


def _migrate_private(source: Path, target: Path) -> None:
    if source.is_dir() and not source.is_symlink():
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in source.iterdir():
            _migrate_private(child, target / child.name)
        source.rmdir()
    elif source.is_symlink():
        temporary = target.with_name(".rcp-migration-" + str(uuid.uuid4()))
        try:
            temporary.symlink_to(source.readlink())
            os.replace(temporary, target)
            source.unlink()
        finally:
            temporary.unlink(missing_ok=True)
    else:
        _move_rollout(source, target)
