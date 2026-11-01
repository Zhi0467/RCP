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
from rcp.agents.memory_file_io import ensure_directory, safe_component
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
    thread_id = str(uuid.UUID(thread["thread_id"]))
    user_id = safe_component(thread["user_id"])
    if thread["kind"] == "space":
        return root / "agent-threads" / user_id / f"{thread_id}.jsonl"
    return root / "chat" / f"private-{user_id}-{thread_id}.jsonl"


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
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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
                or (
                    thread["kind"] == "project"
                    and row.get("graphTarget", {"kind": "main", "branch_id": None})
                    != {"kind": "main", "branch_id": None}
                )
                or (thread["kind"] == "space" and row.get("graphTarget") is not None)
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


def pending_records(path: Path, thread: dict, entries: list[VoiceEntry]) -> list[dict]:
    seen = {row["uuid"] for row in read_records(path, thread)}
    pending = []
    for row in entry_records(thread, entries):
        if row["uuid"] not in seen:
            pending.append(row)
            seen.add(row["uuid"])
    return pending


def append_space_entries(
    store: AppStore, path: Path, thread: dict, entries: list[VoiceEntry]
) -> int:
    # Match the Space task writer: one database write lock spans dedup and append.
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_directory(path.parent)
        pending = pending_records(path, thread, entries)
        if pending:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                for row in pending:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return len(pending)
