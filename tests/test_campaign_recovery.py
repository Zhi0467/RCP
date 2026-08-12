from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.runs.campaign import CampaignRunRequest, CampaignStartRequest
from rcp.runs.campaign_recovery import (
    CampaignOrchestratorTerminalFailure,
    reconcile_campaign_task_settlement,
    reconcile_due_campaign_recoveries,
    schedule_report_reconciliation,
)
from rcp.storage import AppStore, ProjectRecord

from .helpers import fabricated_authorizer, wait_for_task


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _store(tmp_path: Path) -> AppStore:
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
    return store


def _start(tasks: BackgroundAgentTasks, *, operation_id: str = "root"):
    return tasks.start_campaign(
        "project",
        CampaignStartRequest(invocation_ceiling=4, run_truth_scope=["repo"]),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id=operation_id,
    )


def _install_recovery_callback(tasks: BackgroundAgentTasks) -> None:
    tasks.on_campaign_task_settled = lambda campaign, request, execution: (
        reconcile_campaign_task_settlement(tasks, campaign, request, execution)
    )


def _wait_for_recovery(store: AppStore, recovery_id: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        recovery = store.campaign_recovery(recovery_id)
        if recovery is not None:
            return recovery
        time.sleep(0.01)
    raise AssertionError(f"campaign recovery did not appear: {recovery_id}")


def _recovery_delay_seconds(recovery) -> int:
    assert recovery.next_attempt_at is not None
    return round(
        (
            datetime.fromisoformat(recovery.next_attempt_at)
            - datetime.fromisoformat(recovery.updated_at)
        ).total_seconds()
    )


def test_transient_orchestrator_failure_retries_exact_session_without_spend(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()
    observed: list[tuple[str, str | None]] = []

    async def stream(_project_id, _kind, request, execution):
        observed.append((execution.continuation, request.session_id))
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="session-1"))
            yield _sse(AgentEvent(event="error", text="temporary provider failure"))
            return
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    campaign, root = _start(tasks)
    root = wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    assert recovery.retry_mode == "exact"
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 1

    assert (
        reconcile_due_campaign_recoveries(
            tasks,
            reconcile_report=lambda _: True,
            as_of=recovery.next_attempt_at,
        )
        == 1
    )
    admitted = store.campaign_recovery("task:root")
    assert admitted is not None and admitted.admitted_operation_id is not None
    child = wait_for_task(store, admitted.admitted_operation_id, expect="succeeded")
    assert child.native_session_id == root.native_session_id == "session-1"
    assert child.stage_root == root.stage_root == str(stage)
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 1
    assert observed == [("fresh", None), ("retry", "session-1")]


def test_due_recovery_adopts_existing_human_retry_without_spawning_or_deferring(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        if execution.continuation == "fresh":
            yield _sse(AgentEvent(event="error", text="provider unavailable"))
        else:
            yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    _, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    human_child = tasks.retry(root.operation_id)
    wait_for_task(store, human_child.operation_id, expect="succeeded")

    def unexpected_retry(_operation_id):
        raise AssertionError("automatic reconciliation must adopt the existing child")

    monkeypatch.setattr(tasks, "retry", unexpected_retry)
    assert (
        reconcile_due_campaign_recoveries(
            tasks,
            reconcile_report=lambda _: True,
            as_of=recovery.next_attempt_at,
        )
        == 1
    )
    admitted = store.campaign_recovery("task:root")
    assert admitted is not None
    assert admitted.status == "admitted"
    assert admitted.attempts == 1
    assert admitted.admitted_operation_id == human_child.operation_id
    assert admitted.next_attempt_at is None


def test_automatic_recovery_adopts_human_child_that_wins_admission_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        if execution.continuation == "fresh":
            yield _sse(AgentEvent(event="error", text="provider unavailable"))
        else:
            yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    _, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    real_retry = tasks.retry
    automatic_entered = threading.Event()
    human_admitted = threading.Event()

    def racing_automatic_retry(operation_id):
        automatic_entered.set()
        assert human_admitted.wait(5)
        return real_retry(operation_id)

    monkeypatch.setattr(tasks, "retry", racing_automatic_retry)
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            reconcile_due_campaign_recoveries(
                tasks,
                reconcile_report=lambda _: True,
                as_of=recovery.next_attempt_at,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=reconcile)
    thread.start()
    assert automatic_entered.wait(5)
    human_child = real_retry(root.operation_id)
    human_admitted.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    wait_for_task(store, human_child.operation_id, expect="succeeded")

    admitted = store.campaign_recovery("task:root")
    assert admitted is not None
    assert admitted.status == "admitted"
    assert admitted.attempts == 1
    assert admitted.admitted_operation_id == human_child.operation_id
    assert admitted.next_attempt_at is None


def test_precheckpoint_failure_retries_clean_orchestrator_session_and_survives_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()
    observed: list[str | None] = []

    async def stream(_project_id, _kind, request, execution):
        observed.append(request.session_id)
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="error", text="network unavailable"))
            return
        yield _sse(AgentEvent(event="session", session_id="clean-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    campaign, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    pending = _wait_for_recovery(store, "task:root")
    assert pending.retry_mode == "clean"

    restarted = BackgroundAgentTasks(AppStore(store.path), stream)
    assert len(restarted.store.due_campaign_recoveries(as_of=pending.next_attempt_at)) == 1
    assert (
        reconcile_due_campaign_recoveries(
            restarted,
            reconcile_report=lambda _: True,
            as_of=pending.next_attempt_at,
        )
        == 1
    )
    admitted = restarted.store.campaign_recovery("task:root")
    assert admitted is not None and admitted.admitted_operation_id is not None
    child = wait_for_task(restarted.store, admitted.admitted_operation_id, expect="succeeded")
    assert child.native_session_id == "clean-session"
    assert observed == [None, None]
    assert restarted.store.campaign_budget_meter(campaign.campaign_id).invocations_used == 1


def test_worker_failure_never_becomes_campaign_verdict(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, _kind, request, execution):
        execution.checkpoint_stage("", str(tmp_path))
        yield _sse(AgentEvent(event="session", session_id=f"session-{request.role}"))
        if request.role == "worker":
            yield _sse(AgentEvent(event="error", text="worker failed"))
        else:
            yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    campaign, root = _start(tasks)
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    worker = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="worker",
            control_node_id="exp/check",
        ),
        parent_operation_id=root.operation_id,
        operation_id="worker",
    )
    wait_for_task(store, worker.operation_id, expect="failed")
    current = store.campaign(campaign.campaign_id)
    assert current is not None and current.status == "running" and current.ending is None
    assert store.campaign_recovery("task:worker") is None


def test_session_limit_uses_clean_orchestrator_retry_even_after_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="limited-session"))
        yield _sse(AgentEvent(event="error", text="provider session limit reached"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    _, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    assert recovery.failure_kind == "session_limit"
    assert recovery.retry_mode == "clean"


def test_repeated_provider_failures_share_one_bounded_allocation_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        yield _sse(AgentEvent(event="error", text="provider unavailable"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    campaign, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    assert recovery.attempts == 0
    assert _recovery_delay_seconds(recovery) == 120

    for expected_attempt, expected_consumed, expected_delay in (
        (2, 1, 240),
        (3, 2, 480),
        (4, 3, None),
    ):
        expected_status = "exhausted" if expected_delay is None else "pending"
        reconcile_due_campaign_recoveries(
            tasks,
            reconcile_report=lambda _: True,
            as_of=recovery.next_attempt_at,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            recovery = store.campaign_recovery("task:root")
            assert recovery is not None
            current = store.agent_task(recovery.operation_id or "")
            if (
                recovery.attempts == expected_consumed
                and recovery.status == expected_status
                and current is not None
                and current.attempt == expected_attempt
                and current.status == "failed"
            ):
                break
            time.sleep(0.01)
        else:
            raise AssertionError(f"campaign recovery attempt {expected_attempt} did not fail")
        assert recovery.attempts == expected_consumed
        if expected_delay is None:
            assert recovery.status == "exhausted"
            assert recovery.next_attempt_at is None
        else:
            assert recovery.status == "pending"
            assert _recovery_delay_seconds(recovery) == expected_delay

    assert recovery.recovery_id == "task:root"
    assert recovery.attempts == 3
    assert recovery.status == "exhausted"
    current_campaign = store.campaign(campaign.campaign_id)
    assert current_campaign is not None
    assert current_campaign.status == "running"
    assert current_campaign.ending is None
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 1


def test_admission_and_provider_failures_share_durable_allocation_attempt_cap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        yield _sse(AgentEvent(event="error", text="provider unavailable"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    _, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")

    retry = tasks.retry

    def fail_admission(_operation_id):
        raise RuntimeError("recovery admission unavailable")

    monkeypatch.setattr(tasks, "retry", fail_admission)
    reconcile_due_campaign_recoveries(
        tasks,
        reconcile_report=lambda _: True,
        as_of=recovery.next_attempt_at,
    )
    recovery = store.campaign_recovery("task:root")
    assert recovery is not None
    assert recovery.attempts == 1
    assert recovery.status == "pending"
    assert _recovery_delay_seconds(recovery) == 240

    monkeypatch.setattr(tasks, "retry", retry)
    restarted = BackgroundAgentTasks(AppStore(store.path), stream)
    _install_recovery_callback(restarted)
    assert len(restarted.store.due_campaign_recoveries(as_of=recovery.next_attempt_at)) == 1
    reconcile_due_campaign_recoveries(
        restarted,
        reconcile_report=lambda _: True,
        as_of=recovery.next_attempt_at,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        recovery = restarted.store.campaign_recovery("task:root")
        assert recovery is not None
        current = restarted.store.agent_task(recovery.operation_id or "")
        if (
            recovery.attempts == 2
            and recovery.status == "pending"
            and current is not None
            and current.status == "failed"
        ):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("mixed recovery provider attempt did not fail")
    assert recovery.status == "pending"
    assert _recovery_delay_seconds(recovery) == 480

    durable_store = AppStore(store.path)
    durable = durable_store.campaign_recovery("task:root")
    assert durable is not None
    assert durable.attempts == 2
    assert durable.status == "pending"
    assert durable.next_attempt_at == recovery.next_attempt_at

    durable_tasks = BackgroundAgentTasks(durable_store, stream)
    monkeypatch.setattr(durable_tasks, "retry", fail_admission)
    reconcile_due_campaign_recoveries(
        durable_tasks,
        reconcile_report=lambda _: True,
        as_of=durable.next_attempt_at,
    )
    exhausted = AppStore(store.path).campaign_recovery("task:root")
    assert exhausted is not None
    assert exhausted.attempts == 3
    assert exhausted.status == "exhausted"
    assert exhausted.next_attempt_at is None


def test_typed_structural_orchestrator_failure_fences_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="reportable-session"))
        raise CampaignOrchestratorTerminalFailure("typed structural failure")

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    campaign, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        fenced = store.campaign(campaign.campaign_id)
        if fenced is not None and fenced.status == "wrapping_up":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("typed failure did not fence the campaign")
    assert fenced.status == "wrapping_up"
    assert fenced.ending == "failed"
    assert fenced.error == "typed structural failure"
    assert store.campaign_recovery("task:root") is None

    receipts = store.agent_task_receipts(root.operation_id)
    typed = [item for item in receipts if item.category == "campaign_orchestrator_failure"]
    assert len(typed) == 1
    assert typed[0].payload["classification"] == "structural_unrecoverable"
    assert typed[0].payload["recoverable"] is False


def test_stopped_unavailable_recovery_closes_and_admits_report_in_one_pass(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if request.role == "orchestrator":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="session-1"))
            yield _sse(AgentEvent(event="error", text="provider unavailable"))
            return
        assert request.role == "report"
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    campaign, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    stopped = tasks.stop_campaign(campaign.campaign_id)
    assert stopped.status == "stopping"
    stage.rmdir()

    restarted = BackgroundAgentTasks(AppStore(store.path), stream)
    report_tasks: list[str] = []

    def reconcile_report(current):
        report = restarted.reconcile_campaign_report(
            current.campaign_id,
            request_factory=lambda wrapped: CampaignRunRequest(
                campaign_id=wrapped.campaign_id,
                role="report",
                ending=wrapped.ending,
            ),
        )
        assert report is not None
        report_tasks.append(report.operation_id)
        return True

    assert (
        reconcile_due_campaign_recoveries(
            restarted,
            reconcile_report=reconcile_report,
            as_of=recovery.next_attempt_at,
        )
        == 1
    )

    resolved = restarted.store.campaign_recovery("task:root")
    assert resolved is not None
    assert resolved.status == "admitted"
    assert resolved.attempts == 1
    assert resolved.admitted_operation_id is None
    task = restarted.store.agent_task(root.operation_id)
    assert task is not None
    assert task.can_retry is False
    assert [
        receipt.category for receipt in restarted.store.agent_task_receipts(root.operation_id)
    ].count("campaign_recovery_abandoned") == 1
    stopped = restarted.store.campaign(campaign.campaign_id)
    assert stopped is not None
    assert stopped.status == "wrapping_up"
    assert stopped.ending == "stopped"
    assert len(report_tasks) == 1
    report = wait_for_task(restarted.store, report_tasks[0], expect="succeeded")
    assert report.parent_operation_id == root.operation_id
    assert report.attempt == 1
    assert restarted.store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2
    assert [task.operation_id for task in restarted.store.campaign_tasks(campaign.campaign_id)] == [
        root.operation_id,
        report.operation_id,
    ]


def test_live_report_admission_failure_retries_from_durable_backoff(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, _ = _start(tasks)
    wait_for_task(store, "root", expect="succeeded")
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "completed")
    scheduled = schedule_report_reconciliation(
        tasks,
        wrapping,
        diagnostic="temporary report admission failure",
    )
    calls = 0

    def reconcile_report(_campaign):
        nonlocal calls
        calls += 1
        return calls > 1

    reconcile_due_campaign_recoveries(
        tasks,
        reconcile_report=reconcile_report,
        as_of=scheduled.next_attempt_at,
    )
    deferred = store.campaign_recovery(scheduled.recovery_id)
    assert deferred is not None
    assert deferred.status == "pending"
    assert deferred.attempts == 1
    assert deferred.next_attempt_at is not None

    reconcile_due_campaign_recoveries(
        tasks,
        reconcile_report=reconcile_report,
        as_of=deferred.next_attempt_at,
    )
    admitted = store.campaign_recovery(scheduled.recovery_id)
    assert admitted is not None
    assert admitted.status == "admitted"
    assert calls == 2


def test_report_admission_survives_attempt_cap_restart_and_admits_once(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if request.role == "orchestrator":
            execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="succeeded")
    wrapping = store.begin_campaign_wrapup(campaign.campaign_id, "completed")
    recovery = schedule_report_reconciliation(
        tasks,
        wrapping,
        diagnostic="temporary report admission failure",
    )

    for _ in range(4):
        assert (
            reconcile_due_campaign_recoveries(
                tasks,
                reconcile_report=lambda _: False,
                as_of=recovery.next_attempt_at,
            )
            == 1
        )
        current = store.campaign_recovery(recovery.recovery_id)
        assert current is not None
        recovery = current

    restarted = BackgroundAgentTasks(AppStore(store.path), stream)
    for _ in range(5):
        assert (
            reconcile_due_campaign_recoveries(
                restarted,
                reconcile_report=lambda _: False,
                as_of=recovery.next_attempt_at,
            )
            == 1
        )
        current = restarted.store.campaign_recovery(recovery.recovery_id)
        assert current is not None
        recovery = current

    assert recovery.attempts == 9
    assert recovery.max_attempts == 8
    assert recovery.status == "pending"
    assert recovery.next_attempt_at is not None
    assert restarted.store.campaign_budget_meter(campaign.campaign_id).invocations_used == 1

    report_tasks: list[str] = []

    def reconcile_report(current):
        report = restarted.reconcile_campaign_report(
            current.campaign_id,
            request_factory=lambda wrapped: CampaignRunRequest(
                campaign_id=wrapped.campaign_id,
                role="report",
                ending=wrapped.ending,
            ),
            operation_id="report",
        )
        assert report is not None
        report_tasks.append(report.operation_id)
        return True

    assert (
        reconcile_due_campaign_recoveries(
            restarted,
            reconcile_report=reconcile_report,
            as_of=recovery.next_attempt_at,
        )
        == 1
    )
    report = wait_for_task(restarted.store, "report", expect="succeeded")
    admitted = restarted.store.campaign_recovery(recovery.recovery_id)
    assert admitted is not None
    assert admitted.status == "admitted"
    assert admitted.attempts == 10
    assert admitted.next_attempt_at is None
    assert report_tasks == [report.operation_id]
    assert restarted.store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2
    assert [task.operation_id for task in restarted.store.campaign_tasks(campaign.campaign_id)] == [
        root.operation_id,
        report.operation_id,
    ]
    assert (
        reconcile_due_campaign_recoveries(
            restarted,
            reconcile_report=reconcile_report,
            as_of=restarted.store.now(),
        )
        == 0
    )
    assert report_tasks == [report.operation_id]


def test_report_admission_recovery_is_scoped_to_each_reauthorized_ending_cycle(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if request.role == "orchestrator":
            execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="succeeded")
    first_wrap = store.begin_campaign_wrapup(campaign.campaign_id, "exhausted")
    first = schedule_report_reconciliation(
        tasks,
        first_wrap,
        diagnostic="first report admission failure",
    )
    first = store.complete_campaign_recovery(first.recovery_id)
    assert first.status == "admitted"
    assert first.attempts == 1

    report_task = tasks.start_campaign_report(
        campaign.campaign_id,
        "exhausted",
        request_factory=lambda current: CampaignRunRequest(
            campaign_id=current.campaign_id,
            role="report",
            ending=current.ending,
        ),
        operation_id="first-exhaustion-report",
    )
    wait_for_task(store, report_task.operation_id, expect="succeeded")
    ended, _ = tasks.complete_campaign_report(
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="exhausted",
        candidate="<article><h1>First exhaustion</h1></article>",
    )
    assert ended.status == "needs_action"

    store.reauthorize_campaign(campaign.campaign_id, 2)
    second_wrap = store.begin_campaign_wrapup(campaign.campaign_id, "exhausted")
    second = schedule_report_reconciliation(
        tasks,
        second_wrap,
        diagnostic="second report admission failure",
    )

    assert second.recovery_id != first.recovery_id
    assert second.status == "pending"
    assert second.attempts == 0
    assert second.next_attempt_at is not None
    assert store.campaign_recovery(first.recovery_id) == first
    assert (
        store.campaign_control_recovery(
            campaign.campaign_id,
            None,
            ending="exhausted",
        )
        == second
    )

    deferred = store.defer_campaign_recovery(
        second.recovery_id,
        diagnostic="second report admission remains unavailable",
    )
    repeated = schedule_report_reconciliation(
        tasks,
        second_wrap,
        diagnostic="same second report cycle",
    )
    assert repeated.recovery_id == second.recovery_id
    assert repeated.status == "pending"
    assert repeated.attempts == deferred.attempts == 1
    assert repeated.next_attempt_at == deferred.next_attempt_at

    restarted = AppStore(store.path)
    assert restarted.campaign_recovery(first.recovery_id) == first
    assert restarted.campaign_recovery(second.recovery_id) == repeated
