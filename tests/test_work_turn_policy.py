from __future__ import annotations

from typing import cast

import pytest

from rcp.background import AgentTaskContinuation
from rcp.runs.chat import _prepare_local_chat_workspace
from rcp.runs.tasks.work_turn_runtime import clears_stale_turn_handoffs


@pytest.mark.parametrize(
    "continuation",
    [
        "fresh",
        "handoff",
        "retry",
        "watcher_wake",
        "graph_condition_wake",
        "message_wake",
        "lifecycle_wake",
    ],
)
def test_new_logical_work_turns_clear_previous_handoffs(
    continuation: AgentTaskContinuation,
) -> None:
    assert clears_stale_turn_handoffs(continuation) is True


@pytest.mark.parametrize("continuation", ["resume", "graph_repair"])
def test_same_logical_work_turn_continuations_preserve_handoffs(
    continuation: AgentTaskContinuation,
) -> None:
    assert clears_stale_turn_handoffs(continuation) is False


@pytest.mark.parametrize("continuation", ["auto_research_continuation", "episode_report"])
def test_work_rejects_continuations_without_an_explicit_handoff_policy(
    continuation: str,
) -> None:
    with pytest.raises(ValueError, match="Unsupported Work continuation"):
        clears_stale_turn_handoffs(cast(AgentTaskContinuation, continuation))


def test_local_work_workspace_keeps_staged_inputs_outside_the_write_root(tmp_path) -> None:
    stage = tmp_path / "run-stage"
    inputs = stage / "inputs"
    inputs.mkdir(parents=True)
    contract = inputs / "task-contract.md"
    contract.write_text("immutable contract", encoding="utf-8")

    workspace = _prepare_local_chat_workspace(stage, execution=None, saved_stage=False)

    assert workspace == stage / "workspace"
    assert workspace.is_dir()
    assert not (workspace / "inputs").exists()
    assert contract.read_text(encoding="utf-8") == "immutable contract"
