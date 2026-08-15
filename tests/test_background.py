from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.runs.auto_research import AutoResearchRunRequest, AutoResearchStartRequest
from rcp.runs.episode_report import EpisodeReportRunRequest
from rcp.runs.episode_wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup
from rcp.service import RunRequest
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    EpisodeInvocationCeilingReached,
    EpisodeRecord,
    EpisodeReportRecord,
    ProjectRecord,
    WatcherContinuation,
    WatcherRecord,
)

from .helpers import fabricated_authorizer, wait_for_task

_EXPERIMENT_ID = "exp/background-admission"
_EXPERIMENT_EPISODE_ID = "00000000-0000-4000-8000-000000000101"


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _store(tmp_path: Path) -> AppStore:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(tmp_path / "research.yaml"),
            name="Project",
            state_location=str(tmp_path / ".research"),
            state_remote=False,
            added_at=store.now(),
        )
    )
    return store


def _auto_start(**updates: object) -> AutoResearchStartRequest:
    return AutoResearchStartRequest.model_validate(
        {
            "invocation_ceiling": 3,
            "provider": "codex",
            "model": "",
            "reasoning": "medium",
            "run_on": "laptop",
            "run_truth_scope": ["repo"],
            **updates,
        }
    )


def _experiment_request(
    *,
    trigger: str = "experiment_run",
    invocation: int = 1,
    watcher_ids: list[str] | None = None,
    session_id: str | None = None,
) -> RunRequest:
    return RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_id="experiment-chat",
        chat_scope="node",
        node_id=_EXPERIMENT_ID,
        message="Continue the bounded experiment.",
        mode="work",
        trigger=trigger,
        patch_kind="experiment_loop",
        control_node_id=_EXPERIMENT_ID,
        control_revision=1,
        control_episode_id=_EXPERIMENT_EPISODE_ID,
        control_invocation=invocation,
        control_invocation_ceiling=3,
        control_decision_bundle=[],
        control_completion_criteria=["The bounded comparison is analyzed."],
        watcher_ids=list(watcher_ids or []),
        session_id=session_id,
    )


def test_auto_research_root_uses_episode_lineage_and_strict_request_decode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "auto-stage"
    stage.mkdir()

    async def stream(_project_id, kind, request, execution):
        assert kind == "auto_research"
        assert isinstance(request, AutoResearchRunRequest)
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="auto-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    episode, root = tasks.start_auto_research(
        "project",
        _auto_start(starting_instruction="  Begin with the disputed claim.  "),
        authorized_by=fabricated_authorizer("Researcher"),
        episode_id="auto-episode",
        operation_id="auto-root",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")

    assert episode.mode == "auto_research"
    assert root.kind == "auto_research"
    assert root.episode_id == episode.episode_id
    assert root.request["episode_id"] == episode.episode_id
    assert root.request["instruction"] == "Begin with the disputed claim."
    assert "campaign_id" not in root.request
    assert isinstance(tasks._request_from_record(root), AutoResearchRunRequest)
    assert store.episode_budget_meter(episode.episode_id).invocations_used == 1
    assert store.episode_tasks(episode.episode_id) == [root]


def test_auto_research_clean_orchestrator_retry_keeps_paid_allocation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "replacement-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        if execution.continuation == "fresh":
            yield _sse(AgentEvent(event="error", text="Network connection failed."))
            return
        assert execution.continuation == "retry"
        assert request.session_id is None
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="replacement-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    episode, root = tasks.start_auto_research(
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        episode_id="auto-episode",
        operation_id="auto-root",
    )
    root = wait_for_task(store, root.operation_id, expect="failed")

    retried = wait_for_task(store, tasks.retry(root.operation_id).operation_id, expect="succeeded")

    assert retried.parent_operation_id == root.operation_id
    assert retried.episode_id == episode.episode_id
    assert retried.native_session_id == "replacement-session"
    assert retried.stage_root == str(stage)
    assert store.episode_budget_meter(episode.episode_id).invocations_used == 1
    clean = next(
        receipt
        for receipt in store.agent_task_receipts(retried.operation_id)
        if receipt.category == "auto_research_orchestrator_clean_retry"
    )
    assert clean.payload["same_allocation"] is True
    assert clean.payload["classification"] == "checkpoint_missing"


def test_auto_research_stop_skips_report_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    episode, root = tasks.start_auto_research(
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        episode_id="auto-episode",
        operation_id="auto-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")

    stopped = tasks.stop_auto_research(episode.episode_id)

    assert stopped.status == "stopped"
    assert stopped.ending == "stopped"
    assert stopped.wrapup_state == "skipped"
    assert store.episode_report(stopped.episode_id) is None
    assert all(
        task.kind != "episode_report"
        for task in store.episode_tasks(stopped.episode_id, include_hidden=True)
    )


def test_over_ceiling_admission_does_not_fence_an_active_paid_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started = threading.Event()
    release = threading.Event()

    async def stream(_project_id, _kind, _request, _execution):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    episode, root = tasks.start_auto_research(
        "project",
        _auto_start(invocation_ceiling=1),
        authorized_by=fabricated_authorizer("Researcher"),
        episode_id="auto-episode",
        operation_id="auto-root",
    )
    assert started.wait(timeout=2)
    root_request = AutoResearchRunRequest.model_validate(root.request)
    worker_request = root_request.model_copy(
        update={
            "role": "worker",
            "actor_operation_id": None,
            "instruction": "Check the bounded claim.",
            "control_node_id": "exp/check",
        }
    )

    try:
        with pytest.raises(EpisodeInvocationCeilingReached):
            tasks.start_auto_research_turn(
                episode.episode_id,
                worker_request,
                parent_operation_id=root.operation_id,
                operation_id="over-ceiling-worker",
            )
        current = store.episode(episode.episode_id)
        assert current is not None
        assert current.status == "running"
        assert current.ending is None
    finally:
        release.set()
        wait_for_task(store, root.operation_id, expect="succeeded")


def test_experiment_root_and_recovery_use_atomic_episode_admission(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "experiment-recovery-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        execution.checkpoint_stage("", str(stage))
        if execution.continuation == "fresh":
            candidate = "{}"
            store.record_agent_task_contract(
                execution.operation_id,
                "experiment_episode_context_candidate",
                candidate,
                hashlib.sha256(candidate.encode()).hexdigest(),
            )
        yield _sse(AgentEvent(event="session", session_id="experiment-session"))
        if execution.continuation == "fresh":
            yield _sse(AgentEvent(event="error", text="Transient provider failure."))
            return
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    root = tasks.start(
        "project",
        "node_chat",
        _experiment_request(),
        operation_id="experiment-root",
        authorized_by=fabricated_authorizer("Researcher"),
    )
    root = wait_for_task(store, root.operation_id, expect="failed")

    episode = store.episode(_EXPERIMENT_EPISODE_ID)
    assert episode is not None
    assert episode.mode == "experiment_loop"
    assert episode.root_operation_id == root.operation_id
    assert root.episode_id == episode.episode_id
    assert episode.invocations_used == 1

    recovered = wait_for_task(
        store, tasks.retry(root.operation_id).operation_id, expect="succeeded"
    )
    assert recovered.parent_operation_id == root.operation_id
    assert recovered.episode_id == episode.episode_id
    assert store.episode_budget_meter(episode.episode_id).invocations_used == 1


def test_experiment_watcher_wake_uses_atomic_episode_invocation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "experiment-stage"
    stage.mkdir()
    authorizer = fabricated_authorizer("Researcher")

    async def stream(_project_id, _kind, request, execution):
        if request.trigger == "experiment_run":
            execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="experiment-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    root = wait_for_task(
        store,
        tasks.start(
            "project",
            "node_chat",
            _experiment_request(),
            operation_id="experiment-root",
            authorized_by=authorizer,
        ).operation_id,
        expect="succeeded",
    )
    store.commit_experiment_episode_turn(
        episode_id=_EXPERIMENT_EPISODE_ID,
        project_id="project",
        control_node_id=_EXPERIMENT_ID,
        provider="codex",
        execution_machine="laptop",
        execution_host="",
        native_session_id="experiment-session",
        stage_host=None,
        stage_root=str(stage),
        chat_id="experiment-chat",
        operation_id=root.operation_id,
        invocation=1,
        graph_result="applied",
        watcher_ids=[],
        context_baseline={},
    )
    now = store.now()
    watcher = WatcherRecord(
        watcher_id="completed-watcher",
        project_id="project",
        origin_operation_id=root.operation_id,
        origin_task_kind="node_chat",
        chat_id="experiment-chat",
        node_id=_EXPERIMENT_ID,
        execution_host="",
        check_command="true",
        log_path="/tmp/completed-watcher.log",
        cwd="/tmp",
        continuation=WatcherContinuation(
            provider="codex",
            model="",
            reasoning="medium",
            run_on="laptop",
            run_truth_scope=["repo"],
            patch_kind="experiment_loop",
            control_node_id=_EXPERIMENT_ID,
            control_revision=1,
            control_episode_id=_EXPERIMENT_EPISODE_ID,
            control_invocation=1,
            control_invocation_ceiling=3,
            control_decision_bundle=[],
            control_completion_criteria=["The bounded comparison is analyzed."],
        ),
        status="active",
        created_at=now,
    )
    store.create_watchers([watcher])
    store.record_watcher_check(
        watcher.watcher_id,
        status="completed",
        exit_code=0,
        error=None,
    )

    wake = tasks.start_watcher_notification(
        "project",
        "node_chat",
        _experiment_request(
            trigger="watcher",
            invocation=2,
            watcher_ids=[watcher.watcher_id],
            session_id="experiment-session",
        ),
        [watcher.watcher_id],
        authorized_by=authorizer,
        episode_stage_root=str(stage),
    )
    assert wake is not None
    wake = wait_for_task(store, wake.operation_id, expect="succeeded")

    assert wake.episode_id == _EXPERIMENT_EPISODE_ID
    assert store.episode_budget_meter(_EXPERIMENT_EPISODE_ID).invocations_used == 2
    claimed = store.watcher(watcher.watcher_id)
    assert claimed is not None
    assert claimed.notified is True
    assert claimed.notification_operation_id == wake.operation_id


def _report_allocation(store: AppStore, tmp_path: Path) -> AgentTaskRecord:
    now = store.now()
    store.create_episode(
        EpisodeRecord(
            episode_id="report-episode",
            project_id="project",
            mode="experiment_loop",
            control_node_id="exp/report",
            status="queued",
            invocation_ceiling=1,
            authorized_by=fabricated_authorizer("Researcher"),
            created_at=now,
            updated_at=now,
        )
    )
    stage = tmp_path / "report-stage"
    stage.mkdir()
    operational = AgentTaskRecord(
        operation_id="operational",
        project_id="project",
        episode_id="report-episode",
        kind="node_chat",
        status="queued",
        request={
            "provider": "codex",
            "model": "",
            "reasoning": "medium",
            "run_on": "laptop",
        },
        created_at=now,
        updated_at=now,
        status_message="Queued",
        native_session_id="report-session",
        stage_root=str(stage),
    )
    store.allocate_episode_invocation("report-episode", operational)
    store.complete_agent_task(operational.operation_id, applied_revision=None, result={})
    admission = begin_episode_report_wrapup(
        store,
        EpisodeWrapupSpec(
            episode_id="report-episode",
            ending="completed",
            partial=False,
            continuation_operation_id=operational.operation_id,
            receipt={"observations": ["One bounded result."]},
        ),
    )
    assert admission.task is not None
    return admission.task


def test_interrupted_hidden_report_restarts_once_and_runner_owns_success(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hidden = _report_allocation(store, tmp_path)
    store.record_agent_task_receipt(
        hidden.operation_id,
        "operation_created",
        {
            "kind": "episode_report",
            "attempt": 1,
            "has_parent": True,
            "continuation_cause": "episode_report",
            "resumed": True,
        },
    )
    generic_settlements: list[str] = []

    async def stream(_project_id, kind, request, execution):
        assert kind == "episode_report"
        assert isinstance(request, EpisodeReportRunRequest)
        attempt = store.allocate_episode_report_attempt(request.episode_id)
        store.mark_episode_report_attempt_running(attempt.attempt_id)
        html = "<html><body><figure>Evidence map</figure></body></html>"
        store.finish_episode_report_ready(
            attempt.attempt_id,
            EpisodeReportRecord(
                report_id="report",
                episode_id=request.episode_id,
                attempt_id=attempt.attempt_id,
                allocation_operation_id=execution.operation_id,
                ending="completed",
                sha256=hashlib.sha256(html.encode()).hexdigest(),
                html=html,
                created_at=store.now(),
            ),
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(
        store,
        stream,
        on_task_settled=lambda _project, _kind, _request, execution: generic_settlements.append(
            execution.operation_id
        ),
    )
    finished = wait_for_task(store, hidden.operation_id, expect="succeeded")

    assert finished.operation_id == hidden.operation_id
    assert store.episode("report-episode").wrapup_state == "ready"  # type: ignore[union-attr]
    assert store.episode_report("report-episode") is not None
    assert store.episode_tasks("report-episode") == [store.agent_task("operational")]
    assert [task.kind for task in store.episode_tasks("report-episode", include_hidden=True)] == [
        "node_chat",
        "episode_report",
    ]
    receipts = store.agent_task_receipts(hidden.operation_id)
    assert sum(item.category == "operation_created" for item in receipts) == 1
    assert not any(item.category == "operation_completed" for item in receipts)
    assert generic_settlements == []
    assert tasks.start_episode_report("report-episode") is None
    with pytest.raises(ValueError, match="no Retry control"):
        tasks.retry(hidden.operation_id)
    with pytest.raises(ValueError, match="no Resume control"):
        tasks.resume(hidden.operation_id)
    with pytest.raises(ValueError, match="no manual Pause control"):
        tasks.pause(hidden.operation_id)


def test_report_runner_terminal_error_is_not_generically_retried_or_resettled(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    hidden = _report_allocation(store, tmp_path)
    generic_settlements: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    async def stream(_project_id, kind, request, _execution):
        assert kind == "episode_report"
        assert isinstance(request, EpisodeReportRunRequest)
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        store.fail_episode_report_allocation_unlaunchable(
            request.episode_id,
            "The exact report continuation is unavailable.",
        )
        yield _sse(AgentEvent(event="error", text="Report runner already settled the error."))

    tasks = BackgroundAgentTasks(
        store,
        stream,
        on_task_settled=lambda _project, _kind, _request, execution: generic_settlements.append(
            execution.operation_id
        ),
    )
    assert entered.wait(timeout=2)
    duplicate = tasks.start_episode_report("report-episode")
    assert duplicate is not None and duplicate.operation_id == hidden.operation_id
    release.set()
    failed = wait_for_task(store, hidden.operation_id, expect="failed")

    episode = store.episode("report-episode")
    assert episode is not None
    assert episode.status == "completed"
    assert episode.wrapup_state == "failed"
    assert episode.wrapup_error == "The exact report continuation is unavailable."
    assert failed.error == episode.wrapup_error
    assert store.episode_report(episode.episode_id) is None
    assert not any(
        item.category == "operation_failed"
        for item in store.agent_task_receipts(hidden.operation_id)
    )
    assert (
        sum(
            item.category == "operation_created"
            for item in store.agent_task_receipts(hidden.operation_id)
        )
        == 1
    )
    assert generic_settlements == []
    with pytest.raises(ValueError, match="no Retry control"):
        tasks.retry(hidden.operation_id)


def test_report_request_decode_never_accepts_an_auto_research_task_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hidden = _report_allocation(store, tmp_path)
    decoded = BackgroundAgentTasks._request_from_record(hidden)

    assert isinstance(decoded, EpisodeReportRunRequest)
    assert decoded.episode_id == "report-episode"
    assert not hasattr(decoded, "role")
    assert not hasattr(decoded, "campaign_id")
