"""Exact graph snapshots shared by preview, agentless merge, and merge tasks."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import overload

from rcp.core.authority import AgentTaskAuthority, WriteAuthority
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import AuthorizedHuman, GraphState, Patch
from rcp.core.operations import graph_operations_from_proposal
from rcp.core.transition_models import GraphTargetRef
from rcp.history import HistoryManager
from rcp.history.branches import BranchHistoryManager
from rcp.history.queries import HeadSpec, HistoryEngine, StepRecord
from rcp.runs.branch_merge import (
    BranchMergeContext,
    BranchMergeEligibility,
    BranchPatchSummary,
    branch_human_review_changes,
    branch_merge_receipt_from_committed_patch,
    merge_planning_context,
)
from rcp.runs.branch_merge_request import BranchMergeRunRequest, MergeBinding
from rcp.runs.merge_contract import MergePlanningContext, PriorReview, merge_contract_digest
from rcp.runs.merge_plan import compare_graphs
from rcp.runs.task_policy import resolved_dispatch_authority, task_graph_capable
from rcp.service import ProjectService, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord, AppStore, EpisodeRecord
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


def preview_merge_planning_context(
    service: ProjectService,
    store: AppStore,
    context: BranchMergeContext,
    *,
    created_at: datetime,
) -> MergePlanningContext:
    """Capture prospective dispatch for a read-only preview with live membership.

    Preview does not persist a task or authorize publication. Its proposed task
    uses the same dispatch policy and binding checks as admission; preparation
    and commit still require the actual stored task's authority.
    """
    history = merge_history(service, context.main_head.target)
    if store.agent_task(context.merge_task_id) is not None:
        return merge_planning_context(context, history, created_at=created_at)
    episode_id = (
        None
        if context.member_merge
        else context.dispatcher_episode_id or context.metadata.episode_id
    )
    episode = store.episode(episode_id) if episode_id else None
    if episode_id is not None and (
        episode is None or episode.project_id != context.metadata.project_id
    ):
        raise ValueError("merge_dispatcher_episode_missing")
    binding = None
    if context.member_merge or context.dispatcher_episode_id is not None:
        binding = MergeBinding(
            source=context.metadata.head.target,
            source_head=context.metadata.head,
            destination=context.main_head.target,
            dispatcher_episode_id=context.dispatcher_episode_id,
            dispatcher_operation_id=episode.root_operation_id if episode else None,
            command_key="preview" if context.dispatcher_episode_id else None,
        )
    profile = service.resolve_agent_profile("orchestrator")
    request = BranchMergeRunRequest(
        episode_id=episode_id,
        merge_binding=binding,
        provider=profile.provider,
        model=profile.model,
        reasoning=profile.reasoning,
        run_on=profile.run_on,
        run_truth_scope=context.run_truth_scope,
        chat_scope="project",
        mode="work",
    )
    dispatch = resolved_dispatch_authority(
        store,
        resolve_dispatch_authority,
        "branch_merge",
        request,
        project_id=context.metadata.project_id,
        operation_id=context.merge_task_id,
    )
    if binding is not None:
        record = AgentTaskRecord(
            operation_id=context.merge_task_id,
            project_id=context.metadata.project_id,
            episode_id=episode_id,
            graph_target=context.metadata.head.target,
            kind="branch_merge",
            status="queued",
            status_message="Merge preview",
            request=request.model_dump(mode="json"),
            created_at=created_at.isoformat(),
            updated_at=created_at.isoformat(),
            authorized_by=context.authorized_by,
            dispatch_authority=dispatch,
        )
        with store.connection() as connection:
            store._require_merge_binding(connection, record)
    prospective = AgentTaskAuthority(
        operation_id=context.merge_task_id,
        project_id=context.metadata.project_id,
        apply_target=context.main_head.target,
        authorized_by=context.authorized_by,
        episode_id=episode_id,
        dispatch_authority=dispatch,
        merge_source=binding.source if binding else None,
        merge_dispatcher=("member" if context.member_merge else "orchestrator")
        if binding
        else None,
    )

    def resolve(project_id: str, operation_id: str) -> AgentTaskAuthority:
        if project_id != prospective.project_id or operation_id != prospective.operation_id:
            raise KeyError(operation_id)
        return prospective

    owner = history.parent if isinstance(history, BranchHistoryManager) else history
    authority = WriteAuthority(
        agent_authority_resolver=resolve,
        project_membership_check=owner.write_authority.project_membership_check,
    )
    return merge_planning_context(context, history, created_at=created_at, authority=authority)


def _prior_merge_reviews(
    engine: HistoryEngine,
    steps: list[StepRecord],
    destination: GraphState,
    *,
    source: GraphTargetRef,
    target: GraphTargetRef,
) -> tuple[PriorReview, ...]:
    """Recover review ownership from accepted canonical filings, never id prefixes.

    A merge also carries source Proposals unchanged. Their exact source snapshot
    distinguishes them from reviews that the merge itself filed. Subjects describe
    the filed effect, omitting any cancelled/no-op members of the original plan;
    neither task artifacts nor historical planning authority are needed to read it.
    """
    reviews = []
    for step in steps:
        provenance = step.patch.branch_merge
        if (
            not step.accepted
            or provenance is None
            or provenance.branch_head.target != source
            or provenance.rebased_main_head.target != target
            or step.after_head.target != target
        ):
            continue
        source_proposals = engine.state_at(provenance.branch_head).proposals
        for operation in step.patch.ops:
            if operation.op != "create_proposals":
                continue
            for filed in operation.proposals:
                live = destination.proposals.get(filed.id)
                if live is None or filed.id in source_proposals:
                    continue
                proposed = apply_valid_patch(
                    step.after_state,
                    Patch(
                        kind="approval",
                        author="human",
                        producer="human",
                        summary="Inspect canonical merge review",
                        revision=step.after_state.revision + 1,
                        created_at=step.patch.created_at,
                        ops=graph_operations_from_proposal(filed.ops),
                    ),
                )
                changes, _ = compare_graphs(step.after_state, proposed, step.after_state)
                subjects = sorted(
                    {(change.collection, change.entity_id) for change in changes},
                    key=lambda item: (item[0], item[1] or ""),
                )
                reviews.append(
                    PriorReview(
                        subject_key=merge_contract_digest(subjects),
                        proposal=live,
                        filing_graph=step.after_state,
                        provenance=provenance,
                        effect_digest=merge_contract_digest(
                            [op.model_dump(mode="json", exclude_unset=True) for op in filed.ops]
                        ),
                    )
                )
    return tuple(reviews)


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
    destination_steps = engine.steps(main_head)
    receipts = [
        *(
            receipt
            for receipt in branch.validated_merge_receipts()
            if receipt.result_main_head.target == destination
        ),
        *(
            branch_merge_receipt_from_committed_patch(step.patch)
            for step in destination_steps
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
        member_merge=merge_binding is not None and merge_binding.dispatcher_episode_id is None,
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
        prior_reviews=_prior_merge_reviews(
            engine, destination_steps, main_graph, source=source, target=destination
        ),
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
