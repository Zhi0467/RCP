"""Admission for one graph-only episode branch merge.

Branch-merge policy, not engine plumbing: the checks below are about what an
episode graph branch is, and none of them generalise to any other task
kind.  It takes the engine because launching needs the engine's launch gate.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

from rcp.core.models import AuthorizedHuman, BranchMergeReceipt
from rcp.core.transition_models import GraphTargetRef
from rcp.history import BranchMergeAlreadyCommitted, RevisionConflict
from rcp.runs.branch_merge import (
    MAX_BRANCH_MERGE_REBASE_ROUNDS,
    branch_merge_can_resolve_without_patch,
    branch_merge_id,
    branch_merge_provenance,
    branch_merge_receipt_from_committed_patch,
    build_deterministic_merge_ops,
    commit_branch_merge_with_history,
    parse_branch_merge_candidate,
    prepare_branch_merge_with_history,
)
from rcp.runs.branch_merge_context import load_branch_merge_context
from rcp.runs.branch_merge_request import BranchMergeRunRequest, MergeBinding
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
) -> AgentTaskRecord:
    """Dispatch one graph-only merge without reopening or spending the episode."""

    if not isinstance(request, BranchMergeRunRequest):
        raise TypeError("branch_merge requires a BranchMergeRunRequest")
    binding = request.merge_binding
    episode = tasks.store.episode(request.episode_id)
    if (
        episode is None
        or episode.project_id != project_id
        or (
            binding is None
            and episode.graph_target.kind == "branch"
            and tasks.store.branch_owner(project_id, episode.graph_target.branch_id or "")
            != episode.episode_id
        )
    ):
        raise ValueError("branch merge requires its exact owner episode branch")
    isolation = tasks.store.episode_isolation(project_id, episode.episode_id)
    if (
        binding is None
        and episode.graph_target.kind == "main"
        and (
            isolation is None or isolation.graph_branch_id is not None or isolation.worktree is None
        )
    ):
        # Only a code-only isolation owner merges without a graph branch.
        raise ValueError("branch merge requires its exact owner episode branch")
    # Branch facts only: a live graph-capable writer blocks the merge; the
    # episode's status, ending, and paused turns do not.
    source = binding.source if binding else episode.graph_target
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
        episode_id=episode.episode_id,
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
    stored = tasks.store.create_branch_merge_task(record)
    return tasks.launch_admitted(stored.operation_id) if launch else stored


def settle_agentless_merge(tasks, service, operation_id):
    """Deliver the usual post-commit observers without creating a provider turn."""
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution

    record = tasks.store.agent_task(operation_id)
    if record is None or record.status != "succeeded" or record.kind != "branch_merge":
        return
    request = BranchMergeRunRequest.model_validate(record.request)
    target = request.merge_binding.destination if request.merge_binding else GraphTargetRef()
    execution = AgentTaskExecution(
        operation_id=operation_id,
        store=tasks.store,
        control=AgentProcessControl(),
        applied_revision=record.applied_revision,
        applied_graph_state=service.history_engine.materialization(target).state,
    )
    tasks._task_settled(record, request, execution)


async def start_owned_branch_merge(
    tasks: BackgroundAgentTasks,
    service: ProjectService,
    *,
    episode: EpisodeRecord,
    dispatcher_operation_id: str,
    branch_id: str,
    command_key: str,
) -> dict[str, object]:
    """Admit one keyed fork delivery, reserving judgment only when it is needed."""
    operation_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:merge:{dispatcher_operation_id}:{command_key}")
    )
    stored = tasks.store.agent_task(operation_id)
    history = getattr(service.history, "parent", service.history)
    if stored is None:
        source = GraphTargetRef(kind="branch", branch_id=branch_id)
        branch = history.branch(branch_id, expected_project_id=episode.project_id, initialize=False)
        metadata = branch.branch_metadata()
        if episode.authorized_by is None:
            raise ValueError("merge_dispatcher_authorizer_missing")
        binding = MergeBinding(
            source=source,
            destination=metadata.effective_parent_head.target,
            source_head=metadata.head,
            dispatcher_episode_id=episode.episode_id,
            dispatcher_operation_id=dispatcher_operation_id,
            command_key=command_key,
        )
        context = load_branch_merge_context(
            service,
            tasks.store,
            episode,
            operation_id=operation_id,
            authorized_by=episode.authorized_by,
            merge_binding=binding,
        )
        if context.previous_merge_receipt is not None and (
            context.previous_merge_receipt.provenance.branch_head == binding.source_head
        ):
            raise ValueError("no_undelivered_work")
        binding = binding.model_copy(
            update={"requires_agent": bool(build_deterministic_merge_ops(context)[1])}
        )
        profile = service.resolve_agent_profile("orchestrator")
        request = BranchMergeRunRequest(
            episode_id=episode.episode_id,
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
            episode.project_id,
            request,
            authorized_by=episode.authorized_by,
            operation_id=operation_id,
            launch=False,
        )
    request = BranchMergeRunRequest.model_validate(stored.request)
    binding = request.merge_binding
    if binding is None or binding.source.branch_id != branch_id:
        raise ValueError("merge_command_key_conflict")
    if stored.status != "succeeded":
        source = history.branch(branch_id, expected_project_id=episode.project_id, initialize=False)
        pinned = source.branch_metadata().model_copy(update={"head": binding.source_head})
        receipt = source.reconcile_merge_receipt(branch_merge_id(pinned))
        if receipt is not None and receipt.provenance.merge_task_id == operation_id:
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
                _commit_owned_agentless_merge(tasks, service, episode, stored, binding)
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
        "branch_id": branch_id,
        "destination": binding.destination.model_dump(mode="json"),
        "agentless": not binding.requires_agent,
        "status": stored.status,
    }


def _commit_owned_agentless_merge(tasks, service, episode, task, binding):
    history = getattr(service.history, "parent", service.history)
    branch = history.branch(binding.source.branch_id, expected_project_id=episode.project_id)
    destination = history.branch(
        binding.destination.branch_id, expected_project_id=episode.project_id
    )
    receipt = None
    for attempt in range(MAX_BRANCH_MERGE_REBASE_ROUNDS):
        tasks.store.agent_task_authority(episode.project_id, task.operation_id)
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
        ops, residue = build_deterministic_merge_ops(context)
        if residue:
            raise ValueError("merge_destination_advanced_requires_agent")
        try:
            if branch_merge_can_resolve_without_patch(context):
                receipt = BranchMergeReceipt(
                    outcome="no_change",
                    provenance=branch_merge_provenance(context),
                    result_main_head=context.main_head,
                    authorized_by=task.authorized_by,
                )
            else:
                candidate = parse_branch_merge_candidate(
                    json.dumps({"summary": "Merge fork changes.", "ops": []}),
                    context,
                    deterministic_ops=ops,
                )
                prepare_branch_merge_with_history(
                    destination,
                    candidate,
                    expected_main_head=context.main_head,
                    context=context,
                )
                committed = commit_branch_merge_with_history(
                    destination,
                    candidate,
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
