"""Reserve and publish a fork using one durable identity across retries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from rcp.core.models import AuthorizedHuman, GraphBranchMetadata
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history.branches import BranchHistoryManager, _create_branch
from rcp.history.manager import HistoryManager
from rcp.storage import AppStore


def create_graph_fork(
    history: HistoryManager,
    store: AppStore,
    *,
    project_id: str,
    branch_id: str,
    parent_head: GraphHeadRef,
    authorized_by: AuthorizedHuman,
    created_by: str,
    owner_episode_id: str | None = None,
    name: str | None = None,
    auto_archive: bool = False,
    before_publish: Callable[[], None] | None = None,
) -> BranchHistoryManager:
    """A caller retains ``branch_id`` and repeats the same request after uncertainty.

    The row reserves the name before publication; an absent or uncertain canonical
    write retains that reservation and can only be retried with its original head.
    """

    def reserve_metadata(base_head: GraphHeadRef) -> GraphBranchMetadata:
        record = store.create_graph_branch(
            project_id=project_id,
            branch_id=branch_id,
            owner_episode_id=owner_episode_id,
            parent_head=parent_head,
            created_by=created_by,
            name=name,
            auto_archive=auto_archive,
        )
        return GraphBranchMetadata(
            kind="fork",
            branch_id=record.branch_id,
            episode_id=record.owner_episode_id,
            project_id=record.project_id,
            base_head=base_head,
            parent_head=record.parent_head,
            head=GraphHeadRef(
                target=GraphTargetRef(kind="branch", branch_id=record.branch_id),
                revision=record.parent_head.revision,
                transition_id=record.parent_head.transition_id,
            ),
            created_by=record.created_by,
            created_at=datetime.fromisoformat(record.created_at),
            authorized_by=authorized_by,
        )

    return _create_branch(
        history, branch_id, parent_head, reserve_metadata, before_publish=before_publish
    )
