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
