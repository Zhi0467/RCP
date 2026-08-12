from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.core.models import AuthorizedHuman
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    CampaignBudgetMeter,
    CampaignEnding,
    CampaignRecord,
    CampaignRecoveryMode,
    CampaignRecoveryPurpose,
    CampaignRecoveryStatus,
    CampaignStatus,
)

_CAMPAIGN_TEXT_MAX_LENGTH = 16_000
_STOPPABLE_CAMPAIGN_STATUSES: frozenset[CampaignStatus] = frozenset({"queued", "running"})


class StartCampaignBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    invocation_ceiling: int = Field(ge=2)
    starting_instruction: str | None = Field(
        default=None,
        max_length=_CAMPAIGN_TEXT_MAX_LENGTH,
    )

    @field_validator("starting_instruction", mode="before")
    @classmethod
    def trim_starting_instruction(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class ReauthorizeCampaignBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    additional_invocations: int = Field(ge=2)


class CampaignMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    body: str = Field(min_length=1, max_length=_CAMPAIGN_TEXT_MAX_LENGTH)

    @field_validator("body", mode="before")
    @classmethod
    def trim_nonblank_body(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("campaign message body must not be blank")
            return stripped
        return value


class CampaignReportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    report_id: str
    ending: CampaignEnding
    created_at: str


class CampaignRecoverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    purpose: CampaignRecoveryPurpose
    status: CampaignRecoveryStatus
    retry_mode: CampaignRecoveryMode
    operation_id: str | None
    attempts: int
    max_attempts: int
    next_attempt_at: str | None


class CampaignResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    campaign_id: str
    project_id: str
    root_operation_id: str | None
    status: CampaignStatus
    starting_instruction: str | None
    budget: CampaignBudgetMeter
    authorized_by: AuthorizedHuman
    stop_requested_at: str | None
    ending: CampaignEnding | None
    error: str | None
    created_at: str
    updated_at: str
    ended_at: str | None
    tasks: list[AgentTaskRecord]
    current_orchestrator_task_id: str | None
    current_control_task_id: str | None
    recovery: CampaignRecoverySummary | None
    reports: list[CampaignReportSummary]
    can_stop: bool
    can_reauthorize: bool


def campaign_for_project(
    store: AppStore,
    project_id: str,
    campaign_id: str,
) -> CampaignRecord:
    """Load one campaign without allowing a cross-project identifier lookup."""

    campaign = store.campaign(campaign_id)
    if campaign is None or campaign.project_id != project_id:
        raise KeyError(campaign_id)
    return campaign


def serialize_campaign(
    store: AppStore,
    project_id: str,
    campaign: CampaignRecord,
) -> CampaignResponse:
    """Serialize one project-owned campaign from its live meter and durable reports."""

    if campaign.project_id != project_id:
        raise KeyError(campaign.campaign_id)
    reports = sorted(
        store.campaign_reports(campaign.campaign_id),
        key=lambda report: (report.created_at, report.report_id),
    )
    tasks = store.campaign_tasks(campaign.campaign_id)
    current_orchestrator_task_id: str | None = None
    if campaign.root_operation_id is not None:
        try:
            binding = store.campaign_actor_binding(campaign.root_operation_id)
        except (KeyError, ValueError):
            # A legacy or partially migrated campaign remains inspectable even
            # when it cannot offer a safe recovery action. New campaigns always
            # have this binding; recovery itself still fails closed without it.
            binding = None
        if binding is not None:
            current_orchestrator_task_id = binding.current_operation_id
    current_control_task_id = current_orchestrator_task_id
    if campaign.status in {"stopping", "wrapping_up"}:
        completed_report_ids = {report.operation_id for report in reports}
        latest_completed_report_index = max(
            (
                index
                for index, task in enumerate(tasks)
                if task.operation_id in completed_report_ids
            ),
            default=-1,
        )
        report_tasks = [
            task
            for task in tasks[latest_completed_report_index + 1 :]
            if task.request.get("role") == "report"
        ]
        if report_tasks:
            current_control_task_id = report_tasks[-1].operation_id
        else:
            tasks_by_id = {task.operation_id: task for task in tasks}
            recovered_parent_ids = {
                task.parent_operation_id
                for task in tasks
                if task.parent_operation_id is not None
                and (parent := tasks_by_id.get(task.parent_operation_id)) is not None
                and task.attempt == parent.attempt + 1
                and (task.request.get("actor_operation_id") or task.operation_id)
                == (parent.request.get("actor_operation_id") or parent.operation_id)
            }
            current_orchestrator = tasks_by_id.get(current_orchestrator_task_id or "")
            current_orchestrator_recovery = (
                store.campaign_control_recovery(
                    campaign.campaign_id,
                    current_orchestrator.operation_id,
                )
                if current_orchestrator is not None
                and current_orchestrator.status in {"failed", "interrupted"}
                else None
            )
            if (
                current_orchestrator is not None
                and current_orchestrator.operation_id not in recovered_parent_ids
                and (
                    (
                        current_orchestrator.status == "paused"
                        and (current_orchestrator.can_resume or current_orchestrator.can_retry)
                    )
                    or (
                        current_orchestrator.status in {"failed", "interrupted"}
                        and current_orchestrator_recovery is not None
                        and current_orchestrator_recovery.operation_id
                        == current_orchestrator.operation_id
                        and current_orchestrator_recovery.status != "admitted"
                    )
                )
                and store.campaign_invocation_role(current_orchestrator.operation_id)
                == "orchestrator"
            ):
                current_control_task_id = current_orchestrator.operation_id
            else:
                paused_workers = [
                    task
                    for task in tasks
                    if task.status == "paused"
                    and task.operation_id not in recovered_parent_ids
                    and (task.can_resume or task.can_retry)
                    and store.campaign_invocation_role(task.operation_id) == "worker"
                ]
                current_control_task_id = (
                    max(
                        paused_workers, key=lambda task: (task.created_at, task.operation_id)
                    ).operation_id
                    if paused_workers
                    else None
                )
    control_recovery = store.campaign_control_recovery(
        campaign.campaign_id,
        current_control_task_id,
        ending=campaign.ending,
    )
    return CampaignResponse(
        campaign_id=campaign.campaign_id,
        project_id=campaign.project_id,
        root_operation_id=campaign.root_operation_id,
        status=campaign.status,
        starting_instruction=campaign.starting_instruction,
        budget=store.campaign_budget_meter(campaign.campaign_id),
        authorized_by=campaign.authorized_by,
        stop_requested_at=campaign.stop_requested_at,
        ending=campaign.ending,
        error=campaign.error,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        ended_at=campaign.ended_at,
        tasks=tasks,
        current_orchestrator_task_id=current_orchestrator_task_id,
        current_control_task_id=current_control_task_id,
        recovery=(
            CampaignRecoverySummary(
                purpose=control_recovery.purpose,
                status=control_recovery.status,
                retry_mode=control_recovery.retry_mode,
                operation_id=control_recovery.operation_id,
                attempts=control_recovery.attempts,
                max_attempts=control_recovery.max_attempts,
                next_attempt_at=control_recovery.next_attempt_at,
            )
            if control_recovery is not None
            else None
        ),
        reports=[
            CampaignReportSummary(
                report_id=report.report_id,
                ending=report.ending,
                created_at=report.created_at,
            )
            for report in reports
        ],
        can_stop=(
            campaign.status in _STOPPABLE_CAMPAIGN_STATUSES
            and campaign.stop_requested_at is None
            and campaign.ending is None
        ),
        can_reauthorize=(campaign.status == "needs_action" and campaign.ending == "exhausted"),
    )


def serialize_campaigns(store: AppStore, project_id: str) -> list[CampaignResponse]:
    """Serialize the store's ordered campaign list while retaining project ownership."""

    return [
        serialize_campaign(store, project_id, campaign) for campaign in store.campaigns(project_id)
    ]
