"""Repeatable post-commit effects, proved by the canonical merge receipt.

Task success does not imply these observers finished. Protected summary receipts
keep their independent progress through pruning, restart, and callback failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rcp.agents import AgentProcessControl
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history.branches import BranchHistoryManager
from rcp.history.queries import HeadSpec
from rcp.keyed_locks import KeyedLocks
from rcp.runs.branch_merge import branch_merge_id
from rcp.runs.branch_merge_context import merge_history
from rcp.runs.branch_merge_request import BranchMergeRunRequest

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks
    from rcp.service import ProjectService

_LOCKS = KeyedLocks()


def settle_merge_effects(
    tasks: BackgroundAgentTasks, service: ProjectService, operation_id: str
) -> bool:
    """Carry warnings and publish current display without replaying graph delivery."""
    from rcp.background import AgentTaskExecution

    store = tasks.store
    with _LOCKS(f"{store.path.resolve()}:{operation_id}"):
        if store.agent_task_has_receipt(operation_id, "merge_effects_complete"):
            return True
        required = store.agent_task_receipts_by_category(operation_id, "merge_effects_required")
        task = store.agent_task(operation_id)
        if not required or task is None or task.kind != "branch_merge":
            return False
        effect = "canonical_receipt"
        try:
            specification = required[0].payload
            source = GraphTargetRef.model_validate(specification["source"])
            destination = GraphTargetRef.model_validate(specification["destination"])
            branch = merge_history(service, source)
            assert isinstance(branch, BranchHistoryManager)
            pinned = specification.get("source_head")
            if pinned is not None:
                metadata = branch.branch_metadata().model_copy(
                    update={"head": GraphHeadRef.model_validate(pinned)}
                )
                receipt = branch.reconcile_merge_receipt(branch_merge_id(metadata))
            else:
                # Compatibility for old in-process callers which supplied only
                # the episode; a verified receipt, never task prose, pins the head.
                receipt = next(
                    (
                        item
                        for item in branch.validated_merge_receipts()
                        if item.provenance.merge_task_id == operation_id
                    ),
                    None,
                )
            if receipt is None or receipt.provenance.merge_task_id != operation_id:
                if task.status in {"failed", "succeeded"}:
                    return _complete_terminal_non_delivery(
                        tasks, service, branch, destination, operation_id
                    )
                return False
            if (
                receipt.result_main_head.target != destination
                or task.authorized_by != receipt.authorized_by
            ):
                raise ValueError("merge_effects_receipt_mismatch")
            superseded = specification.get("superseded") or (
                specification.get("legacy")
                and receipt
                != max(
                    branch.validated_merge_receipts(),
                    key=lambda item: (item.result_main_head.revision, item.created_at),
                )
            )
            if superseded:
                store.record_agent_task_receipt(
                    operation_id,
                    "merge_effects_complete",
                    {"skipped": "superseded_legacy_delivery"},
                )
                return True
            engine = service.history_engine
            source_state = engine.state_at(receipt.provenance.branch_head)
            destination_state = merge_history(service, destination).state()
            effect = "warning_carry"
            if not store.agent_task_has_receipt(operation_id, "merge_warning_carry_complete"):
                store.carry_merge_evidence_flags(
                    task.project_id,
                    source=source,
                    destination=destination,
                    source_state=source_state,
                    destination_state=destination_state,
                    recorded_state=lambda revision: engine.state_at(
                        engine.resolve(
                            HeadSpec(target=source, revision=revision), default_target=source
                        )
                    ),
                    created_before=receipt.created_at.isoformat()
                    if specification.get("legacy")
                    else None,
                )
                store.record_agent_task_receipt(operation_id, "merge_warning_carry_complete", {})
            effect = "display_publication"
            if not store.agent_task_has_receipt(operation_id, "merge_display_publication_complete"):
                if tasks.on_stream_closed is None:
                    raise ValueError("merge_display_publisher_unavailable")
                execution = AgentTaskExecution(
                    operation_id=operation_id,
                    store=store,
                    control=AgentProcessControl(),
                    applied_revision=task.applied_revision,
                    applied_graph_state=destination_state,
                )
                published = tasks.on_stream_closed(
                    task.project_id,
                    task.kind,
                    BranchMergeRunRequest.model_validate(task.request),
                    execution,
                )
                if published is False:
                    raise ValueError("merge_display_publication_failed")
                store.record_agent_task_receipt(
                    operation_id, "merge_display_publication_complete", {}
                )
            store.record_agent_task_receipt(operation_id, "merge_effects_complete", {})
            return True
        except Exception as exc:
            store.record_agent_task_receipt(
                operation_id,
                "merge_effects_failed",
                {
                    "code": f"merge_{effect}_failed",
                    "exception_type": type(exc).__name__,
                    "detail": str(exc),
                },
                tier="diagnostic",
            )
            return False


def _complete_terminal_non_delivery(tasks, service, branch, destination, operation_id):
    """A terminal verdict alone cannot prove an uncertain graph write was absent."""
    with branch.workspace.transaction():
        branch.require_writable()
        history = merge_history(service, destination)
        materialization = history.current_materialization()
        history.require_writable(materialization.state)
        if any(
            patch.admission == "accepted"
            and patch.branch_merge is not None
            and patch.branch_merge.merge_task_id == operation_id
            for patch in materialization.patches
        ) or any(
            receipt.provenance.merge_task_id == operation_id
            for receipt in branch.validated_merge_receipts()
        ):
            return False
        return tasks.store.complete_terminal_merge_effects_without_delivery(operation_id)


def reconcile_merge_effects(tasks: BackgroundAgentTasks, service: ProjectService) -> int:
    """Reconcile durable effects and one finite compatibility pass for legacy merges."""
    store = tasks.store
    project_id = merge_history(service).project_id
    assert project_id is not None
    branches = {}
    for task in store.legacy_merge_effect_tasks(project_id):
        if task.graph_target.key not in branches:
            branch = merge_history(service, task.graph_target)
            assert isinstance(branch, BranchHistoryManager)
            branches[task.graph_target.key] = branch.validated_merge_receipts()
        receipts = branches[task.graph_target.key]
        receipt = next(
            (item for item in receipts if item.provenance.merge_task_id == task.operation_id), None
        )
        if receipt is None:
            with _LOCKS(f"{store.path.resolve()}:{task.operation_id}"):
                if store.agent_task_has_receipt(task.operation_id, "merge_effects_required"):
                    continue
                source = merge_history(service, task.graph_target)
                assert isinstance(source, BranchHistoryManager)
                store.record_agent_task_receipt(
                    task.operation_id,
                    "merge_effects_required",
                    {
                        "source": task.graph_target.model_dump(mode="json"),
                        "source_head": None,
                        "destination": source.branch_metadata().effective_parent_head.target.model_dump(
                            mode="json"
                        ),
                        "legacy": True,
                    },
                )
            continue
        latest = max(
            (
                item
                for item in receipts
                if item.result_main_head.target == receipt.result_main_head.target
            ),
            key=lambda item: (item.result_main_head.revision, item.created_at),
        )
        with _LOCKS(f"{store.path.resolve()}:{task.operation_id}"):
            if store.agent_task_has_receipt(task.operation_id, "merge_effects_required"):
                continue
            store.record_agent_task_receipt(
                task.operation_id,
                "merge_effects_required",
                {
                    "source": receipt.provenance.branch_head.target.model_dump(mode="json"),
                    "source_head": receipt.provenance.branch_head.model_dump(mode="json"),
                    "destination": receipt.result_main_head.target.model_dump(mode="json"),
                    "legacy": True,
                    "superseded": receipt != latest,
                },
            )
            if receipt != latest:
                store.record_agent_task_receipt(
                    task.operation_id,
                    "merge_effects_complete",
                    {"skipped": "superseded_legacy_delivery"},
                )
    return sum(
        settle_merge_effects(tasks, service, task.operation_id)
        for task in store.pending_merge_effect_tasks(project_id)
    )
