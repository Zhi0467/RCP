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
    assert not store.graph_branch(parent.project_id, history.branch_id).archived
    store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    archive_settled_auto_research_forks(
        store, lambda _: service, logger=logging.getLogger(__name__)
    )
    assert store.graph_branch(parent.project_id, history.branch_id).archived
    assert history.head_ref().revision == source.revision


def test_autoarchive_waits_for_incoming_merge(manifest, tmp_path):
    from rcp.runs.branch_merge_request import BranchMergeRunRequest
    from tests.test_owned_merge_storage import _owned_merge

    harness, task = _owned_merge(manifest, tmp_path)
    store, history = harness.store, harness.service.history
    destination = history.branch(task.graph_target.branch_id, initialize=False)
    assert harness.episode.authorized_by is not None
    source = create_graph_fork(
        history,
        store,
        project_id=task.project_id,
        branch_id=str(uuid.uuid4()),
        owner_episode_id=task.episode_id,
        parent_head=destination.head_ref(),
        authorized_by=harness.episode.authorized_by,
        created_by="orchestrator:test",
    )
    # The first fork is unchanged and eligible once its incoming merge settles.
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_branches SET auto_archive=1 WHERE branch_id=?",
            (destination.branch_id,),
        )
    request = BranchMergeRunRequest.model_validate(task.request)
    assert request.merge_binding is not None
    binding = request.merge_binding.model_copy(
        update={
            "source": source.graph_target,
            "source_head": source.head_ref(),
            "destination": destination.graph_target,
        }
    )
    task = task.model_copy(
        update={
            "graph_target": source.graph_target,
            "request": request.model_copy(update={"merge_binding": binding}).model_dump(
                mode="json"
            ),
        }
    )
    store.create_branch_merge_task(task)
    archive_settled_auto_research_forks(
        store, lambda _: harness.service, logger=logging.getLogger(__name__)
    )
    assert not store.graph_branch(task.project_id, destination.branch_id).archived
    assert store.agent_task_authority(task.project_id, task.operation_id).apply_target == (
        destination.graph_target
    )
    store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    archive_settled_auto_research_forks(
        store, lambda _: harness.service, logger=logging.getLogger(__name__)
    )
    assert store.graph_branch(task.project_id, destination.branch_id).archived
