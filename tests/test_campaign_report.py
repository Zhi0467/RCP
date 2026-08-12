from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.models import AuthorizedHuman
from rcp.runs.campaign import (
    CampaignReportCorrectionRequired,
    CampaignRunRequest,
    begin_campaign_wrapup,
    campaign_report_correction,
    complete_campaign_report,
    validate_campaign_report,
)
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    CampaignNotRunning,
    CampaignRecord,
    ProjectRecord,
)

_RUN_TRUTH_SCOPE = ["repo-a"]


def _orchestrator_authority(campaign_id: str) -> AgentDispatchAuthority:
    return AgentDispatchAuthority(
        profile="orchestrator",
        task_contract="orchestrate",
        scope=AgentDispatchScope(
            run_truth_scope=_RUN_TRUTH_SCOPE,
            campaign_id=campaign_id,
            patch_kind="work",
        ),
    )


def _campaign(
    tmp_path,
    *,
    ceiling: int = 3,
    root_status: str = "succeeded",
) -> tuple[AppStore, CampaignRecord, AgentTaskRecord]:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="/tmp/project/research.yaml",
            name="project",
            state_location="/tmp/project/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )
    authorizer = AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name="Campaign owner",
    )
    now = store.now()
    root_request = CampaignRunRequest(
        campaign_id="campaign",
        role="orchestrator",
        actor_operation_id="root",
        run_truth_scope=_RUN_TRUTH_SCOPE,
        session_id="report-session",
    )
    return store, *store.create_campaign_with_root_task(
        CampaignRecord(
            campaign_id="campaign",
            project_id="project",
            status="queued",
            invocation_ceiling=ceiling,
            authorized_by=authorizer,
            created_at=now,
            updated_at=now,
        ),
        AgentTaskRecord(
            operation_id="root",
            project_id="project",
            campaign_id="campaign",
            kind="campaign",
            status=root_status,
            request=root_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            native_session_id="report-session",
            stage_root="/tmp/campaign-report-stage",
            authorized_by=authorizer,
            dispatch_authority=_orchestrator_authority("campaign"),
        ),
    )


def _report_task(
    store: AppStore,
    campaign: CampaignRecord,
    root: AgentTaskRecord,
    operation_id: str,
    *,
    ending: str = "completed",
) -> AgentTaskRecord:
    binding = store.campaign_actor_binding(root.operation_id)
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="report",
        ending=ending,
        actor_operation_id=root.operation_id,
        session_id=binding.native_session_id,
    )
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=campaign.project_id,
        campaign_id=campaign.campaign_id,
        kind="campaign",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="queued",
        parent_operation_id=binding.current_operation_id,
        native_session_id=binding.native_session_id,
        stage_host=binding.stage_host,
        stage_root=binding.stage_root,
        authorized_by=campaign.authorized_by,
    )


def test_report_waits_for_every_already_admitted_non_report_turn(tmp_path) -> None:
    store, campaign, root = _campaign(tmp_path)
    now = store.now()
    worker = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="worker",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="worker",
                actor_operation_id="worker",
                run_truth_scope=_RUN_TRUTH_SCOPE,
                control_node_id="exp/check",
            ).model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="queued",
            parent_operation_id=root.operation_id,
            authorized_by=campaign.authorized_by,
            dispatch_authority=AgentDispatchAuthority(
                profile="ordinary",
                task_contract="work_auto",
                scope=AgentDispatchScope(
                    run_truth_scope=_RUN_TRUTH_SCOPE,
                    campaign_id=campaign.campaign_id,
                    patch_kind="work",
                ),
            ),
        ),
        role="worker",
    )
    begin_campaign_wrapup(store, campaign.campaign_id, "completed")
    fenced = store.campaign(campaign.campaign_id)
    assert fenced is not None
    assert fenced.status == "wrapping_up"
    assert fenced.ending == "completed"
    assert not any(
        store.campaign_invocation_role(task.operation_id) == "report"
        for task in store.campaign_tasks(campaign.campaign_id)
    )
    # The settlement guard lives inside the allocating transaction, so exercise it
    # there rather than through a separate read that could disagree with it.
    with pytest.raises(CampaignNotRunning, match="waiting for already-admitted turns"):
        store.allocate_campaign_report_task(
            _report_task(store, fenced, root, "live-worker-report", ending="completed"),
            ending="completed",
        )

    store.complete_agent_task(worker.operation_id, applied_revision=None, result={})
    settled, _record = store.allocate_campaign_report_task(
        _report_task(store, fenced, root, "settled-worker-report", ending="completed"),
        ending="completed",
    )
    assert settled.ending == "completed"
    wrapping = begin_campaign_wrapup(store, campaign.campaign_id, "completed")
    assert wrapping.status == "wrapping_up"
    assert wrapping.ending == "completed"


@pytest.mark.parametrize("ending", ["completed", "exhausted", "failed"])
def test_report_waits_for_paused_worker_same_allocation_recovery(
    tmp_path,
    ending: str,
) -> None:
    store, campaign, root = _campaign(tmp_path)
    now = store.now()
    worker_request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="worker",
        actor_operation_id="paused-worker",
        run_truth_scope=_RUN_TRUTH_SCOPE,
        control_node_id="exp/check",
    )
    worker = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="paused-worker",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=worker_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="queued",
            parent_operation_id=root.operation_id,
            authorized_by=campaign.authorized_by,
            dispatch_authority=AgentDispatchAuthority(
                profile="ordinary",
                task_contract="work_auto",
                scope=AgentDispatchScope(
                    run_truth_scope=_RUN_TRUTH_SCOPE,
                    campaign_id=campaign.campaign_id,
                    patch_kind="work",
                ),
            ),
        ),
        role="worker",
    )
    store.checkpoint_agent_task(
        worker.operation_id,
        native_session_id="worker-session",
        stage_root="/tmp/campaign-worker-stage",
    )
    store.pause_agent_task(worker.operation_id)
    wrapping = begin_campaign_wrapup(store, campaign.campaign_id, ending)
    meter_before_recovery = store.campaign_budget_meter(campaign.campaign_id)
    with pytest.raises(CampaignNotRunning, match="waiting for already-admitted turns"):
        store.allocate_campaign_report_task(
            _report_task(
                store,
                wrapping,
                root,
                "paused-worker-report",
                ending=ending,
            ),
            ending=ending,
        )

    paused = store.agent_task(worker.operation_id)
    assert paused is not None
    recovery_request = CampaignRunRequest.model_validate(paused.request).model_copy(
        update={"session_id": "worker-session"}
    )
    recovered = store.create_campaign_recovery_task(
        AgentTaskRecord(
            operation_id="paused-worker-recovery",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=recovery_request.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="done",
            attempt=2,
            parent_operation_id=worker.operation_id,
            native_session_id="worker-session",
            stage_root="/tmp/campaign-worker-stage",
            authorized_by=campaign.authorized_by,
            dispatch_authority=paused.dispatch_authority,
        )
    )

    assert recovered.parent_operation_id == paused.operation_id
    assert store.campaign_budget_meter(campaign.campaign_id) == meter_before_recovery
    admitted = store.allocate_campaign_report_task(
        _report_task(
            store,
            wrapping,
            root,
            "paused-worker-report",
            ending=ending,
        ),
        ending=ending,
    )[1]
    assert admitted.operation_id == "paused-worker-report"
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == (
        meter_before_recovery.invocations_used + 1
    )


def test_report_admission_blocks_pending_and_admitted_orchestrator_recovery(
    tmp_path,
) -> None:
    store, campaign, root = _campaign(tmp_path)
    continuation_request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="orchestrator",
        actor_operation_id=root.operation_id,
        run_truth_scope=_RUN_TRUTH_SCOPE,
        session_id="report-session",
    )
    now = store.now()
    continuation = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="interrupted-orchestrator",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=continuation_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="queued",
            parent_operation_id=root.operation_id,
            native_session_id="report-session",
            stage_root="/tmp/campaign-report-stage",
            authorized_by=campaign.authorized_by,
            dispatch_authority=_orchestrator_authority(campaign.campaign_id),
        ),
        role="orchestrator",
    )
    store.fail_agent_task(continuation.operation_id, "network lost", status="interrupted")
    wrapping = begin_campaign_wrapup(store, campaign.campaign_id, "completed")
    with pytest.raises(CampaignNotRunning, match="already-admitted turns"):
        store.allocate_campaign_report_task(
            _report_task(store, wrapping, root, "crash-gap-report"),
            ending="completed",
        )

    pending = store.schedule_campaign_task_recovery(
        continuation.operation_id,
        failure_kind="network",
        retry_mode="exact",
        diagnostic="Retry the exact orchestrator allocation.",
    )

    with pytest.raises(CampaignNotRunning, match="already-admitted turns"):
        store.allocate_campaign_report_task(
            _report_task(store, wrapping, root, "pending-recovery-report"),
            ending="completed",
        )

    recovery = store.create_campaign_recovery_task(
        AgentTaskRecord(
            operation_id="orchestrator-recovery",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=continuation_request.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="queued",
            attempt=2,
            parent_operation_id=continuation.operation_id,
            native_session_id="report-session",
            stage_root="/tmp/campaign-report-stage",
            authorized_by=campaign.authorized_by,
            dispatch_authority=_orchestrator_authority(campaign.campaign_id),
        )
    )
    store.complete_campaign_recovery(
        pending.recovery_id,
        admitted_operation_id=recovery.operation_id,
        expected_operation_id=continuation.operation_id,
    )
    with pytest.raises(CampaignNotRunning, match="already-admitted turns"):
        store.allocate_campaign_report_task(
            _report_task(store, wrapping, root, "active-recovery-report"),
            ending="completed",
        )

    store.complete_agent_task(recovery.operation_id, applied_revision=None, result={})
    report = store.allocate_campaign_report_task(
        _report_task(store, wrapping, root, "settled-recovery-report"),
        ending="completed",
    )[1]
    assert report.operation_id == "settled-recovery-report"


def test_failed_worker_is_terminal_for_report_readiness(tmp_path) -> None:
    store, campaign, root = _campaign(tmp_path)
    now = store.now()
    worker_request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="worker",
        actor_operation_id="failed-worker",
        run_truth_scope=_RUN_TRUTH_SCOPE,
        control_node_id="blocker/check",
    )
    worker = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="failed-worker",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=worker_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="queued",
            parent_operation_id=root.operation_id,
            authorized_by=campaign.authorized_by,
            dispatch_authority=AgentDispatchAuthority(
                profile="ordinary",
                task_contract="work_auto",
                scope=AgentDispatchScope(
                    run_truth_scope=_RUN_TRUTH_SCOPE,
                    campaign_id=campaign.campaign_id,
                    patch_kind="work",
                ),
            ),
        ),
        role="worker",
    )
    store.fail_agent_task(worker.operation_id, "Worker result remains for the orchestrator.")
    wrapping = begin_campaign_wrapup(store, campaign.campaign_id, "completed")

    report = store.allocate_campaign_report_task(
        _report_task(store, wrapping, root, "failed-worker-report"),
        ending="completed",
    )[1]

    assert report.operation_id == "failed-worker-report"


def test_typed_structural_failure_is_terminal_for_partial_report(tmp_path) -> None:
    store, campaign, root = _campaign(tmp_path, root_status="failed")
    store.record_agent_task_receipt(
        root.operation_id,
        "campaign_orchestrator_failure",
        {
            "classification": "structural_unrecoverable",
            "recoverable": False,
            "diagnostic": "The orchestrator contract cannot continue.",
        },
        tier="summary",
    )
    wrapping = store.fence_campaign_terminal_failure(
        root.operation_id,
        diagnostic="The orchestrator contract cannot continue.",
    )
    assert wrapping is not None

    report = store.allocate_campaign_report_task(
        _report_task(
            store,
            wrapping,
            root,
            "structural-failure-report",
            ending="failed",
        ),
        ending="failed",
    )[1]

    assert report.operation_id == "structural-failure-report"


def test_stop_abandoned_recovery_is_terminal_for_report_readiness(tmp_path) -> None:
    store, campaign, root = _campaign(tmp_path, root_status="failed")
    stopping = store.request_campaign_stop(campaign.campaign_id)
    assert stopping.status == "stopping"
    store.abandon_campaign_recovery(
        root.operation_id,
        diagnostic="The exact saved session is no longer available.",
    )
    wrapping = store.campaign(campaign.campaign_id)
    assert wrapping is not None
    assert wrapping.ending == "stopped"

    report = store.allocate_campaign_report_task(
        _report_task(
            store,
            wrapping,
            root,
            "stopped-report",
            ending="stopped",
        ),
        ending="stopped",
    )[1]

    assert report.operation_id == "stopped-report"


def test_missing_or_invalid_report_requests_same_session_correction_without_more_budget(
    tmp_path,
) -> None:
    store, campaign, root = _campaign(tmp_path)
    wrapping = begin_campaign_wrapup(
        store,
        campaign.campaign_id,
        "completed",
    )
    now = store.now()
    report_request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="report",
        ending="completed",
        session_id="report-session",
        actor_operation_id=root.operation_id,
    )
    report_task = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="report-turn",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=report_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            parent_operation_id=root.operation_id,
            native_session_id="report-session",
            stage_root="/tmp/campaign-report-stage",
            authorized_by=campaign.authorized_by,
        ),
        role="report",
    )
    used_before_correction = store.campaign_budget_meter(campaign.campaign_id).invocations_used

    with pytest.raises(CampaignReportCorrectionRequired) as missing:
        complete_campaign_report(
            store,
            campaign_id=campaign.campaign_id,
            operation_id=report_task.operation_id,
            ending="completed",
            candidate=None,
        )
    assert missing.value.diagnostic == (
        "Campaign report is missing. Return a non-empty UTF-8 HTML report."
    )
    with pytest.raises(CampaignReportCorrectionRequired, match="invalid"):
        validate_campaign_report(b"\xff")

    correction = campaign_report_correction(
        store,
        report_task.operation_id,
        round=1,
        diagnostic=missing.value.diagnostic,
    )
    assert correction.campaign_id == campaign.campaign_id
    assert correction.operation_id == report_task.operation_id
    assert correction.reuse_native_session is True
    assert correction.repeat_operational_work is False
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == (
        used_before_correction
    )
    still_wrapping = store.campaign(campaign.campaign_id)
    assert still_wrapping is not None
    assert still_wrapping.status == wrapping.status == "wrapping_up"
    assert still_wrapping.ending == wrapping.ending == "completed"
    with pytest.raises(ValidationError):
        campaign_report_correction(
            store,
            report_task.operation_id,
            round=3,
            diagnostic="Too many correction rounds.",
        )

    html = "<article><h1>What the campaign learned</h1><p>One result.</p></article>"
    ended, report = complete_campaign_report(
        store,
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="completed",
        candidate=html,
    )
    replayed_campaign, replayed_report = complete_campaign_report(
        store,
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="completed",
        candidate=html,
    )
    assert ended.status == "succeeded"
    assert report.html == html
    assert replayed_campaign == ended
    assert replayed_report == report
    assert store.campaign_reports(campaign.campaign_id) == [report]


def test_concurrent_identical_report_completion_returns_one_canonical_report(
    tmp_path,
) -> None:
    store, campaign, root = _campaign(tmp_path)
    begin_campaign_wrapup(
        store,
        campaign.campaign_id,
        "completed",
    )
    now = store.now()
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="report",
        ending="completed",
        actor_operation_id=root.operation_id,
        session_id="report-session",
    )
    report_task = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="report-turn",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            parent_operation_id=root.operation_id,
            native_session_id="report-session",
            stage_root="/tmp/campaign-report-stage",
            authorized_by=campaign.authorized_by,
        ),
        role="report",
    )
    html = "<article><h1>One immutable campaign report</h1></article>"
    barrier = threading.Barrier(2)

    def finish(_contender: int):
        barrier.wait(timeout=2)
        return complete_campaign_report(
            store,
            campaign_id=campaign.campaign_id,
            operation_id=report_task.operation_id,
            ending="completed",
            candidate=html,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(finish, range(2)))

    assert outcomes[0] == outcomes[1]
    ended, report = outcomes[0]
    assert ended.status == "succeeded"
    assert report.html == html
    assert store.campaign_reports(campaign.campaign_id) == [report]
    with pytest.raises(ValueError, match="other bytes"):
        complete_campaign_report(
            store,
            campaign_id=campaign.campaign_id,
            operation_id=report_task.operation_id,
            ending="completed",
            candidate="<article><h1>Different bytes</h1></article>",
        )
    with pytest.raises(ValueError, match="other bytes"):
        complete_campaign_report(
            store,
            campaign_id=campaign.campaign_id,
            operation_id=report_task.operation_id,
            ending="failed",
            candidate=html,
        )


def test_reserved_report_invocation_has_one_atomic_claim(tmp_path) -> None:
    store, campaign, root = _campaign(tmp_path)
    begin_campaign_wrapup(
        store,
        campaign.campaign_id,
        "completed",
    )
    barrier = threading.Barrier(2)

    def claim(operation_id: str):
        request = CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="report",
            ending="completed",
            actor_operation_id=root.operation_id,
            session_id="report-session",
        )
        now = store.now()
        record = AgentTaskRecord(
            operation_id=operation_id,
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="queued",
            parent_operation_id=root.operation_id,
            native_session_id="report-session",
            stage_root="/tmp/campaign-report-stage",
            authorized_by=campaign.authorized_by,
        )
        barrier.wait(timeout=2)
        try:
            return store.create_campaign_agent_task(record, role="report")
        except Exception as exc:  # Return both contenders so the atomic outcome is asserted below.
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ("report-one", "report-two")))

    claimed = [outcome for outcome in outcomes if isinstance(outcome, AgentTaskRecord)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(claimed) == len(refused) == 1
    assert "report" in str(refused[0]).lower()
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2
    assert [
        task.operation_id
        for task in store.campaign_tasks(campaign.campaign_id)
        if store.campaign_invocation_role(task.operation_id) == "report"
    ] == [claimed[0].operation_id]


def test_failed_campaign_does_not_reach_its_verdict_until_partial_report_is_durable(
    tmp_path,
) -> None:
    store, campaign, root = _campaign(tmp_path)
    wrapping = begin_campaign_wrapup(
        store,
        campaign.campaign_id,
        "failed",
        error="The provider became unavailable.",
    )
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="report",
        ending="failed",
        actor_operation_id=root.operation_id,
        session_id="report-session",
    )
    now = store.now()
    report_task = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="failure-report",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            parent_operation_id=root.operation_id,
            native_session_id="report-session",
            stage_root="/tmp/campaign-report-stage",
            authorized_by=campaign.authorized_by,
        ),
        role="report",
    )
    assert wrapping.status == "wrapping_up"
    assert store.campaign_reports(campaign.campaign_id) == []

    ended, report = complete_campaign_report(
        store,
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="failed",
        candidate=(
            "<article><h1>Partial campaign record</h1>"
            "<p>The provider failed before the remaining work completed.</p></article>"
        ),
    )
    assert ended.status == "failed"
    assert ended.error == "The provider became unavailable."
    assert "Partial" in report.html
    assert AppStore(store.path).campaign_report(report.report_id) == report
