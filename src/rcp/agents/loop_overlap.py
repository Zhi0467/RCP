"""Shared interference guidance for Experiment loops and Auto-research."""

from __future__ import annotations

import json
from collections.abc import Sequence

from rcp.loop_status import LoopStatusRow


def render_loop_overlap(loops: Sequence[LoopStatusRow]) -> str:
    """Render the same read-only evidence and rule for every loop agent."""

    return (
        "Ask the human if your work could interfere with episodes on another branch, "
        "for example on the same node and in the same checkout.\n"
        + json.dumps([row.model_dump(mode="json") for row in loops], ensure_ascii=False)
    )
