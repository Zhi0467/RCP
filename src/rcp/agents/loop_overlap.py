"""Shared interference guidance for Experiment loops and Auto-research."""

from __future__ import annotations

import json

from rcp.loop_status import AutoResearchOverlap, LoopOverlap


def render_loop_interference_rule(*, ask_allowed: bool) -> str:
    """Select guidance from the launch's resolved command authority."""

    rule = {
        "id": "ask_human" if ask_allowed else "escalate_to_orchestrator",
        "instruction": (
            "Ask the human if your work could interfere with RCP episodes on another branch, "
            "for example on the same node and in the same checkout. Queueing jobs on a "
            "shared scheduler is ordinary use, not interference."
            if ask_allowed
            else "If your work could interfere with RCP episodes on another branch, for example "
            "on the same node and in the same checkout, pause that work and report the "
            "conflict in your answer; your orchestrator asks the human. Queueing jobs on a "
            "shared scheduler is ordinary use, not interference."
        ),
    }
    return json.dumps({"interference_rule": rule}, ensure_ascii=False)


def render_loop_overlap(loops: LoopOverlap) -> str:
    """Read-only evidence of live loops elsewhere; the master states the interference rule."""

    return json.dumps(loops.model_dump(mode="json", exclude_none=True), ensure_ascii=False)


def render_auto_research_overlap(runs: AutoResearchOverlap) -> str:
    """Read-only evidence of Auto-research live on other branches."""

    return json.dumps(
        {"other_auto_research": runs.model_dump(mode="json", exclude_none=True)},
        ensure_ascii=False,
    )
