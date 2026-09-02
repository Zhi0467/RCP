from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.auto_research import AutoResearchRunRequest, AutoResearchStartRequest
from rcp.runs.auto_research_admission import start_auto_research
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.runs.tasks.episode_report import EpisodeReportRunRequest
from rcp.service import CoachRequest, RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskKind, AgentTaskRecord, AppStore, ProjectRecord

from .helpers import fabricated_authorizer, wait_for_task

# Every engine-owned policy cell is reachable with a deterministic fake stream;
# none of these rows needs or skips for a real provider.
POLICY_MATRIX = [
    pytest.param("chat Discuss", "start", "fresh task admitted", id="discuss-start"),
    pytest.param("chat Work", "start", "fresh task admitted", id="work-start"),
    pytest.param("ingestion", "start", "fresh task admitted", id="ingestion-start"),
    pytest.param("paper coach", "start", "fresh task admitted", id="paper-start"),
    pytest.param("Experiment loop", "start", "root admitted atomically", id="experiment-start"),
    pytest.param(
        "Experiment watcher wake",
        "start",
        "dedicated admission required",
        id="experiment-wake-start",
    ),
    pytest.param(
        "Auto-research",
        "start",
        "dedicated admission required",
        id="auto-research-start",
    ),
    pytest.param(
        "branch merge",
        "start",
        "dedicated admission required",
        id="branch-merge-start",
    ),
    pytest.param(
        "episode report",
        "start",
        "dedicated admission required",
        id="episode-report-start",
    ),
    pytest.param(
        "result-view revision",
        "start",
        "saved session and stage required",
        id="result-view-start",
    ),
    pytest.param("chat Work", "resume", "owned checkpoint reused", id="work-resume"),
    pytest.param("ingestion", "resume", "owned checkpoint reused", id="ingestion-resume"),
    pytest.param("paper coach", "resume", "owned checkpoint reused", id="paper-resume"),
    pytest.param("Experiment loop", "resume", "episode recovery reused", id="experiment-resume"),
    pytest.param("Auto-research", "resume", "paid allocation reused", id="auto-resume"),
    pytest.param(
        "branch merge",
        "resume",
        "fresh merge dispatch required",
        id="branch-resume",
    ),
    pytest.param(
        "episode report",
        "resume",
        "automatic recovery only",
        id="episode-report-resume",
    ),
    pytest.param(
        "graph repair",
        "resume",
        "patch-only continuation preserved",
        id="graph-repair-resume",
    ),
    pytest.param(
        "chat Work",
        "retry",
        "same-provider checkpoint reused",
        id="work-retry-checkpoint",
    ),
    pytest.param(
        "chat Work without checkpoint",
        "retry",
        "clean handoff started",
        id="work-retry-handoff",
    ),
    pytest.param(
        "ingestion",
        "retry",
        "clean handoff started",
        id="ingestion-retry-handoff",
    ),
    pytest.param(
        "Experiment loop",
        "retry",
        "execution machine remains pinned",
        id="experiment-retry",
    ),
    pytest.param(
        "Auto-research",
        "retry",
        "provider profile remains pinned",
        id="auto-research-retry",
    ),
    pytest.param(
        "branch merge",
        "retry",
        "fresh merge dispatch required",
        id="branch-retry",
    ),
    pytest.param(
        "episode report",
        "retry",
        "automatic recovery only",
        id="episode-report-retry",
    ),
    pytest.param(
        "result-view revision",
        "retry",
        "provider profile remains pinned",
        id="result-view-retry",
    ),
    pytest.param(
        "graph repair",
        "retry",
        "patch-only continuation preserved",
        id="graph-repair-retry",
    ),
    pytest.param(
        "chat Discuss",
        "repair_graph_update",
        "Work turn required",
        id="discuss-graph-repair",
    ),
    pytest.param(
        "ingestion",
        "repair_graph_update",
        "conversation required",
        id="ingestion-graph-repair",
    ),
    pytest.param(
        "paper coach",
        "repair_graph_update",
        "conversation required",
        id="paper-graph-repair",
    ),
    pytest.param(
        "Auto-research",
        "repair_graph_update",
        "conversation required",
        id="auto-research-graph-repair",
    ),
    pytest.param(
        "branch merge",
        "repair_graph_update",
        "conversation required",
        id="branch-merge-graph-repair",
    ),
    pytest.param(
        "episode report",
        "repair_graph_update",
        "conversation required",
        id="episode-report-graph-repair",
    ),
    pytest.param(
        "chat Work",
        "repair_graph_update",
        "repair child admitted atomically",
        id="work-graph-repair",
    ),
    pytest.param(
        "chat Work",
        "recover_at_startup",
        "active task interrupted",
        id="work-startup",
    ),
    pytest.param(
        "ingestion",
        "recover_at_startup",
        "active task interrupted",
        id="ingestion-startup",
    ),
    pytest.param(
        "paper coach",
        "recover_at_startup",
        "active task interrupted",
        id="paper-startup",
    ),
    pytest.param(
        "result-view revision",
        "recover_at_startup",
        "active task interrupted",
        id="result-view-startup",
    ),
    pytest.param(
        "Auto-research",
        "recover_at_startup",
        "committed dispatch preserved",
        id="auto-research-startup",
    ),
    pytest.param(
        "Experiment loop",
        "recover_at_startup",
        "stopping recovery delegated",
        id="experiment-startup",
    ),
    pytest.param(
        "episode report",
        "recover_at_startup",
        "report recovery delegated",
        id="episode-report-startup",
    ),
]


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


def _chat_request(*, mode: str = "work", **updates: object) -> RunRequest:
    return RunRequest.model_validate(
        {
            "provider": "codex",
            "model": "",
            "reasoning": "medium",
            "run_on": "laptop",
            "run_truth_scope": ["repo"],
            "chat_scope": "project",
            "chat_id": "policy-chat",
            "message": "Check the bounded policy.",
            "mode": mode,
            "patch_kind": "work",
            **updates,
        }
    )


def _ingestion_request() -> RunRequest:
    return RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        mode="work",
    )


def _experiment_request(
    *, session_id: str | None = None, trigger: str = "experiment_run"
) -> RunRequest:
    return RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="node",
        chat_id="experiment-chat",
        node_id="exp/policy",
        message="Continue the bounded experiment.",
        mode="work",
        trigger=trigger,
        patch_kind="experiment_loop",
        control_node_id="exp/policy",
        control_revision=1,
        control_episode_id="00000000-0000-4000-8000-000000000901",
        control_invocation=1,
        control_invocation_ceiling=3,
        control_completion_criteria=["The comparison is analyzed."],
        watcher_ids=["watcher-one"] if trigger == "watcher" else [],
        session_id=session_id,
    )


def _result_view_request() -> RunRequest:
    return RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="node",
        chat_id="result-view-chat",
        node_id="exp/result-view",
        message="Revise the result view.",
        mode="work",
        session_id="result-view-session",
        result_view={"action": "revise", "view_id": "a" * 24},
    )


def _auto_request() -> AutoResearchRunRequest:
    return AutoResearchRunRequest(
        episode_id="00000000-0000-4000-8000-000000000902",
        role="orchestrator",
        actor_operation_id="auto-root",
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
    )


def _branch_request() -> BranchMergeRunRequest:
    return BranchMergeRunRequest(
        episode_id="00000000-0000-4000-8000-000000000902",
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="project",
        mode="work",
        patch_kind="work",
    )


def _report_request() -> EpisodeReportRunRequest:
    return EpisodeReportRunRequest(
        episode_id="00000000-0000-4000-8000-000000000903",
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        execution_host="",
        session_id="report-session",
    )


def _record(
    store: AppStore,
    *,
    operation_id: str,
    kind: AgentTaskKind,
    request: RunRequest | CoachRequest,
    status: str,
    native_session_id: str | None = None,
    stage_root: str | None = None,
    result: dict[str, object] | None = None,
    continuation: str = "fresh",
    parent_operation_id: str | None = None,
) -> AgentTaskRecord:
    now = store.now()
    authority = resolve_dispatch_authority(kind, request)
    return store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id="project",
            kind=kind,
            status=status,
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            started_at=now,
            finished_at=now if status in {"failed", "succeeded", "interrupted"} else None,
            status_message=status,
            error="provider failed" if status == "failed" else None,
            result=result,
            parent_operation_id=parent_operation_id,
            native_session_id=native_session_id,
            stage_root=stage_root,
            phase=status,
            last_activity_at=now,
            authorized_by=fabricated_authorizer("Researcher"),
            dispatch_authority=authority,
        ),
        continuation_cause=continuation,
    )


def _detached_record(
    *,
    kind: AgentTaskKind,
    request: RunRequest | AutoResearchRunRequest | BranchMergeRunRequest | EpisodeReportRunRequest,
    status: str,
    native_session_id: str | None = None,
    stage_root: str | None = None,
) -> AgentTaskRecord:
    now = "2026-09-02T00:00:00+00:00"
    return AgentTaskRecord(
        operation_id=f"detached-{kind}",
        project_id="project",
        kind=kind,
        status=status,
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=now if status == "failed" else None,
        status_message=status,
        error="provider failed" if status == "failed" else None,
        native_session_id=native_session_id,
        stage_root=stage_root,
        phase=status,
        last_activity_at=now,
        can_resume=status == "paused" and native_session_id is not None,
        can_retry=status in {"paused", "failed", "interrupted"},
    )


async def _done_stream(_project_id, kind, _request, _execution):
    if kind in {"seed", "refresh"}:
        yield _sse(AgentEvent(event="message", text=json.dumps({"applied_revision": 1})))
    yield _sse(AgentEvent(event="done"))


def _start_case(family: str, store: AppStore, tmp_path: Path) -> str:
    tasks = BackgroundAgentTasks(store, _done_stream)
    authorizer = fabricated_authorizer("Researcher")
    if family == "chat Discuss":
        kind, request = "project_chat", _chat_request(mode="discuss")
    elif family == "chat Work":
        kind, request = "project_chat", _chat_request()
    elif family == "ingestion":
        kind, request = "refresh", _ingestion_request()
    elif family == "paper coach":
        kind, request = "paper_coach", CoachRequest(message="Review the introduction.")
    elif family == "Experiment loop":
        task = tasks.start(
            "project",
            "node_chat",
            _experiment_request(),
            operation_id="experiment-root",
            authorized_by=authorizer,
        )
        task = wait_for_task(store, task.operation_id, expect="succeeded")
        episode = store.episode("00000000-0000-4000-8000-000000000901")
        assert episode is not None and episode.root_operation_id == task.operation_id
        assert episode.invocations_used == 1
        return "root admitted atomically"
    elif family == "Experiment watcher wake":
        with pytest.raises(ValueError, match="dedicated admission path"):
            tasks.start(
                "project",
                "node_chat",
                _experiment_request(session_id="experiment-session", trigger="watcher"),
                authorized_by=authorizer,
            )
        return "dedicated admission required"
    elif family == "Auto-research":
        with pytest.raises(ValueError, match="start_auto_research"):
            tasks.start("project", "auto_research", _auto_request(), authorized_by=authorizer)
        return "dedicated admission required"
    elif family == "branch merge":
        with pytest.raises(ValueError, match="start_branch_merge"):
            tasks.start("project", "branch_merge", _branch_request(), authorized_by=authorizer)
        return "dedicated admission required"
    elif family == "episode report":
        with pytest.raises(ValueError, match="start_episode_report"):
            tasks.start("project", "episode_report", _report_request(), authorized_by=authorizer)
        return "dedicated admission required"
    elif family == "result-view revision":
        with pytest.raises(ValueError, match="saved native session and exact stage"):
            tasks.start(
                "project",
                "node_chat",
                _result_view_request(),
                authorized_by=authorizer,
            )
        return "saved session and stage required"
    else:  # pragma: no cover - the table is the closed caller set
        raise AssertionError(family)

    task = tasks.start("project", kind, request, authorized_by=authorizer)
    task = wait_for_task(store, task.operation_id, expect="succeeded")
    if family == "ingestion":
        assert task.applied_revision == 1
    return "fresh task admitted"


def _experiment_parent(
    store: AppStore,
    tasks: BackgroundAgentTasks,
    stage: Path,
    *,
    terminal_event: str,
) -> AgentTaskRecord:
    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        candidate = "{}"
        store.record_agent_task_contract(
            execution.operation_id,
            "experiment_episode_context_candidate",
            candidate,
            hashlib.sha256(candidate.encode()).hexdigest(),
        )
        yield _sse(AgentEvent(event="session", session_id="experiment-session"))
        yield _sse(AgentEvent(event=terminal_event, text="provider paused or failed"))

    tasks.stream = stream
    task = tasks.start(
        "project",
        "node_chat",
        _experiment_request(),
        operation_id="experiment-root",
        authorized_by=fabricated_authorizer("Researcher"),
    )
    return wait_for_task(
        store,
        task.operation_id,
        expect="paused" if terminal_event == "paused" else "failed",
    )


def _auto_parent(
    store: AppStore,
    tasks: BackgroundAgentTasks,
    stage: Path,
    *,
    terminal_event: str,
) -> AgentTaskRecord:
    async def stream(_project_id, _kind, _request, execution):
        if terminal_event == "paused":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="auto-session"))
        yield _sse(AgentEvent(event=terminal_event, text="provider paused or failed"))

    tasks.stream = stream
    _episode, task = start_auto_research(
        tasks,
        "project",
        AutoResearchStartRequest(
            invocation_ceiling=3,
            provider="codex",
            model="",
            reasoning="medium",
            run_on="laptop",
            run_truth_scope=["repo"],
        ),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="00000000-0000-4000-8000-000000000902",
        operation_id="auto-root",
    )
    return wait_for_task(
        store,
        task.operation_id,
        expect="paused" if terminal_event == "paused" else "failed",
    )


def _resume_case(
    family: str,
    store: AppStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    stage = tmp_path / "resume-stage"
    stage.mkdir()
    continuations: list[str] = []

    async def stream(_project_id, kind, _request, execution):
        continuations.append(execution.continuation)
        if kind in {"seed", "refresh"}:
            yield _sse(AgentEvent(event="message", text=json.dumps({"applied_revision": 1})))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    if family == "episode report":
        previous = _detached_record(
            kind="episode_report", request=_report_request(), status="paused"
        )
        monkeypatch.setattr(tasks, "_require_operation", lambda _operation_id: previous)
        with pytest.raises(ValueError, match="automatic.*no Resume"):
            tasks.resume(previous.operation_id)
        return "automatic recovery only"
    if family == "branch merge":
        previous = _detached_record(
            kind="branch_merge",
            request=_branch_request(),
            status="paused",
            native_session_id="branch-session",
            stage_root=str(stage),
        )
        monkeypatch.setattr(tasks, "_require_operation", lambda _operation_id: previous)
        with pytest.raises(TypeError, match="requires start_branch_merge"):
            tasks.resume(previous.operation_id, authorized_by=fabricated_authorizer("Researcher"))
        return "fresh merge dispatch required"
    if family == "Experiment loop":
        previous = _experiment_parent(store, tasks, stage, terminal_event="paused")
        tasks.stream = stream
        resumed = tasks.resume(previous.operation_id, authorized_by=previous.authorized_by)
        resumed = wait_for_task(store, resumed.operation_id, expect="succeeded")
        assert resumed.episode_id == "00000000-0000-4000-8000-000000000901"
        episode = store.episode("00000000-0000-4000-8000-000000000901")
        assert episode is not None and episode.invocations_used == 1
        assert continuations == ["resume"]
        return "episode recovery reused"
    if family == "Auto-research":
        previous = _auto_parent(store, tasks, stage, terminal_event="paused")
        tasks.stream = stream
        resumed = tasks.resume(previous.operation_id, authorized_by=previous.authorized_by)
        resumed = wait_for_task(store, resumed.operation_id, expect="succeeded")
        assert resumed.episode_id == "00000000-0000-4000-8000-000000000902"
        assert (
            store.episode_budget_meter("00000000-0000-4000-8000-000000000902").invocations_used == 1
        )
        assert continuations == ["resume"]
        return "paid allocation reused"
    if family == "graph repair":
        previous = _repair_child(store, tasks, stage, terminal_event="paused")
        tasks.stream = stream
        resumed = tasks.resume(previous.operation_id, authorized_by=previous.authorized_by)
        wait_for_task(store, resumed.operation_id, expect="succeeded")
        assert continuations == ["graph_repair"]
        return "patch-only continuation preserved"

    if family == "chat Work":
        kind, request = "project_chat", _chat_request()
    elif family == "ingestion":
        kind, request = "refresh", _ingestion_request()
    elif family == "paper coach":
        kind, request = "paper_coach", CoachRequest(message="Review the introduction.")
    else:  # pragma: no cover - the table is the closed caller set
        raise AssertionError(family)
    previous = _record(
        store,
        operation_id="resume-parent",
        kind=kind,
        request=request,
        status="paused",
        native_session_id="owned-session",
        stage_root=str(stage),
    )
    resumed = tasks.resume(previous.operation_id, authorized_by=previous.authorized_by)
    resumed = wait_for_task(store, resumed.operation_id, expect="succeeded")
    assert resumed.parent_operation_id == previous.operation_id
    assert resumed.native_session_id == previous.native_session_id
    assert resumed.stage_root == str(stage)
    assert continuations == ["resume"]
    return "owned checkpoint reused"


def _retry_case(
    family: str,
    store: AppStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    stage = tmp_path / "retry-stage"
    stage.mkdir()
    continuations: list[str] = []

    async def stream(_project_id, kind, _request, execution):
        continuations.append(execution.continuation)
        if kind in {"seed", "refresh"}:
            yield _sse(AgentEvent(event="message", text=json.dumps({"applied_revision": 1})))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    if family == "episode report":
        previous = _detached_record(
            kind="episode_report", request=_report_request(), status="failed"
        )
        monkeypatch.setattr(tasks, "_require_operation", lambda _operation_id: previous)
        with pytest.raises(ValueError, match="automatic.*no Retry"):
            tasks.retry(previous.operation_id)
        return "automatic recovery only"
    if family == "Experiment loop":
        previous = _experiment_parent(store, tasks, stage, terminal_event="error")
        with pytest.raises(ValueError, match="cannot change its pinned execution machine"):
            tasks.retry(
                previous.operation_id,
                run_on="remote",
                authorized_by=previous.authorized_by,
            )
        return "execution machine remains pinned"
    if family == "Auto-research":
        previous = _auto_parent(store, tasks, stage, terminal_event="error")
        with pytest.raises(ValueError, match="cannot change its pinned provider"):
            tasks.retry(
                previous.operation_id,
                provider="claude",
                authorized_by=previous.authorized_by,
            )
        return "provider profile remains pinned"
    if family == "branch merge":
        previous = _detached_record(
            kind="branch_merge",
            request=_branch_request(),
            status="failed",
        )
        monkeypatch.setattr(tasks, "_require_operation", lambda _operation_id: previous)
        with pytest.raises(TypeError, match="requires start_branch_merge"):
            tasks.retry(previous.operation_id, authorized_by=fabricated_authorizer("Researcher"))
        return "fresh merge dispatch required"
    if family == "result-view revision":
        previous = _record(
            store,
            operation_id="result-view-parent",
            kind="node_chat",
            request=_result_view_request(),
            status="failed",
            native_session_id="result-view-session",
            stage_root=str(stage),
        )
        with pytest.raises(ValueError, match="cannot start a fresh provider session"):
            tasks.retry(
                previous.operation_id,
                provider="claude",
                authorized_by=previous.authorized_by,
            )
        return "provider profile remains pinned"
    if family == "graph repair":
        previous = _repair_child(store, tasks, stage, terminal_event="error")
        tasks.stream = stream
        retried = tasks.retry(previous.operation_id, authorized_by=previous.authorized_by)
        wait_for_task(store, retried.operation_id, expect="succeeded")
        assert continuations == ["graph_repair"]
        return "patch-only continuation preserved"

    if family == "ingestion":
        kind, request = "refresh", _ingestion_request()
        checkpoint = False
    else:
        kind, request = "project_chat", _chat_request()
        checkpoint = family == "chat Work"
    previous = _record(
        store,
        operation_id="retry-parent",
        kind=kind,
        request=request,
        status="failed",
        native_session_id="owned-session" if checkpoint else None,
        stage_root=str(stage) if checkpoint else None,
    )
    retried = tasks.retry(previous.operation_id, authorized_by=previous.authorized_by)
    retried = wait_for_task(store, retried.operation_id, expect="succeeded")
    assert retried.parent_operation_id == previous.operation_id
    if checkpoint:
        assert retried.native_session_id == "owned-session"
        assert retried.stage_root == str(stage)
        assert continuations == ["retry"]
        return "same-provider checkpoint reused"
    assert retried.native_session_id is None
    assert retried.stage_root is None
    assert continuations == ["handoff"]
    return "clean handoff started"


def _repairable_parent(store: AppStore, stage: Path) -> AgentTaskRecord:
    return _record(
        store,
        operation_id="repair-parent",
        kind="project_chat",
        request=_chat_request(),
        status="succeeded",
        native_session_id="repair-session",
        stage_root=str(stage),
        result={
            "messages": ["The graph update needs correction."],
            "graph_update": {
                "status": "rejected",
                "applied_revision": None,
                "change_summary": [],
                "proposal_ids": [],
                "validation_messages": ["Patch requires correction."],
                "correction_rounds": 0,
                "repairable": True,
            },
        },
    )


def _repair_child(
    store: AppStore,
    tasks: BackgroundAgentTasks,
    stage: Path,
    *,
    terminal_event: str,
) -> AgentTaskRecord:
    parent = _repairable_parent(store, stage)

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(AgentEvent(event=terminal_event, text="repair paused or failed"))

    tasks.stream = stream
    child = tasks.repair_graph_update(
        parent.operation_id,
        authorized_by=parent.authorized_by,
    )
    return wait_for_task(
        store,
        child.operation_id,
        expect="paused" if terminal_event == "paused" else "failed",
    )


def _repair_case(
    family: str,
    store: AppStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    tasks = BackgroundAgentTasks(store, _done_stream)
    if family == "chat Work":
        stage = tmp_path / "repair-stage"
        stage.mkdir()
        parent = _repairable_parent(store, stage)
        child = tasks.repair_graph_update(
            parent.operation_id,
            authorized_by=parent.authorized_by,
        )
        child = wait_for_task(store, child.operation_id, expect="succeeded")
        assert child.parent_operation_id == parent.operation_id
        assert child.native_session_id == "repair-session"
        assert child.stage_root == str(stage)
        assert store.agent_task_continuation_cause(child.operation_id) == "graph_repair"
        parent = store.agent_task(parent.operation_id)
        assert parent is not None and parent.result is not None
        assert parent.result["graph_update"]["repairable"] is False
        return "repair child admitted atomically"
    if family == "chat Discuss":
        previous = _record(
            store,
            operation_id="discuss-parent",
            kind="project_chat",
            request=_chat_request(mode="discuss"),
            status="succeeded",
        )
        with pytest.raises(ValueError, match="Only a Work turn"):
            tasks.repair_graph_update(
                previous.operation_id,
                authorized_by=previous.authorized_by,
            )
        return "Work turn required"

    request_by_family = {
        "ingestion": ("refresh", _ingestion_request()),
        "paper coach": ("paper_coach", CoachRequest(message="Review the introduction.")),
        "Auto-research": ("auto_research", _auto_request()),
        "branch merge": ("branch_merge", _branch_request()),
        "episode report": ("episode_report", _report_request()),
    }
    kind, request = request_by_family[family]
    previous = _detached_record(kind=kind, request=request, status="succeeded")  # type: ignore[arg-type]
    monkeypatch.setattr(tasks, "_require_operation", lambda _operation_id: previous)
    with pytest.raises(ValueError, match="Only a conversation Work task"):
        tasks.repair_graph_update(previous.operation_id)
    return "conversation required"


def _recovery_case(
    family: str,
    store: AppStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    tasks = BackgroundAgentTasks(store, _done_stream)
    if family == "Auto-research":
        captured: list[set[str]] = []
        monkeypatch.setattr(
            "rcp.background.proven_committed_auto_research_dispatches",
            lambda _tasks: [SimpleNamespace(operation_id="committed-auto-dispatch")],
        )
        monkeypatch.setattr("rcp.background.proven_reserved_auto_research_roots", lambda _tasks: [])
        monkeypatch.setattr(
            store,
            "interrupt_active_agent_tasks",
            lambda *, preserve_operation_ids: captured.append(set(preserve_operation_ids)),
        )
        tasks.recover_at_startup()
        assert captured == [{"committed-auto-dispatch"}]
        return "committed dispatch preserved"
    if family == "Experiment loop":
        called: list[str] = []
        monkeypatch.setattr(
            "rcp.background.restart_stopping_experiment_recoveries",
            lambda _tasks: called.append("restart"),
        )
        monkeypatch.setattr(
            store,
            "settle_ready_experiment_loop_stops",
            lambda: called.append("settle"),
        )
        tasks.recover_at_startup()
        assert called == ["restart", "settle"]
        return "stopping recovery delegated"
    if family == "episode report":
        called: list[str] = []
        monkeypatch.setattr(
            "rcp.background.restart_interrupted_episode_reports",
            lambda _tasks: called.append("report"),
        )
        tasks.recover_at_startup()
        assert called == ["report"]
        return "report recovery delegated"

    stage = tmp_path / "startup-stage"
    stage.mkdir()
    if family == "chat Work":
        kind, request = "project_chat", _chat_request()
    elif family == "ingestion":
        kind, request = "refresh", _ingestion_request()
    elif family == "paper coach":
        kind, request = "paper_coach", CoachRequest(message="Review the introduction.")
    elif family == "result-view revision":
        kind, request = "node_chat", _result_view_request()
    else:  # pragma: no cover - the table is the closed caller set
        raise AssertionError(family)
    task = _record(
        store,
        operation_id="active-at-startup",
        kind=kind,
        request=request,
        status="running",
        native_session_id="startup-session",
        stage_root=str(stage),
    )
    tasks.recover_at_startup()
    recovered = store.agent_task(task.operation_id)
    assert recovered is not None and recovered.status == "interrupted"
    return "active task interrupted"


@pytest.mark.parametrize(("family", "entry_point", "expected"), POLICY_MATRIX)
def test_background_policy_matrix(
    family: str,
    entry_point: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    observed = {
        "start": lambda: _start_case(family, store, tmp_path),
        "resume": lambda: _resume_case(family, store, tmp_path, monkeypatch),
        "retry": lambda: _retry_case(family, store, tmp_path, monkeypatch),
        "repair_graph_update": lambda: _repair_case(family, store, tmp_path, monkeypatch),
        "recover_at_startup": lambda: _recovery_case(family, store, tmp_path, monkeypatch),
    }[entry_point]()

    assert observed == expected
