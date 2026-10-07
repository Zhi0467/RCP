"""Shared interference guidance for Experiment loops and Auto-research."""

from __future__ import annotations

import json

from rcp.loop_status import LoopOverlap


def render_loop_interference_rule(*, ask_allowed: bool) -> str:
    """Select guidance from the launch's resolved command authority."""

    rule = {
        "id": "ask_human" if ask_allowed else "escalate_to_orchestrator",
        "instruction": (
            "Ask the human if your work could interfere with episodes on another branch, "
            "for example on the same node and in the same checkout."
            if ask_allowed
            else "If your work could interfere with episodes on another branch, for example "
            "on the same node and in the same checkout, pause that work and report the "
            "conflict in your answer; your orchestrator asks the human."
        ),
    }
    return json.dumps({"interference_rule": rule}, ensure_ascii=False)


def render_loop_overlap(loops: LoopOverlap, *, ask_allowed: bool) -> str:
    """Render read-only evidence beside capability-specific interference guidance."""

    return (
        render_loop_interference_rule(ask_allowed=ask_allowed)
        + "\n"
        + json.dumps(loops.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
    )
