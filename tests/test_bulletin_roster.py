from __future__ import annotations

import json
from unittest.mock import Mock
from uuid import uuid4

from rcp.bulletin import bulletin_roster
from rcp.core.transition_models import GraphTargetRef
from tests.test_auto_research_child_work_watchers_storage import _waiting_child


def test_roster_keeps_sleeping_worker_waiting_on_its_target(tmp_path) -> None:
    store, episode, work, _watcher, _wake = _waiting_child(tmp_path)
    roster = bulletin_roster(store, episode.project_id, episode.graph_target)
    actor = next(row for row in roster if row["id"] == f"worker:{work.worker_id}")
    assert actor["state"] == "waiting"
    assert actor["seat_node_id"] == work.control_node_id
    assert bulletin_roster(store, "other", episode.graph_target) == []
    assert bulletin_roster(store, episode.project_id, GraphTargetRef()) == []
    store.request_auto_research_child_work_stop(work.worker_id)
    actor = next(
        row
        for row in bulletin_roster(store, episode.project_id, episode.graph_target)
        if row["id"] == f"worker:{work.worker_id}"
    )
    assert actor["state"] == "done"


def test_human_work_waits_for_undelivered_watcher_by_chat_id(tmp_path) -> None:
    store, episode, work, watcher, _wake = _waiting_child(tmp_path)
    task = store.agent_task(work.current_operation_id)
    assert task is not None
    target = GraphTargetRef()
    request = {**task.request, "chat_id": "human-chat", "trigger": "human"}
    human = task.model_copy(
        update={
            "operation_id": "human-latest",
            "episode_id": None,
            "graph_target": target,
            "request": request,
        }
    )
    store.create_agent_task(human)
    observer = watcher.model_copy(
        update={
            "watcher_id": "human-observer",
            "episode_id": None,
            "worker_id": None,
            "chat_id": "human-chat",
            "graph_target": target,
            "origin_operation_id": "human-earlier",
        }
    )
    store.create_watchers([observer])
    assert bulletin_roster(store, episode.project_id, target)[0]["state"] == "waiting"
    with store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET notified=1 WHERE watcher_id=?", (observer.watcher_id,)
        )
    assert bulletin_roster(store, episode.project_id, target)[0]["state"] == "done"
    with store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET notified=0, graph_target_json=? WHERE watcher_id=?",
            (episode.graph_target.model_dump_json(), observer.watcher_id),
        )
    assert bulletin_roster(store, episode.project_id, target)[0]["state"] == "done"


def test_bulletin_watermark_ignores_live_status_and_observer_poll_ticks(tmp_path) -> None:
    store, episode, work, watcher, _wake = _waiting_child(tmp_path)
    with store.connection() as connection:

        def generation():
            return connection.execute(
                "SELECT SUM(generation) FROM resource_versions WHERE scope=? AND resource='bulletin'",
                (episode.project_id,),
            ).fetchone()[0]

        before = generation()
        connection.execute(
            "UPDATE graph_runs SET agent_status_json=?,last_activity_at=?,updated_at=? WHERE operation_id=?",
            ("{}", store.now(), store.now(), work.current_operation_id),
        )
        connection.execute(
            "UPDATE watchers SET last_checked_at=? WHERE watcher_id=?",
            (store.now(), watcher.watcher_id),
        )
        assert generation() == before
        connection.execute(
            "UPDATE watchers SET notified=1 WHERE watcher_id=?", (watcher.watcher_id,)
        )
        assert generation() > before
        before = generation()
        connection.execute(
            "UPDATE graph_runs SET status='running' WHERE operation_id=?",
            (work.current_operation_id,),
        )
        assert generation() > before


def test_conversation_roster_matches_inventory_and_archives(tmp_path) -> None:
    from tests.test_auto_research_children_storage import (
        _experiment_route,
        _experiment_task,
        _setup_parent,
        _work_pair,
    )

    store, parent, root = _setup_parent(tmp_path)
    work, task = _work_pair(store, parent, root, worker_id="worker", chat_id="worker")
    store.create_auto_research_child_work(work, task)
    for _ in range(2):
        task = _experiment_task(
            store, str(uuid4()), parent.authorized_by, node_id="exp/one", trigger="experiment_run"
        )
        task.request["chat_id"] = "shared-chat"
        store.create_experiment_episode_with_invocation(task)
        with store.connection() as connection:
            connection.execute(
                "UPDATE episodes SET status='completed' WHERE episode_id=?", (task.episode_id,)
            )
            connection.execute(
                "UPDATE graph_runs SET status='succeeded' WHERE operation_id=?",
                (task.operation_id,),
            )
    child_id = str(uuid4())
    child = _experiment_task(store, child_id, parent.authorized_by, node_id="exp/child")
    child.request["chat_id"] = child_id
    store.create_experiment_episode_with_invocation(
        child, auto_research_route=_experiment_route(store, parent, root, child)
    )
    inventory = {row["chat_id"]: row for row in store.chat_inventory(parent.project_id)}
    roster = bulletin_roster(store, parent.project_id, parent.graph_target)
    assert {row["chat_id"] for row in roster} == set(inventory)
    assert {row["id"] for row in roster} == {
        f"episode:{parent.episode_id}",
        f"worker:{work.worker_id}",
        "chat:shared-chat",
        f"chat:{child_id}",
    }
    assert all(row["name"] == inventory[row["chat_id"]]["title"] for row in roster)
    shared = next(row for row in roster if row["chat_id"] == "shared-chat")
    assert shared["started_at"] == min(
        t.created_at
        for t in store.graph_target_tasks(parent.project_id, parent.graph_target)
        if t.request.get("chat_id") == "shared-chat"
    )
    store.set_chat_archived(
        parent.project_id, "shared-chat", store.local_owner.user_id, archived=True
    )
    assert "shared-chat" not in {
        row["chat_id"] for row in bulletin_roster(store, parent.project_id)
    }


def test_http_roster_spans_channels_but_agent_snapshot_is_scoped(tmp_path, monkeypatch) -> None:
    from rcp.api.bulletin import list_bulletin
    from rcp.runs.bulletin import _add_context

    store, episode, work, _watcher, _wake = _waiting_child(tmp_path)
    task = store.agent_task(work.current_operation_id)
    human = task.model_copy(
        update={
            "operation_id": "human",
            "episode_id": None,
            "graph_target": GraphTargetRef(),
            "request": {**task.request, "chat_id": "human", "message": "Main discussion"},
        }
    )
    store.create_agent_task(human)
    reads = {}
    for method in ("chat_inventory", "episodes", "list_questions"):
        reads[method] = Mock(wraps=getattr(store, method))
        monkeypatch.setattr(store, method, reads[method])
    page = list_bulletin(project=episode.project_id, store=store)
    for read in reads.values():
        read.assert_called_once()
    assert {row["target_key"] for row in page["roster"]} == {"main", episode.graph_target.key}
    snapshot = {}
    _add_context(store, task.operation_id, snapshot)
    assert {row["target_key"] for row in snapshot["roster"]} == {episode.graph_target.key}
    assert "human" not in {row["chat_id"] for row in snapshot["roster"]}


def test_orchestrator_chat_turns_keep_the_chain_actor(tmp_path) -> None:
    from rcp.bulletin import bulletin_actor_id
    from tests.test_auto_research_children_storage import _setup_parent

    store, episode, root = _setup_parent(tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status='completed' WHERE episode_id=?", (episode.episode_id,)
        )
        connection.execute(
            "UPDATE graph_runs SET status='succeeded' WHERE operation_id=?", (root.operation_id,)
        )
    for kind in ("artifact_edit", "project_chat"):
        task = root.model_copy(
            update={
                "operation_id": kind,
                "episode_id": None,
                "kind": kind,
                "dispatch_authority": None,
                "request": {"chat_id": episode.episode_id, "message": "Continue research"},
            }
        )
        store.create_agent_task(task)
        with store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id=?", (task.operation_id,)
            ).fetchone()
            assert bulletin_actor_id(connection, row) == f"episode:{episode.episode_id}"
    roster = bulletin_roster(store, episode.project_id)
    assert [row["id"] for row in roster] == [f"episode:{episode.episode_id}"]


def test_seated_workers_are_not_roster_conversations(tmp_path) -> None:
    from tests.test_auto_research_stream import _setup_auto_research

    store, episode, _root, _worker = _setup_auto_research(tmp_path)
    assert [row["id"] for row in bulletin_roster(store, episode.project_id)] == [
        f"episode:{episode.episode_id}"
    ]


def test_upgrade_moves_experiment_posts_to_their_conversation_identity(tmp_path) -> None:
    from rcp.storage.bulletin import migrate_bulletin_actor_ids
    from tests.test_auto_research_children_storage import _experiment_task, _setup_parent

    store, parent, _root = _setup_parent(tmp_path)
    task = _experiment_task(
        store, str(uuid4()), parent.authorized_by, node_id="exp/one", trigger="experiment_run"
    )
    task.request["chat_id"] = "loop-chat"
    store.create_experiment_episode_with_invocation(task)
    old = f"episode:{task.episode_id}"
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO bulletin_posts(post_id,project_id,target_key,author_kind,operation_id,"
            "actor_label,text,refs_json,created_at,author_actor_id,mentions_json) "
            "VALUES('p',?,?,'agent',?,'Experiment',?,'[]',?,?,?)",
            (
                parent.project_id,
                task.graph_target.key,
                task.operation_id,
                f"see <@agent:{old}>",
                store.now(),
                old,
                json.dumps([{"kind": "agent", "id": old}]),
            ),
        )
        migrate_bulletin_actor_ids(connection)
        row = connection.execute("SELECT * FROM bulletin_posts WHERE post_id='p'").fetchone()
    assert row["author_actor_id"] == "chat:loop-chat"
    assert row["text"] == "see <@agent:chat:loop-chat>"
    assert json.loads(row["mentions_json"]) == [{"kind": "agent", "id": "chat:loop-chat"}]


def test_orchestrator_posts_keep_its_conversation_name(tmp_path) -> None:
    from tests.test_auto_research_children_storage import _setup_parent

    store, episode, root = _setup_parent(tmp_path)
    owner = store.local_owner.user_id
    store.seat_project_member(episode.project_id, owner)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_user_id=?, "
            "dispatch_authority_json=json_set(COALESCE(dispatch_authority_json,'{}'),"
            "'$.task_contract','orchestrate') WHERE operation_id=?",
            (owner, root.operation_id),
        )
    store.set_chat_title(episode.project_id, episode.episode_id, owner, "Sweep lead")
    post = store.execute_bulletin_command(
        episode.project_id, root.operation_id, "post", key="k", text="hello"
    )["post"]
    assert post["author"]["actor_id"] == f"episode:{episode.episode_id}"
    assert post["author"]["actor_label"] == "Sweep lead"
