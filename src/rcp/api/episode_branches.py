from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from rcp.api.dependencies import get_project_service
from rcp.core.attention import project_graph_attention
from rcp.core.models import (
    BranchMergeReceipt,
    GraphBranchMetadata,
    GraphBranchSummary,
)
from rcp.core.transition_models import GraphHeadRef
from rcp.limits import REMOTE_STATE_RECONCILE_WINDOW_SECONDS
from rcp.projects import ProjectCatalog
from rcp.runs.task_policy import task_graph_capable
from rcp.storage import ACTIVE_AGENT_TASK_STATUSES, AppStore, EpisodeRecord
from rcp.storage.models import EpisodeMode


class MainGraphRefResponse(BaseModel):
    """Main has a head but no branch base, episode owner, or merge state."""

    model_config = ConfigDict(extra="forbid")

    name: Literal["main"] = "main"
    running: Literal[False] = False
    needs_you: Literal[False] = False
    created_at: None = None
    kind: Literal["main"] = "main"
    branch_id: None = None
    episode_id: None = None
    current_episode_id: None = None
    base_head: None = None
    head: GraphHeadRef
    merge_eligible: Literal[False] = False
    merge_blocked_reason: None = None
    merge_state: None = None
    latest_successful_merge: None = None
    active_merge_task_id: None = None
    merge_diagnostic: None = None
    archived: Literal[False] = False


class BranchGraphRefResponse(GraphBranchSummary):
    kind: Literal["branch"] = "branch"
    archived: bool
    mode: EpisodeMode | None
    name: str
    running: bool
    needs_you: bool
    created_at: str


GraphRefResponse = Annotated[
    MainGraphRefResponse | BranchGraphRefResponse, Field(discriminator="kind")
]


def project_graph_refs(
    project_id: str,
    *,
    store: AppStore,
    catalog: ProjectCatalog,
    refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
) -> list[GraphRefResponse]:
    """Main first, then durable branch rows, including human forks and archives."""

    service = get_project_service(catalog, project_id)
    with service.history.workspace.read_snapshot():
        records = sorted(
            store.graph_branches(project_id),
            key=lambda record: (
                store.episode_chain(record.owner_episode_id)[-1].created_at
                if record.owner_episode_id
                else record.created_at,
                record.branch_id,
            ),
            reverse=True,
        )
        summaries, attention = _branch_projections(
            project_id,
            [record.branch_id for record in records],
            store=store,
            catalog=catalog,
            refresh_max_age_seconds=refresh_max_age_seconds,
        )
        refs: list[GraphRefResponse] = [MainGraphRefResponse(head=service.history.head_ref())]
        question_branches = {
            question.origin.graph_target.branch_id
            for question in store.list_questions(project_id=project_id, open_only=True)
            if question.origin.graph_target.kind == "branch"
        }
        for record in records:
            summary = summaries.get(record.branch_id)
            if summary is None:
                continue  # A reserved fork has not completed canonical publication yet.
            owner = store.episode(record.owner_episode_id) if record.owner_episode_id else None
            refs.append(
                BranchGraphRefResponse(
                    **summary.model_dump(),
                    archived=record.archived,
                    mode=owner.mode if owner else None,
                    name=record.name,
                    running=any(
                        task.status in ACTIVE_AGENT_TASK_STATUSES
                        for task in store.unsettled_graph_target_tasks(
                            project_id, summary.head.target
                        )
                    ),
                    needs_you=attention[record.branch_id] or record.branch_id in question_branches,
                    created_at=record.created_at,
                )
            )
        return refs


def ensure_episode_graph_target(
    episode: EpisodeRecord,
    *,
    catalog: ProjectCatalog,
) -> None:
    if (
        episode.graph_target.kind != "branch"
        or episode.graph_target.branch_id is None
        or episode.graph_base_head is None
        or episode.authorized_by is None
    ):
        raise ValueError("Episode reservation lost its exact graph branch identity.")
    record = catalog.store.graph_branch(episode.project_id, episode.graph_target.branch_id)
    if (
        record is None
        or record.owner_episode_id != episode.episode_id
        or record.parent_head != episode.graph_base_head
    ):
        raise ValueError("Episode reservation lost its exact graph branch record.")
    service = catalog.open_execution(episode.project_id)
    service.history.create_branch(
        GraphBranchMetadata(
            kind=episode.mode,
            branch_id=episode.graph_target.branch_id,
            episode_id=episode.episode_id,
            project_id=episode.project_id,
            base_head=episode.graph_base_head,
            parent_head=(
                episode.graph_base_head
                if episode.graph_target.branch_id != episode.episode_id
                else None
            ),
            created_by=(
                record.created_by if episode.graph_target.branch_id != episode.episode_id else None
            ),
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
    return _graph_branch_projections(
        episodes,
        store=store,
        catalog=catalog,
        refresh_max_age_seconds=refresh_max_age_seconds,
    )[0]


def _graph_branch_projections(
    episodes: list[EpisodeRecord],
    *,
    store: AppStore,
    catalog: ProjectCatalog,
    refresh_max_age_seconds: float,
) -> tuple[dict[str, GraphBranchSummary], dict[str, bool]]:
    """One summary per episode; every member of a chain shares its branch's summary."""

    grouped: dict[str, set[str]] = {}
    for episode in episodes:
        if episode.graph_target.kind != "branch" or episode.graph_target.branch_id is None:
            raise ValueError("only a branch-target episode has a graph branch summary")
        grouped.setdefault(episode.project_id, set()).add(episode.graph_target.branch_id)
    by_branch: dict[str, GraphBranchSummary] = {}
    attention: dict[str, bool] = {}
    for project_id, branch_ids in grouped.items():
        summaries, branch_attention = _branch_projections(
            project_id,
            list(branch_ids),
            store=store,
            catalog=catalog,
            refresh_max_age_seconds=refresh_max_age_seconds,
        )
        by_branch.update(summaries)
        attention.update(branch_attention)
    return (
        {
            episode.episode_id: by_branch[episode.graph_target.branch_id or ""]
            for episode in episodes
        },
        attention,
    )


def _branch_projections(
    project_id: str,
    branch_ids: list[str],
    *,
    store: AppStore,
    catalog: ProjectCatalog,
    refresh_max_age_seconds: float,
) -> tuple[dict[str, GraphBranchSummary], dict[str, bool]]:
    records = {record.branch_id: record for record in store.graph_branches(project_id)}
    service = get_project_service(catalog, project_id)
    snapshots = service.history.branch_read_snapshots(
        [(branch_id, records[branch_id].owner_episode_id, project_id) for branch_id in branch_ids],
        refresh_max_age_seconds=refresh_max_age_seconds,
    )
    summaries: dict[str, GraphBranchSummary] = {}
    attention: dict[str, bool] = {}
    for branch_id in branch_ids:
        record = records[branch_id]
        root = store.episode(record.owner_episode_id) if record.owner_episode_id else None
        snapshot = snapshots[branch_id]
        projection = project_graph_attention(snapshot.state) if snapshot else None
        attention[branch_id] = bool(
            projection and (projection.decisions_awaiting_choice_ids or projection.open_blocker_ids)
        )
        if snapshot is None:
            if root is not None and root.graph_target.branch_id == branch_id:
                summaries[branch_id] = missing_graph_branch_summary(root, store=store)
            continue
        summary = graph_branch_summary_from_snapshot(
            root,
            snapshot.metadata,
            list(snapshot.receipts),
            store=store,
            current_episode_id=store.episode_chain(root.episode_id)[-1].episode_id
            if root
            else None,
        )
        if snapshot.state.replay_status == "degraded":
            summary = summary.model_copy(
                update={
                    "merge_eligible": False,
                    "merge_blocked_reason": "This branch's history is damaged and read-only.",
                }
            )
        summaries[branch_id] = summary
    return summaries, attention


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
        branch_id=episode.graph_target.branch_id or "",
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
    episode: EpisodeRecord | None,
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
        for item in (store.episode_tasks(episode.episode_id) if episode else [])
        if item.kind == "branch_merge"
        and item.project_id == metadata.project_id
        and item.graph_target == metadata.head.target
    ]
    active_task = next(
        (item for item in reversed(merge_tasks) if item.status in ACTIVE_AGENT_TASK_STATUSES),
        None,
    )
    latest_task = merge_tasks[-1] if merge_tasks else None
    active_branch_writers = [
        item
        for item in store.unsettled_graph_target_tasks(
            metadata.project_id,
            metadata.head.target,
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
    elif metadata.head.revision <= metadata.effective_parent_head.revision:
        blocked_reason = "This branch has no changes to merge."
    elif active_branch_writers:
        writers = ", ".join(
            f"{item.kind} {item.operation_id} ({item.status})" for item in active_branch_writers
        )
        blocked_reason = f"Branch writers must settle before merging: {writers}."
    else:
        blocked_reason = None
    if episode is None or episode.graph_target != metadata.head.target:
        blocked_reason = "This fork has no episode merge dispatcher."
    binding = store.episode_isolation_for_branch(metadata.project_id, metadata.branch_id)
    state = (
        store.episode_isolation_state(metadata.project_id, binding.owner_episode_id)
        if binding
        else None
    )
    if state and state.merge_reservation:
        blocked_reason = "episode_merge_reserved"
    # Code status is the Merge panel's own preview; a list read never probes Git.
    return GraphBranchSummary(
        branch_id=metadata.branch_id,
        episode_id=metadata.episode_id,
        current_episode_id=current_episode_id or metadata.episode_id,
        base_head=metadata.base_head,
        parent_head=metadata.effective_parent_head,
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
    "GraphRefResponse",
    "project_graph_refs",
    "ensure_episode_graph_target",
    "graph_branch_summaries",
    "graph_branch_summary",
    "graph_branch_summary_from_snapshot",
    "missing_graph_branch_summary",
]
