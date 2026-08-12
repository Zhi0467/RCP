from __future__ import annotations

import asyncio
import threading

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.runs.campaign import CampaignRunRequest, CampaignStartRequest
from rcp.storage import (
    AppStore,
    CampaignBudgetExhausted,
    CampaignMessageRecord,
    CampaignNotRunning,
    ProjectRecord,
)

from .helpers import fabricated_authorizer, wait_for_task


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _start_request(**updates) -> CampaignStartRequest:
    return CampaignStartRequest(run_truth_scope=["repo"], **updates)


def _store(tmp_path) -> AppStore:
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


def test_background_campaign_wakes_keep_actor_role_and_all_spend_the_same_pot(tmp_path) -> None:
    store = _store(tmp_path)
    observed: list[tuple[str, str, str | None]] = []
    wake_admissions: list[tuple[str, str, str]] = []

    def admit_wake(record, role, cause):
        wake_admissions.append((record.operation_id, role, cause))
        return store.create_campaign_agent_task(record, role=role)

    async def stream(_project_id, kind, request, execution):
        assert kind == "campaign"
        assert isinstance(request, CampaignRunRequest)
        observed.append((execution.operation_id, request.role, request.wake_cause))
        if request.wake_cause is None:
            execution.checkpoint_stage("", f"/tmp/{execution.operation_id}-stage")
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or f"session-{execution.operation_id}",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=7, starting_instruction="Start here."),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    assert root.request["instruction"] == "Start here."
    assert "profile" not in root.request
    assert "authority" not in root.request
    worker = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="worker",
            control_node_id="exp/check",
            instruction="Run the bounded check.",
        ),
        parent_operation_id=root.operation_id,
        operation_id="worker",
    )
    worker = wait_for_task(store, worker.operation_id, expect="succeeded")
    worker_wake = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="worker",
            control_node_id="exp/check",
            wake_cause="graph_condition",
            session_id=worker.native_session_id,
        ),
        parent_operation_id=worker.operation_id,
        operation_id="worker-graph-wake",
        wake_admission=admit_wake,
    )
    worker_wake = wait_for_task(store, worker_wake.operation_id, expect="succeeded")
    orchestrator_wake = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="orchestrator",
            wake_cause="watcher",
            session_id=root.native_session_id,
        ),
        parent_operation_id=root.operation_id,
        operation_id="orchestrator-watcher-wake",
        wake_admission=admit_wake,
    )
    orchestrator_wake = wait_for_task(store, orchestrator_wake.operation_id, expect="succeeded")
    message = store.record_campaign_message(
        CampaignMessageRecord(
            message_id="human-message",
            campaign_id=campaign.campaign_id,
            sender_role="human",
            authorized_by=campaign.authorized_by,
            recipient_task_id=root.operation_id,
            body="Please also check the new observation.",
            created_at=store.now(),
        )
    )
    delivery = tasks.pending_campaign_mail(
        campaign_id=campaign.campaign_id,
        recipient_task_id=root.operation_id,
    )
    assert delivery.graph_authority == "none"
    assert delivery.message_ids == [message.message_id]
    message_wake = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="orchestrator",
            wake_cause="message",
            session_id=root.native_session_id,
        ),
        parent_operation_id=root.operation_id,
        operation_id="orchestrator-message-wake",
        mail_delivery=delivery,
    )
    message_wake = wait_for_task(store, message_wake.operation_id, expect="succeeded")

    assert observed == [
        ("root", "orchestrator", None),
        ("worker", "worker", None),
        ("worker-graph-wake", "worker", "graph_condition"),
        ("orchestrator-watcher-wake", "orchestrator", "watcher"),
        ("orchestrator-message-wake", "orchestrator", "message"),
    ]
    assert wake_admissions == [
        ("worker-graph-wake", "worker", "graph_condition"),
        ("orchestrator-watcher-wake", "orchestrator", "watcher"),
    ]
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 5
    assert store.campaign_invocation_role(worker_wake.operation_id) == "worker"
    assert store.campaign_invocation_role(orchestrator_wake.operation_id) == "orchestrator"
    assert store.campaign_invocation_role(message_wake.operation_id) == "orchestrator"
    assert root.dispatch_authority is not None
    assert root.dispatch_authority.profile == "orchestrator"
    assert root.dispatch_authority.task_contract == "orchestrate"
    assert root.dispatch_authority.scope.run_truth_scope == ["repo"]
    assert root.dispatch_authority.scope.campaign_id == campaign.campaign_id
    assert worker.dispatch_authority is not None
    assert worker.dispatch_authority.profile == "ordinary"
    assert worker.dispatch_authority.task_contract == "work_auto"
    assert worker.dispatch_authority.scope == root.dispatch_authority.scope
    assert worker_wake.dispatch_authority == worker.dispatch_authority
    assert orchestrator_wake.dispatch_authority == root.dispatch_authority
    assert message_wake.dispatch_authority == root.dispatch_authority
    assert store.pending_campaign_messages(campaign.campaign_id, root.operation_id) == []
    continuation_receipts = {
        task.operation_id: store.agent_task_receipts(task.operation_id)[0].payload[
            "continuation_cause"
        ]
        for task in (worker_wake, orchestrator_wake, message_wake)
    }
    assert continuation_receipts == {
        "worker-graph-wake": "graph_condition_wake",
        "orchestrator-watcher-wake": "watcher_wake",
        "orchestrator-message-wake": "message_wake",
    }


def test_human_orchestrator_continuation_reuses_root_actor_binding_and_one_budget_unit(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "campaign-root-stage"
    stage.mkdir()
    observed: list[tuple[str, str, str | None]] = []

    async def stream(_project_id, _kind, request, execution):
        observed.append((execution.operation_id, execution.continuation, request.session_id))
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="orchestrator-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=4),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    used_before = store.campaign_budget_meter(campaign.campaign_id).invocations_used

    with pytest.raises(ValueError, match="saved native session"):
        tasks.start_campaign_turn(
            campaign.campaign_id,
            CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="orchestrator",
                session_id="another-session",
            ),
            parent_operation_id=root.operation_id,
            operation_id="mismatched-continuation",
        )
    assert store.agent_task("mismatched-continuation") is None
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == used_before

    continued = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="orchestrator",
            instruction="Continue after the human reauthorized the campaign.",
        ),
        parent_operation_id=root.operation_id,
        operation_id="human-continuation",
    )
    continued = wait_for_task(store, continued.operation_id, expect="succeeded")

    assert continued.request["actor_operation_id"] == root.operation_id
    assert continued.native_session_id == root.native_session_id == "orchestrator-session"
    assert continued.stage_host == root.stage_host
    assert continued.stage_root == root.stage_root == str(stage)
    assert continued.dispatch_authority == root.dispatch_authority
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == used_before + 1
    assert store.agent_task_continuation_cause(continued.operation_id) == "campaign_continuation"
    assert observed == [
        (root.operation_id, "fresh", None),
        (continued.operation_id, "campaign_continuation", "orchestrator-session"),
    ]


def test_interrupted_orchestrator_can_retry_after_spawning_a_worker(tmp_path) -> None:
    store = _store(tmp_path)
    root_stage = tmp_path / "campaign-root-stage"
    root_stage.mkdir()
    worker_stage = tmp_path / "campaign-worker-stage"
    worker_stage.mkdir()
    worker_finished = threading.Event()

    async def stream(_project_id, _kind, request, execution):
        if execution.operation_id == "root" and execution.continuation == "fresh":
            execution.checkpoint_stage("", str(root_stage))
            yield _sse(AgentEvent(event="session", session_id="orchestrator-session"))
            worker = tasks.start_campaign_turn(
                "campaign",
                CampaignRunRequest(
                    campaign_id="campaign",
                    role="worker",
                    control_node_id="exp/check",
                    instruction="Run the bounded check.",
                ),
                parent_operation_id="root",
                operation_id="worker",
            )
            wait_for_task(store, worker.operation_id, expect="succeeded")
            worker_finished.set()
            while not execution.control.pause_requested.is_set():
                await asyncio.sleep(0.01)
        if request.role == "worker":
            execution.checkpoint_stage("", str(worker_stage))
            yield _sse(AgentEvent(event="session", session_id="worker-session"))
        elif execution.continuation == "retry":
            assert request.session_id == "orchestrator-session"
            yield _sse(AgentEvent(event="session", session_id=request.session_id))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=4),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    assert worker_finished.wait(timeout=2)
    store.interrupt_active_agent_tasks()
    tasks.shutdown(timeout=2)

    interrupted = store.agent_task(root.operation_id)
    worker = store.agent_task("worker")
    assert interrupted is not None and interrupted.status == "interrupted"
    assert worker is not None and worker.status == "succeeded"
    used_before = store.campaign_budget_meter(campaign.campaign_id).invocations_used

    recovered = tasks.retry(interrupted.operation_id)
    recovered = wait_for_task(store, recovered.operation_id, expect="succeeded")

    assert recovered.parent_operation_id == interrupted.operation_id
    assert recovered.request["actor_operation_id"] == interrupted.operation_id
    assert recovered.native_session_id == interrupted.native_session_id
    assert recovered.stage_host == interrupted.stage_host
    assert recovered.stage_root == interrupted.stage_root == str(root_stage)
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == used_before


def test_campaign_continuation_cannot_replace_its_saved_native_session(tmp_path) -> None:
    store = _store(tmp_path)
    root_stage = tmp_path / "campaign-root-stage"
    root_stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(root_stage))
            session_id = "saved-session"
        elif execution.continuation == "retry":
            session_id = "saved-session"
        else:
            session_id = "different-session"
        yield _sse(AgentEvent(event="session", session_id=session_id))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=4),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    root_before = store.agent_task(root.operation_id)

    continuation = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest(campaign_id=campaign.campaign_id, role="orchestrator"),
        parent_operation_id=root.operation_id,
        operation_id="continuation",
    )
    continuation = wait_for_task(store, continuation.operation_id, expect="failed")

    assert store.agent_task(root.operation_id) == root_before
    assert continuation.native_session_id == "saved-session"
    assert continuation.stage_root == str(root_stage)
    assert "conflicts with its saved RCP checkpoint" in (continuation.error or "")
    assert "native_agent_checkpoint" not in {
        receipt.category for receipt in store.agent_task_receipts(continuation.operation_id)
    }
    binding = store.campaign_actor_binding(continuation.operation_id)
    assert binding.current_operation_id == continuation.operation_id
    assert binding.native_session_id == "saved-session"
    used_before_retry = store.campaign_budget_meter(campaign.campaign_id).invocations_used

    recovered = tasks.retry(continuation.operation_id)
    recovered = wait_for_task(store, recovered.operation_id, expect="succeeded")

    assert recovered.native_session_id == "saved-session"
    assert recovered.stage_root == str(root_stage)
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == used_before_retry


def test_stop_persists_before_new_admission_but_current_turn_finishes_and_reports(tmp_path) -> None:
    store = _store(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    async def stream(_project_id, kind, request, execution):
        assert kind == "campaign"
        assert isinstance(request, CampaignRunRequest)
        if request.role != "report":
            execution.checkpoint_stage("", str(tmp_path / "campaign-stage"))
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        yield _sse(
            AgentEvent(event="session", session_id=request.session_id or "orchestrator-session")
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=3),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    assert entered.wait(timeout=2)

    stopped = tasks.stop_campaign(campaign.campaign_id)
    assert stopped.status == "stopping"
    with pytest.raises(CampaignNotRunning, match="not admitting new work"):
        tasks.start_campaign_turn(
            campaign.campaign_id,
            CampaignRunRequest(campaign_id=campaign.campaign_id, role="orchestrator"),
            parent_operation_id=root.operation_id,
            operation_id="after-stop",
        )
    assert store.agent_task(root.operation_id).status == "running"  # type: ignore[union-attr]

    release.set()
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    assert root.status == "succeeded"
    report_task = tasks.start_campaign_report(
        campaign.campaign_id,
        "stopped",
        request_factory=lambda current: CampaignRunRequest(
            campaign_id=current.campaign_id,
            role="report",
            ending=current.ending,
        ),
        operation_id="stop-report",
    )
    report_task = wait_for_task(store, report_task.operation_id, expect="succeeded")
    assert report_task.parent_operation_id == root.operation_id
    assert report_task.request["actor_operation_id"] == root.operation_id
    assert report_task.native_session_id == root.native_session_id == "orchestrator-session"
    assert report_task.stage_root == root.stage_root
    assert report_task.request["workflow_ids"] == []
    assert report_task.request["skill_ids"] == ["campaign-report"]
    assert report_task.request["invoked_skill_ids"] == ["campaign-report"]
    assert report_task.request["resolved_skill_packages"] == [
        {"id": "campaign-report", "kind": "skill", "version": "1.0.0"}
    ]
    ended, report = tasks.complete_campaign_report(
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="stopped",
        candidate="<article><h1>Stopped by the human</h1></article>",
    )

    assert ended.status == "stopped"
    assert ended.stop_requested_at == stopped.stop_requested_at
    assert store.campaign_report(report.report_id) == report


def test_background_exhaustion_callback_leads_to_needs_action_only_after_report(tmp_path) -> None:
    store = _store(tmp_path)
    exhausted: list[str] = []

    async def stream(_project_id, _kind, request, execution):
        if request.role != "report":
            execution.checkpoint_stage("", str(tmp_path / "campaign-stage"))
        yield _sse(
            AgentEvent(event="session", session_id=request.session_id or "orchestrator-session")
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(
        store,
        stream,
        on_campaign_admission_exhausted=lambda campaign: exhausted.append(campaign.campaign_id),
    )
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=2),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")

    with pytest.raises(CampaignBudgetExhausted):
        tasks.start_campaign_turn(
            campaign.campaign_id,
            CampaignRunRequest(campaign_id=campaign.campaign_id, role="orchestrator"),
            operation_id="over-budget",
        )
    assert exhausted == [campaign.campaign_id]
    assert store.campaign(campaign.campaign_id).status == "wrapping_up"  # type: ignore[union-attr]
    assert any(
        "report unit remains reserved" in event.message
        for event in store.agent_task_events(root.operation_id)
    )

    report_task = tasks.start_campaign_report(
        campaign.campaign_id,
        "exhausted",
        request_factory=lambda current: CampaignRunRequest(
            campaign_id=current.campaign_id,
            role="report",
            ending=current.ending,
        ),
        operation_id="exhaustion-report",
    )
    wait_for_task(store, report_task.operation_id, expect="succeeded")
    ended, _ = tasks.complete_campaign_report(
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="exhausted",
        candidate="<article><h1>Budget exhausted</h1></article>",
    )
    assert ended.status == "needs_action"


def test_report_reconciliation_is_restart_safe_and_allocates_only_once(tmp_path) -> None:
    store = _store(tmp_path)
    release_report = threading.Event()

    async def stream(_project_id, _kind, request, execution):
        if request.role == "orchestrator":
            execution.checkpoint_stage("", str(tmp_path / "campaign-stage"))
            yield _sse(AgentEvent(event="session", session_id="orchestrator-session"))
        else:
            while not release_report.is_set():
                await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=3),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    store.begin_campaign_wrapup(campaign.campaign_id, "completed")
    request_factory = lambda current: CampaignRunRequest(  # noqa: E731
        campaign_id=current.campaign_id,
        role="report",
        ending=current.ending,
    )

    first = tasks.reconcile_campaign_report(
        campaign.campaign_id,
        request_factory=request_factory,
        operation_id="report-one",
    )
    raced = tasks.reconcile_campaign_report(
        campaign.campaign_id,
        request_factory=request_factory,
        operation_id="report-two",
    )

    assert first is not None and raced is not None
    assert first.operation_id == raced.operation_id == "report-one"
    assert store.agent_task("report-two") is None
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2
    assert store.campaigns_awaiting_report() == [store.campaign(campaign.campaign_id)]
    release_report.set()
    wait_for_task(store, first.operation_id, expect="succeeded")


def test_campaign_wake_cannot_relabel_a_worker_as_the_orchestrator(tmp_path) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, _kind, request, execution):
        if request.wake_cause is None:
            execution.checkpoint_stage("", f"/tmp/{execution.operation_id}-stage")
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or f"session-{execution.operation_id}",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=5),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
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
    worker = wait_for_task(store, worker.operation_id, expect="succeeded")
    admission_called = False

    def admit(record, role, _cause):
        nonlocal admission_called
        admission_called = True
        return store.create_campaign_agent_task(record, role=role)

    with pytest.raises(ValueError, match="actor|role"):
        tasks.start_campaign_turn(
            campaign.campaign_id,
            CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="orchestrator",
                wake_cause="watcher",
                session_id=worker.native_session_id,
            ),
            parent_operation_id=worker.operation_id,
            operation_id="relabelled-wake",
            wake_admission=admit,
        )
    assert not admission_called
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 2


@pytest.mark.parametrize(
    ("ending", "wake_cause", "expected_error"),
    [
        ("exhausted", "message", CampaignBudgetExhausted),
        ("exhausted", "watcher", CampaignBudgetExhausted),
        ("stopped", "message", CampaignNotRunning),
        ("stopped", "watcher", CampaignNotRunning),
    ],
)
def test_terminal_existing_actor_wake_fails_typed_without_task_or_spend(
    tmp_path,
    ending: str,
    wake_cause: str,
    expected_error: type[ValueError],
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "campaign-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.operation_id == "root":
            execution.checkpoint_stage("", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=2 if ending == "exhausted" else 4),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")

    mail_delivery = None
    wake_admission = None
    if wake_cause == "message":
        message = store.record_campaign_message(
            CampaignMessageRecord(
                message_id="pending-message",
                campaign_id=campaign.campaign_id,
                sender_role="human",
                authorized_by=campaign.authorized_by,
                recipient_task_id=root.operation_id,
                body="Retain this after terminal admission is refused.",
                created_at=store.now(),
            )
        )
        mail_delivery = tasks.pending_campaign_mail(
            campaign_id=campaign.campaign_id,
            recipient_task_id=root.operation_id,
        )
        assert mail_delivery.message_ids == [message.message_id]
    else:

        def admit_watcher(record, role, _cause):
            return store.create_campaign_agent_task(record, role=role)

        wake_admission = admit_watcher

    if ending == "stopped":
        stopped = tasks.stop_campaign(campaign.campaign_id)
        assert stopped.status == "wrapping_up"
        assert stopped.ending == "stopped"

    before_meter = store.campaign_budget_meter(campaign.campaign_id)
    before_tasks = store.campaign_tasks(campaign.campaign_id)
    with pytest.raises(expected_error):
        tasks.start_campaign_turn(
            campaign.campaign_id,
            CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="orchestrator",
                wake_cause=wake_cause,
                session_id=root.native_session_id,
            ),
            parent_operation_id=root.operation_id,
            operation_id="terminal-wake",
            mail_delivery=mail_delivery,
            wake_admission=wake_admission,
        )

    assert store.agent_task("terminal-wake") is None
    assert store.campaign_budget_meter(campaign.campaign_id) == before_meter
    assert store.campaign_tasks(campaign.campaign_id) == before_tasks
    if wake_cause == "message":
        assert store.pending_campaign_messages(campaign.campaign_id, root.operation_id) == list(
            mail_delivery.messages
        )


def test_new_worker_cannot_inherit_the_orchestrator_native_session(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "campaign-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.operation_id == "root":
            execution.checkpoint_stage("", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=4),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    before_meter = store.campaign_budget_meter(campaign.campaign_id)
    before_tasks = store.campaign_tasks(campaign.campaign_id)

    with pytest.raises(ValueError):
        tasks.start_campaign_turn(
            campaign.campaign_id,
            CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="worker",
                control_node_id="exp/check",
                session_id=root.native_session_id,
            ),
            parent_operation_id=root.operation_id,
            operation_id="worker-with-orchestrator-session",
        )

    assert store.agent_task("worker-with-orchestrator-session") is None
    assert store.campaign_budget_meter(campaign.campaign_id) == before_meter
    assert store.campaign_tasks(campaign.campaign_id) == before_tasks


def test_same_campaign_actor_cannot_have_two_concurrent_wakes(tmp_path) -> None:
    store = _store(tmp_path)
    wake_entered = threading.Event()
    release_wake = threading.Event()

    async def stream(_project_id, _kind, request, execution):
        if request.wake_cause is None:
            execution.checkpoint_stage("", f"/tmp/{execution.operation_id}-stage")
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or f"session-{execution.operation_id}",
            )
        )
        if request.wake_cause is not None:
            wake_entered.set()
            while not release_wake.is_set():
                await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=6),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
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
    worker = wait_for_task(store, worker.operation_id, expect="succeeded")

    def admit(record, role, _cause):
        return store.create_campaign_agent_task(record, role=role)

    first_wake = tasks.start_campaign_turn(
        campaign.campaign_id,
        CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="worker",
            control_node_id="exp/check",
            wake_cause="graph_condition",
            session_id=worker.native_session_id,
        ),
        parent_operation_id=worker.operation_id,
        operation_id="first-wake",
        wake_admission=admit,
    )
    assert wake_entered.wait(timeout=2)
    try:
        with pytest.raises(ValueError, match="active|concurrent|wake"):
            tasks.start_campaign_turn(
                campaign.campaign_id,
                CampaignRunRequest(
                    campaign_id=campaign.campaign_id,
                    role="worker",
                    control_node_id="exp/check",
                    wake_cause="watcher",
                    session_id=worker.native_session_id,
                ),
                parent_operation_id=worker.operation_id,
                operation_id="second-wake",
                wake_admission=admit,
            )
    finally:
        release_wake.set()
    wait_for_task(store, first_wake.operation_id, expect="succeeded")
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == 3
    assert store.agent_task("second-wake") is None


def test_report_recovery_reuses_the_reserved_invocation_instead_of_spending_again(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "report-recovery-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if request.role != "report":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="report-session"))
        elif execution.operation_id == "report-attempt":
            yield _sse(AgentEvent(event="error", text="Report transport interrupted."))
            return
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=2),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    report_task = tasks.start_campaign_report(
        campaign.campaign_id,
        "completed",
        request_factory=lambda current: CampaignRunRequest(
            campaign_id=current.campaign_id,
            role="report",
            ending=current.ending,
        ),
        operation_id="report-attempt",
    )
    report_task = wait_for_task(store, report_task.operation_id, expect="failed")
    used_before_recovery = store.campaign_budget_meter(campaign.campaign_id).invocations_used

    recovered = tasks.retry(report_task.operation_id)
    recovered = wait_for_task(store, recovered.operation_id, expect="succeeded")

    assert recovered.parent_operation_id == report_task.operation_id
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == (
        used_before_recovery
    )
    assert store.campaign(campaign.campaign_id).status == "wrapping_up"  # type: ignore[union-attr]


def test_failed_campaign_report_recovery_remains_in_its_reserved_allocation(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "failed-report-recovery-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if request.role != "report":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="failed-report-session"))
        elif execution.operation_id == "failed-report-attempt":
            yield _sse(AgentEvent(event="error", text="Report transport interrupted."))
            return
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=2),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    fenced = store.fence_campaign_terminal_failure(
        root.operation_id,
        diagnostic="The orchestrator failed structurally after its checkpoint.",
    )
    assert fenced is not None and fenced.status == "wrapping_up" and fenced.ending == "failed"
    report_task = tasks.start_campaign_report(
        campaign.campaign_id,
        "failed",
        request_factory=lambda current: CampaignRunRequest(
            campaign_id=current.campaign_id,
            role="report",
            ending=current.ending,
        ),
        operation_id="failed-report-attempt",
    )
    report_task = wait_for_task(store, report_task.operation_id, expect="failed")
    used_before_recovery = store.campaign_budget_meter(campaign.campaign_id).invocations_used

    recovered = tasks.retry(report_task.operation_id)
    recovered = wait_for_task(store, recovered.operation_id, expect="succeeded")

    assert recovered.parent_operation_id == report_task.operation_id
    assert recovered.native_session_id == report_task.native_session_id == root.native_session_id
    assert recovered.stage_root == report_task.stage_root == root.stage_root
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == (
        used_before_recovery
    )


@pytest.mark.parametrize("failure_kind", ["before-checkpoint", "session-limit"])
def test_orchestrator_clean_retry_keeps_actor_stage_authority_and_paid_allocation(
    tmp_path,
    failure_kind,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            if failure_kind == "session-limit":
                execution.checkpoint_stage("", str(stage))
                yield _sse(AgentEvent(event="session", session_id="spent-session"))
                yield _sse(
                    AgentEvent(
                        event="error",
                        text="You have 0 weighted tokens left in this session",
                    )
                )
            else:
                yield _sse(AgentEvent(event="error", text="Network connection failed."))
            return
        assert execution.continuation == "retry"
        assert request.role == "orchestrator"
        assert request.actor_operation_id == "root"
        assert request.session_id is None
        assert execution.stage_root == (str(stage) if failure_kind == "session-limit" else None)
        if execution.stage_root is None:
            execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="replacement-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=3),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
    root = wait_for_task(store, root.operation_id, expect="failed")
    used_before = store.campaign_budget_meter(campaign.campaign_id).invocations_used

    retried = tasks.retry(root.operation_id)
    retried = wait_for_task(store, retried.operation_id, expect="succeeded")

    assert retried.parent_operation_id == root.operation_id
    assert retried.attempt == root.attempt + 1
    assert retried.dispatch_authority == root.dispatch_authority
    assert retried.native_session_id == "replacement-session"
    assert retried.stage_root == str(stage)
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == used_before
    binding = store.campaign_actor_binding(root.operation_id)
    assert binding.actor_operation_id == root.operation_id
    assert binding.current_operation_id == retried.operation_id
    assert binding.native_session_id == "replacement-session"
    receipt = next(
        receipt
        for receipt in store.agent_task_receipts(retried.operation_id)
        if receipt.category == "campaign_orchestrator_clean_retry"
    )
    assert receipt.payload == {
        "classification": (
            "session_limit" if failure_kind == "session-limit" else "checkpoint_missing"
        ),
        "same_allocation": True,
        "actor_operation_id": "root",
        "retry_mode": "clean_native_session",
    }


def test_exhaustion_blocks_new_turns_but_does_not_cancel_the_turn_already_running(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    worker_entered = threading.Event()
    release_worker = threading.Event()

    async def stream(_project_id, _kind, request, execution):
        if request.role == "orchestrator":
            execution.checkpoint_stage("", str(tmp_path / "campaign-stage"))
            yield _sse(
                AgentEvent(
                    event="session",
                    session_id=request.session_id or "orchestrator-session",
                )
            )
        if request.role == "worker":
            worker_entered.set()
            while not release_worker.is_set():
                await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="done"))

    exhausted: list[str] = []
    tasks = BackgroundAgentTasks(
        store,
        stream,
        on_campaign_admission_exhausted=lambda campaign: exhausted.append(campaign.campaign_id),
    )
    campaign, root = tasks.start_campaign(
        "project",
        _start_request(invocation_ceiling=3),
        authorized_by=fabricated_authorizer(),
        campaign_id="campaign",
        operation_id="root",
    )
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
    assert worker_entered.wait(timeout=2)
    try:
        with pytest.raises(CampaignBudgetExhausted):
            tasks.start_campaign_turn(
                campaign.campaign_id,
                CampaignRunRequest(campaign_id=campaign.campaign_id, role="orchestrator"),
                parent_operation_id=root.operation_id,
                operation_id="after-exhaustion",
            )
        assert store.agent_task(worker.operation_id).status == "running"  # type: ignore[union-attr]
        assert store.agent_task("after-exhaustion") is None
        assert (
            tasks.reconcile_campaign_report(
                campaign.campaign_id,
                request_factory=lambda current: CampaignRunRequest(
                    campaign_id=current.campaign_id,
                    role="report",
                    ending=current.ending,
                ),
                operation_id="too-early-report",
            )
            is None
        )
    finally:
        release_worker.set()
    wait_for_task(store, worker.operation_id, expect="succeeded")
    assert exhausted == [campaign.campaign_id]

    report_task = tasks.reconcile_campaign_report(
        campaign.campaign_id,
        request_factory=lambda current: CampaignRunRequest(
            campaign_id=current.campaign_id,
            role="report",
            ending=current.ending,
        ),
        operation_id="exhaustion-report",
    )
    assert report_task is not None
    wait_for_task(store, report_task.operation_id, expect="succeeded")
    ended, _ = tasks.complete_campaign_report(
        campaign_id=campaign.campaign_id,
        operation_id=report_task.operation_id,
        ending="exhausted",
        candidate="<article><h1>Partial budget-exhausted report</h1></article>",
    )
    assert ended.status == "needs_action"
