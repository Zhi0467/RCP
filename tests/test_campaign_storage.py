from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.models import AuthorizedHuman
from rcp.providers import ProviderUsage
from rcp.runs.campaign import CampaignRunRequest
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    CampaignActorBusy,
    CampaignBudgetExhausted,
    CampaignMessageRecord,
    CampaignNotRunning,
    CampaignRecord,
    CampaignReportRecord,
    GraphWatcherRecord,
    ProjectRecord,
    WatcherContinuation,
)

from .helpers import fabricated_authorizer


def _project(store: AppStore, project_id: str = "project") -> None:
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            locator=f"/tmp/{project_id}/research.yaml",
            name=project_id,
            state_location=f"/tmp/{project_id}/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )


def _campaign_authority(request: CampaignRunRequest) -> AgentDispatchAuthority | None:
    if request.role == "report":
        return None
    return AgentDispatchAuthority(
        profile="orchestrator" if request.role == "orchestrator" else "ordinary",
        task_contract="orchestrate" if request.role == "orchestrator" else "work_auto",
        scope=AgentDispatchScope(
            run_truth_scope=list(request.run_truth_scope or ()),
            campaign_id=request.campaign_id,
            patch_kind="work",
        ),
    )


def _campaign(
    store: AppStore,
    *,
    campaign_id: str = "campaign-a",
    project_id: str = "project",
    ceiling: int = 6,
    authorizer: AuthorizedHuman | None = None,
    root_status: str = "succeeded",
) -> tuple[CampaignRecord, AgentTaskRecord]:
    authorizer = authorizer or fabricated_authorizer()
    now = store.now()
    root_request = CampaignRunRequest(
        campaign_id=campaign_id,
        role="orchestrator",
        run_truth_scope=["repo"],
        instruction="Investigate the project.",
    )
    return store.create_campaign_with_root_task(
        CampaignRecord(
            campaign_id=campaign_id,
            project_id=project_id,
            status="queued",
            starting_instruction="Investigate the project.",
            invocation_ceiling=ceiling,
            authorized_by=authorizer,
            created_at=now,
            updated_at=now,
        ),
        AgentTaskRecord(
            operation_id=f"{campaign_id}-root",
            project_id=project_id,
            campaign_id=campaign_id,
            kind="campaign",
            status=root_status,
            request=root_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            authorized_by=authorizer,
            dispatch_authority=_campaign_authority(root_request),
        ),
    )


def _turn(
    store: AppStore,
    campaign: CampaignRecord,
    *,
    operation_id: str,
    role: str,
    parent_operation_id: str | None = None,
    control_node_id: str | None = None,
    wake_cause: str | None = None,
    session_id: str | None = None,
) -> AgentTaskRecord:
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role=role,
        run_truth_scope=["repo"],
        control_node_id=control_node_id,
        wake_cause=wake_cause,
        session_id=session_id,
    )
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=campaign.project_id,
        campaign_id=campaign.campaign_id,
        kind="campaign",
        status="succeeded",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="done",
        parent_operation_id=parent_operation_id,
        native_session_id=session_id,
        authorized_by=campaign.authorized_by,
        dispatch_authority=_campaign_authority(request),
    )


def _running_campaign_worker(
    store: AppStore,
    campaign: CampaignRecord,
    root: AgentTaskRecord,
    *,
    operation_id: str = "worker",
) -> AgentTaskRecord:
    stage_root = f"/tmp/{operation_id}-stage"
    return store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id=operation_id,
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/check",
            session_id=f"{operation_id}-session",
        ).model_copy(update={"status": "running", "stage_root": stage_root}),
        role="worker",
    )


def _recover_campaign_worker(
    store: AppStore,
    campaign: CampaignRecord,
    worker: AgentTaskRecord,
    *,
    operation_id: str = "worker-recovery",
) -> AgentTaskRecord:
    request = CampaignRunRequest.model_validate(worker.request).model_copy(
        update={"session_id": worker.native_session_id}
    )
    now = store.now()
    return store.create_campaign_recovery_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="queued",
            attempt=worker.attempt + 1,
            parent_operation_id=worker.operation_id,
            native_session_id=worker.native_session_id,
            stage_host=worker.stage_host,
            stage_root=worker.stage_root,
            authorized_by=campaign.authorized_by,
            dispatch_authority=worker.dispatch_authority,
        )
    )


def _report_task(
    store: AppStore,
    campaign: CampaignRecord,
    *,
    operation_id: str,
    ending: str,
) -> AgentTaskRecord:
    assert campaign.root_operation_id is not None
    binding = store.campaign_actor_binding(campaign.root_operation_id)
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="report",
        ending=ending,
        actor_operation_id=campaign.root_operation_id,
        session_id=binding.native_session_id,
    )
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=campaign.project_id,
        campaign_id=campaign.campaign_id,
        kind="campaign",
        status="succeeded",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="done",
        parent_operation_id=binding.current_operation_id,
        native_session_id=binding.native_session_id,
        stage_host=binding.stage_host,
        stage_root=binding.stage_root,
        authorized_by=campaign.authorized_by,
    )


def _campaign_watcher(
    store: AppStore,
    campaign: CampaignRecord,
    origin: AgentTaskRecord,
    *,
    watcher_id: str,
    claimed: bool = False,
) -> GraphWatcherRecord:
    return GraphWatcherRecord(
        watcher_id=watcher_id,
        project_id=campaign.project_id,
        origin_operation_id=origin.operation_id,
        origin_task_kind="campaign",
        chat_id=origin.operation_id,
        continuation=WatcherContinuation(
            provider="codex",
            run_on="local",
            patch_kind="work",
        ),
        condition={"node_id": "hyp/result", "status_in": ["active"]},
        armed_revision=1,
        status="completed" if claimed else "active",
        created_at=store.now(),
        completed_at=store.now() if claimed else None,
        notified=claimed,
        notification_operation_id=origin.operation_id if claimed else None,
    )


def _interrupted_root_with_spawned_worker(
    store: AppStore,
    tmp_path,
) -> tuple[CampaignRecord, AgentTaskRecord, AgentTaskRecord]:
    campaign, root = _campaign(store, root_status="queued")
    store.checkpoint_agent_task(
        root.operation_id,
        native_session_id="orchestrator-session",
        stage_root=str(tmp_path / "orchestrator-stage"),
    )
    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="spawned-worker",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/check",
        ),
        role="worker",
    )
    store.fail_agent_task(
        root.operation_id,
        "The orchestrator was interrupted after the worker started.",
        status="interrupted",
    )
    interrupted = store.agent_task(root.operation_id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    return campaign, interrupted, worker


def test_only_one_live_campaign_exists_per_project(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    first, _ = _campaign(store)

    with pytest.raises(ValueError, match="Only one live auto-research campaign"):
        _campaign(store, campaign_id="campaign-b", authorizer=first.authorized_by)

    assert store.active_campaign("project") == first


def test_native_session_checkpoint_is_write_once_and_mismatch_changes_nothing(
    tmp_path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    _campaign_record, root = _campaign(store)

    store.checkpoint_agent_task(
        root.operation_id,
        native_session_id="saved-session",
        stage_host="saved-host",
        stage_root="/tmp/saved-stage",
    )
    store.checkpoint_agent_task(
        root.operation_id,
        native_session_id="saved-session",
    )
    before = store.agent_task(root.operation_id)
    assert before is not None
    assert before.native_session_id == "saved-session"

    with pytest.raises(ValueError, match="conflicts with its saved RCP checkpoint"):
        store.checkpoint_agent_task(
            root.operation_id,
            native_session_id="different-session",
            stage_host="different-host",
            stage_root="/tmp/different-stage",
        )

    assert store.agent_task(root.operation_id) == before


def test_one_pot_counts_actor_turns_and_wakes_without_changing_actor_role(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=6)

    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/check",
        ),
        role="worker",
    )
    worker_wake = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker-wake",
            role="worker",
            parent_operation_id=worker.operation_id,
            control_node_id="exp/check",
            wake_cause="graph_condition",
            session_id="worker-session",
        ),
        role="worker",
    )
    orchestrator_wake = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="orchestrator-wake",
            role="orchestrator",
            parent_operation_id=root.operation_id,
            wake_cause="watcher",
            session_id="orchestrator-session",
        ),
        role="orchestrator",
    )

    meter = store.campaign_budget_meter(campaign.campaign_id)
    assert meter.invocation_ceiling == 6
    assert meter.invocations_used == 4
    assert meter.invocations_remaining == 2
    assert meter.report_units_reserved == 1
    assert store.campaign_invocation_role(root.operation_id) == "orchestrator"
    assert store.campaign_invocation_role(worker.operation_id) == "worker"
    assert store.campaign_invocation_role(worker_wake.operation_id) == "worker"
    assert store.campaign_invocation_role(orchestrator_wake.operation_id) == "orchestrator"
    assert store.agent_task_profile(orchestrator_wake.operation_id) == "orchestrator"
    assert store.agent_task_profile(worker_wake.operation_id) == "ordinary"
    assert "invocation_ceiling" not in worker.request
    assert "invocation_ceiling" not in worker_wake.request

    store.record_agent_usage(
        root.operation_id,
        ProviderUsage(
            provider_profile="codex.turn.v1",
            provider_event_type="turn.completed",
            dedupe_key="root-usage",
            processed_input_tokens=100,
            generated_tokens=10,
        ),
    )
    store.record_agent_usage(
        worker.operation_id,
        ProviderUsage(
            provider_profile="codex.turn.v1",
            provider_event_type="turn.completed",
            dedupe_key="worker-usage",
            processed_input_tokens=50,
            generated_tokens=5,
        ),
    )
    store.record_agent_usage(
        worker.operation_id,
        ProviderUsage(
            provider_profile="codex.turn.v1",
            provider_event_type="turn.completed",
            dedupe_key="worker-usage",
            processed_input_tokens=999,
            generated_tokens=999,
        ),
    )
    observed = store.campaign_budget_meter(campaign.campaign_id)
    assert observed.observed_input_tokens == 150
    assert observed.observed_generated_tokens == 15


def test_handoff_clear_fence_is_private_durable_and_allocation_scoped(tmp_path) -> None:
    path = tmp_path / "rcp.sqlite3"
    store = AppStore(path)
    _project(store)
    campaign, root = _campaign(store)
    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/check",
        ),
        role="worker",
    )

    assert store.campaign_handoffs_cleared(root.operation_id) is False
    assert store.campaign_handoffs_cleared(worker.operation_id) is False
    before = store.agent_task(worker.operation_id)
    root_before = store.agent_task(root.operation_id)
    store.mark_campaign_handoffs_cleared(root.operation_id)
    store.mark_campaign_handoffs_cleared(root.operation_id)
    store.mark_campaign_handoffs_cleared(worker.operation_id)
    store.mark_campaign_handoffs_cleared(worker.operation_id)

    assert store.campaign_handoffs_cleared(root.operation_id) is True
    assert store.campaign_handoffs_cleared(worker.operation_id) is True
    assert store.campaign_worker_handoffs_cleared(worker.operation_id) is True
    assert store.agent_task(root.operation_id) == root_before
    assert store.agent_task(worker.operation_id) == before
    assert AppStore(path).campaign_handoffs_cleared(root.operation_id) is True
    assert AppStore(path).campaign_handoffs_cleared(worker.operation_id) is True
    with pytest.raises(KeyError):
        store.mark_campaign_handoffs_cleared("missing")

    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "completed")
    report = store.create_campaign_agent_task(
        _report_task(
            store,
            wrapping,
            operation_id="report",
            ending="completed",
        ),
        role="report",
    )
    with pytest.raises(ValueError, match="orchestrator or worker campaign allocation"):
        store.mark_campaign_handoffs_cleared(report.operation_id)


def test_campaign_usage_uses_latest_resumed_input_and_additive_generated_output(
    tmp_path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=6)
    store.checkpoint_agent_task(root.operation_id, native_session_id="orchestrator-session")
    resumed = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="orchestrator-resumed",
            role="orchestrator",
            parent_operation_id=root.operation_id,
            session_id="orchestrator-session",
        ),
        role="orchestrator",
    )
    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker-session-turn",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/check",
            session_id="worker-session",
        ),
        role="worker",
    )
    usages = [
        (root.operation_id, "root-context", 100, 10),
        (resumed.operation_id, "resumed-context", 150, 15),
        (worker.operation_id, "worker-context", 50, 5),
    ]
    for operation_id, key, input_tokens, generated_tokens in usages:
        store.record_agent_usage(
            operation_id,
            ProviderUsage(
                provider_profile="codex.turn.v1",
                provider_event_type="turn.completed",
                dedupe_key=key,
                processed_input_tokens=input_tokens,
                generated_tokens=generated_tokens,
            ),
        )
    with store.connection() as connection:
        for index, (operation_id, *_rest) in enumerate(usages):
            connection.execute(
                "UPDATE agent_usage SET created_at = ? WHERE operation_id = ?",
                (f"2026-08-12T00:00:0{index}+00:00", operation_id),
            )

    meter = store.campaign_budget_meter(campaign.campaign_id)
    assert meter.observed_input_tokens == 200
    assert meter.observed_generated_tokens == 30
    snapshot = store.agent_usage_snapshot(campaign.project_id)
    assert snapshot.input_processed.total_tokens == 200
    assert snapshot.generated.total_tokens == 30


def test_exhaustion_keeps_last_unit_for_durable_report_then_can_be_reauthorized(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=3)
    store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="research-turn",
            role="orchestrator",
            parent_operation_id=root.operation_id,
        ),
        role="orchestrator",
    )

    with pytest.raises(CampaignBudgetExhausted, match="one invocation remains reserved"):
        store.create_campaign_agent_task(
            _turn(
                store,
                campaign,
                operation_id="over-budget",
                role="worker",
                parent_operation_id=root.operation_id,
                control_node_id="blocker/missing-data",
            ),
            role="worker",
        )

    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "exhausted")
    report_task = store.create_campaign_agent_task(
        _report_task(
            store,
            wrapping,
            operation_id="exhaustion-report",
            ending="exhausted",
        ),
        role="report",
    )
    html = "<article><h1>Campaign stopped at the budget</h1></article>"
    report = CampaignReportRecord(
        report_id="report-a",
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="exhausted",
        sha256=hashlib.sha256(html.encode()).hexdigest(),
        html=html,
        created_at=store.now(),
    )
    exhausted, stored_report = store.finish_campaign_wrapup(report)

    assert exhausted.status == "needs_action"
    assert exhausted.ending == "exhausted"
    assert stored_report == report
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 3
    assert store.campaign_reports(campaign.campaign_id) == [report]
    assert AppStore(store.path).campaign_report(report.report_id) == report

    reauthorized = store.reauthorize_campaign(campaign.campaign_id, 2)
    assert reauthorized.status == "running"
    assert reauthorized.ending is None
    assert reauthorized.invocation_ceiling == 5
    assert reauthorized.invocations_used == 3
    resumed = store.create_campaign_agent_task(
        _turn(
            store,
            reauthorized,
            operation_id="reauthorized-turn",
            role="orchestrator",
            parent_operation_id=root.operation_id,
        ),
        role="orchestrator",
    )
    assert store.campaign_invocation_role(resumed.operation_id) == "orchestrator"


def test_depleted_campaign_exhaustion_fence_is_atomic_idempotent_and_retires_watchers(
    tmp_path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=3)
    store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="last-research-turn",
            role="orchestrator",
            parent_operation_id=root.operation_id,
        ),
        role="orchestrator",
    )
    watcher = store.create_watchers(
        [_campaign_watcher(store, campaign, root, watcher_id="depleted-watcher")]
    )[0]

    barrier = Barrier(2)

    def fence(_contender: int) -> CampaignRecord:
        barrier.wait(timeout=2)
        return AppStore(store.path).fence_campaign_exhaustion_if_depleted(campaign.campaign_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(fence, range(2)))

    assert outcomes[0] == outcomes[1]
    assert outcomes[0].status == "wrapping_up"
    assert outcomes[0].ending == "exhausted"
    assert outcomes[0].invocations_used == 2
    assert store.fence_campaign_exhaustion_if_depleted(campaign.campaign_id) == outcomes[0]
    retired = store.watcher(watcher.watcher_id)
    assert retired is not None
    assert retired.status == "stopped"
    assert retired.notified is True
    assert retired.stopped_by == "loop"


def test_depleted_exhaustion_fence_waits_for_the_last_admitted_turn(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=3)
    last_turn = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="active-last-research-turn",
            role="orchestrator",
            parent_operation_id=root.operation_id,
        ).model_copy(update={"status": "running"}),
        role="orchestrator",
    )

    still_running = store.fence_campaign_exhaustion_if_depleted(campaign.campaign_id)
    assert (still_running.status, still_running.ending) == ("running", None)

    store.complete_agent_task(last_turn.operation_id, applied_revision=None, result={})
    exhausted = store.fence_campaign_exhaustion_if_depleted(campaign.campaign_id)
    assert (exhausted.status, exhausted.ending) == ("wrapping_up", "exhausted")


def test_exhaustion_fence_does_not_override_stop_or_an_undepleted_campaign(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, _root = _campaign(store, ceiling=4)

    assert store.fence_campaign_exhaustion_if_depleted(campaign.campaign_id) == campaign
    stopping = store.request_campaign_stop(campaign.campaign_id)
    assert store.fence_campaign_exhaustion_if_depleted(campaign.campaign_id) == stopping


def test_stop_wins_while_the_depleted_campaigns_last_research_turn_is_active(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=3)
    last_turn = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="active-last-research-turn",
            role="orchestrator",
            parent_operation_id=root.operation_id,
        ).model_copy(update={"status": "running"}),
        role="orchestrator",
    )
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2
    assert store.agent_task(last_turn.operation_id).status == "running"  # type: ignore[union-attr]
    barrier = Barrier(2)

    def stop() -> CampaignRecord | CampaignNotRunning:
        barrier.wait(timeout=2)
        try:
            return AppStore(store.path).request_campaign_stop(campaign.campaign_id)
        except CampaignNotRunning as exc:
            return exc

    def exhaust() -> CampaignRecord:
        barrier.wait(timeout=2)
        return AppStore(store.path).fence_campaign_exhaustion_if_depleted(campaign.campaign_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        stop_future = executor.submit(stop)
        exhaustion_future = executor.submit(exhaust)
        stop_result = stop_future.result()
        exhaustion_result = exhaustion_future.result()

    stored = store.campaign(campaign.campaign_id)
    assert stored is not None
    assert not isinstance(stop_result, CampaignNotRunning)
    assert stop_result.stop_requested_at is not None
    assert stored.stop_requested_at == stop_result.stop_requested_at
    assert stored.status == "stopping"
    assert stored.ending is None
    assert exhaustion_result.ending is None


def test_stop_does_not_replace_an_exhausted_durable_ending(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, _root = _campaign(store, ceiling=2)
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "exhausted")
    report_task = store.create_campaign_agent_task(
        _report_task(
            store,
            wrapping,
            operation_id="exhaustion-report",
            ending="exhausted",
        ),
        role="report",
    )
    html = "<article><h1>Budget exhausted</h1></article>"
    exhausted, _ = store.finish_campaign_wrapup(
        CampaignReportRecord(
            report_id="exhaustion-report-record",
            campaign_id=campaign.campaign_id,
            operation_id=report_task.operation_id,
            ending="exhausted",
            sha256=hashlib.sha256(html.encode()).hexdigest(),
            html=html,
            created_at=store.now(),
        )
    )

    with pytest.raises(CampaignNotRunning, match="Stop was not recorded"):
        store.request_campaign_stop(campaign.campaign_id)

    assert store.campaign(campaign.campaign_id) == exhausted
    assert store.begin_campaign_wrapup(campaign.campaign_id, "exhausted") == exhausted
    with pytest.raises(ValueError, match="already ended differently"):
        store.begin_campaign_wrapup(campaign.campaign_id, "completed")


def test_stop_does_not_replace_an_in_progress_wrapup(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, _root = _campaign(store, ceiling=2)
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "exhausted")

    with pytest.raises(CampaignNotRunning, match="Stop was not recorded"):
        store.request_campaign_stop(campaign.campaign_id)

    assert store.campaign(campaign.campaign_id) == wrapping


def test_stop_blocks_new_admission_without_cancelling_the_current_task(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=4, root_status="queued")
    store.mark_agent_task_running(root.operation_id)

    stopping = store.request_campaign_stop(campaign.campaign_id)
    assert stopping.status == "stopping"
    assert stopping.stop_requested_at is not None
    reopened = AppStore(store.path)
    assert reopened.campaign(campaign.campaign_id) == stopping
    assert reopened.request_campaign_stop(campaign.campaign_id).stop_requested_at == (
        stopping.stop_requested_at
    )
    with pytest.raises(CampaignNotRunning, match="not admitting new work"):
        store.create_campaign_agent_task(
            _turn(
                store,
                stopping,
                operation_id="after-stop",
                role="orchestrator",
                parent_operation_id=root.operation_id,
            ),
            role="orchestrator",
        )

    reopened.complete_agent_task(root.operation_id, applied_revision=None, result={"messages": []})
    assert reopened.agent_task(root.operation_id).status == "succeeded"  # type: ignore[union-attr]
    assert reopened.request_campaign_stop(campaign.campaign_id).stop_requested_at == (
        stopping.stop_requested_at
    )


def test_campaign_stop_retains_watchers_and_settles_only_after_the_current_leaf(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=5, root_status="running")
    unclaimed, claimed = store.create_watchers(
        [
            _campaign_watcher(
                store,
                campaign,
                root,
                watcher_id="unclaimed-before-stop",
            ),
            _campaign_watcher(
                store,
                campaign,
                root,
                watcher_id="claimed-before-stop",
                claimed=True,
            ),
        ]
    )

    stopping = store.request_campaign_stop(campaign.campaign_id)

    assert stopping.status == "stopping"
    stopped_unclaimed = store.watcher(unclaimed.watcher_id)
    assert stopped_unclaimed is not None
    assert stopped_unclaimed.status == "stopped"
    assert stopped_unclaimed.notified is True
    assert stopped_unclaimed.stopped_by == "loop"
    still_claimed = store.watcher(claimed.watcher_id)
    assert still_claimed is not None
    assert still_claimed.status == "completed"
    assert still_claimed.notification_operation_id == root.operation_id
    assert store.settle_ready_campaign_stops() == 0

    armed_after_stop = store.create_watchers(
        [
            _campaign_watcher(
                store,
                stopping,
                root,
                watcher_id="armed-after-stop",
            )
        ]
    )[0]
    assert armed_after_stop.status == "stopped"
    assert armed_after_stop.notified is True
    assert armed_after_stop.stopped_by == "loop"
    assert armed_after_stop.stopped_at == stopping.stop_requested_at

    store.complete_agent_task(root.operation_id, applied_revision=None, result={"messages": []})
    assert store.campaign(campaign.campaign_id).status == "stopping"  # type: ignore[union-attr]
    assert store.settle_ready_campaign_stops() == 1
    wrapping = store.campaign(campaign.campaign_id)
    assert wrapping is not None
    assert wrapping.status == "wrapping_up"
    assert wrapping.ending == "stopped"
    settled_claim = store.watcher(claimed.watcher_id)
    assert settled_claim is not None
    assert settled_claim.status == "stopped"
    assert settled_claim.notification_operation_id == root.operation_id


def test_campaign_stop_preserves_recoverable_leaf_until_explicit_abandonment(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, root_status="failed")

    stopping = store.request_campaign_stop(campaign.campaign_id)

    assert stopping.status == "stopping"
    assert store.settle_ready_campaign_stops() == 0
    assert store.campaign(campaign.campaign_id).status == "stopping"  # type: ignore[union-attr]

    preserved = store.abandon_campaign_recovery(
        root.operation_id,
        diagnostic="The exact saved provider session is no longer available.",
    )

    assert preserved.status == "failed"
    assert any(
        receipt.category == "campaign_recovery_abandoned"
        for receipt in store.agent_task_receipts(root.operation_id)
    )
    wrapping = store.campaign(campaign.campaign_id)
    assert wrapping is not None
    assert wrapping.status == "wrapping_up"
    assert wrapping.ending == "stopped"


def test_campaign_stop_waits_for_a_paused_worker_same_allocation_recovery(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    worker = _running_campaign_worker(store, campaign, root)
    store.request_campaign_worker_pause(worker.operation_id, campaign.campaign_id)
    store.pause_agent_task(worker.operation_id)
    meter_before_recovery = store.campaign_budget_meter(campaign.campaign_id)

    stopping = store.request_campaign_stop(campaign.campaign_id)

    assert stopping.status == "stopping"
    assert store.settle_ready_campaign_stops() == 0
    recovery = _recover_campaign_worker(store, stopping, worker)
    assert store.campaign_budget_meter(campaign.campaign_id) == meter_before_recovery
    store.mark_agent_task_running(recovery.operation_id)
    store.complete_agent_task(recovery.operation_id, applied_revision=None, result={})
    wrapping = store.settle_campaign_stop(campaign.campaign_id)
    assert wrapping.status == "wrapping_up"
    assert wrapping.ending == "stopped"


@pytest.mark.parametrize("worker_status", ["failed", "interrupted"])
def test_campaign_stop_treats_terminal_worker_as_settled_and_report_ready(
    tmp_path,
    worker_status: str,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id=f"{worker_status}-worker",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/check",
        ).model_copy(update={"status": worker_status}),
        role="worker",
    )

    wrapping = store.request_campaign_stop(campaign.campaign_id)

    assert wrapping.status == "wrapping_up"
    assert wrapping.ending == "stopped"
    assert store.agent_task(worker.operation_id) == worker
    report = store.create_campaign_agent_task(
        _report_task(
            store,
            wrapping,
            operation_id=f"{worker_status}-worker-stop-report",
            ending="stopped",
        ),
        role="report",
    )
    assert report.status == "succeeded"


def test_spawned_worker_does_not_block_exact_orchestrator_retry(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root, worker = _interrupted_root_with_spawned_worker(store, tmp_path)
    meter_before = store.campaign_budget_meter(campaign.campaign_id)
    request = CampaignRunRequest.model_validate(root.request).model_copy(
        update={"session_id": root.native_session_id}
    )

    recovery = store.create_campaign_recovery_task(
        AgentTaskRecord(
            operation_id="orchestrator-retry",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="queued",
            attempt=root.attempt + 1,
            parent_operation_id=root.operation_id,
            native_session_id=root.native_session_id,
            stage_host=root.stage_host,
            stage_root=root.stage_root,
            authorized_by=campaign.authorized_by,
            dispatch_authority=root.dispatch_authority,
        )
    )

    assert recovery.parent_operation_id == root.operation_id
    assert recovery.request["actor_operation_id"] == root.operation_id
    assert worker.parent_operation_id == root.operation_id
    assert worker.request["actor_operation_id"] == worker.operation_id
    assert store.campaign_budget_meter(campaign.campaign_id) == meter_before
    with pytest.raises(ValueError, match="already has a recovery child"):
        store.create_campaign_recovery_task(
            recovery.model_copy(
                update={
                    "operation_id": "duplicate-retry",
                    "created_at": store.now(),
                    "updated_at": store.now(),
                }
            )
        )


def test_spawned_worker_does_not_hide_busy_or_stopped_orchestrator_recovery(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root, _worker = _interrupted_root_with_spawned_worker(store, tmp_path)
    meter_before = store.campaign_budget_meter(campaign.campaign_id)
    paid_continuation = _turn(
        store,
        campaign,
        operation_id="paid-orchestrator-continuation",
        role="orchestrator",
        parent_operation_id=root.operation_id,
        session_id=root.native_session_id,
    ).model_copy(update={"stage_host": root.stage_host, "stage_root": root.stage_root})

    with pytest.raises(CampaignActorBusy) as exc_info:
        store.create_campaign_agent_task(paid_continuation, role="orchestrator")

    assert exc_info.value.actor_operation_id == root.operation_id
    assert exc_info.value.operation_id == root.operation_id
    assert store.campaign_budget_meter(campaign.campaign_id) == meter_before
    stopping = store.request_campaign_stop(campaign.campaign_id)
    assert stopping.status == "stopping"
    assert store.settle_ready_campaign_stops() == 0

    store.abandon_campaign_recovery(
        root.operation_id,
        diagnostic="The exact orchestrator session is no longer available.",
    )

    wrapping = store.campaign(campaign.campaign_id)
    assert wrapping is not None
    assert wrapping.status == "wrapping_up"
    assert wrapping.ending == "stopped"


def test_campaign_children_retain_lineage_and_root_human_authorizer(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    other_authorizer = fabricated_authorizer()

    with pytest.raises(ValueError, match="authorizer snapshot"):
        store.create_campaign_agent_task(
            _turn(
                store,
                campaign.model_copy(update={"authorized_by": other_authorizer}),
                operation_id="wrong-authorizer",
                role="worker",
                parent_operation_id=root.operation_id,
                control_node_id="exp/check",
            ),
            role="worker",
        )
    with pytest.raises(ValueError, match="campaign lineage"):
        store.create_campaign_agent_task(
            _turn(
                store,
                campaign,
                operation_id="missing-parent",
                role="worker",
                parent_operation_id="not-a-campaign-task",
                control_node_id="exp/check",
            ),
            role="worker",
        )

    child = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="valid-child",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/check",
        ),
        role="worker",
    )
    assert child.campaign_id == campaign.campaign_id
    assert child.parent_operation_id == root.operation_id
    assert child.authorized_by == campaign.authorized_by


def test_campaign_storage_requires_root_authority_and_actor_origin_scope(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    authorizer = fabricated_authorizer()
    now = store.now()
    request = CampaignRunRequest(
        campaign_id="unbound",
        role="orchestrator",
        run_truth_scope=["repo"],
        actor_operation_id="unbound-root",
    )
    campaign = CampaignRecord(
        campaign_id="unbound",
        project_id="project",
        root_operation_id="unbound-root",
        status="queued",
        invocation_ceiling=4,
        authorized_by=authorizer,
        created_at=now,
        updated_at=now,
    )
    root = AgentTaskRecord(
        operation_id="unbound-root",
        project_id="project",
        campaign_id="unbound",
        kind="campaign",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="queued",
        authorized_by=authorizer,
    )

    with pytest.raises(ValueError, match="server-owned dispatch authority"):
        store.create_campaign_with_root_task(campaign, root)
    assert store.campaign("unbound") is None
    assert store.agent_task("unbound-root") is None

    campaign, stored_root = _campaign(store)
    meter_before = store.campaign_budget_meter(campaign.campaign_id)
    forged = _turn(
        store,
        campaign,
        operation_id="forged-worker",
        role="worker",
        parent_operation_id=stored_root.operation_id,
        control_node_id="exp/check",
    ).model_copy(
        update={
            "dispatch_authority": AgentDispatchAuthority(
                profile="ordinary",
                task_contract="work_auto",
                scope=AgentDispatchScope(
                    run_truth_scope=["other-repo"],
                    campaign_id=campaign.campaign_id,
                    patch_kind="work",
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="server-owned dispatch authority|project-wide scope"):
        store.create_campaign_agent_task(forged, role="worker")
    assert store.agent_task(forged.operation_id) is None
    assert store.campaign_budget_meter(campaign.campaign_id) == meter_before


@pytest.mark.parametrize("leaf_status", ["paused", "interrupted", "failed"])
def test_paid_wake_waits_for_recoverable_actor_leaf_but_exact_recovery_is_unpaid(
    tmp_path,
    leaf_status: str,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    stage_root = str(tmp_path / "worker-stage")
    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/check",
            session_id="worker-session",
        ).model_copy(update={"status": leaf_status, "stage_root": stage_root}),
        role="worker",
    )
    meter_before = store.campaign_budget_meter(campaign.campaign_id)
    paid_wake = _turn(
        store,
        campaign,
        operation_id="paid-wake",
        role="worker",
        parent_operation_id=worker.operation_id,
        control_node_id="exp/check",
        wake_cause="graph_condition",
        session_id="worker-session",
    ).model_copy(update={"stage_root": stage_root})

    with pytest.raises(CampaignActorBusy) as exc_info:
        store.create_campaign_agent_task(paid_wake, role="worker")

    assert exc_info.value.actor_operation_id == worker.operation_id
    assert exc_info.value.operation_id == worker.operation_id
    assert store.agent_task(paid_wake.operation_id) is None
    assert store.campaign_budget_meter(campaign.campaign_id) == meter_before

    recovery_request = CampaignRunRequest.model_validate(worker.request).model_copy(
        update={"session_id": worker.native_session_id}
    )
    recovery = store.create_campaign_recovery_task(
        AgentTaskRecord(
            operation_id="same-allocation-recovery",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=recovery_request.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="queued",
            attempt=worker.attempt + 1,
            parent_operation_id=worker.operation_id,
            native_session_id=worker.native_session_id,
            stage_host=worker.stage_host,
            stage_root=worker.stage_root,
            authorized_by=campaign.authorized_by,
            dispatch_authority=worker.dispatch_authority,
        )
    )

    assert recovery.parent_operation_id == worker.operation_id
    assert store.campaign_invocation_role(recovery.operation_id) == "worker"
    assert store.campaign_budget_meter(campaign.campaign_id) == meter_before


def test_mail_wake_remains_pending_and_unspent_behind_recoverable_actor_leaf(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    stage_root = str(tmp_path / "worker-stage")
    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="blocker/check",
            session_id="worker-session",
        ).model_copy(update={"status": "paused", "stage_root": stage_root}),
        role="worker",
    )
    message = store.record_campaign_message(
        CampaignMessageRecord(
            message_id="message-to-paused-worker",
            campaign_id=campaign.campaign_id,
            sender_role="orchestrator",
            sender_task_id=root.operation_id,
            recipient_task_id=worker.operation_id,
            control_node_id="blocker/check",
            body="Continue after recovery.",
            created_at=store.now(),
        )
    )
    meter_before = store.campaign_budget_meter(campaign.campaign_id)
    wake = _turn(
        store,
        campaign,
        operation_id="mail-wake",
        role="worker",
        parent_operation_id=worker.operation_id,
        control_node_id="blocker/check",
        wake_cause="message",
        session_id="worker-session",
    ).model_copy(update={"stage_root": stage_root})

    with pytest.raises(CampaignActorBusy):
        store.create_campaign_message_wake_task(
            wake,
            role="worker",
            recipient_task_id=worker.operation_id,
            message_ids=[message.message_id],
        )

    assert store.agent_task(wake.operation_id) is None
    assert store.pending_campaign_messages(campaign.campaign_id, worker.operation_id) == [message]
    assert store.campaign_budget_meter(campaign.campaign_id) == meter_before


def test_new_campaign_message_sender_snapshots_fail_closed(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)

    with pytest.raises(ValueError, match="human campaign message requires its sender snapshot"):
        store.record_campaign_message(
            CampaignMessageRecord(
                message_id="missing-human-snapshot",
                campaign_id=campaign.campaign_id,
                sender_role="human",
                recipient_task_id=root.operation_id,
                body="Missing identity.",
                created_at=store.now(),
            )
        )

    with pytest.raises(ValueError, match="agent campaign message cannot claim"):
        CampaignMessageRecord(
            message_id="forged-agent-snapshot",
            campaign_id=campaign.campaign_id,
            sender_role="orchestrator",
            sender_task_id=root.operation_id,
            authorized_by=campaign.authorized_by,
            recipient_task_id=root.operation_id,
            body="Forged identity.",
            created_at=store.now(),
        )


def test_message_admission_rechecks_campaign_fence_in_its_write_transaction(
    tmp_path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="settling-worker",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/result",
        ),
        role="worker",
    )
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "completed")

    with pytest.raises(CampaignNotRunning, match="not accepting new human mail"):
        store.record_campaign_message(
            CampaignMessageRecord(
                message_id="late-human-message",
                campaign_id=campaign.campaign_id,
                sender_role="human",
                authorized_by=campaign.authorized_by,
                recipient_task_id=root.operation_id,
                body="This raced the ending fence.",
                created_at=store.now(),
            )
        )
    assert store.campaign_message("late-human-message") is None

    with pytest.raises(CampaignNotRunning, match="accepting orchestrator mail"):
        store.record_campaign_message(
            CampaignMessageRecord(
                message_id="late-orchestrator-message",
                campaign_id=campaign.campaign_id,
                sender_role="orchestrator",
                sender_task_id=root.operation_id,
                recipient_task_id=worker.operation_id,
                body="This also raced the ending fence.",
                created_at=store.now(),
            )
        )
    assert store.campaign_message("late-orchestrator-message") is None

    stored_reply = store.record_campaign_message(
        CampaignMessageRecord(
            message_id="settling-agent-reply",
            campaign_id=campaign.campaign_id,
            sender_role="worker",
            sender_task_id=worker.operation_id,
            recipient_task_id=root.operation_id,
            control_node_id="exp/result",
            body="My admitted turn's final retrospective result.",
            created_at=store.now(),
        )
    )
    assert stored_reply.message_id == "settling-agent-reply"
    assert store.campaign(campaign.campaign_id) == wrapping


@pytest.mark.parametrize("ending", ["completed", "exhausted", "stopped", "failed"])
def test_campaign_watcher_created_after_any_ending_fence_is_born_stopped(
    tmp_path,
    ending: str,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    fenced = store.begin_campaign_wrapup(campaign.campaign_id, ending)

    stored = store.create_watchers(
        [
            _campaign_watcher(
                store,
                fenced,
                root,
                watcher_id=f"late-{ending}-watcher",
            )
        ]
    )[0]

    assert stored.status == "stopped"
    assert stored.notified is True
    assert stored.stopped_by == "loop"
    assert stored.stopped_at == fenced.updated_at


def test_finish_campaign_requires_the_live_root_orchestrator_atomically(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker-finish-caller",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/result",
        ),
        role="worker",
    )

    with pytest.raises(ValueError, match="sole orchestrator"):
        store.finish_campaign_from_orchestrator(campaign.campaign_id, worker.operation_id)
    assert store.campaign(campaign.campaign_id).status == "running"  # type: ignore[union-attr]

    finished = store.finish_campaign_from_orchestrator(
        campaign.campaign_id,
        root.operation_id,
    )
    assert (finished.status, finished.ending) == ("wrapping_up", "completed")
    with pytest.raises(CampaignNotRunning, match="no longer accepting Finish"):
        store.finish_campaign_from_orchestrator(campaign.campaign_id, root.operation_id)


@pytest.mark.parametrize("ending_fence", ["finish", "stop"])
@pytest.mark.parametrize("pause_first", [True, False])
def test_worker_pause_and_campaign_ending_are_settled_in_either_order(
    tmp_path,
    ending_fence: str,
    pause_first: bool,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    worker = _running_campaign_worker(
        store,
        campaign,
        root,
        operation_id="worker-to-pause",
    )

    if pause_first:
        store.request_campaign_worker_pause(worker.operation_id, campaign.campaign_id)
    if ending_fence == "finish":
        store.finish_campaign_from_orchestrator(campaign.campaign_id, root.operation_id)
    else:
        store.request_campaign_stop(campaign.campaign_id)
    if pause_first:
        store.pause_agent_task(worker.operation_id)
        current = store.campaign(campaign.campaign_id)
        assert current is not None
        meter_before_recovery = store.campaign_budget_meter(campaign.campaign_id)
        with pytest.raises(CampaignNotRunning, match="already-admitted turns"):
            store.allocate_campaign_report_task(
                _report_task(
                    store,
                    current,
                    operation_id=f"blocked-{ending_fence}-report",
                    ending="completed" if ending_fence == "finish" else "stopped",
                ),
                ending="completed" if ending_fence == "finish" else "stopped",
            )
        recovery = _recover_campaign_worker(store, current, worker)
        assert store.campaign_budget_meter(campaign.campaign_id) == meter_before_recovery
        store.mark_agent_task_running(recovery.operation_id)
        store.complete_agent_task(recovery.operation_id, applied_revision=None, result={})
    else:
        with pytest.raises(CampaignNotRunning, match="worker-control commands"):
            store.request_campaign_worker_pause(worker.operation_id, campaign.campaign_id)
        store.complete_agent_task(worker.operation_id, applied_revision=None, result={})

    if ending_fence == "stop":
        fenced = store.settle_campaign_stop(campaign.campaign_id)
        expected_ending = "stopped"
    else:
        fenced = store.campaign(campaign.campaign_id)
        expected_ending = "completed"
    assert fenced is not None
    assert (fenced.status, fenced.ending) == ("wrapping_up", expected_ending)
    report = store.allocate_campaign_report_task(
        _report_task(
            store,
            fenced,
            operation_id=f"{ending_fence}-after-pause-report",
            ending=expected_ending,
        ),
        ending=expected_ending,
    )[1]
    assert report.status == "succeeded"


@pytest.mark.parametrize("ending_fence", ["finish", "stop"])
def test_concurrent_worker_pause_and_campaign_ending_cannot_deadlock_report(
    tmp_path,
    ending_fence: str,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    worker = _running_campaign_worker(store, campaign, root, operation_id="racing-worker")
    barrier = Barrier(2)

    def pause() -> bool:
        barrier.wait()
        try:
            store.request_campaign_worker_pause(worker.operation_id, campaign.campaign_id)
        except CampaignNotRunning:
            return False
        return True

    def fence() -> None:
        barrier.wait()
        if ending_fence == "finish":
            store.finish_campaign_from_orchestrator(campaign.campaign_id, root.operation_id)
        else:
            store.request_campaign_stop(campaign.campaign_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        pause_future = executor.submit(pause)
        fence_future = executor.submit(fence)
        pause_committed = pause_future.result()
        fence_future.result()

    if pause_committed:
        store.pause_agent_task(worker.operation_id)
        current = store.campaign(campaign.campaign_id)
        assert current is not None
        meter_before_recovery = store.campaign_budget_meter(campaign.campaign_id)
        with pytest.raises(CampaignNotRunning, match="already-admitted turns"):
            store.allocate_campaign_report_task(
                _report_task(
                    store,
                    current,
                    operation_id=f"blocked-concurrent-{ending_fence}-report",
                    ending="completed" if ending_fence == "finish" else "stopped",
                ),
                ending="completed" if ending_fence == "finish" else "stopped",
            )
        recovery = _recover_campaign_worker(store, current, worker)
        assert store.campaign_budget_meter(campaign.campaign_id) == meter_before_recovery
        store.mark_agent_task_running(recovery.operation_id)
        store.complete_agent_task(recovery.operation_id, applied_revision=None, result={})
    else:
        store.complete_agent_task(worker.operation_id, applied_revision=None, result={})
    if ending_fence == "stop":
        fenced = store.settle_campaign_stop(campaign.campaign_id)
        expected_ending = "stopped"
    else:
        fenced = store.campaign(campaign.campaign_id)
        expected_ending = "completed"
    assert fenced is not None
    report = store.allocate_campaign_report_task(
        _report_task(
            store,
            fenced,
            operation_id=f"concurrent-{ending_fence}-report",
            ending=expected_ending,
        ),
        ending=expected_ending,
    )[1]
    assert report.status == "succeeded"


def test_campaign_message_snapshot_reads_legacy_null_and_rejects_partial_rows(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    now = store.now()
    with store.connection() as connection:
        connection.executemany(
            """
            INSERT INTO campaign_messages (
                message_id, campaign_id, sender_role, sender_task_id,
                authorized_space_id, authorized_user_id, authorized_display_name,
                recipient_task_id, body, created_at
            ) VALUES (?, ?, 'human', NULL, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "legacy-human",
                    campaign.campaign_id,
                    None,
                    None,
                    None,
                    root.operation_id,
                    "Legacy steering.",
                    now,
                ),
                (
                    "partial-human",
                    campaign.campaign_id,
                    campaign.authorized_by.space_id,
                    None,
                    campaign.authorized_by.display_name,
                    root.operation_id,
                    "Corrupt steering.",
                    now,
                ),
            ],
        )

    legacy = store.campaign_message("legacy-human")
    assert legacy is not None
    assert legacy.sender_role == "human"
    assert legacy.authorized_by is None
    with pytest.raises(RuntimeError, match="snapshot is partial"):
        store.campaign_message("partial-human")


def test_campaign_message_snapshot_columns_migrate_existing_table_without_backfill_or_index(
    tmp_path,
) -> None:
    path = tmp_path / "rcp.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE campaign_messages (
                message_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                sender_role TEXT NOT NULL,
                sender_task_id TEXT,
                recipient_task_id TEXT NOT NULL,
                control_node_id TEXT,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                delivery_operation_id TEXT
            );
            INSERT INTO campaign_messages (
                message_id, campaign_id, sender_role, recipient_task_id, body, created_at
            ) VALUES (
                'legacy-human', 'campaign', 'human', 'root', 'Legacy steering.',
                '2026-08-12T00:00:00+00:00'
            );
            """
        )
        original_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(campaign_messages)")
        }

    AppStore(path)

    snapshot_columns = {
        "authorized_space_id",
        "authorized_user_id",
        "authorized_display_name",
    }
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(campaign_messages)")
        }
        assert set(columns) - original_columns == snapshot_columns
        assert all(columns[name][3] == 0 and columns[name][4] is None for name in snapshot_columns)
        row = connection.execute(
            """
            SELECT authorized_space_id, authorized_user_id, authorized_display_name
            FROM campaign_messages WHERE message_id = 'legacy-human'
            """
        ).fetchone()
        assert row == (None, None, None)
        for index in connection.execute("PRAGMA index_list(campaign_messages)"):
            indexed_columns = {
                item[2] for item in connection.execute(f"PRAGMA index_info('{index[1]}')")
            }
            assert indexed_columns.isdisjoint(snapshot_columns)


def test_campaign_mail_is_star_shaped_and_one_wake_atomically_claims_a_batch(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store, ceiling=8)
    first_worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker-one",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="exp/one",
        ),
        role="worker",
    )
    second_worker = store.create_campaign_agent_task(
        _turn(
            store,
            campaign,
            operation_id="worker-two",
            role="worker",
            parent_operation_id=root.operation_id,
            control_node_id="blocker/two",
        ),
        role="worker",
    )

    def message(message_id: str, body: str) -> CampaignMessageRecord:
        return CampaignMessageRecord(
            message_id=message_id,
            campaign_id=campaign.campaign_id,
            sender_role="worker",
            sender_task_id=first_worker.operation_id,
            recipient_task_id=root.operation_id,
            control_node_id="exp/one",
            body=body,
            created_at=store.now(),
        )

    first = store.record_campaign_message(message("message-one", "First result"))
    second = store.record_campaign_message(message("message-two", "Second result"))
    with pytest.raises(ValueError, match="reply only to the campaign orchestrator"):
        store.record_campaign_message(
            message("worker-to-worker", "Do not route this").model_copy(
                update={"recipient_task_id": second_worker.operation_id}
            )
        )
    with pytest.raises(ValueError, match="human may message only"):
        store.record_campaign_message(
            CampaignMessageRecord(
                message_id="human-to-worker",
                campaign_id=campaign.campaign_id,
                sender_role="human",
                authorized_by=campaign.authorized_by,
                recipient_task_id=first_worker.operation_id,
                body="This must go through the orchestrator.",
                created_at=store.now(),
            )
        )

    delivery_ids = [
        item.message_id
        for item in store.pending_campaign_messages(campaign.campaign_id, root.operation_id)
    ]
    assert delivery_ids == [first.message_id, second.message_id]
    before = store.campaign(campaign.campaign_id)
    assert before is not None
    wake = _turn(
        store,
        before,
        operation_id="mail-wake",
        role="orchestrator",
        parent_operation_id=root.operation_id,
        wake_cause="message",
        session_id="orchestrator-session",
    )
    store.create_campaign_message_wake_task(
        wake,
        role="orchestrator",
        recipient_task_id=root.operation_id,
        message_ids=delivery_ids,
    )

    assert store.pending_campaign_messages(campaign.campaign_id, root.operation_id) == []
    claimed = store.campaign_messages(campaign.campaign_id)
    assert {item.delivery_operation_id for item in claimed} == {wake.operation_id}
    assert store.campaign(campaign.campaign_id).invocations_used == before.invocations_used + 1  # type: ignore[union-attr]
    assert (
        store.create_campaign_message_wake_task(
            wake.model_copy(update={"operation_id": "duplicate-mail-wake"}),
            role="orchestrator",
            recipient_task_id=root.operation_id,
            message_ids=delivery_ids,
        )
        is None
    )
    assert store.campaign(campaign.campaign_id).invocations_used == before.invocations_used + 1  # type: ignore[union-attr]


def test_concurrent_campaign_mail_claim_has_one_paid_winner_and_one_benign_loser(
    tmp_path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)
    message = store.record_campaign_message(
        CampaignMessageRecord(
            message_id="message",
            campaign_id=campaign.campaign_id,
            sender_role="human",
            authorized_by=campaign.authorized_by,
            recipient_task_id=root.operation_id,
            body="New direction",
            created_at=store.now(),
        )
    )
    meter_before = store.campaign_budget_meter(campaign.campaign_id)
    barrier = Barrier(2)

    def claim(operation_id: str) -> AgentTaskRecord | None:
        contender = AppStore(store.path)
        wake = _turn(
            contender,
            campaign,
            operation_id=operation_id,
            role="orchestrator",
            parent_operation_id=root.operation_id,
            wake_cause="message",
            session_id="orchestrator-session",
        )
        barrier.wait()
        return contender.create_campaign_message_wake_task(
            wake,
            role="orchestrator",
            recipient_task_id=root.operation_id,
            message_ids=[message.message_id],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ["mail-wake-one", "mail-wake-two"]))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert len([result for result in results if result is None]) == 1
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == (
        meter_before.invocations_used + 1
    )
    assert store.pending_campaign_messages(campaign.campaign_id, root.operation_id) == []
    claimed = store.campaign_messages(campaign.campaign_id)
    assert claimed[0].delivery_operation_id == winners[0].operation_id
    assert len(store.campaign_tasks(campaign.campaign_id)) == 2

    with pytest.raises(ValueError, match="missing message"):
        store.create_campaign_message_wake_task(
            _turn(
                store,
                campaign,
                operation_id="missing-message-wake",
                role="orchestrator",
                parent_operation_id=root.operation_id,
                wake_cause="message",
                session_id="orchestrator-session",
            ),
            role="orchestrator",
            recipient_task_id=root.operation_id,
            message_ids=["missing-message"],
        )


def test_command_start_and_exit_are_separate_durable_events(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    campaign, root = _campaign(store)

    started = store.start_agent_command(
        operation_id=root.operation_id,
        command_id="command-one",
        campaign_id=campaign.campaign_id,
        verb="spawn",
        idempotency_key="spawn-one",
        payload={"planned_worker_id": "worker-one"},
    )
    assert started.exited_at is None
    assert started.status is None
    assert [item.command_phase for item in store.agent_task_events(root.operation_id)] == ["start"]

    finished = store.finish_agent_command(
        started.command_id,
        status="ok",
        payload={"result": {"worker_id": "worker-one"}},
        message="Spawn finished.",
    )
    assert finished.exited_at is not None
    assert finished.status == "ok"
    assert [item.command_phase for item in store.agent_task_events(root.operation_id)] == [
        "start",
        "exit",
    ]
    assert AppStore(store.path).agent_command("command-one") == finished

    replay = store.start_agent_command(
        operation_id=root.operation_id,
        command_id="a-different-request-id",
        campaign_id=campaign.campaign_id,
        verb="spawn",
        idempotency_key="spawn-one",
        payload={"planned_worker_id": "must-not-replace-the-first-record"},
    )
    assert replay == finished
    assert len(store.agent_task_events(root.operation_id)) == 2
