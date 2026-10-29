"""The concrete command surfaces for Auto-research actors."""

from __future__ import annotations

from typing import Literal

from rcp.agents.command_protocol import CommandVerb
from rcp.agents.graph_read_scope import GraphReadScope


def auto_research_allowed_verbs(
    role: Literal["orchestrator", "worker"],
    *,
    graph_read_scope: GraphReadScope | None = None,
    bulletin: bool = True,
    lesson_actions: tuple[str, ...] = (),
    can_self_name: bool = False,
) -> tuple[CommandVerb, ...]:
    history_verbs = tuple(sorted(graph_read_scope.verbs)) if graph_read_scope else ()
    bulletin_verbs: tuple[CommandVerb, ...] = ("bulletin",) if bulletin else ()
    lesson_verbs: tuple[CommandVerb, ...] = ("lesson",) if lesson_actions else ()
    if role == "worker":
        return ("validate", "status", "message", *lesson_verbs, *bulletin_verbs, *history_verbs)
    return (
        "validate",
        "apply",
        "status",
        "spawn",
        "merge",
        "branch",
        "pause",
        "resume",
        "stop",
        "message",
        "watch_graph",
        "episode",
        "inbox",
        "finish",
        "ask",
        *lesson_verbs,
        *(("name",) if can_self_name else ()),
        *bulletin_verbs,
        *history_verbs,
    )
