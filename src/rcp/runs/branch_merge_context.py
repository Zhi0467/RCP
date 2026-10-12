"""Exact graph snapshots shared by preview, agentless merge, and merge tasks."""

from __future__ import annotations

from contextlib import suppress

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.history.queries import HeadSpec, HistoryEngine
from rcp.runs.branch_merge import (
    BranchMergeContext,
    BranchMergeEligibility,
    BranchPatchSummary,
    branch_human_review_changes,
    branch_merge_receipt_from_committed_patch,
)
from rcp.runs.task_policy import task_graph_capable
from rcp.service import ProjectService
from rcp.storage import AppStore, EpisodeRecord
from rcp.transport import StateUnavailable


def _active_branch_writer_task_ids(
    store: AppStore, episode: EpisodeRecord, *, exclude_operation_id: str
) -> list[str]:
    return [
        item.operation_id
        for item in store.unsettled_graph_target_tasks(episode.project_id, episode.graph_target)
        if item.operation_id != exclude_operation_id
        and item.kind != "branch_merge"
        and item.status in {"queued", "running", "pausing"}
        and task_graph_capable(item.kind, item.request)
    ]


def load_branch_merge_context(
    service: ProjectService,
    store: AppStore,
    episode: EpisodeRecord,
    *,
    operation_id: str,
    authorized_by: AuthorizedHuman,
) -> BranchMergeContext:
    current = store.episode(episode.episode_id)
    if (
        current is None
        or current.project_id != episode.project_id
        or current.graph_target != episode.graph_target
    ):
        raise ValueError("episode_isolation_owner_changed")
    branch = service.history.branch(
        episode.episode_id,
        expected_episode_id=episode.episode_id,
        expected_project_id=episode.project_id,
        initialize=False,
    )
    engine: HistoryEngine = service.history_engine
    metadata = branch.branch_metadata()
    branch_head = engine.resolve(HeadSpec(revision="head"), default_target=episode.graph_target)
    branch_graph = engine.state_at(branch_head)
    if metadata.head != branch_head:
        raise StateUnavailable("The graph branch changed while its merge context loaded.")
    active_writers = _active_branch_writer_task_ids(
        store,
        episode,
        exclude_operation_id=operation_id,
    )
    eligibility = BranchMergeEligibility(
        branch_head=branch_head,
        active_branch_writer_task_ids=active_writers,
    )
    main_head = engine.resolve(HeadSpec(revision="head"), default_target=GraphTargetRef())
    main_graph = engine.state_at(main_head)
    merge_truth_scope = sorted(set(main_graph.project_truth_scope))
    steps = engine.steps(branch_head)
    base_graph = engine.state_at(metadata.base_head)
    receipts = [
        *branch.validated_merge_receipts(),
        *(
            branch_merge_receipt_from_committed_patch(step.patch)
            for step in engine.steps(main_head)
            if step.accepted
            and step.patch.branch_merge is not None
            and step.patch.branch_merge.branch_id == metadata.branch_id
        ),
    ]
    previous_receipt = max(
        receipts, key=lambda item: item.provenance.branch_head.revision, default=None
    )
    previous_graph = None
    if previous_receipt is not None:
        source_head = previous_receipt.provenance.branch_head
        if source_head.revision == metadata.base_head.revision:
            previous_graph = base_graph
        else:
            # The exact source may end on a retained rejected receipt.
            with suppress(ValueError):
                previous_graph = engine.state_at(source_head)
        if previous_graph is None:
            raise StateUnavailable(
                "The prior merge receipt lost its exact canonical source snapshot."
            )
    return BranchMergeContext.create(
        merge_task_id=operation_id,
        authorized_by=authorized_by,
        metadata=metadata,
        eligibility=eligibility,
        base_graph=base_graph,
        branch_graph=branch_graph,
        main_head=main_head,
        main_graph=main_graph,
        # Graph provenance covers current repository truth membership;
        # the merge does not read or gain write access to repositories.
        run_truth_scope=merge_truth_scope,
        human_review_changes=branch_human_review_changes(
            steps, previous_graph or base_graph, branch_graph
        ),
        previous_merge_receipt=previous_receipt,
        previous_branch_graph=previous_graph,
        branch_patch_summaries=[
            BranchPatchSummary(
                revision=step.patch.revision,
                transition_id=(
                    step.patch.transition.transition_id
                    if step.patch.transition is not None
                    else None
                ),
                summary=step.patch.summary,
                change_summary=list(step.patch.change_summary),
                task_id=step.patch.task_id,
                profile=step.patch.profile,
            )
            for step in steps
        ],
    )
