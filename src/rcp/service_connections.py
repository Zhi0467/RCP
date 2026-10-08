"""Private member-owned service connections, independent of provider credentials."""

from __future__ import annotations

import json
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp import limits
from rcp.agents.provider_environment import _write_private
from rcp.keyed_locks import KeyedLocks
from rcp.storage import AppStore

_MEMBER_LOCKS = KeyedLocks()
ModelId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")]
IdleMinutes = Annotated[
    int, Field(ge=limits.VOICE_IDLE_MINUTES_MIN, le=limits.VOICE_IDLE_MINUTES_MAX)
]


class PurposesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # Empty is allowed: turning off a connection's last use keeps its key, unused.
    purposes: list[Literal["transcription", "voice"]] = Field(max_length=2)

    @field_validator("purposes")
    @classmethod
    def unique_purposes(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Duplicate purpose")
        return value


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    delegation_model: ModelId = "gpt-6-luna"
    live_model: ModelId = "gpt-live-1"
    confirm: Literal["tap", "none"] = "tap"
    idle_minutes: IdleMinutes = limits.VOICE_IDLE_MINUTES_DEFAULT


# Account-level voice models: the live voice and the model it hands work to.
VOICE_MODELS = ("live_model", "delegation_model")
# (each VOICE_MODELS value, voice connection id) as a check saw them.
VoiceCheck = tuple[tuple[str, ...], str | None]


def connection_purposes(connection: dict) -> list[str]:
    return connection.get("purposes", ["transcription"])


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
            connection = json.loads(path.read_text())
            return {**connection, "purposes": connection_purposes(connection)}
        except FileNotFoundError:
            raise ConnectionError("connection_not_found", 404) from None

    def _settings(self) -> dict:
        try:
            return json.loads((self.root / "settings.json").read_text())
        except FileNotFoundError:
            return {}

    def _selection(self) -> str:
        return self._settings().get("dictation", "system")

    def _connections(self) -> list[dict]:
        return [
            self._connection(p.parent.name)
            for p in sorted((self.root / "connections").glob("*/connection.json"))
        ]

    def voice_settings(self, settings: VoiceSettings | None = None) -> dict:
        with self.locked():
            if settings is not None:
                # Each writer sends only the field it changed; the others keep their value.
                saved = self._settings().get("voice", {})
                self._write_setting("voice", {**saved, **settings.model_dump(exclude_unset=True)})
            return VoiceSettings.model_validate(self._settings().get("voice", {})).model_dump()

    def voice_credentials(self) -> tuple[dict, str]:
        with self.locked():
            for connection in self._connections():
                if "voice" in connection["purposes"]:
                    key = (self.root / "connections" / connection["id"] / "key").read_text()
                    return connection, key
            raise ConnectionError("voice_not_connected", 409)

    def _publish(self, connection: dict) -> None:
        # Revoke first so interrupted publication cannot leave two voice payers.
        if "voice" in connection_purposes(connection):
            for other in self._connections():
                if other["id"] != connection["id"] and "voice" in other["purposes"]:
                    other["purposes"].remove("voice")
                    self._write_connection(other)
        if (
            "transcription" not in connection_purposes(connection)
            and self._selection() == connection["id"]
        ):
            self._write_selection("system")
        self._write_connection(connection)

    def _write_connection(self, connection: dict) -> None:
        path = self.root / "connections" / _component(connection["id"]) / "connection.json"
        _write_private(path, json.dumps(connection))

    def update_connection(
        self,
        previous: dict,
        checked: dict,
        voice_models: dict[str, str] | None = None,
        voice_check: VoiceCheck | None = None,
    ) -> dict:
        with self.locked():
            if self._connection(previous["id"]) != previous:
                raise ConnectionError("connection_changed", 409)
            self._require_voice_check(voice_check)
            self._publish(checked)
            self._write_voice_models(voice_models)
            return checked

    def _require_voice_check(self, checked: VoiceCheck | None) -> None:
        # Voice models were checked against these values and this voice connection; a
        # concurrent change to any of them means the check no longer proves they work.
        if checked is None:
            return
        saved = VoiceSettings.model_validate(self._settings().get("voice", {}))
        models = tuple(getattr(saved, name) for name in VOICE_MODELS)
        if (models, self._voice_connection_id()) != checked:
            raise ConnectionError("connection_changed", 409)

    def _voice_connection_id(self) -> str | None:
        return next((c["id"] for c in self._connections() if "voice" in c["purposes"]), None)

    def _write_voice_models(self, models: dict[str, str] | None) -> None:
        if models:
            self._write_setting("voice", {**self._settings().get("voice", {}), **models})

    def summary(self) -> dict:
        with self.locked():
            connections = self._connections()
            selection = self._selection()
            if selection not in {c["id"] for c in connections if "transcription" in c["purposes"]}:
                selection = "system"
            return {"dictation": selection, "connections": connections}

    def credentials(self, connection_id: str) -> tuple[dict, str]:
        with self.locked():
            connection = self._connection(connection_id)
            key = (self.root / "connections" / connection_id / "key").read_text()
            return connection, key

    def save(
        self,
        connection: dict,
        key: str,
        voice_models: dict[str, str] | None = None,
        voice_check: VoiceCheck | None = None,
    ) -> None:
        with self.locked():
            self._require_voice_check(voice_check)
            path = self.root / "connections" / _component(connection["id"])
            self._mkdir(path)
            _write_private(path / "key", key)
            # Publish the metadata last: incomplete writes are not usable connections.
            self._publish(connection)
            self._write_voice_models(voice_models)

    def select(self, connection_id: str) -> dict:
        with self.locked():
            if (
                connection_id != "system"
                and "transcription" not in self._connection(connection_id)["purposes"]
            ):
                raise ConnectionError("transcription_not_enabled", 409)
            self._write_selection(connection_id)
            return {"dictation": connection_id}

    def _write_selection(self, connection_id: str) -> None:
        self._write_setting("dictation", connection_id)

    def _write_setting(self, name: str, value: object) -> None:
        self._mkdir(self.root)
        settings = self._settings()
        settings[name] = value
        _write_private(self.root / "settings.json", json.dumps(settings))

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
