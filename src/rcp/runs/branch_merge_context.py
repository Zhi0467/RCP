"""Exact graph snapshots shared by preview, agentless merge, and merge tasks."""

from __future__ import annotations

from contextlib import suppress
from typing import overload

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.history import HistoryManager
from rcp.history.branches import BranchHistoryManager
from rcp.history.queries import HeadSpec, HistoryEngine
from rcp.runs.branch_merge import (
    BranchMergeContext,
    BranchMergeEligibility,
    BranchPatchSummary,
    branch_human_review_changes,
    branch_merge_receipt_from_committed_patch,
)
from rcp.runs.branch_merge_request import MergeBinding
from rcp.runs.task_policy import task_graph_capable
from rcp.service import ProjectService
from rcp.storage import AppStore, EpisodeRecord
from rcp.transport import StateUnavailable


@overload
def merge_history(service: ProjectService, target: None = None) -> HistoryManager: ...


@overload
def merge_history(
    service: ProjectService, target: GraphTargetRef
) -> HistoryManager | BranchHistoryManager: ...


def merge_history(
    service: ProjectService, target: GraphTargetRef | None = None
) -> HistoryManager | BranchHistoryManager:
    """Resolve a merge ref through the canonical project history owner."""
    history = service.history
    root = history.parent if isinstance(history, BranchHistoryManager) else history
    if target is None or target.kind == "main":
        return root
    return root.branch(
        target.branch_id or "", expected_project_id=root.project_id, initialize=False
    )


def _active_branch_writer_task_ids(
    store: AppStore, project_id: str, *, target: GraphTargetRef, exclude_operation_id: str
) -> list[str]:
    return [
        item.operation_id
        for item in store.unsettled_graph_target_tasks(project_id, target)
        if item.operation_id != exclude_operation_id
        and item.kind != "branch_merge"
        and item.status in {"queued", "running", "pausing"}
        and task_graph_capable(item.kind, item.request)
    ]


def load_branch_merge_context(
    service: ProjectService,
    store: AppStore,
    episode: EpisodeRecord | None,
    *,
    operation_id: str,
    authorized_by: AuthorizedHuman,
    merge_binding: MergeBinding | None = None,
) -> BranchMergeContext:
    if episode is not None:
        current = store.episode(episode.episode_id)
        if (
            current is None
            or current.project_id != episode.project_id
            or current.graph_target != episode.graph_target
        ):
            raise ValueError("episode_isolation_owner_changed")
    if merge_binding is None and episode is None:
        raise ValueError("merge_source_binding_missing")
    if merge_binding is not None:
        source = merge_binding.source
    else:
        assert episode is not None
        source = episode.graph_target
    destination = merge_binding.destination if merge_binding else GraphTargetRef()
    branch = merge_history(service, source)
    assert isinstance(branch, BranchHistoryManager)
    metadata = branch.branch_metadata()
    project_id = metadata.project_id
    if episode is not None and episode.project_id != project_id:
        raise ValueError("merge_project_mismatch")
    engine: HistoryEngine = service.history_engine
    branch_head = engine.resolve(HeadSpec(revision="head"), default_target=source)
    if merge_binding is not None and branch_head != merge_binding.source_head:
        raise ValueError("merge_source_head_changed")
    if metadata.effective_parent_head.target != destination:
        raise ValueError("merge_destination_not_source_parent")
    branch_graph = engine.state_at(branch_head)
    if metadata.head != branch_head:
        raise StateUnavailable("The graph branch changed while its merge context loaded.")
    active_writers = _active_branch_writer_task_ids(
        store,
        project_id,
        target=source,
        exclude_operation_id=operation_id,
    )
    eligibility = BranchMergeEligibility(
        branch_head=branch_head,
        active_branch_writer_task_ids=active_writers,
    )
    main_head = engine.resolve(HeadSpec(revision="head"), default_target=destination)
    main_graph = engine.state_at(main_head)
    merge_truth_scope = sorted(set(main_graph.project_truth_scope))
    steps = engine.steps(branch_head)
    base_graph = engine.state_at(metadata.effective_parent_head)
    receipts = [
        *(
            receipt
            for receipt in branch.validated_merge_receipts()
            if receipt.result_main_head.target == destination
        ),
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
        if source_head.revision == metadata.effective_parent_head.revision:
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
        dispatcher_episode_id=merge_binding.dispatcher_episode_id if merge_binding else None,
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
