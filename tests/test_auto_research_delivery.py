from __future__ import annotations

import asyncio
import threading

from rcp.agents import AgentEvent
from rcp.agents.command_protocol import WatchGraphArguments
from rcp.background import BackgroundAgentTasks
from rcp.core.models import Blocker, GraphState
from rcp.runs.auto_research import (
    AutoResearchCommandContext,
    AutoResearchRunRequest,
    AutoResearchStartRequest,
)
from rcp.runs.auto_research_delivery import (
    arm_auto_research_graph_condition,
    deliver_auto_research_watcher_group,
    deliver_pending_auto_research_mail,
    pending_auto_research_mail_recipients,
    reconcile_auto_research_graph_condition,
    reconcile_pending_auto_research_mail,
    record_auto_research_message,
)
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    EpisodeRecord,
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    ProjectRecord,
)

from .helpers import fabricated_authorizer, wait_for_task


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


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


def _start_auto_research(
    tasks: BackgroundAgentTasks,
    *,
    invocation_ceiling: int = 6,
) -> tuple[EpisodeRecord, AgentTaskRecord]:
    auto_research, root = tasks.start_auto_research(
        "project",
        AutoResearchStartRequest(
            invocation_ceiling=invocation_ceiling,
            provider="codex",
            run_on="local",
            run_truth_scope=["repo-a"],
        ),
        authorized_by=fabricated_authorizer(),
        episode_id="auto_research",
        operation_id="root",
    )
    return auto_research, wait_for_task(tasks.store, root.operation_id, expect="succeeded")


def _arm_completed_graph_condition(
    store: AppStore,
    auto_research: EpisodeRecord,
    origin: AgentTaskRecord,
    *,
    watcher_id: str = "auto_research-watcher",
) -> GraphWatcherRecord:
    current = store.episode(auto_research.episode_id)
    assert current is not None
    condition = NodeStatusGraphCondition(node_id="blk/result", status_in=["resolved"])
    blocker = Blocker(
        id="blk/result",
        type="blocker",
        title="Wait for the result",
        description="The auto_research continues after this blocker resolves.",
        status="resolved",
    )
    watcher = arm_auto_research_graph_condition(
        store,
        AutoResearchCommandContext(
            episode=current,
            task=origin,
            request=AutoResearchRunRequest.model_validate(origin.request),
        ),
        WatchGraphArguments(
            condition=condition,
            reason="Continue after the canonical result is available.",
        ),
        watcher_id=watcher_id,
        state=GraphState(revision=2, nodes={blocker.id: blocker}),
        execution_host=origin.stage_host or "",
    )
    assert watcher.watcher_id == watcher_id
    assert watcher.status == "completed"
    assert watcher.origin_task_kind == "auto_research"
    assert watcher.origin_operation_id == origin.operation_id
    assert watcher.chat_id == origin.operation_id
    assert watcher.notified is False
    return watcher


def test_auto_research_effect_ids_are_exact_and_graph_reconciliation_is_read_only(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "auto_research-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("execution-host", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    auto_research, root = _start_auto_research(tasks)
    message = record_auto_research_message(
        store,
        message_id="planned-message",
        episode_id=auto_research.episode_id,
        sender_role="human",
        sender_task_id=None,
        authorized_by=auto_research.authorized_by,
        recipient_task_id=root.operation_id,
        body="Persist this under the command's planned identity.",
    )
    assert message.message_id == "planned-message"
    assert store.auto_research_message("planned-message") == message

    watcher = _arm_completed_graph_condition(
        store,
        auto_research,
        root,
        watcher_id="planned-watcher",
    )
    context = AutoResearchCommandContext(
        episode=auto_research,
        task=root,
        request=AutoResearchRunRequest.model_validate(root.request),
    )
    arguments = WatchGraphArguments(
        condition=watcher.condition,
        reason="Continue after the canonical result is available.",
    )
    events_before = store.agent_task_events(root.operation_id)
    receipts_before = store.agent_task_receipts(root.operation_id)

    assert (
        reconcile_auto_research_graph_condition(
            store,
            context,
            arguments,
            watcher_id="planned-watcher",
            execution_host=root.stage_host or "",
        )
        == watcher
    )
    assert (
        reconcile_auto_research_graph_condition(
            store,
            context,
            arguments,
            watcher_id="missing-watcher",
            execution_host=root.stage_host or "",
        )
        is None
    )
    mismatched_arguments = WatchGraphArguments(
        condition=NodeStatusGraphCondition(node_id="blk/other", status_in=["resolved"]),
        reason=arguments.reason,
    )
    assert (
        reconcile_auto_research_graph_condition(
            store,
            context,
            mismatched_arguments,
            watcher_id="planned-watcher",
            execution_host=root.stage_host or "",
        )
        is None
    )
    assert (
        reconcile_auto_research_graph_condition(
            store,
            context,
            arguments,
            watcher_id="planned-watcher",
            execution_host="different-host",
        )
        is None
    )
    assert store.agent_task_events(root.operation_id) == events_before
    assert store.agent_task_receipts(root.operation_id) == receipts_before
    assert store.watcher("planned-watcher") == watcher


def test_auto_research_graph_watcher_wake_is_one_atomic_paid_actor_continuation(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "auto_research-stage"
    stage.mkdir()
    observed: list[tuple[str, str, str | None]] = []

    async def stream(_project_id, _kind, request, execution):
        observed.append((execution.operation_id, execution.continuation, request.session_id))
        if execution.continuation == "fresh":
            execution.checkpoint_stage("execution-host", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    auto_research, root = _start_auto_research(tasks)
    watcher = _arm_completed_graph_condition(store, auto_research, root)
    before = store.episode_budget_meter(auto_research.episode_id)

    wake_id = deliver_auto_research_watcher_group(tasks, [watcher])

    assert wake_id is not None
    wake = wait_for_task(store, wake_id, expect="succeeded")
    delivered = store.watcher(watcher.watcher_id)
    assert isinstance(delivered, GraphWatcherRecord)
    assert delivered.notified is True
    assert delivered.notification_operation_id == wake.operation_id
    assert store.episode_budget_meter(auto_research.episode_id).invocations_used == (
        before.invocations_used + 1
    )
    assert store.auto_research_invocation_role(wake.operation_id) == "orchestrator"
    assert wake.request["actor_operation_id"] == root.operation_id
    assert wake.request["role"] == "orchestrator"
    assert wake.request["control_node_id"] is None
    assert wake.request["wake_cause"] == "graph_condition"
    assert wake.request["watcher_ids"] == [watcher.watcher_id]
    assert wake.native_session_id == root.native_session_id == "orchestrator-session"
    assert wake.stage_host == root.stage_host == "execution-host"
    assert wake.stage_root == root.stage_root == str(stage)
    assert store.agent_task_continuation_cause(wake.operation_id) == "graph_condition_wake"
    assert observed == [
        (root.operation_id, "fresh", None),
        (wake.operation_id, "graph_condition_wake", "orchestrator-session"),
    ]


def test_busy_auto_research_actor_leaves_completed_watcher_unclaimed_and_unspent(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "auto_research-stage"
    stage.mkdir()
    busy_entered = threading.Event()
    release_busy = threading.Event()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("execution-host", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        if execution.operation_id == "busy-turn":
            busy_entered.set()
            while not release_busy.is_set():
                await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    auto_research, root = _start_auto_research(tasks)
    watcher = _arm_completed_graph_condition(store, auto_research, root)
    busy = tasks.start_auto_research_turn(
        auto_research.episode_id,
        AutoResearchRunRequest(
            episode_id=auto_research.episode_id,
            role="orchestrator",
            provider="codex",
            run_on="local",
            instruction="Keep the actor occupied while delivery races.",
        ),
        parent_operation_id=root.operation_id,
        operation_id="busy-turn",
    )
    assert busy is not None
    assert busy_entered.wait(timeout=2)
    before = store.episode_budget_meter(auto_research.episode_id)
    task_ids_before = [task.operation_id for task in store.auto_research_tasks(auto_research.episode_id)]

    try:
        assert deliver_auto_research_watcher_group(tasks, [watcher]) is None
        unchanged = store.watcher(watcher.watcher_id)
        assert isinstance(unchanged, GraphWatcherRecord)
        assert unchanged.notified is False
        assert unchanged.notification_operation_id is None
        assert store.episode_budget_meter(auto_research.episode_id) == before
        assert [
            task.operation_id for task in store.auto_research_tasks(auto_research.episode_id)
        ] == task_ids_before
    finally:
        release_busy.set()
        wait_for_task(store, busy.operation_id, expect="succeeded")


def test_pending_auto_research_mail_coalesces_into_one_paid_message_wake(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "auto_research-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("execution-host", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    auto_research, root = _start_auto_research(tasks)
    messages = [
        record_auto_research_message(
            store,
            episode_id=auto_research.episode_id,
            sender_role="human",
            sender_task_id=None,
            authorized_by=auto_research.authorized_by,
            recipient_task_id=root.operation_id,
            body=body,
        )
        for body in ("Review the new evidence.", "Also resolve the blocker.")
    ]
    before = store.episode_budget_meter(auto_research.episode_id)

    wake_id = deliver_pending_auto_research_mail(
        tasks,
        episode_id=auto_research.episode_id,
        recipient_task_id=root.operation_id,
    )

    assert wake_id is not None
    wake = wait_for_task(store, wake_id, expect="succeeded")
    claimed = {
        message.message_id: message for message in store.auto_research_messages(auto_research.episode_id)
    }
    assert {claimed[message.message_id].delivery_operation_id for message in messages} == {
        wake.operation_id
    }
    assert all(claimed[message.message_id].delivered_at is not None for message in messages)
    assert store.pending_auto_research_messages(auto_research.episode_id, root.operation_id) == []
    assert store.episode_budget_meter(auto_research.episode_id).invocations_used == (
        before.invocations_used + 1
    )
    assert store.auto_research_invocation_role(wake.operation_id) == "orchestrator"
    assert wake.request["wake_cause"] == "message"
    assert wake.request["actor_operation_id"] == root.operation_id
    assert wake.native_session_id == root.native_session_id == "orchestrator-session"
    assert wake.stage_host == root.stage_host == "execution-host"
    assert wake.stage_root == root.stage_root == str(stage)
    assert store.agent_task_continuation_cause(wake.operation_id) == "message_wake"
    assert len(store.auto_research_tasks(auto_research.episode_id)) == 2


def test_reconciliation_retries_every_pending_canonical_actor(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "auto_research-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("execution-host", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or f"{execution.operation_id}-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    auto_research, root = _start_auto_research(tasks)
    worker = tasks.start_auto_research_turn(
        auto_research.episode_id,
        AutoResearchRunRequest(
            episode_id=auto_research.episode_id,
            role="worker",
            control_node_id="blk/check-result",
            provider="codex",
            run_on="local",
            instruction="Check the result and report back.",
        ),
        parent_operation_id=root.operation_id,
        operation_id="worker",
    )
    assert worker is not None
    worker = wait_for_task(store, worker.operation_id, expect="succeeded")
    root_message = record_auto_research_message(
        store,
        episode_id=auto_research.episode_id,
        sender_role="human",
        sender_task_id=None,
        authorized_by=auto_research.authorized_by,
        recipient_task_id=root.operation_id,
        body="Review the worker result.",
    )
    worker_message = record_auto_research_message(
        store,
        episode_id=auto_research.episode_id,
        sender_role="orchestrator",
        sender_task_id=root.operation_id,
        authorized_by=None,
        recipient_task_id=worker.operation_id,
        body="Re-check the canonical blocker.",
    )

    assert pending_auto_research_mail_recipients(
        store,
        episode_id=auto_research.episode_id,
    ) == [(auto_research.episode_id, root.operation_id), (auto_research.episode_id, worker.operation_id)]
    wake_ids = reconcile_pending_auto_research_mail(
        tasks,
        episode_id=auto_research.episode_id,
    )

    assert len(wake_ids) == 2
    for wake_id in wake_ids:
        wait_for_task(store, wake_id, expect="succeeded")
    claimed = {
        message.message_id: message for message in store.auto_research_messages(auto_research.episode_id)
    }
    assert claimed[root_message.message_id].delivery_operation_id in wake_ids
    assert claimed[worker_message.message_id].delivery_operation_id in wake_ids
    assert (
        claimed[root_message.message_id].delivery_operation_id
        != claimed[worker_message.message_id].delivery_operation_id
    )
    assert (
        pending_auto_research_mail_recipients(
            store,
            episode_id=auto_research.episode_id,
        )
        == []
    )


def test_bounded_mail_overflow_stays_pending_for_the_next_paid_retry(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("rcp.runs.auto_research_mail.AUTO_RESEARCH_MAIL_MAX_MESSAGES", 2)
    store = _store(tmp_path)
    stage = tmp_path / "auto_research-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("execution-host", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    auto_research, root = _start_auto_research(tasks)
    messages = [
        record_auto_research_message(
            store,
            episode_id=auto_research.episode_id,
            sender_role="human",
            sender_task_id=None,
            authorized_by=auto_research.authorized_by,
            recipient_task_id=root.operation_id,
            body=f"Bounded message {index}.",
        )
        for index in range(3)
    ]

    first_wake_id = deliver_pending_auto_research_mail(
        tasks,
        episode_id=auto_research.episode_id,
        recipient_task_id=root.operation_id,
    )

    assert first_wake_id is not None
    wait_for_task(store, first_wake_id, expect="succeeded")
    first_batch = [
        message
        for message in store.auto_research_messages(auto_research.episode_id)
        if message.delivery_operation_id == first_wake_id
    ]
    assert [message.message_id for message in first_batch] == [
        messages[0].message_id,
        messages[1].message_id,
    ]
    assert store.pending_auto_research_messages(auto_research.episode_id, root.operation_id) == [messages[2]]

    retry_wake_id = deliver_pending_auto_research_mail(
        tasks,
        episode_id=auto_research.episode_id,
        recipient_task_id=root.operation_id,
    )

    assert retry_wake_id is not None
    wait_for_task(store, retry_wake_id, expect="succeeded")
    retried = store.auto_research_message(messages[2].message_id)
    assert retried is not None
    assert retried.delivery_operation_id == retry_wake_id
    assert store.pending_auto_research_messages(auto_research.episode_id, root.operation_id) == []


def test_busy_auto_research_actor_leaves_pending_mail_unclaimed_and_unspent(tmp_path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "auto_research-stage"
    stage.mkdir()
    busy_entered = threading.Event()
    release_busy = threading.Event()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            execution.checkpoint_stage("execution-host", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        if execution.operation_id == "busy-turn":
            busy_entered.set()
            while not release_busy.is_set():
                await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    auto_research, root = _start_auto_research(tasks)
    busy = tasks.start_auto_research_turn(
        auto_research.episode_id,
        AutoResearchRunRequest(
            episode_id=auto_research.episode_id,
            role="orchestrator",
            provider="codex",
            run_on="local",
            instruction="Keep the actor occupied while mail arrives.",
        ),
        parent_operation_id=root.operation_id,
        operation_id="busy-turn",
    )
    assert busy is not None
    assert busy_entered.wait(timeout=2)
    message = record_auto_research_message(
        store,
        episode_id=auto_research.episode_id,
        sender_role="human",
        sender_task_id=None,
        authorized_by=auto_research.authorized_by,
        recipient_task_id=root.operation_id,
        body="Wait until the current turn settles.",
    )
    before = store.episode_budget_meter(auto_research.episode_id)

    try:
        assert (
            deliver_pending_auto_research_mail(
                tasks,
                episode_id=auto_research.episode_id,
                recipient_task_id=root.operation_id,
            )
            is None
        )
        assert store.episode_budget_meter(auto_research.episode_id) == before
        assert store.pending_auto_research_messages(auto_research.episode_id, root.operation_id) == [message]
        assert store.auto_research_messages(auto_research.episode_id) == [message]
    finally:
        release_busy.set()
        wait_for_task(store, busy.operation_id, expect="succeeded")


def test_not_yet_checkpointed_auto_research_actor_leaves_mail_pending(tmp_path) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, _kind, request, _execution):
        yield _sse(
            AgentEvent(
                event="session",
                session_id=request.session_id or "orchestrator-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    auto_research, root = _start_auto_research(tasks)
    assert root.native_session_id == "orchestrator-session"
    assert root.stage_root is None
    message = record_auto_research_message(
        store,
        episode_id=auto_research.episode_id,
        sender_role="human",
        sender_task_id=None,
        authorized_by=auto_research.authorized_by,
        recipient_task_id=root.operation_id,
        body="Deliver this after a stage checkpoint exists.",
    )
    before = store.episode_budget_meter(auto_research.episode_id)

    assert (
        deliver_pending_auto_research_mail(
            tasks,
            episode_id=auto_research.episode_id,
            recipient_task_id=root.operation_id,
        )
        is None
    )
    assert store.episode_budget_meter(auto_research.episode_id) == before
    assert store.pending_auto_research_messages(auto_research.episode_id, root.operation_id) == [message]
    assert store.auto_research_messages(auto_research.episode_id) == [message]
