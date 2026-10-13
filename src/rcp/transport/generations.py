"""Immutable display copies; the workspace owns transfer and write sequencing."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)


def _remove_retired(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError:
        _LOG.warning("Could not remove retired display generation %s", path, exc_info=True)


_EXCLUDED = {".refresh.lock", ".agent-run.lock", ".append.lock", ".chat.lock", ".publish"}
_OWNERS_LOCK = threading.Lock()
_OWNERS: dict[str, ReadGenerations] = {}


def owner(root: Path) -> ReadGenerations:
    key = os.path.normcase(str(root.resolve()))
    with _OWNERS_LOCK:
        if key not in _OWNERS:
            _OWNERS[key] = ReadGenerations(root)
        return _OWNERS[key]


def families_for_path(path: str) -> set[str]:
    parts = list(Path(path).parts)
    if not parts:
        return set()
    if parts[0] == "branches" and len(parts) > 1:
        return {"refs", f"graph:branch:{parts[1]}"}
    if parts[0] == "chat":
        return {"chats", f"chat:{Path(parts[1]).stem[-36:]}"} if len(parts) > 1 else {"chats"}
    if parts[0] in {"paper", "paper.md", "introduction.md"}:
        return {"paper"}
    if parts[0] == "manifest.toml":
        return {"settings"}
    if parts[0] in {
        "patches",
        "graph.json",
        "research.md",
        "glossary.json",
        "proposals.json",
        "coverage.json",
        "cursors.json",
        "scope-base.json",
    }:
        return {"graph:main"}
    return set()


_GRAPH_JSON = r"(?:graph|glossary|proposals|coverage|cursors|scope-base)\.json"
_CANONICAL_JSON = re.compile(
    rf"(?:{_GRAPH_JSON}|patches/(?:batch-[^/]+/)?[0-9]{{6}}\.json|"
    rf"branches/[^/]+/(?:{_GRAPH_JSON}|branch\.json|"
    r"patches/[0-9]{6}\.json|merges/[a-f0-9]{64}\.json))"
)


def _validate_content(relative: Path, data: bytes) -> None:
    """Reject broken canonical documents, without parsing scratch or replaying history."""
    try:
        if relative.parent == Path("chat") and relative.suffix == ".jsonl":
            for line in data.decode("utf-8").splitlines():
                if line.strip():
                    json.loads(line)
        elif _CANONICAL_JSON.fullmatch(relative.as_posix()):
            json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid canonical document {relative}: {exc}") from exc


class ReadGenerations:
    def __init__(self, root: Path) -> None:
        self.directory = root.parent / f"{root.name}-generations"
        self.guard = threading.Lock()
        self.thread = threading.local()
        self.restored = False
        self.current: Path | None = None
        self.readers: dict[Path, int] = {}
        self.hashes: dict[str, str] = {}
        self.epoch = uuid.uuid4().hex
        self.versions: dict[str, int] = {}
        self.completed: datetime | None = None
        self.error: str | None = None
        self.syncing = False
        self.request_sync: Callable[[], None] | None = None

    def restore(self) -> None:
        """Load the last published descriptor once, only for remote workspaces."""
        with self.guard:
            if self.restored:
                return
            self.restored = True
            descriptor = self.directory / "current.json"
            try:
                data = json.loads(descriptor.read_text(encoding="utf-8"))
                name = data["directory"]
                if not isinstance(name, str) or uuid.UUID(hex=name).hex != name:
                    raise ValueError("Invalid generation directory")
                current = self.directory / name
                if current.is_symlink() or not current.is_dir():
                    raise ValueError("Published generation is unavailable")
                hashes, versions = data["hashes"], data["versions"]
                if not isinstance(hashes, dict) or not all(
                    isinstance(key, str) and isinstance(value, str) for key, value in hashes.items()
                ):
                    raise ValueError("Invalid generation fingerprints")
                if not isinstance(versions, dict) or not all(
                    isinstance(key, str) and isinstance(value, int)
                    for key, value in versions.items()
                ):
                    raise ValueError("Invalid generation versions")
                self.completed = datetime.fromisoformat(data["completed"])
                self.current, self.hashes, self.versions = current, hashes, versions
            except FileNotFoundError:
                return
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.error = f"Cannot restore display generation: {exc}"
                return
            for path in self.directory.iterdir():
                if path != current and path.is_dir() and not path.is_symlink():
                    try:
                        if uuid.UUID(hex=path.name).hex == path.name:
                            _remove_retired(path)
                    except ValueError:
                        continue

    def tokens(self, families: Iterable[str]) -> dict[str, str]:
        with self.guard:
            return {family: f"{self.epoch}:{self.versions.get(family, 0)}" for family in families}

    def bump(self, paths: Iterable[Path | str]) -> None:
        families = set().union(*(families_for_path(str(path)) for path in paths))
        with self.guard:
            for family in families:
                self.versions[family] = self.versions.get(family, 0) + 1

    @contextmanager
    def pin(self) -> Iterator[Path | None]:
        with self.guard:
            path = self.current
            if path is not None:
                self.readers[path] = self.readers.get(path, 0) + 1
        try:
            yield path
        finally:
            retired = None
            with self.guard:
                if path is not None:
                    self.readers[path] -= 1
                    if not self.readers[path]:
                        del self.readers[path]
                        if path != self.current:
                            retired = path
            if retired is not None:
                _remove_retired(retired)

    def stage(self) -> Path:
        path = self.directory / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path

    def copy(self, root: Path) -> None:
        stage = self.stage()
        try:
            shutil.copytree(
                root, stage, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*_EXCLUDED)
            )
            self.publish(stage)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _write_descriptor(
        self,
        path: Path,
        hashes: dict[str, str],
        versions: dict[str, int],
        completed: datetime,
    ) -> Path:
        descriptor = self.directory / ".current.tmp"
        descriptor.write_text(
            json.dumps(
                {
                    "directory": path.name,
                    "hashes": hashes,
                    "versions": versions,
                    "completed": completed.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return descriptor

    def publish(self, stage: Path) -> None:
        hashes: dict[str, str] = {}
        for path in stage.rglob("*"):
            if path.is_symlink():
                raise ValueError("Canonical display state contains a symbolic link")
            if path.is_file():
                relative = path.relative_to(stage)
                data = path.read_bytes()
                _validate_content(relative, data)
                hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
        changed = {
            path
            for path in self.hashes.keys() | hashes.keys()
            if self.hashes.get(path) != hashes.get(path)
        }
        if not changed and self.current is not None:
            shutil.rmtree(stage)
            completed = datetime.now(UTC)
            descriptor = self._write_descriptor(self.current, hashes, self.versions, completed)
            with self.guard:
                descriptor.replace(self.directory / "current.json")
                self.completed = completed
                self.error = None
            return
        versions = self.versions.copy()
        for family in set().union(*(families_for_path(path) for path in changed)):
            versions[family] = versions.get(family, 0) + 1
        completed = datetime.now(UTC)
        descriptor = self._write_descriptor(stage, hashes, versions, completed)
        retired = None
        with self.guard:
            descriptor.replace(self.directory / "current.json")
            old = self.current
            self.current = stage
            self.hashes = hashes
            self.versions = versions
            self.completed = completed
            self.error = None
            if old is not None and old not in self.readers:
                retired = old
        if retired is not None:
            _remove_retired(retired)

    def clear(self) -> None:
        """A confirmed archive invalidates the published home, retaining active pins."""
        with self.guard:
            old = self.current
            self.current = None
            self.hashes = {}
            self.completed = None
            for family in self.versions:
                self.versions[family] += 1
            (self.directory / "current.json").unlink(missing_ok=True)
            retired = old if old is not None and old not in self.readers else None
        if retired is not None:
            _remove_retired(retired)

    def health(self) -> dict:
        with self.guard:
            return {
                "last_completed_at": self.completed.isoformat() if self.completed else None,
                "status": "syncing" if self.syncing else "error" if self.error else "ok",
                "error": self.error,
            }
