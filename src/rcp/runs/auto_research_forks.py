"""Settlement of automatically created, unchanged Auto-research branches."""

from __future__ import annotations

import logging
from collections.abc import Callable

from rcp.core.transition_models import GraphTargetRef
from rcp.service import ProjectService
from rcp.storage import AppStore


def archive_settled_auto_research_forks(
    store: AppStore,
    project_service: Callable[[str], ProjectService],
    *,
    episode_id: str | None = None,
    logger: logging.Logger,
) -> None:
    """Recheck fork heads and operational fences on the existing child poll."""

    visited: set[tuple[str, str]] = set()
    for current_id in store.auto_research_episode_ids(episode_id):
        episode = store.episode(current_id)
        if episode is None:
            continue
        for branch in store.branches_owned_by(episode.project_id, current_id):
            key = (branch.project_id, branch.branch_id)
            if key in visited or not branch.auto_archive or branch.archived:
                continue
            visited.add(key)
            try:
                service = project_service(branch.project_id)
                target = GraphTargetRef(kind="branch", branch_id=branch.branch_id)
                history = service.history.branch(branch.branch_id, initialize=False)
                with history.workspace.transaction():
                    if any(history.patches_dir.glob(".unconfirmed-*")):
                        continue
                    generation = history.history_engine.snapshot(target, refresh=False)
                    if generation.result.state.replay_status != "complete":
                        continue
                    store.archive_empty_auto_research_fork(
                        branch.project_id, generation.head(generation.result.state.revision)
                    )
            except Exception:
                logger.exception(
                    "Could not reconcile automatic archive for branch %s", branch.branch_id
                )
