"""Private member-owned service connections, independent of provider credentials."""

from __future__ import annotations

import json
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

from rcp.agents.provider_environment import _write_private
from rcp.keyed_locks import KeyedLocks
from rcp.storage import AppStore

_MEMBER_LOCKS = KeyedLocks()


class ConnectionError(Exception):
    def __init__(self, code: str, status: int, message: str = "") -> None:
        self.code, self.status, self.message = code, status, message or code
        super().__init__(self.message)


def member_connection_lock(store: AppStore, member_id: str):
    return _MEMBER_LOCKS(f"{store.path.resolve()}:{member_id}")


def _component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ConnectionError("connection_not_found", 404)
    return value


class ServiceConnections:
    def __init__(self, store: AppStore, member_id: str):
        self.store, self.member_id = store, member_id
        self.root = store.path.parent / "service-connections" / _component(member_id)

    def require_member(self) -> None:
        member = self.store.space_user(self.member_id)
        if member is None or member.removal_started_at is not None or member.removed_at is not None:
            raise ConnectionError("team_identity_invalid", 403)

    @contextmanager
    def locked(self):
        with member_connection_lock(self.store, self.member_id):
            self.require_member()
            yield

    def _mkdir(self, path: Path) -> None:
        directory = self.root.parent
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        for part in (self.root.name, *path.relative_to(self.root).parts):
            directory /= part
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)

    def _connection(self, connection_id: str) -> dict:
        path = self.root / "connections" / _component(connection_id) / "connection.json"
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            raise ConnectionError("connection_not_found", 404) from None

    def _selection(self) -> str:
        try:
            return json.loads((self.root / "settings.json").read_text())["dictation"]
        except FileNotFoundError:
            return "system"

    def summary(self) -> dict:
        with self.locked():
            connections = [
                json.loads(p.read_text())
                for p in sorted((self.root / "connections").glob("*/connection.json"))
            ]
            selection = self._selection()
            if selection not in {c["id"] for c in connections}:
                selection = "system"
            return {"dictation": selection, "connections": connections}

    def credentials(self, connection_id: str) -> tuple[dict, str]:
        with self.locked():
            connection = self._connection(connection_id)
            key = (self.root / "connections" / connection_id / "key").read_text()
            return connection, key

    def save(self, connection: dict, key: str) -> None:
        with self.locked():
            path = self.root / "connections" / _component(connection["id"])
            self._mkdir(path)
            _write_private(path / "key", key)
            # Publish the metadata last: incomplete writes are not usable connections.
            _write_private(path / "connection.json", json.dumps(connection))

    def select(self, connection_id: str) -> dict:
        with self.locked():
            if connection_id != "system":
                self._connection(connection_id)
            self._write_selection(connection_id)
            return {"dictation": connection_id}

    def _write_selection(self, connection_id: str) -> None:
        self._mkdir(self.root)
        path = self.root / "settings.json"
        settings = json.loads(path.read_text()) if path.exists() else {}
        settings["dictation"] = connection_id
        _write_private(path, json.dumps(settings))

    def disconnect(self, connection_id: str) -> None:
        with self.locked():
            self._connection(connection_id)
            if self._selection() == connection_id:
                self._write_selection("system")
            shutil.rmtree(self.root / "connections" / connection_id)

    def remove_member_data(self) -> None:
        # Removal deliberately accepts a fenced/tombstoned identity.
        with member_connection_lock(self.store, self.member_id):
            if self.root.exists():
                shutil.rmtree(self.root)
