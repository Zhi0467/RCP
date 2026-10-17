"""The concrete command surfaces for Auto-research actors."""

from __future__ import annotations

from typing import Literal

from rcp.agents.command_protocol import CommandVerb
from rcp.agents.graph_read_scope import GraphReadScope


def auto_research_allowed_verbs(
    role: Literal["orchestrator", "worker"],
    *,
    graph_read_scope: GraphReadScope | None = None,
) -> tuple[CommandVerb, ...]:
    history_verbs = tuple(sorted(graph_read_scope.verbs)) if graph_read_scope else ()
    if role == "worker":
        return ("validate", "status", "message", "lesson", "bulletin", *history_verbs)
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
        "lesson",
        "bulletin",
        *history_verbs,
    )
