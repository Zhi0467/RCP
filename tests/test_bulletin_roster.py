from __future__ import annotations

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
