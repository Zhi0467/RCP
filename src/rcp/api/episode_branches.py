from __future__ import annotations

from typing import Literal

from rcp.api.dependencies import get_project_service
from rcp.core.models import BranchMergeReceipt, GraphBranchMetadata, GraphBranchSummary
from rcp.core.transition_models import GraphHeadRef
from rcp.limits import REMOTE_STATE_RECONCILE_WINDOW_SECONDS
from rcp.projects import ProjectCatalog
from rcp.runs.task_policy import task_graph_capable
from rcp.storage import ACTIVE_AGENT_TASK_STATUSES, AppStore, EpisodeRecord


def ensure_auto_research_graph_target(
    episode: EpisodeRecord,
    *,
    catalog: ProjectCatalog,
) -> None:
    if (
        episode.mode != "auto_research"
        or episode.graph_target.kind != "branch"
        or episode.graph_target.branch_id != episode.episode_id
        or episode.graph_base_head is None
        or episode.authorized_by is None
    ):
        raise ValueError("Auto-research reservation lost its exact graph branch identity.")
    service = get_project_service(catalog, episode.project_id)
    service.history.create_auto_research_branch(
        GraphBranchMetadata(
            branch_id=episode.episode_id,
            episode_id=episode.episode_id,
            project_id=episode.project_id,
            base_head=episode.graph_base_head,
            head=GraphHeadRef(
                target=episode.graph_target,
                revision=episode.graph_base_head.revision,
                transition_id=episode.graph_base_head.transition_id,
            ),
            created_at=episode.created_at,
            authorized_by=episode.authorized_by,
        )
    )


def graph_branch_summaries(
    episodes: list[EpisodeRecord],
    *,
    store: AppStore,
    catalog: ProjectCatalog,
    refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
) -> dict[str, GraphBranchSummary]:
    """One summary per episode; every member of a chain shares its branch's summary."""

    branches: dict[str, EpisodeRecord] = {}
    for episode in episodes:
        if episode.mode != "auto_research" or episode.graph_target.kind != "branch":
            raise ValueError("only an Auto-research branch has a graph branch summary")
        branch_id = episode.graph_target.branch_id
        assert branch_id is not None
        root = episode if episode.episode_id == branch_id else store.episode(branch_id)
        if (
            root is None
            or root.project_id != episode.project_id
            or root.graph_target != episode.graph_target
        ):
            raise ValueError("an Auto-research branch requires its chain root episode")
        branches[branch_id] = root

    grouped: dict[str, list[EpisodeRecord]] = {}
    for root in branches.values():
        grouped.setdefault(root.project_id, []).append(root)
    by_branch: dict[str, GraphBranchSummary] = {}
    for project_id, roots in grouped.items():
        service = get_project_service(catalog, project_id)
        snapshots = service.history.branch_read_snapshots(
            [(root.episode_id, root.episode_id, root.project_id) for root in roots],
            refresh_max_age_seconds=refresh_max_age_seconds,
        )
        for root in roots:
            snapshot = snapshots[root.episode_id]
            current_episode_id = store.episode_chain(root.episode_id)[-1].episode_id
            by_branch[root.episode_id] = (
                missing_graph_branch_summary(root, store=store)
                if snapshot is None
                else graph_branch_summary_from_snapshot(
                    root,
                    snapshot.metadata,
                    list(snapshot.receipts),
                    store=store,
                    current_episode_id=current_episode_id,
                )
            )
    return {
        episode.episode_id: by_branch[episode.graph_target.branch_id or ""] for episode in episodes
    }


def missing_graph_branch_summary(
    episode: EpisodeRecord,
    *,
    store: AppStore,
) -> GraphBranchSummary:
    if episode.status not in {"queued", "failed"} or episode.graph_base_head is None:
        raise KeyError(episode.episode_id)
    root = (
        store.agent_task(episode.root_operation_id)
        if episode.root_operation_id is not None
        else None
    )
    return GraphBranchSummary(
        branch_id=episode.episode_id,
        episode_id=episode.episode_id,
        current_episode_id=episode.episode_id,
        base_head=episode.graph_base_head,
        head=GraphHeadRef(
            target=episode.graph_target,
            revision=episode.graph_base_head.revision,
            transition_id=episode.graph_base_head.transition_id,
        ),
        merge_eligible=False,
        merge_blocked_reason="The episode graph branch has not been established yet.",
        merge_state="failed" if episode.status == "failed" else "unmerged",
        merge_diagnostic=(
            root.error
            if root is not None and root.error
            else episode.ending_diagnostic
            if episode.status == "failed"
            else "Establishing the episode graph branch before provider launch."
        ),
    )


def graph_branch_summary_from_snapshot(
    episode: EpisodeRecord,
    metadata: GraphBranchMetadata,
    receipts: list[BranchMergeReceipt],
    *,
    store: AppStore,
    current_episode_id: str | None = None,
) -> GraphBranchSummary:
    """Merge eligibility from branch facts alone: head, receipts, and live writers.

    Nothing about the episode is a condition. Its status, ending, wrap-up, and
    paused turns do not matter; a paused writer cannot restart while a merge
    runs because admission fences the branch.
    """

    current_receipt = next(
        (item for item in reversed(receipts) if item.provenance.branch_head == metadata.head),
        None,
    )
    merge_tasks = [
        item
        for item in store.episode_tasks(episode.episode_id)
        if item.kind == "branch_merge"
        and item.project_id == episode.project_id
        and item.graph_target == episode.graph_target
    ]
    active_task = next(
        (item for item in reversed(merge_tasks) if item.status in ACTIVE_AGENT_TASK_STATUSES),
        None,
    )
    latest_task = merge_tasks[-1] if merge_tasks else None
    active_branch_writers = [
        item
        for item in store.unsettled_graph_target_tasks(
            episode.project_id,
            episode.graph_target,
        )
        if item.kind != "branch_merge"
        and item.status in {"queued", "running", "pausing"}
        and task_graph_capable(item.kind, item.request)
    ]
    if active_task is not None:
        merge_state: Literal["unmerged", "running", "merged", "needs_action", "failed"] = "running"
    elif current_receipt is not None:
        merge_state = "merged"
    elif latest_task is not None and latest_task.status in {"paused", "interrupted"}:
        merge_state = "needs_action"
    elif latest_task is not None and latest_task.status == "failed":
        merge_state = "failed"
    else:
        merge_state = "unmerged"
    diagnostic = (
        (latest_task.error or latest_task.status_message)
        if merge_state in {"needs_action", "failed"} and latest_task is not None
        else None
    )
    if active_task is not None:
        blocked_reason = "A merge is already running for this branch. Wait for it to finish."
    elif current_receipt is not None:
        blocked_reason = "This branch head has already been merged to main."
    elif metadata.head.revision <= metadata.base_head.revision:
        blocked_reason = "This branch has no changes to merge."
    elif active_branch_writers:
        writers = ", ".join(
            f"{item.kind} {item.operation_id} ({item.status})" for item in active_branch_writers
        )
        blocked_reason = f"Branch writers must settle before merging: {writers}."
    else:
        blocked_reason = None
    return GraphBranchSummary(
        branch_id=metadata.branch_id,
        episode_id=metadata.episode_id,
        current_episode_id=current_episode_id or metadata.episode_id,
        base_head=metadata.base_head,
        head=metadata.head,
        merge_eligible=blocked_reason is None,
        merge_blocked_reason=blocked_reason,
        merge_state=merge_state,
        latest_successful_merge=receipts[-1] if receipts else None,
        active_merge_task_id=(active_task.operation_id if active_task is not None else None),
        merge_diagnostic=diagnostic,
    )


def graph_branch_summary(
    episode: EpisodeRecord,
    *,
    store: AppStore,
    catalog: ProjectCatalog,
    refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
) -> GraphBranchSummary:
    return graph_branch_summaries(
        [episode],
        store=store,
        catalog=catalog,
        refresh_max_age_seconds=refresh_max_age_seconds,
    )[episode.episode_id]


__all__ = [
    "ensure_auto_research_graph_target",
    "graph_branch_summaries",
    "graph_branch_summary",
    "graph_branch_summary_from_snapshot",
    "missing_graph_branch_summary",
]
