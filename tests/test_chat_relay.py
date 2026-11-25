"""A chat's dock: its direct child episodes and their questions, apart from other relays."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

from rcp.api import agent_threads
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
    mode: str = "experiment_loop",
    node_id: str | None = None,
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
        _episode(
            store,
            episode_id,
            project_id=project_id,
            mode=mode,
            control_node_id=(node_id or episode_id) if mode == "experiment_loop" else None,
        )
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


def test_chat_runs_follow_continuations_and_keep_long_ended_runs(tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    _chat_turn(store, "project", "chat", "turn")
    _chat_turn(store, "project", "other-chat", "other-turn")
    _child(store, "project", "first", started_from="turn")
    _end(store, "first")
    _child(store, "project", "continued", started_from=None, continues="first")
    _child(store, "project", "elsewhere", started_from="other-turn")
    _child(store, "project", "stale", started_from="turn")
    _end(store, "stale", ago=timedelta(days=30))

    children = store.chat_dock_episodes("project", "chat", node_id=None, ended_limit=5)

    assert {(episode.episode_id, source) for episode, source in children} == {
        ("continued", "chat"),
        ("stale", "chat"),
    }
    started = store.episode_started_from_chat("continued")
    assert started is not None and started.chat_id == "chat"
    assert store.episode_started_from_chat("elsewhere").chat_id == "other-chat"  # type: ignore[union-attr]


def test_chat_dock_work_is_bounded_by_its_own_history(tmp_path, monkeypatch) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    _chat_turn(store, "project", "chat", "turn")
    _chat_turn(store, "project", "other-chat", "other-turn")
    for index in range(6):
        owned = _child(store, "project", f"owned-{index}", started_from="turn")
        _end(store, owned)
    _child(store, "project", "still-live", started_from="turn")
    _child(store, "project", "node-run", started_from=None, node_id="node")
    records = Mock(wraps=store._episode_record)

    def read_dock() -> int:
        steps = 0

        def count_step() -> int:
            nonlocal steps
            steps += 1
            return 0

        with store.connection() as connection, monkeypatch.context() as scope:
            scope.setattr(store, "connection", lambda: nullcontext(connection))
            scope.setattr(store, "_episode_record", records)
            connection.execute("ANALYZE")
            connection.set_progress_handler(count_step, 1)
            result = store.chat_dock_episodes("project", "chat", node_id="node", ended_limit=1)
            connection.set_progress_handler(None, 0)
        assert {episode.episode_id for episode, _ in result} == {
            "owned-5",
            "still-live",
            "node-run",
        }
        assert records.call_count == len(result)
        records.reset_mock()
        return steps

    before = read_dock()
    for index in range(64):
        _child(store, "project", f"other-{index}", started_from="other-turn")
    assert read_dock() < 2 * before


def test_continuations_keep_dock_scope_at_the_project_and_current_node(tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    _chat_turn(store, "project", "chat", "turn")
    _child(store, "project", "chat-root", started_from="turn")
    _end(store, "chat-root")
    _child(store, "foreign", "foreign-child", started_from=None, continues="chat-root")
    _child(store, "project", "page-root", started_from=None, node_id="previous-node")
    _end(store, "page-root")
    _child(store, "project", "page-tip", started_from=None, continues="page-root", node_id="node")
    _end(store, "page-tip")
    _child(store, "foreign", "foreign-tip", started_from=None, continues="page-tip", node_id="node")

    def listed(node_id: str) -> set[tuple[str, str]]:
        return {
            (episode.episode_id, source)
            for episode, source in store.chat_dock_episodes(
                "project", "chat", node_id=node_id, ended_limit=2
            )
        }

    assert listed("previous-node") == {("chat-root", "chat")}
    assert listed("node") == {("chat-root", "chat"), ("page-tip", "node")}


def test_node_chat_lists_runs_started_from_its_node_page(tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    _chat_turn(store, "project", "other-chat", "other-turn")
    _child(store, "project", "other-node", started_from=None, node_id="elsewhere")
    _child(store, "project", "from-other-chat", started_from="other-turn", node_id="node")
    _end(store, "from-other-chat")
    _child(store, "project", "orchestrator", started_from=None, mode="auto_research")
    _child(store, "project", "dispatched", started_from=None, node_id="node")
    _end(store, "dispatched")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO auto_research_child_experiments "
            "(child_episode_id, auto_research_episode_id, project_id, control_node_id, state, "
            "request_json, parent_operation_id, created_at, updated_at) "
            "VALUES ('dispatched', 'orchestrator', 'project', 'node', 'terminal', '{}', "
            "'orchestrator-root', ?, ?)",
            (store.now(), store.now()),
        )
    _child(store, "project", "from-page", started_from=None, node_id="node")
    _end(store, "from-page")
    _child(
        store, "project", "page-continued", started_from=None, continues="from-page", node_id="node"
    )

    def listed(node_id: str | None) -> set[tuple[str, str]]:
        return {
            (episode.episode_id, source)
            for episode, source in store.chat_dock_episodes(
                "project", "chat", node_id=node_id, ended_limit=5
            )
        }

    assert listed("node") == {("page-continued", "node")}
    assert listed(None) == set()


def test_dock_bounds_ended_runs_and_keeps_every_open_one(questions_api, monkeypatch) -> None:
    _app, client, store, project_id = questions_api
    monkeypatch.setattr(agent_threads, "CHAT_DOCK_ENDED_EPISODE_LIMIT", 2)
    thread_id = _project_thread(client)["thread_id"]
    _chat_turn(store, project_id, thread_id, "thread-turn")
    for age, child in enumerate(("newest", "newer", "old", "oldest")):
        _child(store, project_id, child, started_from="thread-turn")
        _end(store, child, ago=timedelta(days=10 * (age + 1)))
        with store.connection() as connection:
            connection.execute(
                "UPDATE episodes SET created_at=ended_at WHERE episode_id=?", (child,)
            )
    for child in ("live-a", "live-b", "live-c"):
        _child(store, project_id, child, started_from="thread-turn")

    body = client.get(f"/api/agent-threads/cards?thread_id={thread_id}").json()

    rows = {row["episode"]["episode_id"]: row for row in body["episodes"]}
    assert set(rows) == {"live-a", "live-b", "live-c", "newest", "newer"}
    assert {row["source"] for row in rows.values()} == {"chat"}
    assert rows["newest"]["episode"]["presentation"]["section"] == "ended"
    assert rows["live-a"]["episode"]["presentation"]["section"] == "running"
    assert rows["newest"]["title"] == rows["newest"]["episode"]["presentation"]["title"]


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


def test_dock_serializes_branch_owning_child(questions_api) -> None:
    _app, client, store, project_id = questions_api
    thread_id = _project_thread(client)["thread_id"]
    _chat_turn(store, project_id, thread_id, "thread-turn")
    episode_id = _child(
        store, project_id, str(uuid4()), started_from="thread-turn", mode="auto_research"
    )
    assert store.branch_owner(project_id, episode_id) == episode_id
    response = client.get("/api/agent-threads/cards", params={"thread_id": thread_id})
    assert response.status_code == 200, response.text
    episode = response.json()["episodes"][0]["episode"]
    assert episode["episode_id"] == episode_id
    assert episode["graph_target"]["branch_id"] == episode_id


def test_dock_batches_archives_and_skips_unrelated_continuations(
    questions_api, monkeypatch
) -> None:
    _app, client, store, project_id = questions_api
    thread_id = _project_thread(client)["thread_id"]
    _chat_turn(store, project_id, thread_id, "thread-turn")
    _chat_turn(store, project_id, "elsewhere", "elsewhere-turn")
    for child in ("first", "second"):
        _child(store, project_id, child, started_from="thread-turn")
    _child(store, project_id, "other", started_from="elsewhere-turn")
    unrelated = _question(store, project_id, owner_kind="episode", owner_id="other")
    archives = Mock(wraps=store.episode_archive_states)
    newest = Mock(wraps=agent_threads._newest_episode)
    monkeypatch.setattr(store, "episode_archive_states", archives)
    monkeypatch.setattr(agent_threads, "_newest_episode", newest)
    children = agent_threads._chat_children(store, project_id, thread_id, [unrelated])
    assert len(children) == 2
    newest.assert_not_called()
    response = client.get("/api/agent-threads/cards", params={"thread_id": thread_id})
    assert response.status_code == 200, response.text
    archives.assert_called_once_with(project_id)


def test_node_chat_dock_shows_its_nodes_page_started_run_with_its_question(questions_api) -> None:
    _app, client, store, project_id = questions_api
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="node-turn",
            project_id=project_id,
            kind="node_chat",
            request={"chat_id": "node-chat", "chat_scope": "node", "node_id": "node"},
        )
    )
    _child(store, project_id, "from-page", started_from=None, node_id="node")
    asked = _question(store, project_id, owner_kind="episode", owner_id="from-page", key="page")

    body = client.get(f"/api/projects/{project_id}/chats/node-chat/cards").json()

    assert [(row["episode"]["episode_id"], row["source"]) for row in body["episodes"]] == [
        ("from-page", "node")
    ]
    assert [row["question_id"] for row in body["episodes"][0]["questions"]] == [asked.question_id]
    assert body["questions"] == []
