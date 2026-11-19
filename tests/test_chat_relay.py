"""A chat's dock: its direct child episodes and their questions, apart from other relays."""

from __future__ import annotations

from datetime import datetime, timedelta

from rcp.storage import AppStore
from tests import test_api_questions as api_fixtures
from tests.test_api_questions import _question
from tests.test_episode_storage import _episode
from tests.test_space_relay import _other_member_thread, _project_thread
from tests.test_storage import _task_record

questions_api = api_fixtures.questions_api


def _chat_turn(store: AppStore, project_id: str, chat_id: str, operation_id: str) -> None:
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id=operation_id,
            project_id=project_id,
            kind="project_chat",
            request={"chat_id": chat_id},
        )
    )


def _child(
    store: AppStore,
    project_id: str,
    episode_id: str,
    *,
    started_from: str | None,
    continues: str | None = None,
) -> str:
    root = f"{episode_id}-root"
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id=root,
            project_id=project_id,
            kind="node_chat",
            request={"started_from_operation_id": started_from} if started_from else {},
        )
    )
    store.create_episode(
        _episode(store, episode_id, project_id=project_id, control_node_id=episode_id)
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET root_operation_id=?, continues_episode_id=? WHERE episode_id=?",
            (root, continues, episode_id),
        )
    return episode_id


def _end(store: AppStore, episode_id: str, *, ago: timedelta = timedelta(0)) -> None:
    ended = (datetime.fromisoformat(store.now()) - ago).isoformat()
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status='completed', ending='completed', ended_at=?, "
            "updated_at=? WHERE episode_id=?",
            (ended, ended, episode_id),
        )


def test_chat_children_follow_continuations_and_drop_long_ended_runs(tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    _chat_turn(store, "project", "chat", "turn")
    _chat_turn(store, "project", "other-chat", "other-turn")
    _child(store, "project", "first", started_from="turn")
    _end(store, "first")
    _child(store, "project", "continued", started_from=None, continues="first")
    _child(store, "project", "elsewhere", started_from="other-turn")
    _child(store, "project", "stale", started_from="turn")
    _end(store, "stale", ago=timedelta(days=3))
    _child(store, "project", "recent", started_from="turn")
    _end(store, "recent", ago=timedelta(hours=1))
    since = (datetime.fromisoformat(store.now()) - timedelta(days=1)).isoformat()

    children = store.chat_child_episodes("project", "chat", ended_since=since)

    assert {episode.episode_id for episode in children} == {"continued", "recent"}
    started = store.episode_started_from_chat("continued")
    assert started is not None and started.chat_id == "chat"
    assert store.episode_started_from_chat("elsewhere").chat_id == "other-chat"  # type: ignore[union-attr]


def test_dock_buckets_are_disjoint_and_space_skips_what_a_thread_shows(questions_api) -> None:
    _app, client, store, project_id = questions_api
    thread_id = _project_thread(client)["thread_id"]
    _chat_turn(store, project_id, thread_id, "thread-turn")
    _chat_turn(store, project_id, "shared-chat", "shared-turn")
    _child(store, project_id, "mine", started_from="thread-turn")
    _child(store, project_id, "shared-run", started_from="shared-turn")
    mine = _question(store, project_id, owner_kind="episode", owner_id="mine", key="mine")
    shared = _question(store, project_id, owner_kind="episode", owner_id="shared-run", key="shared")
    own = _question(store, project_id, owner_id=thread_id, key="own")

    def ids(rows: list[dict]) -> set[str]:
        return {row["question_id"] for row in rows}

    named = client.get("/api/agent-threads/cards", params={"thread_id": thread_id}).json()
    assert [entry["episode"]["episode_id"] for entry in named["episodes"]] == ["mine"]
    assert ids(named["episodes"][0]["questions"]) == {mine.question_id}
    assert ids(named["questions"]) == {shared.question_id}

    space = client.get("/api/agent-threads/cards").json()
    assert ids(space["questions"]) == {shared.question_id, own.question_id}
    assert space["episodes"] == []

    chat = client.get(f"/api/projects/{project_id}/chats/shared-chat/cards")
    assert chat.status_code == 200, chat.text
    body = chat.json()
    assert [entry["episode"]["episode_id"] for entry in body["episodes"]] == ["shared-run"]
    assert ids(body["episodes"][0]["questions"]) == {shared.question_id}
    assert body["questions"] == []
    assert body["proposal_cards"] == []

    hidden = _other_member_thread(store, project_id)
    refused = client.get(f"/api/projects/{project_id}/chats/{hidden.thread_id}/cards")
    assert refused.status_code == 404
