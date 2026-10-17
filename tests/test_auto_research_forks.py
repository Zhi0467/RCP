from __future__ import annotations

import logging
import uuid

from rcp.history import HistoryManager
from rcp.runs.auto_research_forks import archive_settled_auto_research_forks
from rcp.runs.graph_forks import create_graph_fork
from tests.test_auto_research_children_storage import _setup_parent, _work_pair
from tests.test_auto_research_experiments import _service


def test_autoarchive_uses_exact_canonical_head_and_all_writer_settlement(manifest, tmp_path):
    service = _service(manifest, tmp_path)
    store, parent, root = _setup_parent(tmp_path)
    assert isinstance(service.history, HistoryManager)
    assert parent.authorized_by is not None
    source = service.history.head_ref()
    history = create_graph_fork(
        service.history,
        store,
        project_id=parent.project_id,
        branch_id=str(uuid.uuid4()),
        owner_episode_id=parent.episode_id,
        parent_head=source,
        authorized_by=parent.authorized_by,
        created_by="orchestrator:test",
        auto_archive=True,
    )
    route, task = _work_pair(store, parent, root, worker_id="empty-worker")
    task = task.model_copy(update={"graph_target": history.graph_target})
    store.create_auto_research_child_work(route, task)
    archive_settled_auto_research_forks(
        store, lambda _: service, logger=logging.getLogger(__name__)
    )
    assert store.branch_admits_new_work(parent.project_id, history.branch_id)
    store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    archive_settled_auto_research_forks(
        store, lambda _: service, logger=logging.getLogger(__name__)
    )
    assert not store.branch_admits_new_work(parent.project_id, history.branch_id)
    assert history.head_ref().revision == source.revision
