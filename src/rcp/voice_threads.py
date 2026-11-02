"""Bounded display-only voice I/O for member-owned agent transcripts."""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from rcp import limits
from rcp.agents.memory_file_io import safe_component
from rcp.service_connections import ConnectionError
from rcp.storage import AppStore


class VoiceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str = Field(min_length=1, max_length=200)
    speaker: Literal["member", "agent"]
    text: str = Field(min_length=1, max_length=limits.VOICE_TRANSCRIPT_ENTRY_MAX_BYTES)
    at: AwareDatetime

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if len(value.encode()) > limits.VOICE_TRANSCRIPT_ENTRY_MAX_BYTES:
            raise ValueError("Voice entry exceeds the size limit")
        return value


class VoiceEntries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[VoiceEntry] = Field(max_length=limits.VOICE_TRANSCRIPT_MAX_ENTRIES)


def owned_thread(store: AppStore, thread_id: str, user_id: str) -> dict:
    """The thread index, never a transcript filename, establishes ownership."""
    with store.connection() as db:
        row = db.execute(
            "SELECT * FROM agent_threads WHERE thread_id=? AND closed_at IS NULL", (thread_id,)
        ).fetchone()
    if row is None or row["user_id"] != user_id:
        raise ConnectionError("thread_not_found", 404)
    thread = dict(row)
    if thread["project_id"] and not store.is_project_member(thread["project_id"], user_id):
        raise ConnectionError("thread_not_found", 404)
    return thread


def transcript_path(root: Path, thread: dict) -> Path:
    """A project thread's private file in its project's state workspace."""
    thread_id = str(uuid.UUID(thread["thread_id"]))
    user_id = safe_component(thread["user_id"])
    return root / "chat" / f"private-{user_id}-{thread_id}.jsonl"


def space_records(data_dir: Path, thread: dict) -> list[dict]:
    """A Space thread's records, read by the one Space transcript reader."""
    from rcp.service import read_space_chat_records

    return [
        record.model_dump(mode="json", by_alias=True)
        for record in read_space_chat_records(data_dir, thread["user_id"], thread["thread_id"])
    ]


def read_records(path: Path, thread: dict) -> list[dict]:
    try:
        return _read_records(path, thread)
    except (ValueError, TypeError, KeyError):
        raise ConnectionError("thread_transcript_invalid", 409) from None
    except OSError:
        raise ConnectionError("thread_transcript_unavailable", 503) from None


def _read_records(path: Path, thread: dict) -> list[dict]:
    """Read the canonical private file; refuse a relocated or mislabeled record."""
    try:
        for directory in (path.parent.parent, path.parent):
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ValueError("Unsafe transcript directory")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Unsafe transcript file")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            records = [json.loads(line) for line in stream if line.strip()]
        for row in records:
            if (
                not isinstance(row, dict)
                or row.get("sessionId") != thread["thread_id"]
                or row.get("privateOwner") != thread["user_id"]
                or row.get("chatScope") != thread["kind"]
                or row.get("role") not in {"user", "assistant"}
                or row.get("type") != row.get("role")
                or not isinstance(row.get("text"), str)
                or row.get("nodeId") is not None
                or not isinstance(row.get("uuid"), str)
                or str(uuid.UUID(row["uuid"])) != row["uuid"]
                or datetime.fromisoformat(row["timestamp"]).tzinfo is None
                or row.get("graphTarget", {"kind": "main", "branch_id": None})
                != {"kind": "main", "branch_id": None}
            ):
                raise ValueError("Transcript identity mismatch")
        return records
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def entry_records(thread: dict, entries: list[VoiceEntry]) -> list[dict]:
    records = []
    for entry in entries:
        role = "user" if entry.speaker == "member" else "assistant"
        records.append(
            {
                "sessionId": thread["thread_id"],
                "privateOwner": thread["user_id"],
                "chatScope": thread["kind"],
                "uuid": str(uuid.uuid5(uuid.UUID(thread["thread_id"]), "voice:" + entry.entry_id)),
                "timestamp": entry.at.isoformat(),
                "type": role,
                "role": role,
                "text": entry.text,
                "source": "voice",
            }
        )
    return records


def unique_entry_records(thread: dict, entries: list[VoiceEntry]) -> list[dict]:
    """One record per entry id; the locked appender skips ids already written."""
    return list({row["uuid"]: row for row in entry_records(thread, entries)}.values())


def append_space_entries(
    store: AppStore, data_dir: Path, thread: dict, entries: list[VoiceEntry]
) -> int:
    """Append through the one Space appender, under the Space turns' lock."""
    from rcp.runs.space_chat import append_space_records

    record = store.agent_thread(thread["thread_id"])
    if record is None:
        raise ConnectionError("thread_not_found", 404)
    return append_space_records(store, data_dir, record, entry_records(thread, entries))
