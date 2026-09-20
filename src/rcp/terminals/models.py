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
    # What this repository was registered as when the shell started: its
    # declared path, its machine, and that machine's account. The resolved
    # `path` can differ from the declared one, and two machines can share a
    # path, so only the whole identity distinguishes a re-registration.
    declared_path: str = ""
    declared_machine: str = ""
    declared_account: str = ""
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
    # The reason a failed stop has already decided this session ends by. It
    # stays in `sessions` so the stop can be retried, and stops being one a
    # member can reach.
    retiring: str | None = None
