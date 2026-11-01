"""Admission for one graph-only episode branch merge.

Branch-merge policy, not engine plumbing: the checks below are about what an
episode graph branch is, and none of them generalise to any other task
kind.  It takes the engine because launching needs the engine's launch gate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from rcp.core.models import AuthorizedHuman, BranchMergeReceipt
from rcp.core.transition_models import GraphTargetRef
from rcp.history import BranchMergeAlreadyCommitted, RevisionConflict
from rcp.history.branches import BranchHistoryManager
from rcp.runs.branch_merge import (
    MAX_BRANCH_MERGE_REBASE_ROUNDS,
    branch_merge_id,
    branch_merge_provenance,
    branch_merge_receipt_from_committed_patch,
    commit_branch_merge_with_history,
    merge_planning_context,
)
from rcp.runs.branch_merge_context import load_branch_merge_context, merge_history
from rcp.runs.branch_merge_request import BranchMergeRunRequest, MergeBinding
from rcp.runs.merge_contract import MergeAnswers
from rcp.runs.merge_patch import build_merge_patch, prepare_merge_candidate
from rcp.runs.merge_plan import plan_merge
from rcp.runs.task_policy import resolved_dispatch_authority, task_graph_capable
from rcp.service import ProjectService
from rcp.storage import AgentTaskRecord, EpisodeRecord

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


def start_branch_merge(
    tasks: BackgroundAgentTasks,
    project_id: str,
    request: BranchMergeRunRequest,
    *,
    authorized_by: AuthorizedHuman,
    operation_id: str | None = None,
    launch: bool = True,
    merge_effects: dict[str, object] | None = None,
) -> AgentTaskRecord:
    """Dispatch one graph-only merge without reopening or spending the episode."""

    if not isinstance(request, BranchMergeRunRequest):
        raise TypeError("branch_merge requires a BranchMergeRunRequest")
    binding = request.merge_binding
    episode = tasks.store.episode(request.episode_id) if request.episode_id else None
    if binding is None:
        if episode is None or episode.project_id != project_id:
            raise ValueError("branch merge requires its exact owner episode branch")
        if (
            episode.graph_target.kind == "branch"
            and tasks.store.branch_owner(project_id, episode.graph_target.branch_id or "")
            != episode.episode_id
        ):
            raise ValueError("branch merge requires its exact owner episode branch")
        isolation = tasks.store.episode_isolation(project_id, episode.episode_id)
        if episode.graph_target.kind == "main" and (
            isolation is None or isolation.graph_branch_id is not None or isolation.worktree is None
        ):
            raise ValueError("branch merge requires its exact owner episode branch")
        source = episode.graph_target
    else:
        source = binding.source
        if request.episode_id is not None and (episode is None or episode.project_id != project_id):
            raise ValueError("merge_dispatcher_episode_missing")
    active_branch_writers = [
        item
        for item in (
            []
            if source.kind == "main"
            else tasks.store.unsettled_graph_target_tasks(
                project_id,
                source,
            )
        )
        if item.kind != "branch_merge"
        and item.status in {"queued", "running", "pausing"}
        and task_graph_capable(item.kind, item.request)
    ]
    if active_branch_writers:
        writers = ", ".join(
            f"{item.kind} {item.operation_id} ({item.status})" for item in active_branch_writers
        )
        raise ValueError(f"the graph branch still has an active writer: {writers}")
    if not authorized_by.display_name.strip():
        raise ValueError("branch merge requires a named human authorizer snapshot")

    operation_id = operation_id or str(uuid.uuid4())
    authority = resolved_dispatch_authority(
        tasks.store,
        tasks.dispatch_authority_resolver,
        "branch_merge",
        request,
        project_id=project_id,
        operation_id=operation_id,
    )
    assert authority is not None
    request_data = request.model_dump(mode="json")
    estimate, samples = tasks.store.agent_task_estimate(
        project_id,
        "branch_merge",
        request_data,
    )
    now = tasks.store.now()
    record = AgentTaskRecord(
        operation_id=operation_id,
        project_id=project_id,
        episode_id=request.episode_id,
        graph_target=source,
        kind="branch_merge",
        status="queued",
        request=request_data,
        created_at=now,
        updated_at=now,
        status_message="Waiting for the graph branch merge agent to start.",
        estimate_seconds=estimate,
        estimate_samples=samples,
        phase="queued",
        last_activity_at=now,
        authorized_by=authorized_by,
        dispatch_authority=authority,
    )
    stored = tasks.store.create_branch_merge_task(record, merge_effects=merge_effects)
    return tasks.launch_admitted(stored.operation_id) if launch else stored


def settle_agentless_merge(tasks, service, operation_id):
    """Deliver the usual post-commit observers without creating a provider turn."""
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution

    record = tasks.store.agent_task(operation_id)
    if record is None or record.status != "succeeded" or record.kind != "branch_merge":
        return
    request = BranchMergeRunRequest.model_validate(record.request)
    from rcp.runs.merge_effects import settle_merge_effects

    settle_merge_effects(tasks, service, operation_id)
    target = request.merge_binding.destination if request.merge_binding else GraphTargetRef()
    execution = AgentTaskExecution(
        operation_id=operation_id,
        store=tasks.store,
        control=AgentProcessControl(),
        applied_revision=record.applied_revision,
        applied_graph_state=service.history_engine.materialization(target).state,
    )
    tasks._task_settled(record, request, execution)


def start_owned_branch_merge(
    tasks: BackgroundAgentTasks,
    service: ProjectService,
    *,
    episode: EpisodeRecord,
    dispatcher_operation_id: str,
    branch_id: str,
    command_key: str,
) -> dict[str, object]:
    """Admit one keyed owned fork delivery on its lineage allocation."""
    if episode.authorized_by is None:
        raise ValueError("merge_dispatcher_authorizer_missing")
    operation_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:merge:{dispatcher_operation_id}:{command_key}")
    )
    return _start_bound_branch_merge(
        tasks,
        service,
        branch_id=branch_id,
        operation_id=operation_id,
        authorized_by=episode.authorized_by,
        episode=episode,
        dispatcher_operation_id=dispatcher_operation_id,
        command_key=command_key,
    )


def start_human_branch_merge(
    tasks: BackgroundAgentTasks,
    service: ProjectService,
    *,
    branch_id: str,
    request_id: str,
    authorized_by: AuthorizedHuman,
) -> dict[str, object]:
    """Admit a member's fork merge without borrowing an episode or budget."""
    project_id = merge_history(service).project_id
    operation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:member-merge:{project_id}:{authorized_by.user_id}:{request_id}",
        )
    )
    return _start_bound_branch_merge(
        tasks,
        service,
        branch_id=branch_id,
        operation_id=operation_id,
        authorized_by=authorized_by,
        episode=None,
    )


def _start_bound_branch_merge(
    tasks,
    service,
    *,
    branch_id,
    operation_id,
    authorized_by,
    episode,
    dispatcher_operation_id=None,
    command_key=None,
):
    stored = tasks.store.agent_task(operation_id)
    if stored is None:
        source = GraphTargetRef(kind="branch", branch_id=branch_id)
        branch = merge_history(service, source)
        assert isinstance(branch, BranchHistoryManager)
        metadata = branch.branch_metadata()
        binding = MergeBinding(
            source=source,
            destination=metadata.effective_parent_head.target,
            source_head=metadata.head,
            dispatcher_episode_id=episode.episode_id if episode else None,
            dispatcher_operation_id=dispatcher_operation_id,
            command_key=command_key,
            requires_agent=False,
        )
        context = load_branch_merge_context(
            service,
            tasks.store,
            episode,
            operation_id=operation_id,
            authorized_by=authorized_by,
            merge_binding=binding,
        )
        if (
            context.previous_merge_receipt is not None
            and context.previous_merge_receipt.provenance.branch_head == binding.source_head
        ):
            raise ValueError("no_undelivered_work")
        profile = service.resolve_agent_profile("orchestrator")
        request = BranchMergeRunRequest(
            episode_id=episode.episode_id if episode else None,
            merge_binding=binding,
            provider=profile.provider,
            model=profile.model,
            reasoning=profile.reasoning,
            run_on=profile.run_on,
            run_truth_scope=context.run_truth_scope,
            chat_scope="project",
            mode="work",
            trigger="human",
            patch_kind="work",
        )
        stored = start_branch_merge(
            tasks,
            metadata.project_id,
            request,
            authorized_by=authorized_by,
            operation_id=operation_id,
            launch=False,
        )
    binding = BranchMergeRunRequest.model_validate(stored.request).merge_binding
    if (
        binding is None
        or stored.project_id != merge_history(service).project_id
        or binding.source.branch_id != branch_id
        or stored.authorized_by != authorized_by
    ):
        raise ValueError("merge_command_key_conflict")
    return recover_branch_merge_task(tasks, service, stored)


def recover_branch_merge_task(tasks, service, stored):
    """Prove delivery first, otherwise resume the same bound task and allocation."""
    request = BranchMergeRunRequest.model_validate(stored.request)
    binding = request.merge_binding
    if binding is None:
        raise ValueError("merge_source_binding_missing")
    operation_id = stored.operation_id
    episode = tasks.store.episode(request.episode_id) if request.episode_id else None
    if stored.status != "succeeded":
        source = merge_history(service, binding.source)
        assert isinstance(source, BranchHistoryManager)
        pinned = source.branch_metadata().model_copy(update={"head": binding.source_head})
        receipt = source.reconcile_merge_receipt(branch_merge_id(pinned))
        if (
            receipt is not None
            and receipt.provenance.merge_task_id == operation_id
            and (stored.status != "failed" or receipt.outcome == "committed")
        ):
            tasks.store.reconcile_agentless_merge_task(operation_id, receipt)
            settle_agentless_merge(tasks, service, operation_id)
            stored = tasks.store.agent_task(operation_id) or stored
        elif stored.status == "interrupted":
            stored = tasks.store.requeue_owned_merge_task(operation_id)
    if stored.status == "queued":
        if binding.requires_agent:
            stored = tasks.launch_admitted(operation_id)
        else:
            try:
                _commit_agentless_merge(tasks, service, episode, stored, binding)
            except ValueError as exc:
                if str(exc) != "merge_destination_advanced_requires_agent":
                    tasks.store.fail_agent_task(operation_id, str(exc))
                    raise
                try:
                    stored = tasks.store.reserve_branch_merge_invocation(operation_id)
                except ValueError as admission_error:
                    tasks.store.fail_agent_task(operation_id, str(admission_error))
                    raise
                binding = BranchMergeRunRequest.model_validate(stored.request).merge_binding
                assert binding is not None
                stored = tasks.launch_admitted(operation_id)
            else:
                settle_agentless_merge(tasks, service, operation_id)
                stored = tasks.store.agent_task(operation_id) or stored
    return {
        "operation_id": stored.operation_id,
        "branch_id": binding.source.branch_id,
        "destination": binding.destination.model_dump(mode="json"),
        "agentless": not binding.requires_agent,
        "status": stored.status,
    }


def _commit_agentless_merge(tasks, service, episode, task, binding):
    branch = merge_history(service, binding.source)
    assert isinstance(branch, BranchHistoryManager)
    destination = merge_history(service, binding.destination)
    receipt = None
    for attempt in range(MAX_BRANCH_MERGE_REBASE_ROUNDS):
        tasks.store.agent_task_authority(task.project_id, task.operation_id)
        existing = branch.reconcile_merge_receipt(branch_merge_id(branch.branch_metadata()))
        if existing is not None:
            receipt = existing
            break
        context = load_branch_merge_context(
            service,
            tasks.store,
            episode,
            operation_id=task.operation_id,
            authorized_by=task.authorized_by,
            merge_binding=binding,
        )
        plan = plan_merge(
            merge_planning_context(
                context, destination, created_at=datetime.fromisoformat(task.created_at)
            )
        )
        if plan.conflicts or plan.review_groups:
            raise ValueError("merge_destination_advanced_requires_agent")
        try:
            prepared = prepare_merge_candidate(
                plan, MergeAnswers(answers={}, cards={}), destination
            )
            if not prepared.patch.ops:
                receipt = BranchMergeReceipt(
                    outcome="no_change",
                    provenance=branch_merge_provenance(context),
                    result_main_head=context.main_head,
                    authorized_by=task.authorized_by,
                )
            else:
                committed = commit_branch_merge_with_history(
                    destination,
                    build_merge_patch(plan, MergeAnswers(answers={}, cards={})),
                    expected_main_head=context.main_head,
                )
                receipt = branch_merge_receipt_from_committed_patch(committed.patch)
            branch.write_merge_receipt(receipt)
            break
        except BranchMergeAlreadyCommitted as exc:
            receipt = branch_merge_receipt_from_committed_patch(exc.patch)
            branch.write_merge_receipt(receipt)
            break
        except RevisionConflict:
            if attempt + 1 == MAX_BRANCH_MERGE_REBASE_ROUNDS:
                raise
    assert receipt is not None
    tasks.store.record_agent_task_receipt(
        task.operation_id, "branch_merge_outcome", receipt.model_dump(mode="json")
    )
    tasks.store.reconcile_agentless_merge_task(task.operation_id, receipt)
