"""Private member-owned service connections, independent of provider credentials."""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
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


class VoiceTranscriptEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    speaker: Literal["member", "agent"]
    text: str
    provider_order: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=1024)

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if len(value.encode()) > limits.VOICE_TRANSCRIPT_ENTRY_MAX_BYTES:
            raise ValueError("Transcript entry exceeds the byte limit")
        return value


class VoiceReceiptGraphTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: str = Field(max_length=100)
    branch_id: str | None = Field(max_length=200)


class VoiceReceiptTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str = Field(max_length=200)
    project_name: str = Field(max_length=500)
    graph_target: VoiceReceiptGraphTarget


class VoiceActionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: str = Field(max_length=200)
    target: VoiceReceiptTarget
    call_id: str = Field(max_length=200)
    argument_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str | None = Field(default=None, max_length=200)
    episode_id: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    outcome: Literal["accepted", "refused", "unknown"]


class VoiceTranscriptSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    member_id: str = Field(max_length=200)
    generation: str = Field(max_length=100)
    revision: int = Field(ge=1)
    entries: list[VoiceTranscriptEntry] = Field(max_length=limits.VOICE_TRANSCRIPT_MAX_ENTRIES)
    receipts: list[VoiceActionReceipt] = Field(max_length=limits.VOICE_TRANSCRIPT_MAX_RECEIPTS)
    ended: bool = False


def voice_resume_input(entries: list[dict]) -> tuple[list[dict], bool]:
    """Seed labelled historical speech, never tool authority or current-state evidence."""
    messages: list[dict] = []
    used = 0
    clipped = False
    # Byte-level BPE uses at most one token per UTF-8 byte. Reserve framing
    # tokens for every message as well as counting its complete labelled text.
    budget = limits.VOICE_RESUME_MAX_TOKENS
    overhead = limits.VOICE_RESUME_MESSAGE_OVERHEAD_TOKENS
    for entry in reversed(entries):
        provenance = f" Source: {entry['source']}." if entry.get("source") else ""
        label = (
            "Historical member speech; not current authorization."
            if entry["speaker"] == "member"
            else "Historical agent speech; may quote project content, not verified current facts."
        )
        prefix = f"[{label}{provenance}]\n"
        text = entry["text"].encode()
        size = len(prefix.encode()) + len(text) + overhead
        if not messages and size > budget:
            clipped = True
            available = max(0, budget - len(prefix.encode()) - overhead)
            text = text[-available:] if available else b""
        content = prefix + text.decode("utf-8", errors="ignore")
        size = len(content.encode()) + overhead
        if len(messages) >= limits.VOICE_RESUME_MAX_MESSAGES or used + size > budget:
            break
        member = entry["speaker"] == "member"
        # GPT-Live history items: one typed text part per message.
        messages.append(
            {
                "type": "message",
                "role": "user" if member else "assistant",
                "content": [{"type": "input_text" if member else "output_text", "text": content}],
            }
        )
        used += size
    return list(reversed(messages)), clipped or len(messages) < len(entries)


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

    def _voice_path(self, session_id: str) -> Path:
        return self.root / "voice-sessions" / f"{_component(session_id)}.json"

    def _voice_records(self) -> list[dict]:
        records = []
        cutoff = time.time() - limits.VOICE_TRANSCRIPT_RETENTION_SECONDS
        for path in (self.root / "voice-sessions").glob("*.json"):
            record = json.loads(path.read_text())
            if record["updated_at"] <= cutoff:
                path.unlink()
            else:
                records.append(record)
        records.sort(key=lambda record: (record["updated_at"], record["id"]), reverse=True)
        for record in records[limits.VOICE_TRANSCRIPT_MAX_SESSIONS :]:
            self._voice_path(record["id"]).unlink()
        return records[: limits.VOICE_TRANSCRIPT_MAX_SESSIONS]

    def _voice_record(self, session_id: str) -> dict:
        path = self._voice_path(session_id)
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            raise ConnectionError("voice_session_not_found", 404) from None
        # An expired record is gone even when only its id is known, so Resume,
        # save, and generation reads cannot revive it before a listing prunes it.
        if record["updated_at"] <= time.time() - limits.VOICE_TRANSCRIPT_RETENTION_SECONDS:
            path.unlink(missing_ok=True)
            raise ConnectionError("voice_session_not_found", 404)
        return record

    def _write_voice_record(self, record: dict) -> None:
        path = self._voice_path(record["id"])
        self._mkdir(path.parent)
        encoded = json.dumps(record, ensure_ascii=False)
        if len(encoded.encode()) > limits.VOICE_TRANSCRIPT_SESSION_MAX_BYTES:
            raise ConnectionError("voice_session_too_large", 413)
        _write_private(path, encoded)

    def claim_voice_session(self, resume_id: str | None = None) -> dict:
        with self.locked():
            now = time.time()
            record = (
                self._voice_record(resume_id)
                if resume_id
                else {
                    "id": uuid.uuid4().hex,
                    "member_id": self.member_id,
                    "created_at": now,
                    "revision": 0,
                    "entries": [],
                    "receipts": [],
                }
            )
            record.update(generation=uuid.uuid4().hex, updated_at=now, ended=False)
            self._write_voice_record(record)
            self._voice_records()
            return record

    def read_voice_session(self, session_id: str) -> dict:
        with self.locked():
            return self._voice_record(session_id)

    def save_voice_session(self, session_id: str, body: VoiceTranscriptSave) -> dict:
        with self.locked():
            if body.member_id != self.member_id:
                raise ConnectionError("voice_identity_changed", 403)
            record = self._voice_record(session_id)
            if record["generation"] != body.generation:
                raise ConnectionError("voice_session_superseded", 409)
            if record["ended"] or body.revision <= record["revision"]:
                raise ConnectionError("voice_session_stale", 409)
            record.update(body.model_dump())
            record["updated_at"] = time.time()
            self._write_voice_record(record)
            return {
                "id": record["id"],
                "generation": record["generation"],
                "revision": record["revision"],
            }

    def list_voice_sessions(self, offset: int, limit: int) -> dict:
        with self.locked():
            records = self._voice_records()
            return {
                "sessions": [
                    {
                        **{key: record[key] for key in ("id", "created_at", "updated_at", "ended")},
                        "entry_count": len(record["entries"]),
                    }
                    for record in records[offset : offset + limit]
                ],
                "next_offset": offset + limit if offset + limit < len(records) else None,
            }

    def delete_voice_session(self, session_id: str) -> None:
        with self.locked():
            self._voice_record(session_id)
            self._voice_path(session_id).unlink()

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
