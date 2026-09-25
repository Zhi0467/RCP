from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import rcp.storage.agent_tasks as agent_task_storage
from rcp.limits import AGENT_TASK_LIST_DEFAULT_LIMIT
from rcp.storage import AgentTaskRecord, AgentTaskStatus, AppStore
from tests.test_auto_research_children_storage import _identity, _project


def _chat_turn(
    store: AppStore, operation_id: str, chat_id: str, status: AgentTaskStatus, minute: int
) -> None:
    at = (datetime.fromisoformat(store.now()) + timedelta(minutes=minute)).isoformat()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id="project",
            kind="project_chat",
            status=status,
            request={"chat_id": chat_id},
            created_at=at,
            updated_at=at,
            status_message="fixture",
            authorized_by=_identity(store),
        )
    )


def test_open_chat_turns_stay_listed_past_the_recency_limit(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    _chat_turn(store, "old-failed", "chat-failed", "failed", 0)
    _chat_turn(store, "old-paused", "chat-paused", "paused", 1)
    _chat_turn(store, "old-running", "chat-running", "running", 2)
    _chat_turn(store, "superseded-failure", "chat-recovered", "failed", 3)
    _chat_turn(store, "later-success", "chat-recovered", "succeeded", 4)
    _chat_turn(store, "old-success", "chat-done", "succeeded", 5)
    for index in range(AGENT_TASK_LIST_DEFAULT_LIMIT):
        _chat_turn(store, f"newer-{index}", f"chat-newer-{index}", "succeeded", 10 + index)

    listed = [task.operation_id for task in store.agent_tasks("project")]

    newest = [f"newer-{index}" for index in reversed(range(AGENT_TASK_LIST_DEFAULT_LIMIT))]
    assert listed == [*newest, "old-running", "old-paused", "old-failed"]


def test_the_open_chat_cap_keeps_the_most_recent_open_chats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_task_storage, "AGENT_TASK_LIST_OPEN_CHAT_LIMIT", 2)
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    # Three open chats compete for two places; recency decides which stay.
    _chat_turn(store, "oldest-open", "chat-a", "failed", 0)
    _chat_turn(store, "middle-open", "chat-b", "failed", 1)
    _chat_turn(store, "newest-open", "chat-c", "failed", 2)
    for index in range(AGENT_TASK_LIST_DEFAULT_LIMIT):
        _chat_turn(store, f"newer-{index}", f"chat-newer-{index}", "succeeded", 10 + index)

    listed = [task.operation_id for task in store.agent_tasks("project")]

    assert listed[AGENT_TASK_LIST_DEFAULT_LIMIT:] == ["newest-open", "middle-open"]
