"""Shared interference guidance for Experiment loops and Auto-research."""

from __future__ import annotations

import json

from rcp.loop_status import LoopOverlap

LOOP_INTERFERENCE_RULE = (
    "Ask the human if your work could interfere with episodes on another branch, "
    "for example on the same node and in the same checkout."
)


def render_loop_overlap(loops: LoopOverlap) -> str:
    """Render the same read-only evidence and rule for every loop agent."""

    return (
        LOOP_INTERFERENCE_RULE
        + "\n"
        + json.dumps(loops.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
    )
