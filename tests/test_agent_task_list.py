from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import rcp.storage.agent_tasks as agent_task_storage
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import AGENT_TASK_LIST_DEFAULT_LIMIT
from rcp.storage import AgentTaskRecord, AgentTaskStatus, AppStore
from tests.test_auto_research_children_storage import _identity, _project


def _chat_turn(
    store: AppStore,
    operation_id: str,
    chat_id: str,
    status: AgentTaskStatus,
    minute: int,
    finished_ago: timedelta | None = None,
) -> None:
    now = datetime.fromisoformat(store.now())
    at = (now + timedelta(minutes=minute)).isoformat()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id="project",
            kind="project_chat",
            status=status,
            request={"chat_id": chat_id},
            created_at=at,
            updated_at=at,
            finished_at=None if finished_ago is None else (now - finished_ago).isoformat(),
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
    _chat_turn(store, "old-success", "chat-done", "succeeded", 5, timedelta(days=1))
    # A turn that just ended stays listed, so a client that saw it running sees it end.
    _chat_turn(store, "just-finished", "chat-just", "succeeded", 6, timedelta(seconds=5))
    for index in range(AGENT_TASK_LIST_DEFAULT_LIMIT):
        _chat_turn(store, f"newer-{index}", f"chat-newer-{index}", "succeeded", 10 + index)

    listed = [task.operation_id for task in store.agent_tasks("project")]

    newest = [f"newer-{index}" for index in reversed(range(AGENT_TASK_LIST_DEFAULT_LIMIT))]
    assert listed == [*newest, "just-finished", "old-running", "old-paused", "old-failed"]


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
    # A chat that just finished never takes a place from one that needs a person.
    _chat_turn(store, "just-finished", "chat-d", "succeeded", 3, timedelta(seconds=5))
    for index in range(AGENT_TASK_LIST_DEFAULT_LIMIT):
        _chat_turn(store, f"newer-{index}", f"chat-newer-{index}", "succeeded", 10 + index)

    listed = [task.operation_id for task in store.agent_tasks("project")]

    assert listed[AGENT_TASK_LIST_DEFAULT_LIMIT:] == ["newest-open", "middle-open"]


def test_chat_reads_find_a_reply_the_task_list_no_longer_holds(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    _chat_turn(store, "old-success", "chat-done", "succeeded", 0, timedelta(days=1))
    _chat_turn(store, "old-paused", "chat-paused", "paused", 1)
    _chat_turn(store, "old-archived", "chat-archived", "succeeded", 2, timedelta(days=1))
    for index in range(AGENT_TASK_LIST_DEFAULT_LIMIT):
        _chat_turn(store, f"newer-{index}", "chat-newer", "succeeded", 10 + index)
    store.set_chat_archived("project", "chat-archived", "user", archived=True)
    branch = GraphTargetRef(kind="branch", branch_id="branch")
    _chat_turn(store, "branch-success", "chat-branch", "succeeded", 3, timedelta(days=1))
    # Admission needs a real branch; the read projection only filters the stored target.
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET graph_target_json = ? WHERE operation_id = 'branch-success'",
            (branch.model_dump_json(),),
        )
    assert "old-success" not in {task.operation_id for task in store.agent_tasks("project")}

    finished = store.chat_reads("project", "user", GraphTargetRef())["latest_finished"]

    assert set(finished) == {"chat-done"}
    assert set(store.chat_reads("project", "user", branch)["latest_finished"]) == {"chat-branch"}
    assert finished["chat-done"] == store.agent_task("old-success").finished_at
    assert set(store.chat_reads("project", "user", None)["latest_finished"]) == {
        "chat-done",
        "chat-branch",
    }


def test_chat_read_marker_never_moves_back(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    later = datetime.fromisoformat("2026-09-27T12:00:00.5+00:00")
    store.mark_chat_read("project", "chat", "user", later)
    store.mark_chat_read("project", "chat", "user", later - timedelta(hours=1))
    store.mark_chat_read("project", "chat", "other", later - timedelta(hours=1))

    assert store.chat_reads("project", "user", GraphTargetRef())["reads"] == {
        "chat": later.isoformat()
    }
    assert store.chat_reads("project", "other", GraphTargetRef())["reads"] == {
        "chat": (later - timedelta(hours=1)).isoformat()
    }


def test_chat_pins_belong_to_the_user_who_pinned(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.set_chat_pinned("project", "first", "user", pinned=True)
    store.set_chat_pinned("project", "second", "user", pinned=True)
    store.set_chat_pinned("project", "first", "other", pinned=True)
    store.set_chat_pinned("project", "first", "user", pinned=False)

    assert store.chat_display("project", "user")["pinned"] == ["second"]
    assert store.chat_display("project", "other")["pinned"] == ["first"]


def test_experiment_inventory_kind_survives_a_later_human_turn(tmp_path: Path) -> None:
    from tests.test_experiment_episode_storage import _task

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    episode_id, chat_id = str(uuid.uuid4()), str(uuid.uuid4())
    root = _task(store, "experiment-root", episode_id)
    root = root.model_copy(update={"request": {**root.request, "chat_id": chat_id}})
    store.create_experiment_episode_with_invocation(root)
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    now = store.now()
    human = AgentTaskRecord(
        operation_id="human-followup",
        project_id="project",
        kind="node_chat",
        status="queued",
        request={"chat_id": chat_id, "node_id": "exp-one"},
        created_at=now,
        updated_at=now,
        status_message="",
        authorized_by=_identity(store),
    )
    store.create_agent_task(human)
    rows = store.chat_inventory("project")
    assert len(rows) == 1
    assert rows[0]["chat_id"] == chat_id
    assert rows[0]["updated_at"] == now
    assert rows[0]["conversation_kind"] == "episode"
    assert rows[0]["orchestrator_episode_id"] is None


def test_an_artifact_edit_refreshes_only_a_conversation_its_turns_hold(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    _chat_turn(store, "turn", "chat", "succeeded", 0)
    _chat_turn(store, "chat-edit", "chat", "succeeded", 5)
    _chat_turn(store, "lone-edit", "edit-only", "succeeded", 6)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind = 'artifact_edit', "
            "request_json = json_set(request_json, '$.chat_scope', 'node') "
            "WHERE operation_id LIKE '%-edit'"
        )
    rows = store.chat_inventory("project")
    assert [(row["chat_id"], row["kind"]) for row in rows] == [("chat", "project_chat")]
    edit = store.agent_task("chat-edit")
    assert edit is not None and rows[0]["updated_at"] == edit.updated_at


def test_an_orchestrator_turn_stays_listed_and_finishes_its_chain_conversation(
    tmp_path: Path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    _chat_turn(store, "superseded-orchestrator", "first", "running", 0)
    _chat_turn(store, "running-orchestrator", "second", "running", 1)
    branch = GraphTargetRef(kind="branch", branch_id="chain-root")
    # Admission needs a real episode and branch; the projections read only these columns.
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind = 'auto_research', request_json = ?, "
            "graph_target_json = ? WHERE operation_id LIKE '%-orchestrator'",
            ('{"role": "orchestrator"}', branch.model_dump_json()),
        )
    # Children and other chats crowd the orchestrator out of the recency window.
    for index in range(AGENT_TASK_LIST_DEFAULT_LIMIT):
        _chat_turn(store, f"newer-{index}", f"chat-newer-{index}", "succeeded", 10 + index)

    listed = {task.operation_id for task in store.agent_tasks("project")}
    assert "running-orchestrator" in listed
    assert "superseded-orchestrator" not in listed

    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status = 'succeeded', finished_at = ? "
            "WHERE operation_id = 'running-orchestrator'",
            (store.now(),),
        )
    assert set(store.chat_reads("project", "user", branch)["latest_finished"]) == {"chain-root"}


def test_a_paused_rebound_edit_keeps_its_artifact_from_expiry(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    _chat_turn(store, "answer", "chat", "paused", 0)
    # An answer turn rebound to another stage's artifact carries no artifact context.
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json = json_set(request_json, "
            "'$.artifact_edit', json('{\"artifact_id\": \"rebound\"}')) "
            "WHERE operation_id = 'answer'"
        )
    assert "rebound" in store.protected_edit_artifact_ids()
