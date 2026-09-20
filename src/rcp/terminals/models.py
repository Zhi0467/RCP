from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from rcp.terminals.remote import CompletionParser


class TerminalUnavailable(RuntimeError):
    """The required terminal launch or cleanup could not be completed."""


class SubscriberDetached:
    """One viewer fell behind its own output; the session is unaffected.

    Ending a session uses ``None``. Reusing that sentinel here would report a
    live shell as terminated to whoever was merely slow.
    """


DETACHED = SubscriberDetached()
TerminalFrame = bytes | None | SubscriberDetached


@dataclass
class TerminalSession:
    session_id: str
    project_id: str
    member_id: str
    repository_id: str
    path: str
    started_at: str
    last_activity_at: str
    unit: str
    # The path this repository was registered at when the shell started. Its
    # resolved `path` can differ, so a settings change is only detectable by
    # comparing what was declared then against what is declared now.
    declared_path: str = ""
    execution_host: str = ""
    containment: Literal["mirrored", "cooperative"] = "mirrored"
    state: Literal["live", "idle"] = "idle"
    ended_at: str | None = None
    termination_reason: str | None = None


@dataclass
class TerminalRuntime:
    session: TerminalSession
    process: subprocess.Popen[bytes]
    master_fd: int
    last_activity: float
    replay: bytearray = field(default_factory=bytearray)
    subscribers: set[asyncio.Queue[TerminalFrame]] = field(default_factory=set)
    completion: CompletionParser | None = None
