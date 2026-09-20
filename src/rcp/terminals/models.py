from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from typing import Literal


class TerminalUnavailable(RuntimeError):
    """The required terminal launch or cleanup could not be completed."""


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
    subscribers: set[asyncio.Queue[bytes | None]] = field(default_factory=set)
