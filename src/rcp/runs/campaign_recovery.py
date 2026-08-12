from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol

from rcp.runs.campaign import CampaignRunRequest
from rcp.storage import CampaignRecord, CampaignRecoveryRecord

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks
    from rcp.storage import AppStore


class CampaignSettlement(Protocol):
    operation_id: str
    store: AppStore


CampaignRecoveryFailure = Literal[
    "provider",
    "network",
    "rate_limit",
    "session_limit",
    "missing_checkpoint",
    "continuation_unavailable",
    "structural_unrecoverable",
    "report_admission",
]


class CampaignOrchestratorTerminalFailure(RuntimeError):
    """Explicit internal boundary for a structurally unrecoverable orchestrator failure.

    Provider diagnostics and arbitrary exception prose must never construct this verdict.
    Deterministic fixtures may raise it directly after establishing the orchestrator's
    reportable native-session binding.
    """


def record_structural_failure(
    background: BackgroundAgentTasks,
    *,
    operation_id: str,
    diagnostic: str,
) -> None:
    background.store.record_agent_task_receipt(
        operation_id,
        "campaign_orchestrator_failure",
        {
            "classification": "structural_unrecoverable",
            "recoverable": False,
            "diagnostic": " ".join(diagnostic.split())[:2000],
        },
        tier="summary",
    )


def reconcile_campaign_task_settlement(
    background: BackgroundAgentTasks,
    campaign: CampaignRecord,
    request: CampaignRunRequest,
    execution: CampaignSettlement,
) -> CampaignRecord:
    """Schedule default recovery or atomically fence one typed terminal failure."""

    store = background.store
    task = store.agent_task(execution.operation_id)
    current = store.campaign(campaign.campaign_id)
    if task is None or current is None or task.status not in {"failed", "interrupted"}:
        return current or campaign
    if request.role == "worker":
        return current

    structural = any(
        receipt.category == "campaign_orchestrator_failure"
        and receipt.payload.get("classification") == "structural_unrecoverable"
        and receipt.payload.get("recoverable") is False
        for receipt in store.agent_task_receipts(task.operation_id)
    )
    if structural and request.role == "orchestrator":
        fenced = store.fence_campaign_terminal_failure(
            task.operation_id,
            diagnostic=task.error or "The campaign orchestrator failed structurally.",
        )
        if fenced is not None:
            return fenced
        store.schedule_campaign_task_recovery(
            task.operation_id,
            failure_kind="structural_unrecoverable",
            retry_mode="blocked",
            diagnostic=(
                task.error
                or "The structural failure has no exact reportable orchestrator session and stage."
            ),
        )
        return current

    failure_kind, retry_mode = _recoverable_failure(store, task.operation_id, request)
    store.schedule_campaign_task_recovery(
        task.operation_id,
        failure_kind=failure_kind,
        retry_mode=retry_mode,
        diagnostic=task.error or "The campaign actor turn failed.",
    )
    return current


def schedule_report_reconciliation(
    background: BackgroundAgentTasks,
    campaign: CampaignRecord,
    *,
    diagnostic: str,
) -> CampaignRecoveryRecord:
    if campaign.ending is None:
        raise ValueError("campaign report reconciliation requires a durable ending")
    return background.store.schedule_campaign_report_reconciliation(
        campaign.campaign_id,
        ending=campaign.ending,
        diagnostic=diagnostic,
    )


def reconcile_due_campaign_recoveries(
    background: BackgroundAgentTasks,
    *,
    reconcile_report: Callable[[CampaignRecord], bool],
    as_of: str | None = None,
) -> int:
    """Attempt every due durable recovery once; callers provide the process heartbeat."""

    store = background.store
    reconciled = 0
    for recovery in store.due_campaign_recoveries(as_of=as_of):
        if recovery.status != "pending":
            continue
        try:
            if recovery.purpose == "task":
                if recovery.operation_id is None:
                    raise ValueError("campaign task recovery lost its operation id")
                child = store.campaign_task_recovery_child(recovery.operation_id)
                if child is None:
                    child = background.retry(recovery.operation_id)
                store.complete_campaign_recovery(
                    recovery.recovery_id,
                    admitted_operation_id=child.operation_id,
                    expected_operation_id=recovery.operation_id,
                )
            else:
                campaign = store.campaign(recovery.campaign_id)
                if campaign is None:
                    raise KeyError(recovery.campaign_id)
                if not reconcile_report(campaign):
                    raise RuntimeError("campaign report admission remains unavailable")
                store.complete_campaign_recovery(recovery.recovery_id)
        except Exception as exc:
            child = (
                store.campaign_task_recovery_child(recovery.operation_id)
                if recovery.purpose == "task" and recovery.operation_id is not None
                else None
            )
            task = (
                store.agent_task(recovery.operation_id)
                if recovery.purpose == "task" and recovery.operation_id is not None
                else None
            )
            if child is not None:
                store.complete_campaign_recovery(
                    recovery.recovery_id,
                    admitted_operation_id=child.operation_id,
                    expected_operation_id=recovery.operation_id,
                )
            elif (
                task is not None
                and task.status in {"paused", "interrupted", "failed"}
                and not task.can_retry
            ):
                store.complete_campaign_recovery(
                    recovery.recovery_id,
                    expected_operation_id=recovery.operation_id,
                )
                campaign = store.campaign(recovery.campaign_id)
                if (
                    campaign is not None
                    and campaign.status == "wrapping_up"
                    and campaign.ending == "stopped"
                ):
                    reconcile_report(campaign)
            else:
                store.defer_campaign_recovery(recovery.recovery_id, diagnostic=str(exc))
        reconciled += 1
    return reconciled


def reconcile_orphaned_campaign_failures(background: BackgroundAgentTasks) -> int:
    """Rebuild recovery decisions for failures/interruption persisted before restart."""

    reconciled = 0
    for task in background.store.campaign_recovery_candidates():
        request = CampaignRunRequest.model_validate(task.request)
        campaign = background.store.campaign(request.campaign_id)
        if campaign is None:
            continue
        execution = _StoredSettlement(task.operation_id, background.store)
        reconcile_campaign_task_settlement(
            background,
            campaign,
            request,
            execution,
        )
        reconciled += 1
    return reconciled


class _StoredSettlement:
    def __init__(self, operation_id: str, store) -> None:
        self.operation_id = operation_id
        self.store = store


def _recoverable_failure(store, operation_id: str, request: CampaignRunRequest):
    receipts = store.agent_task_receipts(operation_id)
    if any(
        receipt.category == "continuation_context_unavailable"
        and receipt.payload.get("retry_required") is True
        for receipt in receipts
    ):
        return "continuation_unavailable", "clean" if request.role == "orchestrator" else "exact"
    terminal = next(
        (
            receipt.payload.get("classification")
            for receipt in reversed(receipts)
            if receipt.category == "provider_terminal_error"
        ),
        None,
    )
    if terminal == "session_limit":
        return "session_limit", "clean" if request.role == "orchestrator" else "exact"
    task = store.agent_task(operation_id)
    if task is None or not task.native_session_id or not task.stage_root:
        return "missing_checkpoint", "clean" if request.role == "orchestrator" else "exact"
    return "provider", "exact"
