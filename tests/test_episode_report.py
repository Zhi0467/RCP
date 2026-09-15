from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.agents.write_scope import registered_repository_roots
from rcp.api.episodes import serialize_episode
from rcp.api.experiment_controls import _experiment_control_response
from rcp.background import AgentTaskExecution
from rcp.core.models import Experiment, GraphState
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.episodes.reconcile import EpisodeReconciler
from rcp.runs.tasks.episode_report import EpisodeReportRunRequest, stream_episode_report_run
from rcp.skill_registry import official_registry
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    EpisodeRecord,
    EpisodeWrapupRecord,
    ExperimentLoopRuntime,
    ProjectRecord,
)
from rcp.storage.episodes import compact_episode_receipt

from .helpers import fabricated_authorizer


class _ReportLauncher:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []

    async def stream(self, _provider, prompt, **kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        self.kwargs.append(kwargs)
        workspace = Path(kwargs["cwd"])
        if outcome == "valid":
            workspace.joinpath("episode-report.html").write_text(
                "<html><body><figure>Evidence map</figure></body></html>",
                encoding="utf-8",
            )
        elif outcome == "invalid":
            workspace.joinpath("episode-report.html").write_text("\x00", encoding="utf-8")
        elif outcome == "unsafe_output":
            workspace.joinpath("episode-report.html").mkdir()

        if outcome == "raise":
            raise OSError("provider launch failed")
        if outcome == "auth":
            yield AgentEvent(event="error", text="refresh_token_reused")
            return
        if outcome == "error":
            yield AgentEvent(event="session", session_id="native-session")
            yield AgentEvent(event="error", text="provider temporarily failed")
            return
        if outcome == "mismatch":
            yield AgentEvent(event="session", session_id="different-session")
            yield AgentEvent(event="done")
            return
        if outcome == "paused":
            yield AgentEvent(event="paused", text="provider interrupted")
            return
        yield AgentEvent(event="session", session_id="native-session")
        yield AgentEvent(event="answer", text="Report written.")
        yield AgentEvent(event="done")


@pytest.mark.parametrize(
    "wrapup_state",
    ["ready", "failed", "skipped", "legacy_unavailable"],
)
def test_auto_research_reconciliation_keeps_terminal_wrapup_immutable(
    monkeypatch,
    wrapup_state,
) -> None:
    episode = SimpleNamespace(
        mode="auto_research",
        project_id="project",
        graph_target=GraphTargetRef(),
        stop_requested_at=None,
        wrapup_state=wrapup_state,
    )
    store = SimpleNamespace(
        episode=lambda _episode_id: episode,
        unsettled_graph_target_tasks=lambda *_args: [],
        auto_research_child_experiments=lambda _episode_id: [],
    )
    background = SimpleNamespace()
    monkeypatch.setattr(
        "rcp.runs.episodes.reconcile.start_episode_report",
        lambda *_args: pytest.fail("terminal report restarted"),
    )
    monkeypatch.setattr(
        "rcp.runs.episodes.reconcile.auto_research_wrapup_spec",
        lambda *_args, **_kwargs: pytest.fail("terminal receipt rebuilt"),
    )

    EpisodeReconciler(store, background, logger=SimpleNamespace()).reconcile_auto_research_episode(
        "episode",
        source="watcher poll",
    )


@pytest.mark.parametrize("wrapup_state", ["pending", "running"])
def test_auto_research_reconciliation_restarts_persisted_wrapup_without_rebuilding(
    monkeypatch,
    wrapup_state,
) -> None:
    episode = SimpleNamespace(
        mode="auto_research",
        project_id="project",
        graph_target=GraphTargetRef(),
        stop_requested_at=None,
        wrapup_state=wrapup_state,
    )
    store = SimpleNamespace(
        episode=lambda _episode_id: episode,
        unsettled_graph_target_tasks=lambda *_args: [],
        auto_research_child_experiments=lambda _episode_id: [],
    )
    started: list[str] = []
    background = SimpleNamespace()
    monkeypatch.setattr(
        "rcp.runs.episodes.reconcile.start_episode_report",
        lambda _tasks, episode_id: started.append(episode_id),
    )
    monkeypatch.setattr(
        "rcp.runs.episodes.reconcile.auto_research_wrapup_spec",
        lambda *_args, **_kwargs: pytest.fail("persisted receipt rebuilt"),
    )

    EpisodeReconciler(store, background, logger=SimpleNamespace()).reconcile_auto_research_episode(
        "episode",
        source="watcher poll",
    )

    assert started == ["episode"]


def test_auto_research_reconciliation_degrades_persisted_wrapup_restart_failure(
    monkeypatch,
) -> None:
    episode = SimpleNamespace(
        mode="auto_research",
        project_id="project",
        graph_target=GraphTargetRef(),
        stop_requested_at=None,
        wrapup_state="pending",
    )
    receipts: list[tuple[object, ...]] = []
    store = SimpleNamespace(
        episode=lambda _episode_id: episode,
        unsettled_graph_target_tasks=lambda *_args: [],
        auto_research_child_experiments=lambda _episode_id: [],
        record_agent_task_receipt=lambda *args, **kwargs: receipts.append((*args, kwargs)),
        agent_task_receipts=lambda operation_id: [
            SimpleNamespace(category=category, payload=payload)
            for recorded_operation, category, payload, _kwargs in receipts
            if recorded_operation == operation_id
        ],
    )

    def fail_restart(_tasks: object, _episode_id: str) -> None:
        raise ValueError("allocation is unavailable")

    warnings: list[tuple[object, ...]] = []
    background = SimpleNamespace()
    monkeypatch.setattr("rcp.runs.episodes.reconcile.start_episode_report", fail_restart)
    logger = SimpleNamespace(warning=lambda *args: warnings.append(args))

    reconciler = EpisodeReconciler(store, background, logger=logger)
    for _ in range(2):
        reconciler.reconcile_auto_research_episode(
            "episode",
            source="startup",
            operation_id="operation",
        )

    assert len(warnings) == 1
    assert warnings[0][1:3] == ("episode", "startup")
    assert len(receipts) == 2
    assert str(warnings[0][3]) == "allocation is unavailable"
    assert receipts[0][0:2] == ("operation", "episode_report_reconciliation_failed")
    assert receipts[0][2]["detail"] == "allocation is unavailable"


def _setup_report(
    manifest,
    tmp_path: Path,
    *,
    ending: str = "completed",
) -> tuple[SimpleNamespace, AppStore, EpisodeReportRunRequest, AgentTaskExecution, Path]:
    store = AppStore(tmp_path / "app.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(manifest.path),
            name=manifest.name,
            state_location=str(manifest.path.parent),
            state_remote=False,
            added_at=store.now(),
        )
    )
    now = store.now()
    store.create_episode(
        EpisodeRecord(
            episode_id="episode",
            project_id="project",
            mode="experiment_loop",
            control_node_id="experiment-node",
            status="queued",
            invocation_ceiling=1,
            authorized_by=fabricated_authorizer("Episode owner"),
            created_at=now,
            updated_at=now,
        )
    )
    operational = AgentTaskRecord(
        operation_id="operation",
        project_id="project",
        episode_id="episode",
        # The generic episode parent is intentionally indifferent to the adapter's visible task
        # kind. This focused runner fixture uses a plain operational task so it does not have to
        # construct either mode adapter's authority contract.
        kind="node_chat",
        status="queued",
        request={},
        created_at=now,
        updated_at=now,
        status_message="Queued",
    )
    store.allocate_episode_invocation("episode", operational)

    stage = tmp_path / "episode-stage"
    stage.mkdir()
    request = EpisodeReportRunRequest(
        episode_id="episode",
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        execution_host="",
        session_id="native-session",
    )
    receipt_json, receipt_sha256 = compact_episode_receipt(
        {
            "ending": ending,
            "episode_id": "episode",
            "mode": "experiment_loop",
            "observations": ["one compact observation"],
        }
    )
    skill = official_registry().package("skill", "episode-report").reference()
    wrapup = EpisodeWrapupRecord(
        episode_id="episode",
        ending=ending,
        partial=ending != "completed",
        concluding_operation_id=operational.operation_id,
        allocation_operation_id="report-allocation",
        provider=request.provider,
        run_on=request.run_on,
        execution_host=request.execution_host,
        native_session_id=request.session_id,
        stage_host=None,
        stage_root=str(stage),
        skill_id=skill.id,
        skill_version=skill.version,
        output_name="episode-report.html",
        output_path=str(stage / "episode-report.html"),
        receipt_json=receipt_json,
        receipt_sha256=receipt_sha256,
        state="pending",
        created_at=now,
        updated_at=now,
    )
    hidden = AgentTaskRecord(
        operation_id="report-allocation",
        project_id="project",
        episode_id="episode",
        kind="episode_report",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="Wrapping up visualization and report",
        parent_operation_id=operational.operation_id,
        native_session_id=request.session_id,
        stage_host=None,
        stage_root=str(stage),
        visible=False,
        authorized_by=store.episode("episode").authorized_by,
    )
    store.begin_episode_wrapup("episode", wrapup, hidden)
    execution = AgentTaskExecution(
        operation_id=hidden.operation_id,
        store=store,
        control=AgentProcessControl(),
        stage_host=None,
        stage_root=str(stage),
    )
    return (
        SimpleNamespace(
            manifest=manifest,
            repository_ownership_inventory=lambda *, project_id: registered_repository_roots(
                manifest,
                project_id=project_id,
            ),
        ),
        store,
        request,
        execution,
        stage,
    )


async def _events(stream) -> list[AgentEvent]:
    return [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream
    ]


def test_episode_report_request_is_strict() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EpisodeReportRunRequest.model_validate(
            {
                "episode_id": "episode",
                "provider": "codex",
                "model": "",
                "reasoning": "medium",
                "run_on": "laptop",
                "execution_host": "",
                "session_id": "native-session",
                "graph_path": "/forbidden/graph.json",
            }
        )


def test_pending_episode_report_stage_is_protected_from_age_cleanup(manifest, tmp_path) -> None:
    _service, store, _request, _execution, stage = _setup_report(manifest, tmp_path)

    assert store.protected_run_stage_roots("") == (str(stage),)


@pytest.mark.asyncio
async def test_report_runner_stages_only_minimal_resume_inputs(manifest, tmp_path) -> None:
    service, store, request, execution, stage = _setup_report(manifest, tmp_path)
    launcher = _ReportLauncher(["valid"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["message", "done"]
    assert launcher.calls == 1
    assert launcher.kwargs[0]["session_id"] == "native-session"
    assert launcher.kwargs[0]["read_dirs"] == [stage / "inputs"]
    assert launcher.kwargs[0]["write_dirs"] == []
    assert launcher.kwargs[0]["write_scope"].writable_roots == [str(stage)]
    assert launcher.kwargs[0]["write_scope"].repository_roots == []
    assert launcher.kwargs[0]["capability"] == "work_auto"
    assert not any(
        item.name in {"graph.json", "research.md", "transcript.json", "repositories.json"}
        for item in stage.joinpath("inputs").rglob("*")
    )
    wrapup = store.episode_wrapup("episode")
    assert wrapup is not None
    receipt_path = next(stage.joinpath("inputs").glob("episode-report-receipt-*.json"))
    receipt = receipt_path.read_text(encoding="utf-8")
    assert receipt == wrapup.receipt_json
    assert hashlib.sha256(receipt.encode()).hexdigest() == wrapup.receipt_sha256
    report = store.episode_report("episode")
    assert report is not None
    assert report.html.startswith("<html>")
    assert [attempt.status for attempt in store.episode_report_attempts("episode")] == ["succeeded"]


@pytest.mark.asyncio
async def test_report_runner_never_inherits_orchestrator_capability(
    manifest, tmp_path, monkeypatch
) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    monkeypatch.setattr(store, "agent_task_profile", lambda _operation_id: "orchestrator")
    launcher = _ReportLauncher(["valid"])

    await _events(stream_episode_report_run(service, launcher, request, execution))

    assert launcher.kwargs[0]["capability"] == "work_auto"


@pytest.mark.asyncio
async def test_experiment_control_response_owns_wrapup_and_ready_report_state(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    state = GraphState(
        nodes={
            "experiment-node": Experiment(
                id="experiment-node",
                type="experiment",
                title="Report projection",
                objective="Render one bounded report.",
                invocation_ceiling=1,
                status="running",
            )
        }
    )

    def projected() -> dict[str, object]:
        episode = store.episode("episode")
        assert episode is not None
        response = _experiment_control_response(
            state,
            "experiment-node",
            ExperimentLoopRuntime(
                episode_id="episode",
                invocations_used=1,
                invocation_ceiling=1,
            ),
            serialize_episode(store, "project", episode),
        )
        return response.model_dump(mode="json")

    wrapping = projected()
    assert {
        field: wrapping[field]
        for field in ("health", "recommendation", "run_section", "can_open_report")
    } == {
        "health": "wrapping_up",
        "recommendation": "wait",
        "run_section": "running",
        "can_open_report": False,
    }

    assert wrapping["report_is_current"] is False

    events = await _events(
        stream_episode_report_run(
            service,
            _ReportLauncher(["valid"]),
            request,
            execution,
        )
    )

    assert [event.event for event in events] == ["message", "done"]
    ready = projected()
    assert {
        field: ready[field]
        for field in ("health", "recommendation", "run_section", "can_open_report")
    } == {
        "health": "completed",
        "recommendation": "open_report",
        "run_section": "completed",
        "can_open_report": True,
    }
    assert ready["report_episode_id"] == "episode"
    assert ready["report_is_current"] is True


@pytest.mark.asyncio
async def test_experiment_control_keeps_the_latest_report_when_a_newer_episode_is_stopped(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    events = await _events(
        stream_episode_report_run(
            service,
            _ReportLauncher(["valid"]),
            request,
            execution,
        )
    )
    assert [event.event for event in events] == ["message", "done"]

    stopped_at = store.now()
    accidental_episode_id = "00000000-0000-4000-8000-000000000002"
    store.create_episode(
        EpisodeRecord(
            episode_id=accidental_episode_id,
            project_id="project",
            mode="experiment_loop",
            control_node_id="experiment-node",
            status="queued",
            invocation_ceiling=1,
            authorized_by=fabricated_authorizer("Episode owner"),
            created_at=stopped_at,
            updated_at=stopped_at,
        )
    )
    store.allocate_episode_invocation(
        accidental_episode_id,
        AgentTaskRecord(
            operation_id="accidental-operation",
            project_id="project",
            episode_id=accidental_episode_id,
            kind="node_chat",
            status="queued",
            request={
                "trigger": "experiment_run",
                "patch_kind": "experiment_loop",
                "control_node_id": "experiment-node",
                "control_revision": 0,
                "control_episode_id": accidental_episode_id,
                "control_invocation": 1,
                "control_invocation_ceiling": 1,
                "control_decision_bundle": [],
                "control_completion_criteria": [],
            },
            created_at=stopped_at,
            updated_at=stopped_at,
            status_message="Queued",
        ),
    )
    store.mark_agent_task_running("accidental-operation")
    store.complete_agent_task("accidental-operation", applied_revision=None, result={})
    store.request_episode_stop(accidental_episode_id)
    stopped = store.mark_episode_stop_skipped(accidental_episode_id)
    assert stopped.ending == "stopped"

    read_model = store.experiment_control_projection_snapshots(
        "project",
        ["experiment-node"],
    )["experiment-node"]
    assert read_model.episode is not None
    assert read_model.episode.episode.episode_id == accidental_episode_id
    assert read_model.episode.report is None
    assert read_model.latest_report_episode_id == "episode"

    state = GraphState(
        nodes={
            "experiment-node": Experiment(
                id="experiment-node",
                type="experiment",
                title="Report projection",
                objective="Render one bounded report.",
                invocation_ceiling=1,
                status="completed",
            )
        }
    )
    response = _experiment_control_response(
        state,
        "experiment-node",
        read_model.runtime,
        serialize_episode(
            store,
            "project",
            read_model.episode.episode,
            projection_snapshot=read_model.episode,
        ),
        latest_report_episode_id=read_model.latest_report_episode_id,
    )

    assert response.episode is not None
    assert response.episode.episode_id == accidental_episode_id
    assert response.health == "completed"
    assert response.recommendation == "none"
    assert response.can_open_report is True
    assert response.report_episode_id == "episode"
    assert response.report_is_current is False


@pytest.mark.asyncio
async def test_missing_then_invalid_then_valid_uses_three_hidden_attempts(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    launcher = _ReportLauncher(["missing", "invalid", "valid"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["message", "done"]
    assert launcher.calls == 3
    assert [attempt.status for attempt in store.episode_report_attempts("episode")] == [
        "failed",
        "failed",
        "succeeded",
    ]
    episode = store.episode("episode")
    assert episode is not None
    assert episode.invocations_used == 1
    assert episode.report_attempts_used == 3
    assert episode.wrapup_state == "ready"


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [("error", "provider temporarily failed"), ("raise", "provider launch failed")],
)
@pytest.mark.asyncio
async def test_intermediate_provider_error_is_suppressed_and_retried(
    manifest,
    tmp_path,
    failure,
    expected_error,
) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    launcher = _ReportLauncher([failure, "valid"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["message", "done"]
    assert launcher.calls == 2
    attempts = store.episode_report_attempts("episode")
    assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
    assert attempts[0].error == expected_error


@pytest.mark.asyncio
async def test_changed_provider_session_fails_the_wrapup_without_another_call(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    launcher = _ReportLauncher(["mismatch", "valid"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["error"]
    assert "changed the frozen native provider session" in events[0].text
    assert launcher.calls == 1
    episode = store.episode("episode")
    assert episode is not None
    assert episode.wrapup_state == "failed"
    assert episode.report_attempts_used == 1
    assert store.episode_report("episode") is None


@pytest.mark.asyncio
async def test_durable_binding_mismatch_is_unlaunchable_and_terminal(manifest, tmp_path) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    launcher = _ReportLauncher(["valid"])
    changed = request.model_copy(update={"session_id": "other-session"})

    events = await _events(stream_episode_report_run(service, launcher, changed, execution))

    assert [event.event for event in events] == ["error"]
    assert "differs from its durable hidden task" in events[0].text
    assert launcher.calls == 0
    episode = store.episode("episode")
    assert episode is not None
    assert episode.wrapup_state == "failed"
    assert episode.report_attempts_used == 0
    assert store.episode_report_attempts("episode") == []


@pytest.mark.asyncio
async def test_lost_exact_stage_fails_without_fabricating_provider_attempt(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, stage = _setup_report(manifest, tmp_path)
    stage.rmdir()
    launcher = _ReportLauncher(["valid"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["error"]
    assert "saved local stage is unavailable" in events[0].text
    assert launcher.calls == 0
    episode = store.episode("episode")
    assert episode is not None
    assert episode.wrapup_state == "failed"
    assert episode.report_attempts_used == 0
    assert store.episode_report_attempts("episode") == []


@pytest.mark.asyncio
async def test_restart_with_queued_attempt_and_lost_stage_terminalizes(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, stage = _setup_report(manifest, tmp_path)
    queued = store.allocate_episode_report_attempt("episode")
    stage.rmdir()
    launcher = _ReportLauncher(["valid"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["error"]
    assert "saved local stage is unavailable" in events[0].text
    assert launcher.calls == 0
    episode = store.episode("episode")
    assert episode is not None
    assert episode.wrapup_state == "failed"
    assert episode.report_attempts_used == 1
    attempt = store.episode_report_attempt(queued.attempt_id)
    assert attempt is not None
    assert attempt.status == "failed"
    assert store.episode_report("episode") is None


@pytest.mark.asyncio
async def test_setup_loss_between_calls_fails_without_allocating_the_next_attempt(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    launcher = _ReportLauncher(["unsafe_output", "valid"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["error"]
    assert "unsafe directory" in events[0].text
    assert launcher.calls == 1
    attempts = store.episode_report_attempts("episode")
    assert [attempt.status for attempt in attempts] == ["failed"]
    episode = store.episode("episode")
    assert episode is not None
    assert episode.wrapup_state == "failed"
    assert episode.report_attempts_used == 1


@pytest.mark.asyncio
async def test_three_errors_end_nonblocking_without_retry_controls(manifest, tmp_path) -> None:
    service, store, request, execution, _stage = _setup_report(
        manifest,
        tmp_path,
        ending="completed",
    )
    launcher = _ReportLauncher(["missing", "missing", "missing"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["error"]
    assert "failed after 3 attempts" in events[0].text
    episode = store.episode("episode")
    assert episode is not None
    assert episode.status == "completed"
    assert episode.wrapup_state == "failed"
    assert episode.report_attempts_used == 3
    hidden = store.agent_task("report-allocation")
    assert hidden is not None
    assert hidden.visible is False
    assert hidden.can_resume is False
    assert hidden.can_retry is False
    assert store.episode_report("episode") is None


@pytest.mark.asyncio
async def test_provider_pause_without_shutdown_is_suppressed_and_retried(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    launcher = _ReportLauncher(["paused", "valid"])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["message", "done"]
    attempts = store.episode_report_attempts("episode")
    assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
    episode = store.episode("episode")
    assert episode is not None
    assert episode.wrapup_state == "ready"
    assert episode.report_attempts_used == 2


@pytest.mark.asyncio
async def test_shutdown_pause_requeues_same_hidden_allocation_and_resumes_automatically(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, stage = _setup_report(manifest, tmp_path)
    execution.control.request_pause()

    paused_events = await _events(
        stream_episode_report_run(service, _ReportLauncher(["paused"]), request, execution)
    )

    assert [event.event for event in paused_events] == ["paused"]
    store.pause_agent_task(
        execution.operation_id,
        detail="Paused for RCP shutdown or reload.",
    )
    requeued = store.requeue_interrupted_episode_report_allocation("episode")
    assert requeued.operation_id == execution.operation_id
    assert requeued.status == "queued"
    assert requeued.visible is False
    resumed_execution = AgentTaskExecution(
        operation_id=execution.operation_id,
        store=store,
        control=AgentProcessControl(),
        stage_host=None,
        stage_root=str(stage),
    )

    resumed_events = await _events(
        stream_episode_report_run(
            service,
            _ReportLauncher(["valid"]),
            request,
            resumed_execution,
        )
    )

    assert [event.event for event in resumed_events] == ["message", "done"]
    assert [attempt.status for attempt in store.episode_report_attempts("episode")] == [
        "failed",
        "succeeded",
    ]
    assert store.episode_report("episode") is not None


@pytest.mark.asyncio
async def test_restart_reconciles_valid_output_from_running_attempt_without_relaunch(
    manifest,
    tmp_path,
) -> None:
    service, store, request, execution, stage = _setup_report(manifest, tmp_path)
    attempt = store.allocate_episode_report_attempt("episode")
    store.mark_episode_report_attempt_running(attempt.attempt_id)
    stage.joinpath("episode-report.html").write_text(
        "<html><body><figure>Recovered report</figure></body></html>",
        encoding="utf-8",
    )
    launcher = _ReportLauncher([])

    events = await _events(stream_episode_report_run(service, launcher, request, execution))

    assert [event.event for event in events] == ["message", "done"]
    assert launcher.calls == 0
    assert store.episode_report("episode") is not None


@pytest.mark.asyncio
async def test_report_login_failure_marks_account_and_parks_without_retry(manifest, tmp_path):
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")

    class LoginFailureLauncher:
        calls = 0

        async def stream(self, _provider, _prompt, **_kwargs):
            self.calls += 1
            yield AgentEvent(event="error", text="refresh_token_reused")

    launcher = LoginFailureLauncher()
    events = await _events(stream_episode_report_run(service, launcher, request, execution))
    assert launcher.calls == 1
    assert [event.event for event in events] == ["error"]
    assert "signed in again" in events[0].text
    state = store.provider_login_state(request.provider, request.execution_host)
    assert state.state == "signed_out"
    assert state.source == "report"
    assert state.generation == execution.login_generation == 1
    hidden = store.agent_task("report-allocation")
    assert hidden.status == "failed"
    assert hidden.failure_kind == "provider_auth"
    episode = store.episode(request.episode_id)
    assert episode.wrapup_state == "pending"
    assert episode.report_attempts_used == 1


def test_signed_out_report_allocation_stays_queued_and_pending(manifest, tmp_path):
    from rcp.background import BackgroundAgentTasks
    from rcp.runs.episodes.report import start_episode_report

    _service, store, request, _execution, _stage = _setup_report(manifest, tmp_path)
    store.mark_provider_login_failed("codex", "", generation=0, detail="expired", source="report")
    tasks = BackgroundAgentTasks(store, None)
    before = store.episode(request.episode_id)
    with store.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    assert start_episode_report(tasks, request.episode_id) is None
    after = store.episode(request.episode_id)
    assert after.wrapup_state == "pending"
    assert after.invocations_used == before.invocations_used
    assert after.report_attempts_used == before.report_attempts_used
    assert store.agent_task("report-allocation").status == "queued"
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == count


def test_third_report_auth_failure_is_counted_and_parked(manifest, tmp_path):
    _service, store, request, _execution, _stage = _setup_report(manifest, tmp_path)
    for _ in range(2):
        attempt = store.allocate_episode_report_attempt(request.episode_id)
        store.mark_episode_report_attempt_running(attempt.attempt_id)
        store.record_episode_report_attempt_error(attempt.attempt_id, "invalid report")
    attempt = store.allocate_episode_report_attempt(request.episode_id)
    store.mark_episode_report_attempt_running(attempt.attempt_id)
    episode, failed = store.record_episode_report_attempt_error(
        attempt.attempt_id, "refresh_token_reused", provider_auth=True
    )
    assert failed.status == "failed"
    assert episode.report_attempts_used == 3
    assert episode.wrapup_state == "pending"
    assert episode.status == "wrapping_up"


def test_parked_report_resumes_same_allocation_after_verify(manifest, tmp_path):
    from rcp.background import BackgroundAgentTasks
    from rcp.runs.episodes.report import start_episode_report

    from .helpers import wait_for_task, wait_until

    service, store, request, _execution, _stage = _setup_report(manifest, tmp_path)
    launcher = _ReportLauncher(["auth", "valid"])

    async def stream(_project_id, _kind, request, execution):
        async for frame in stream_episode_report_run(service, launcher, request, execution):
            yield frame

    tasks = BackgroundAgentTasks(store, stream)
    first = start_episode_report(tasks, request.episode_id)
    assert first is not None
    wait_for_task(store, first.operation_id, expect="failed")
    wait_until(lambda: first.operation_id not in tasks._workers)
    assert store.episode(request.episode_id).wrapup_state == "pending"
    assert store.agent_task(first.operation_id).failure_kind == "provider_auth"
    assert start_episode_report(tasks, request.episode_id) is None
    assert launcher.calls == 1
    with store.connection() as connection:
        task_count = connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")
    restarted = start_episode_report(tasks, request.episode_id)
    assert restarted.operation_id == first.operation_id
    wait_for_task(store, restarted.operation_id, expect="succeeded")
    assert launcher.calls == 2
    assert store.episode(request.episode_id).wrapup_state == "ready"
    assert store.episode(request.episode_id).report_attempts_used == 2
    assert store.agent_task(restarted.operation_id).failure_kind is None
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == task_count
    assert start_episode_report(tasks, request.episode_id) is None


def test_verified_third_auth_failure_settles_at_existing_attempt_limit(manifest, tmp_path):
    from rcp.background import BackgroundAgentTasks
    from rcp.runs.episodes.report import start_episode_report

    _service, store, request, _execution, _stage = _setup_report(manifest, tmp_path)
    for number in range(3):
        attempt = store.allocate_episode_report_attempt(request.episode_id)
        store.mark_episode_report_attempt_running(attempt.attempt_id)
        store.record_episode_report_attempt_error(
            attempt.attempt_id,
            "refresh_token_reused" if number == 2 else "invalid report",
            provider_auth=number == 2,
        )
    store.mark_provider_login_failed(
        "codex", "", generation=0, detail="refresh_token_reused", source="report"
    )
    tasks = BackgroundAgentTasks(store, None)
    assert start_episode_report(tasks, request.episode_id) is None
    assert store.episode(request.episode_id).wrapup_state == "pending"
    store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")
    assert start_episode_report(tasks, request.episode_id) is None
    episode = store.episode(request.episode_id)
    assert episode.wrapup_state == "failed"
    assert episode.status == "completed"
    assert episode.report_attempts_used == 3


@pytest.mark.asyncio
async def test_login_block_after_report_dispatch_spends_no_attempt_and_is_recoverable(
    manifest, tmp_path
):
    service, store, request, execution, _stage = _setup_report(manifest, tmp_path)
    store.mark_provider_login_failed("codex", "", generation=0, detail="expired", source="turn")
    launcher = _ReportLauncher([])
    events = await _events(stream_episode_report_run(service, launcher, request, execution))
    assert [event.event for event in events] == ["error"]
    assert launcher.calls == 0
    episode = store.episode(request.episode_id)
    assert episode.report_attempts_used == 0 and episode.wrapup_state == "pending"
    task = store.agent_task(execution.operation_id)
    assert task.failure_kind == "provider_auth"
    store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")
    queued = store.requeue_interrupted_episode_report_allocation(request.episode_id)
    assert queued.operation_id == execution.operation_id and queued.status == "queued"
