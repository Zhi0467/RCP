from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from rcp.agents import AgentEvent
from rcp.agents.command_protocol import (
    FinishCommandRequest,
    MessageArguments,
    MessageCommandRequest,
    SpawnArguments,
    StatusArguments,
    StopCommandRequest,
    WatchGraphArguments,
    WatchGraphCommandRequest,
)
from rcp.background import BackgroundAgentTasks
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.models import Blocker, GraphState
from rcp.runs.campaign import (
    CampaignCommandContext,
    CampaignCommandDispatcher,
    CampaignCommandEffectResult,
    CampaignRunRequest,
)
from rcp.runs.campaign_delivery import record_campaign_message
from rcp.runs.campaign_effects import campaign_command_effects
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    CampaignRecord,
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    ProjectRecord,
)

from .helpers import fabricated_authorizer, wait_for_task

MAILBOX_ID = "a" * 32
CREDENTIAL = "b" * 64
_RUN_TRUTH_SCOPE = ["repo-a"]


def _campaign_authority(campaign_id: str, role: str) -> AgentDispatchAuthority:
    return AgentDispatchAuthority(
        profile="orchestrator" if role == "orchestrator" else "ordinary",
        task_contract="orchestrate" if role == "orchestrator" else "work_auto",
        scope=AgentDispatchScope(
            run_truth_scope=_RUN_TRUTH_SCOPE,
            campaign_id=campaign_id,
            patch_kind="work",
        ),
    )


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


async def _successful_stream(_project_id, _kind, _request, _execution):
    yield _sse(AgentEvent(event="done"))


def _setup_campaign(tmp_path) -> tuple[AppStore, CampaignRecord, AgentTaskRecord]:
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
    now = store.now()
    authorizer = fabricated_authorizer()
    request = CampaignRunRequest(
        campaign_id="campaign",
        role="orchestrator",
        actor_operation_id="root",
        provider="codex",
        run_on="local",
        run_truth_scope=_RUN_TRUTH_SCOPE,
    )
    return (
        store,
        *store.create_campaign_with_root_task(
            CampaignRecord(
                campaign_id="campaign",
                project_id="project",
                root_operation_id="root",
                status="queued",
                invocation_ceiling=8,
                authorized_by=authorizer,
                created_at=now,
                updated_at=now,
            ),
            AgentTaskRecord(
                operation_id="root",
                project_id="project",
                campaign_id="campaign",
                kind="campaign",
                status="succeeded",
                request=request.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
                status_message="orchestrator ready",
                authorized_by=authorizer,
                dispatch_authority=_campaign_authority("campaign", "orchestrator"),
            ),
        ),
    )


def _context(
    store: AppStore,
    campaign: CampaignRecord,
    task: AgentTaskRecord,
) -> CampaignCommandContext:
    current = store.campaign(campaign.campaign_id)
    stored_task = store.agent_task(task.operation_id)
    assert current is not None and stored_task is not None
    return CampaignCommandContext(
        campaign=current,
        task=stored_task,
        request=CampaignRunRequest.model_validate(stored_task.request),
    )


def _worker_request(
    context: CampaignCommandContext,
    arguments: SpawnArguments,
) -> CampaignRunRequest:
    return CampaignRunRequest(
        campaign_id=context.campaign.campaign_id,
        role="worker",
        provider="codex",
        run_on="local",
        run_truth_scope=_RUN_TRUTH_SCOPE,
        control_node_id=arguments.seat_node_id,
        instruction=arguments.instruction,
    )


def _create_worker(
    store: AppStore,
    campaign: CampaignRecord,
    parent: AgentTaskRecord,
    *,
    operation_id: str = "worker",
    status: str = "succeeded",
    native_session_id: str | None = None,
    stage_root: str | None = None,
) -> AgentTaskRecord:
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="worker",
        actor_operation_id=operation_id,
        provider="codex",
        run_on="local",
        run_truth_scope=_RUN_TRUTH_SCOPE,
        control_node_id="blk/check",
        instruction="Resolve the blocker.",
    )
    now = store.now()
    return store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status=status,
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message=f"{operation_id} {status}",
            parent_operation_id=parent.operation_id,
            native_session_id=native_session_id,
            stage_root=stage_root,
            authorized_by=campaign.authorized_by,
            dispatch_authority=_campaign_authority(campaign.campaign_id, "worker"),
        ),
        role="worker",
    )


def _create_worker_recovery(
    store: AppStore,
    campaign: CampaignRecord,
    parent: AgentTaskRecord,
    *,
    operation_id: str = "worker-recovery",
) -> AgentTaskRecord:
    request = CampaignRunRequest.model_validate(parent.request).model_copy(
        update={
            "actor_operation_id": "worker",
            "session_id": parent.native_session_id,
        }
    )
    now = store.now()
    return store.create_campaign_recovery_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="paused",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="latest paused attempt",
            attempt=parent.attempt + 1,
            parent_operation_id=parent.operation_id,
            native_session_id=parent.native_session_id,
            stage_host=parent.stage_host,
            stage_root=parent.stage_root,
            authorized_by=campaign.authorized_by,
            dispatch_authority=parent.dispatch_authority,
        )
    )


def _blocker_state(*, status: str = "open", revision: int = 1) -> GraphState:
    blocker = Blocker(
        id="blk/check",
        type="blocker",
        title="Check the result",
        description="Resolve this after checking the external result.",
        status=status,
    )
    return GraphState(revision=revision, nodes={blocker.id: blocker})


def _effects(store, background, *, state=None, on_watcher_ready=None):
    return campaign_command_effects(
        store=store,
        background=background,
        validate=lambda _context, _arguments: CampaignCommandEffectResult(),
        worker_request_factory=_worker_request,
        graph_state=lambda: state or _blocker_state(),
        execution_host="execution.example",
        on_watcher_ready=on_watcher_ready,
    )


@dataclass
class _RecordingBackground:
    store: AppStore
    paused: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)

    def pause_campaign_worker(
        self,
        operation_id: str,
        _campaign_id: str,
    ) -> AgentTaskRecord:
        self.paused.append(operation_id)
        task = self.store.agent_task(operation_id)
        assert task is not None
        return task

    def resume(self, operation_id: str) -> AgentTaskRecord:
        self.resumed.append(operation_id)
        task = self.store.agent_task(operation_id)
        assert task is not None
        return task


def test_status_and_controls_resolve_the_latest_canonical_worker_leaf(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    stage = tmp_path / "worker-stage"
    stage.mkdir()
    worker = _create_worker(
        store,
        campaign,
        root,
        status="paused",
        native_session_id="worker-session",
        stage_root=str(stage),
    )
    latest = _create_worker_recovery(store, campaign, worker)
    background = _RecordingBackground(store)
    effects = _effects(store, background)
    context = _context(store, campaign, root)

    status = effects.status(context, StatusArguments(worker_id=worker.operation_id))
    paused = effects.pause(context, worker.operation_id)

    assert status.result["campaign"]["status"] == "running"  # type: ignore[index]
    assert status.result["budget"]["report_units_reserved"] == 1  # type: ignore[index]
    assert status.result["worker"] == {
        "worker_id": worker.operation_id,
        "current_operation_id": latest.operation_id,
        "control_node_id": "blk/check",
        "status": "paused",
        "status_message": "latest paused attempt",
        "can_pause": False,
        "can_resume": True,
        "can_retry": True,
    }
    assert background.paused == [latest.operation_id]
    assert paused.result["current_operation_id"] == latest.operation_id
    assert "current worker task attempt" in (paused.message or "")


def test_resume_uses_the_latest_leaf_exact_session_without_spending_another_unit(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    stage = tmp_path / "worker-stage"
    stage.mkdir()
    worker = _create_worker(
        store,
        campaign,
        root,
        status="paused",
        native_session_id="worker-session",
        stage_root=str(stage),
    )
    background = BackgroundAgentTasks(store, _successful_stream)
    effects = _effects(store, background)
    before = store.campaign_budget_meter(campaign.campaign_id)

    outcome = effects.resume(_context(store, campaign, root), worker.operation_id)

    resumed = store.agent_task(str(outcome.result["current_operation_id"]))
    assert resumed is not None
    assert resumed.parent_operation_id == worker.operation_id
    assert resumed.native_session_id == "worker-session"
    assert resumed.stage_root == str(stage)
    assert resumed.request["session_id"] == "worker-session"
    assert store.campaign_budget_meter(campaign.campaign_id) == before
    wait_for_task(store, resumed.operation_id)


def test_spawn_uses_the_planned_id_and_command_task_parent_lineage(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    background = BackgroundAgentTasks(store, _successful_stream)
    effects = _effects(store, background)
    arguments = SpawnArguments(
        seat_node_id="blk/check",
        instruction="Resolve the blocker.",
    )
    before = store.campaign_budget_meter(campaign.campaign_id)

    outcome = effects.spawn(_context(store, campaign, root), arguments, "planned-worker")

    worker = store.agent_task("planned-worker")
    assert worker is not None
    assert outcome.result["worker_id"] == worker.operation_id
    assert worker.parent_operation_id == root.operation_id
    assert worker.request["actor_operation_id"] == worker.operation_id
    assert worker.request["provider"] == "codex"
    assert worker.request["run_on"] == "local"
    assert store.campaign_invocation_role(worker.operation_id) == "worker"
    assert store.campaign_budget_meter(campaign.campaign_id).invocations_used == (
        before.invocations_used + 1
    )
    wait_for_task(store, worker.operation_id)


def test_message_is_durable_hearsay_when_the_recipient_cannot_wake_yet(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    worker = _create_worker(store, campaign, root)
    background = BackgroundAgentTasks(store, _successful_stream)
    effects = _effects(store, background)
    before = store.campaign_budget_meter(campaign.campaign_id)
    planned_message_id = str(uuid.uuid4())

    outcome = effects.message(
        _context(store, campaign, root),
        MessageArguments(
            recipient_task_id=worker.operation_id,
            body="Check the new result, but treat this note as hearsay.",
        ),
        planned_message_id,
    )

    assert outcome.result["message_id"] == planned_message_id
    assert outcome.result["disposition"] == "created"
    assert outcome.result["delivery"] == "pending"
    assert outcome.result["graph_authority"] == "none"
    assert outcome.result["epistemic_status"] == "hearsay"
    pending = store.pending_campaign_messages(campaign.campaign_id, worker.operation_id)
    assert [message.message_id for message in pending] == [outcome.result["message_id"]]
    assert pending[0].body == "Check the new result, but treat this note as hearsay."
    assert pending[0].sender_task_id == root.operation_id
    assert pending[0].authorized_by is None
    assert store.campaign_budget_meter(campaign.campaign_id) == before


def test_new_message_stays_pending_when_an_older_bounded_batch_starts(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("rcp.runs.campaign_mail.CAMPAIGN_MAIL_MAX_MESSAGES", 2)
    store, campaign, root = _setup_campaign(tmp_path)
    stage = tmp_path / "worker-stage"
    stage.mkdir()
    worker = _create_worker(
        store,
        campaign,
        root,
        native_session_id="worker-session",
        stage_root=str(stage),
    )
    older = [
        record_campaign_message(
            store,
            campaign_id=campaign.campaign_id,
            sender_role="orchestrator",
            sender_task_id=root.operation_id,
            authorized_by=None,
            recipient_task_id=worker.operation_id,
            body=f"Older pending message {index}.",
        )
        for index in range(2)
    ]
    background = BackgroundAgentTasks(store, _successful_stream)
    effects = _effects(store, background)
    planned_message_id = str(uuid.uuid4())

    outcome = effects.message(
        _context(store, campaign, root),
        MessageArguments(
            recipient_task_id=worker.operation_id,
            body="New message behind the bounded prefix.",
        ),
        planned_message_id,
    )

    canonical = store.campaign_message(planned_message_id)
    assert canonical is not None
    assert canonical.delivery_operation_id is None
    assert outcome.result["message_id"] == planned_message_id
    assert outcome.result["delivery"] == "pending"
    assert outcome.result["delivery_operation_id"] is None
    assert "older pending batch started first" in (outcome.message or "")
    claimed = [store.campaign_message(message.message_id) for message in older]
    assert all(message is not None for message in claimed)
    delivery_ids = {message.delivery_operation_id for message in claimed if message is not None}
    assert len(delivery_ids) == 1
    delivery_operation_id = delivery_ids.pop()
    assert delivery_operation_id is not None
    wait_for_task(store, delivery_operation_id)


def test_watch_graph_uses_live_state_and_the_explicit_execution_host(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    background = BackgroundAgentTasks(store, _successful_stream)
    ready: list[str] = []
    calls = 0

    def live_state() -> GraphState:
        nonlocal calls
        calls += 1
        return _blocker_state(status="resolved", revision=2)

    effects = campaign_command_effects(
        store=store,
        background=background,
        validate=lambda _context, _arguments: CampaignCommandEffectResult(),
        worker_request_factory=_worker_request,
        graph_state=live_state,
        execution_host="ssh.execution.example",
        on_watcher_ready=ready.append,
    )
    context = _context(store, campaign, root)
    planned_watcher_id = str(uuid.uuid4())

    assert effects.seat_node_type(campaign.project_id, "blk/check") == "blocker"
    outcome = effects.watch_graph(
        context,
        WatchGraphArguments(
            condition=NodeStatusGraphCondition(
                node_id="blk/check",
                status_in=["resolved"],
            ),
            reason="Continue after the canonical blocker is resolved.",
        ),
        planned_watcher_id,
    )

    assert outcome.result["watcher_id"] == planned_watcher_id
    assert outcome.result["disposition"] == "created"
    watcher = store.watcher(str(outcome.result["watcher_id"]))
    assert isinstance(watcher, GraphWatcherRecord)
    assert watcher.execution_host == "ssh.execution.example"
    assert watcher.status == "completed"
    assert outcome.result["completed_immediately"] is True
    assert ready == [campaign.project_id]
    assert calls == 2


def test_graph_watcher_born_after_stop_reports_its_durable_stopped_status(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    background = BackgroundAgentTasks(store, _successful_stream)
    effects = _effects(store, background, state=_blocker_state(status="resolved"))
    store.request_campaign_stop(campaign.campaign_id)

    outcome = effects.watch_graph(
        _context(store, campaign, root),
        WatchGraphArguments(
            condition=NodeStatusGraphCondition(
                node_id="blk/check",
                status_in=["resolved"],
            ),
            reason="Retain this condition under the existing Stop intent.",
        ),
        str(uuid.uuid4()),
    )

    watcher = store.watcher(str(outcome.result["watcher_id"]))
    assert isinstance(watcher, GraphWatcherRecord)
    assert watcher.status == "stopped"
    assert outcome.result["status"] == "stopped"
    assert outcome.result["completed_immediately"] is False


def test_finish_fences_completed_and_unknown_reconciliation_never_reexecutes(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    background = BackgroundAgentTasks(store, _successful_stream)
    effects = _effects(store, background)
    context = _context(store, campaign, root)
    request = FinishCommandRequest(
        mailbox_id="a" * 32,
        request_id="f" * 32,
        credential="b" * 64,
        verb="finish",
        idempotency_key="finish-once",
    )

    finished = effects.finish(context)
    reconciled = effects.reconcile_unknown(context, request, None)

    assert finished.status == "ok"
    assert reconciled is not None
    assert reconciled.status == "ok"
    assert reconciled.result["ending"] == "completed"
    fenced = store.campaign(campaign.campaign_id)
    assert fenced is not None
    assert (fenced.status, fenced.ending) == ("wrapping_up", "completed")


def test_unknown_message_reconciliation_returns_the_exact_row_without_redelivery(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    stage = tmp_path / "worker-stage"
    stage.mkdir()
    worker = _create_worker(
        store,
        campaign,
        root,
        native_session_id="worker-session",
        stage_root=str(stage),
    )
    background = BackgroundAgentTasks(store, _successful_stream)
    effects = _effects(store, background)
    context = _context(store, campaign, root)
    planned_message_id = str(uuid.uuid4())
    request = MessageCommandRequest(
        mailbox_id=MAILBOX_ID,
        request_id="d" * 32,
        credential=CREDENTIAL,
        verb="message",
        idempotency_key="message-once",
        arguments={
            "recipient_task_id": worker.operation_id,
            "body": "Deliver this instruction exactly once.",
        },
    )

    saved = record_campaign_message(
        store,
        message_id=planned_message_id,
        campaign_id=campaign.campaign_id,
        sender_role="orchestrator",
        sender_task_id=root.operation_id,
        authorized_by=None,
        recipient_task_id=worker.operation_id,
        body=request.arguments.body,
    )
    tasks_before = store.campaign_tasks(campaign.campaign_id)
    budget_before = store.campaign_budget_meter(campaign.campaign_id)

    reconciled = effects.reconcile_unknown(context, request, planned_message_id)

    assert reconciled is not None
    assert reconciled.result == {
        "message_id": saved.message_id,
        "recipient_task_id": worker.operation_id,
        "delivery_operation_id": None,
        "delivery": "pending",
        "graph_authority": "none",
        "epistemic_status": "hearsay",
        "disposition": "existing",
    }
    assert store.campaign_tasks(campaign.campaign_id) == tasks_before
    assert store.campaign_budget_meter(campaign.campaign_id) == budget_before
    assert store.pending_campaign_messages(campaign.campaign_id, worker.operation_id) == [saved]
    messages = store.campaign_messages(campaign.campaign_id)
    assert [message.message_id for message in messages] == [planned_message_id]
    assert effects.reconcile_unknown(context, request, str(uuid.uuid4())) is None
    assert effects.reconcile_unknown(context, request, "not-a-uuid") is None
    mismatched = request.model_copy(
        update={
            "arguments": request.arguments.model_copy(
                update={"body": "A different instruction must not match."}
            )
        }
    )
    assert effects.reconcile_unknown(context, mismatched, planned_message_id) is None
    assert store.campaign_tasks(campaign.campaign_id) == tasks_before
    assert store.campaign_budget_meter(campaign.campaign_id) == budget_before
    assert [message.message_id for message in store.campaign_messages(campaign.campaign_id)] == [
        planned_message_id
    ]


def test_unknown_graph_watch_reconciliation_is_read_only_and_fail_closed(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    background = BackgroundAgentTasks(store, _successful_stream)
    ready: list[str] = []
    graph_reads = 0

    def live_state() -> GraphState:
        nonlocal graph_reads
        graph_reads += 1
        return _blocker_state(status="resolved", revision=2)

    effects = campaign_command_effects(
        store=store,
        background=background,
        validate=lambda _context, _arguments: CampaignCommandEffectResult(),
        worker_request_factory=_worker_request,
        graph_state=live_state,
        execution_host="ssh.execution.example",
        on_watcher_ready=ready.append,
    )
    context = _context(store, campaign, root)
    planned_watcher_id = str(uuid.uuid4())
    request = WatchGraphCommandRequest(
        mailbox_id=MAILBOX_ID,
        request_id="e" * 32,
        credential=CREDENTIAL,
        verb="watch_graph",
        idempotency_key="watch-once",
        arguments={
            "condition": {"node_id": "blk/check", "status_in": ["resolved"]},
            "reason": "Continue after the durable condition is satisfied.",
        },
    )

    created = effects.watch_graph(context, request.arguments, planned_watcher_id)
    assert graph_reads == 1
    assert ready == [campaign.project_id]

    reconciled = effects.reconcile_unknown(context, request, planned_watcher_id)

    assert reconciled is not None
    assert reconciled.result == {
        **created.result,
        "disposition": "existing",
    }
    assert graph_reads == 1
    assert ready == [campaign.project_id]
    assert effects.reconcile_unknown(context, request, str(uuid.uuid4())) is None
    assert effects.reconcile_unknown(context, request, "not-a-uuid") is None
    mismatched = request.model_copy(
        update={
            "arguments": request.arguments.model_copy(
                update={
                    "condition": NodeStatusGraphCondition(
                        node_id="blk/check",
                        status_in=["open"],
                    )
                }
            )
        }
    )
    assert effects.reconcile_unknown(context, mismatched, planned_watcher_id) is None
    assert graph_reads == 1
    assert ready == [campaign.project_id]


def test_individual_worker_stop_is_visibly_unavailable_and_never_reinterpreted(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    worker = _create_worker(store, campaign, root)
    background = _RecordingBackground(store)
    effects = _effects(store, background)
    dispatcher = CampaignCommandDispatcher(store, effects)

    response = dispatcher.dispatch(
        root.operation_id,
        StopCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="c" * 32,
            credential=CREDENTIAL,
            verb="stop",
            idempotency_key="stop-worker",
            arguments={"worker_id": worker.operation_id},
        ),
    )

    assert response.status == "unavailable"
    assert "no durable worker-stop primitive" in (response.message or "")
    assert "not mapped to campaign Stop or task Pause" in (response.message or "")
    assert store.campaign(campaign.campaign_id).stop_requested_at is None  # type: ignore[union-attr]
    assert background.paused == []
    assert background.resumed == []
