"""The concrete command surfaces for Auto-research actors."""

from __future__ import annotations

from typing import Literal

from rcp.agents.command_protocol import CommandVerb


def auto_research_allowed_verbs(role: Literal["orchestrator", "worker"]) -> tuple[CommandVerb, ...]:
    if role == "worker":
        return ("validate", "status", "message")
    return (
        "validate",
        "apply",
        "status",
        "spawn",
        "pause",
        "resume",
        "stop",
        "message",
        "watch_graph",
        "episode",
        "inbox",
        "finish",
        "ask",
    )
