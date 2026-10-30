from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.auto_research import AutoResearchStartRequest
from rcp.runs.auto_research_admission import start_auto_research, start_auto_research_child_work
from rcp.runs.auto_research_delivery import (
    deliver_auto_research_watcher_group,
    deliver_pending_auto_research_mail,
    record_auto_research_message,
)
from rcp.runs.shared import _sse
from rcp.runs.watcher_admission import start_watcher_notification
from rcp.service import RunRequest
from rcp.storage import WatcherContinuation, WatcherRecord

from .helpers import wait_for_task
from .provider_login_switch_helpers import PROJECT, switch_background, switched_session
from .test_auto_research_delivery import _arm_completed_graph_condition
from .test_background import _experiment_request
from .test_provider_login_resolver import accounts as accounts


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("direction", ["personal_to_team", "team_to_personal"])
@pytest.mark.parametrize(
    "operation",
    ["auto_watcher", "worker_mail", "worker_watcher", "worker_dispatch", "experiment_watcher"],
)
def test_episode_wake_after_login_switch(
    accounts, tmp_path, monkeypatch, provider, direction, operation
):
    store = accounts.store
    stage = tmp_path / "stage"
    old, new, member, _ = switched_session(accounts, stage, provider, direction)
    # Setup the pre-switch episode through its normal admission, then persist the
    # login/receipt shape emitted by the old writer on its exact native session.
    store.mark_provider_login_verified(old, member_id=member.user_id, detail="setup")
    store.mark_provider_login_signed_out(
        new, member_id=member.user_id, source="restore", detail="setup"
    )

    async def setup_stream(_project, kind, request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(
            AgentEvent(
                event="session",
                session_id="root-session" if kind == "auto_research" else "worker-session",
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, setup_stream)
    if operation == "experiment_watcher":
        root_request = _experiment_request().model_copy(
            update={"provider": provider, "run_on": "local"}
        )
        root = wait_for_task(
            store,
            tasks.start(PROJECT, "node_chat", root_request, authorized_by=member).operation_id,
            expect="succeeded",
        )
        episode = store.episode(root.episode_id)
        store.commit_experiment_episode_turn(
            episode_id=episode.episode_id,
            project_id=PROJECT,
            control_node_id=root_request.control_node_id,
            provider=provider,
            execution_machine="local",
            execution_host="",
            native_session_id=root.native_session_id,
            stage_host="",
            stage_root=str(stage),
            chat_id=root_request.chat_id,
            operation_id=root.operation_id,
            invocation=1,
            graph_result="applied",
            watcher_ids=[],
            context_baseline={},
        )
    else:
        episode, root = start_auto_research(
            tasks,
            PROJECT,
            AutoResearchStartRequest(
                invocation_ceiling=6,
                provider=provider,
                run_on="local",
                run_truth_scope=["repo-a"],
            ),
            authorized_by=member,
            graph_base_head=GraphHeadRef(revision=0),
            ensure_graph_target=lambda _: None,
        )
        root = wait_for_task(store, root.operation_id, expect="succeeded")
    source = root
    worker_id = str(uuid4())
    worker_request = RunRequest(
        provider=provider,
        run_on="local",
        run_truth_scope=["repo-a"],
        chat_scope="node",
        node_id="blk/result",
        chat_id=worker_id,
        message="Inspect result",
        mode="work",
        trigger="orchestrator",
        patch_kind="work",
    )
    if operation in {"worker_mail", "worker_watcher"}:
        child = start_auto_research_child_work(
            tasks,
            episode.episode_id,
            worker_request,
            admitted_by_operation_id=root.operation_id,
            worker_id=worker_id,
            instruction=worker_request.message,
            instruction_sha256=hashlib.sha256(worker_request.message.encode()).hexdigest(),
        )
        source = wait_for_task(store, child.operation_id, expect="succeeded")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET login_id=? WHERE operation_id=?",
            (old.login_id, source.operation_id),
        )
    store.record_agent_task_receipt(source.operation_id, "provider_runtime_selected", {})
    store.mark_provider_login_signed_out(
        old, member_id=member.user_id, source="restore", detail="expired"
    )
    store.mark_provider_login_verified(new, member_id=member.user_id, detail="usable")
    selected = []
    tasks.stream = switch_background(store, stage, selected).stream
    monkeypatch.setattr("rcp.runs.auto_research_delivery.seconds_until_automatic_launch", lambda: 0)
    if operation == "auto_watcher":
        watcher = _arm_completed_graph_condition(store, episode, root)
        after_grace = datetime.fromisoformat(store.now()) + timedelta(minutes=1)
        monkeypatch.setattr(store, "now", lambda: after_grace.isoformat())
        operation_id = deliver_auto_research_watcher_group(tasks, [watcher])
    elif operation == "worker_mail":
        record_auto_research_message(
            store,
            episode_id=episode.episode_id,
            sender_role="orchestrator",
            sender_task_id=root.operation_id,
            authorized_by=None,
            recipient_task_id=worker_id,
            body="Continue",
        )
        operation_id = deliver_pending_auto_research_mail(
            tasks, episode_id=episode.episode_id, recipient_task_id=worker_id
        )
    elif operation == "worker_dispatch":
        child = start_auto_research_child_work(
            tasks,
            episode.episode_id,
            worker_request,
            admitted_by_operation_id=root.operation_id,
            worker_id=worker_id,
            instruction=worker_request.message,
            instruction_sha256=hashlib.sha256(worker_request.message.encode()).hexdigest(),
        )
        operation_id = child.operation_id
    else:
        continuation = WatcherContinuation.model_validate(
            {
                key: source.request[key]
                for key in WatcherContinuation.model_fields
                if key in source.request and source.request[key] is not None
            }
        )
        watcher = WatcherRecord(
            watcher_id="ready",
            project_id=PROJECT,
            origin_operation_id=source.operation_id,
            origin_task_kind="node_chat",
            episode_id=episode.episode_id,
            graph_target=episode.graph_target,
            worker_id=worker_id if operation == "worker_watcher" else None,
            chat_id=source.request["chat_id"],
            node_id=source.request["node_id"],
            execution_host="",
            check_command="true",
            log_path=str(stage / "job.log"),
            cwd=str(stage),
            continuation=continuation,
            status="completed",
            created_at=store.now(),
        )
        store.create_watchers([watcher])
        if operation == "experiment_watcher":
            request = root_request.model_copy(
                update={
                    "trigger": "watcher",
                    "control_invocation": 2,
                    "watcher_ids": [watcher.watcher_id],
                    "session_id": source.native_session_id,
                }
            )
        else:
            from rcp.api.app import _generic_watcher_delivery_request

            request = _generic_watcher_delivery_request([watcher])
        wake = start_watcher_notification(
            tasks,
            PROJECT,
            "node_chat",
            request,
            [watcher.watcher_id],
            authorized_by=member,
            episode_stage_root=str(stage) if operation == "experiment_watcher" else None,
        )
        operation_id = wake.operation_id if wake else None
    assert operation_id is not None
    finished = wait_for_task(store, operation_id, expect="succeeded")
    expected_session = None if operation == "worker_dispatch" else source.native_session_id
    assert selected == [(new, expected_session, None)]
    assert finished.login_id == new.login_id
    if expected_session:
        assert finished.native_session_id == expected_session
