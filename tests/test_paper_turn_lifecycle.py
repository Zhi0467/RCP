from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks, TaskPaused
from rcp.paper import PaperLive
from rcp.paper.folder import LocalPaperFolder
from rcp.paper.folder_io import paper_root
from rcp.runs.paper_turns import end_settled_paper_turns
from tests.helpers import wait_for_task, wait_until
from tests.lifecycles.agent_task import (
    admitted_launch_task,
    done_stream,
    seeded_store,
    sse,
    work_request,
)
from tests.paper_helpers import close_paper_owners, create, start_paper


@pytest.fixture(name="paper")
def paper_fixture(tmp_path: Path):
    store = seeded_store(tmp_path)
    live = start_paper(
        PaperLive(store, lambda project: LocalPaperFolder(paper_root(tmp_path, project)))
    )
    create(live, "a.md", "one\n", project="project")
    yield store, live, tmp_path / "paper" / "project"
    close_paper_owners()
    store.close()


def _held_turn(store) -> str | None:
    with store.connection() as conn:
        row = conn.execute("SELECT turn_operation_id FROM paper_state").fetchone()
    return row[0]


def _receipt(store, operation_id: str) -> dict | None:
    receipts = store.agent_task_receipts_by_category(operation_id, "paper_turn")
    return receipts[-1].payload if receipts else None


def _paper_turn(store, operation_id: str):
    request = work_request(f"chat-{operation_id}").model_copy(update={"focus": "paper"})
    return admitted_launch_task(store, operation_id=operation_id, request=request)


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "paused"])
def test_a_paper_turn_holds_the_paper_from_launch_until_its_process_exits(outcome, paper):
    store, live, folder = paper
    seen: list[str | None] = []

    async def stream(_project, _kind, _request, execution):
        seen.append(_held_turn(store))
        (folder / "a.md").write_text("one\nagent\n")
        seen.append(None if _receipt(store, execution.operation_id) else "no receipt yet")
        if outcome == "failed":
            raise RuntimeError("provider failed")
        if outcome == "paused":
            raise TaskPaused("stopped")
        yield sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream, paper_live=live)
    try:
        tasks.launch_admitted(_paper_turn(store, "turn").operation_id)
        wait_for_task(store, "turn", expect=outcome)
        receipt = wait_until(lambda: _receipt(store, "turn"))
    finally:
        tasks.shutdown()

    assert seen == ["turn", "no receipt yet"]
    assert _held_turn(store) is None
    assert receipt["before_version_id"] != receipt["after_version_id"]
    assert [(row["path"], row["kind"]) for row in receipt["files"]] == [("a.md", "write")]


def test_an_ordinary_chat_turn_never_takes_the_paper(paper):
    store, live, _folder = paper
    seen: list[str | None] = []

    async def stream(_project, _kind, _request, _execution):
        seen.append(_held_turn(store))
        yield sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream, paper_live=live)
    try:
        record = admitted_launch_task(store, operation_id="plain", request=work_request("plain"))
        tasks.launch_admitted(record.operation_id)
        wait_for_task(store, "plain", expect="succeeded")
    finally:
        tasks.shutdown()
    assert seen == [None]
    assert _receipt(store, "plain") is None


def test_recovery_ends_a_turn_the_previous_process_left_holding_the_paper(paper):
    store, live, folder = paper
    _paper_turn(store, "crashed")
    assert store.mark_agent_task_running("crashed")
    live.run_sync(live.begin_turn("project", "crashed"))
    (folder / "b.md").write_text("written before the crash\n")

    end_settled_paper_turns(store, live)
    assert _held_turn(store) == "crashed"

    BackgroundAgentTasks(store, done_stream, paper_live=live).recover_at_startup()
    end_settled_paper_turns(store, live)

    assert _held_turn(store) is None
    assert [row["path"] for row in _receipt(store, "crashed")["files"]] == ["b.md"]


def test_a_new_paper_turn_first_ends_a_settled_turn_still_holding_the_paper(paper):
    store, live, _folder = paper
    _paper_turn(store, "settled")
    live.run_sync(live.begin_turn("project", "settled"))
    store.fail_agent_task("settled", "provider failed")
    seen: list[str | None] = []

    async def stream(_project, _kind, _request, _execution):
        seen.append(_held_turn(store))
        yield sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream, paper_live=live)
    try:
        tasks.launch_admitted(_paper_turn(store, "next").operation_id)
        wait_for_task(store, "next", expect="succeeded")
        wait_until(lambda: _receipt(store, "next"))
    finally:
        tasks.shutdown()
    assert seen == ["next"]
    assert _receipt(store, "settled") is not None
