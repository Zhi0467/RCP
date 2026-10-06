from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.api.episodes import serialize_episode
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.config import Manifest, write_agent_settings
from rcp.core.models import Experiment, Patch
from rcp.core.transition_models import GraphHeadRef
from rcp.history import HistoryManager
from rcp.loop_status import other_branch_loops
from rcp.paper import PaperService
from rcp.runs.auto_research import AutoResearchStartRequest
from rcp.runs.auto_research_admission import (
    start_auto_research,
    start_auto_research_child_experiment,
)
from rcp.runs.auto_research_experiments import (
    AutoResearchExperimentCoordinator,
    AutoResearchExperimentLimitInvalid,
)
from rcp.runs.experiment_admission import experiment_start_message, fresh_experiment_run_request
from rcp.service import ProjectService, RunRequest
from rcp.storage import (
    AppStore,
    AutoResearchChildAdmissionRecord,
    AutoResearchChildExperimentRecord,
    AutoResearchExperimentAllowanceReached,
    ProjectRecord,
)

from .helpers import (
    async_wait_until,
    fabricated_authorizer,
    record_launched_experiment_turn,
    store_test_claude_token,
    wait_for_task,
)

EXPERIMENT_ID = "exp/orchestrated-loop"
PROJECT_ID = "project"
CHILD_EXPLICIT = "00000000-0000-4000-8000-000000000401"
CHILD_FALLBACK = "00000000-0000-4000-8000-000000000402"
CHILD_OVER_LIMIT = "00000000-0000-4000-8000-000000000403"
CHILD_REPLAY = "00000000-0000-4000-8000-000000000404"
HUMAN_PREDECESSOR = "00000000-0000-4000-8000-000000000405"
EXECUTION_PROFILES = (
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
    "orchestrator",
)


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _experiment_patch() -> Patch:
    return Patch(
        kind="seed",
        author="agent",
        summary="Added the orchestrated Experiment fixture.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": EXPERIMENT_ID,
                        "type": "experiment",
                        "title": "Orchestrated loop",
                        "objective": "Exercise child Experiment admission.",
                        "completion_criteria": ["The bounded comparison finishes."],
                        "invocation_ceiling": 4,
                    }
                ],
            }
        ],
    )


def _blocking_patch() -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Recorded a new Experiment blocker.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "blk/new-readiness-failure",
                        "type": "blocker",
                        "title": "New readiness failure",
                        "description": "The replacement cannot start until this is resolved.",
                        "status": "open",
                    }
                ],
            },
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": EXPERIMENT_ID,
                        "target": "blk/new-readiness-failure",
                        "relation": "blocked_by",
                    }
                ],
            },
        ],
    )


def _service(manifest: Manifest, tmp_path: Path) -> ProjectService:
    history = HistoryManager(manifest)
    history.append(_experiment_patch())
    paper_store = AppStore(tmp_path / "paper.sqlite3")
    return ProjectService(
        manifest,
        history,
        PaperService(manifest, paper_store, history.workspace, project_id=PROJECT_ID),
        data_dir=tmp_path / "service-data",
        project_id=PROJECT_ID,
    )


def _auto_start(*, ceiling: int) -> AutoResearchStartRequest:
    return AutoResearchStartRequest(
        code_worktree=False,
        invocation_ceiling=ceiling,
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo-a"],
    )


def _admit(
    store: AppStore,
    *,
    parent_episode_id: str,
    child_episode_id: str,
    admission_id: str,
) -> None:
    parent = store.episode(parent_episode_id)
    assert parent is not None
    now = store.now()
    store.record_auto_research_child_admission(
        AutoResearchChildAdmissionRecord(
            admission_id=admission_id,
            episode_id=parent_episode_id,
            project_id=parent.project_id,
            child_kind="experiment",
            child_id=child_episode_id,
            state="accepted",
            created_at=now,
            updated_at=now,
        )
    )


def _spend_child_experiment_allowance(
    store: AppStore,
    background: BackgroundAgentTasks,
    *,
    parent_episode_id: str,
    parent_operation_id: str,
    child_episode_id: str,
    node_id: str,
) -> None:
    now = store.now()
    route = AutoResearchChildExperimentRecord(
        child_episode_id=child_episode_id,
        auto_research_episode_id=parent_episode_id,
        project_id=PROJECT_ID,
        control_node_id=node_id,
        state="running",
        request={"goal": None, "invocation_limit": None},
        parent_operation_id=parent_operation_id,
        created_at=now,
        updated_at=now,
    )
    request = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_id=child_episode_id,
        chat_scope="node",
        node_id=node_id,
        message=experiment_start_message(None, node_id),
        mode="work",
        trigger="orchestrator",
        patch_kind="experiment_loop",
        control_node_id=node_id,
        control_revision=1,
        control_episode_id=child_episode_id,
        control_invocation=1,
        control_invocation_ceiling=1,
        control_decision_bundle=[],
        control_completion_criteria=["The bounded allowance probe is analyzed."],
    )
    spent = start_auto_research_child_experiment(background, route, request)
    wait_for_task(store, spent.operation_id, expect="succeeded")


def _setup(
    manifest: Manifest,
    tmp_path: Path,
    *,
    ceiling: int = 2,
    child_stream=None,
) -> tuple[
    ProjectService,
    AppStore,
    BackgroundAgentTasks,
    AutoResearchExperimentCoordinator,
    str,
    str,
]:
    service = _service(manifest, tmp_path)
    store = AppStore(tmp_path / "app.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id=PROJECT_ID,
            locator=str(manifest.path),
            name="Project",
            state_location=str(manifest.research_dir),
            state_remote=False,
            added_at=store.now(),
        )
    )

    async def stream(
        project_id: str,
        kind: str,
        request: object,
        execution: AgentTaskExecution,
    ) -> AsyncIterator[str]:
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        if child_stream is None:
            yield _sse(AgentEvent(event="done"))
            return
        async for frame in child_stream(project_id, kind, request, execution):
            yield frame

    background = BackgroundAgentTasks(store, stream)
    parent, root = start_auto_research(
        background,
        PROJECT_ID,
        _auto_start(ceiling=ceiling),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id=f"parent-{tmp_path.name}",
        operation_id=f"root-{tmp_path.name}",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    assert parent.graph_target.kind == "branch"
    assert parent.graph_target.branch_id == parent.episode_id
    assert parent.graph_base_head == GraphHeadRef(revision=0)
    assert root.graph_target == parent.graph_target
    coordinator = AutoResearchExperimentCoordinator(
        store,
        background,
        project_service=lambda project_id, _episode_id: (
            service if project_id == PROJECT_ID else None
        ),  # type: ignore[return-value]
        operation_lock=lambda _project_id: nullcontext(),
    )
    return service, store, background, coordinator, parent.episode_id, root.operation_id


@pytest.mark.parametrize(
    ("goal", "invocation_limit", "expected_message", "expected_ceiling"),
    [
        (
            "Measure whether the repaired runtime survives a concise probe.",
            6,
            "Measure whether the repaired runtime survives a concise probe.",
            6,
        ),
        (None, None, f"Begin a bounded Experiment-loop episode for {EXPERIMENT_ID}.", 4),
    ],
)
def test_kickoff_preserves_optional_goal_and_uses_current_node_work_profile(
    manifest: Manifest,
    tmp_path: Path,
    goal: str | None,
    invocation_limit: int | None,
    expected_message: str,
    expected_ceiling: int,
) -> None:
    profiles = {
        name: manifest.agent_profile(name).model_copy(deep=True) for name in EXECUTION_PROFILES
    }
    profiles["node_chat"] = profiles["node_chat"].model_copy(
        update={
            "provider": "claude",
            "runtime": "stream-json",
            "model": "current-node-work",
            "reasoning": "low",
        }
    )
    profiles["orchestrator"] = profiles["orchestrator"].model_copy(
        update={"provider": "codex", "model": "orchestrator-only", "reasoning": "high"}
    )
    manifest = write_agent_settings(
        manifest,
        list(manifest.agent.default_run_truth_scope),
        profiles,
    )
    _, store, _, coordinator, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        ceiling=2,
    )
    store_test_claude_token(store)
    child_id = CHILD_EXPLICIT if goal is not None else CHILD_FALLBACK
    admission_id = f"admission-{child_id}"
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=child_id,
        admission_id=admission_id,
    )

    action = coordinator.kick_off(
        auto_research_episode_id=parent_id,
        parent_operation_id=root_id,
        child_episode_id=child_id,
        node_id=EXPERIMENT_ID,
        goal=goal,
        goal_sha256=hashlib.sha256(goal.encode()).hexdigest() if goal is not None else None,
        invocation_limit=invocation_limit,
        admission_id=admission_id,
    )

    assert action.disposition == "created"
    task = store.agent_task(action.operation_id or "")
    assert task is not None
    child = store.episode(child_id)
    assert child is not None
    assert child.graph_target == task.graph_target
    assert child.graph_target.kind == "branch"
    assert child.graph_target.branch_id == parent_id
    assert child.graph_base_head == GraphHeadRef(revision=0)
    assert task.request["message"] == expected_message
    assert task.request["control_invocation_ceiling"] == expected_ceiling
    assert (task.request["provider"], task.request["model"], task.request["reasoning"]) == (
        "claude",
        "current-node-work",
        "low",
    )
    assert task.request["model"] != "orchestrator-only"
    assert task.request["trigger"] == "orchestrator"
    assert action.allowance.model_dump() == {"total": 10, "used": 1, "remaining": 9}


def test_explicit_invocation_limit_over_total_allowance_is_pre_admission(
    manifest: Manifest,
    tmp_path: Path,
) -> None:
    _, store, _, coordinator, parent_id, root_id = _setup(manifest, tmp_path, ceiling=1)
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=CHILD_OVER_LIMIT,
        admission_id="admission-over-limit",
    )

    with pytest.raises(AutoResearchExperimentLimitInvalid) as caught:
        coordinator.kick_off(
            auto_research_episode_id=parent_id,
            parent_operation_id=root_id,
            child_episode_id=CHILD_OVER_LIMIT,
            node_id=EXPERIMENT_ID,
            goal=None,
            goal_sha256=None,
            invocation_limit=6,
            admission_id="admission-over-limit",
        )

    assert caught.value.allowance.model_dump() == {"total": 5, "used": 0, "remaining": 5}
    assert store.auto_research_child_experiment(CHILD_OVER_LIMIT) is None
    admission = store.auto_research_child_admission("admission-over-limit")
    assert admission is not None and admission.state == "accepted"


def test_exhausted_allowance_does_not_reserve_or_stop_an_active_predecessor(
    manifest: Manifest,
    tmp_path: Path,
) -> None:
    predecessor_started = threading.Event()
    release_predecessor = threading.Event()

    async def child_stream(_project_id, _kind, request, _execution):
        if request.control_episode_id == HUMAN_PREDECESSOR:
            predecessor_started.set()
            await async_wait_until(release_predecessor.is_set)
        yield _sse(AgentEvent(event="done"))

    service, store, background, coordinator, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        ceiling=1,
        child_stream=child_stream,
    )
    parent = store.episode(parent_id)
    assert parent is not None and parent.authorized_by is not None

    for index in range(5):
        _spend_child_experiment_allowance(
            store,
            background,
            parent_episode_id=parent_id,
            parent_operation_id=root_id,
            child_episode_id=f"00000000-0000-4000-8000-00000000042{index}",
            node_id=f"exp/allowance-{index}",
        )

    exhausted = store.auto_research_experiment_allowance(parent_id)
    assert exhausted.model_dump() == {"total": 5, "used": 5, "remaining": 0}
    predecessor = background.start(
        PROJECT_ID,
        "node_chat",
        _human_experiment_request(service, HUMAN_PREDECESSOR),
        authorized_by=parent.authorized_by,
    )
    assert predecessor_started.wait(timeout=2)
    child_id = "00000000-0000-4000-8000-000000000429"
    admission_id = "admission-exhausted-with-predecessor"
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=child_id,
        admission_id=admission_id,
    )

    with pytest.raises(AutoResearchExperimentAllowanceReached) as caught:
        coordinator.kick_off(
            auto_research_episode_id=parent_id,
            parent_operation_id=root_id,
            child_episode_id=child_id,
            node_id=EXPERIMENT_ID,
            goal=None,
            goal_sha256=None,
            invocation_limit=None,
            admission_id=admission_id,
        )

    assert caught.value.allowance == exhausted
    assert store.auto_research_child_experiment(child_id) is None
    predecessor_episode = store.episode(HUMAN_PREDECESSOR)
    assert predecessor_episode is not None
    assert predecessor_episode.stop_requested_at is None
    admission = store.auto_research_child_admission(admission_id)
    assert admission is not None and admission.state == "accepted"

    release_predecessor.set()
    wait_for_task(store, predecessor.operation_id, expect="succeeded")


def test_allowance_is_rechecked_after_waiting_for_experiment_operation_lock(
    manifest: Manifest,
    tmp_path: Path,
) -> None:
    predecessor_started = threading.Event()
    release_predecessor = threading.Event()

    async def child_stream(_project_id, _kind, request, _execution):
        if request.control_episode_id == HUMAN_PREDECESSOR:
            predecessor_started.set()
            await async_wait_until(release_predecessor.is_set)
        yield _sse(AgentEvent(event="done"))

    service, store, background, _, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        ceiling=1,
        child_stream=child_stream,
    )
    parent = store.episode(parent_id)
    assert parent is not None and parent.authorized_by is not None
    for index in range(4):
        _spend_child_experiment_allowance(
            store,
            background,
            parent_episode_id=parent_id,
            parent_operation_id=root_id,
            child_episode_id=f"00000000-0000-4000-8000-00000000043{index}",
            node_id=f"exp/race-allowance-{index}",
        )
    assert store.auto_research_experiment_allowance(parent_id).remaining == 1

    predecessor = background.start(
        PROJECT_ID,
        "node_chat",
        _human_experiment_request(service, HUMAN_PREDECESSOR),
        authorized_by=parent.authorized_by,
    )
    assert predecessor_started.wait(timeout=2)
    replacement_id = "00000000-0000-4000-8000-000000000435"
    admission_id = "admission-exhausted-while-waiting"
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=replacement_id,
        admission_id=admission_id,
    )

    operation_gate = threading.Lock()
    operation_gate.acquire()
    waiting_for_lock = threading.Event()

    @contextmanager
    def locked_operation():
        waiting_for_lock.set()
        with operation_gate:
            yield

    coordinator = AutoResearchExperimentCoordinator(
        store,
        background,
        project_service=lambda project_id, _episode_id: (
            service if project_id == PROJECT_ID else None
        ),  # type: ignore[return-value]
        operation_lock=lambda _project_id: locked_operation(),
    )
    arguments = {
        "auto_research_episode_id": parent_id,
        "parent_operation_id": root_id,
        "child_episode_id": replacement_id,
        "node_id": EXPERIMENT_ID,
        "goal": None,
        "goal_sha256": None,
        "invocation_limit": None,
        "admission_id": admission_id,
    }

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(coordinator.kick_off, **arguments)
            assert waiting_for_lock.wait(timeout=2)
            _spend_child_experiment_allowance(
                store,
                background,
                parent_episode_id=parent_id,
                parent_operation_id=root_id,
                child_episode_id="00000000-0000-4000-8000-000000000434",
                node_id="exp/race-allowance-final",
            )
            operation_gate.release()
            with pytest.raises(AutoResearchExperimentAllowanceReached) as caught:
                future.result(timeout=2)

        assert caught.value.allowance.model_dump() == {
            "total": 5,
            "used": 5,
            "remaining": 0,
        }
        assert store.auto_research_child_experiment(replacement_id) is None
        predecessor_episode = store.episode(HUMAN_PREDECESSOR)
        assert predecessor_episode is not None
        assert predecessor_episode.stop_requested_at is None
    finally:
        if operation_gate.locked():
            operation_gate.release()
        release_predecessor.set()
        wait_for_task(store, predecessor.operation_id, expect="succeeded")


def test_kickoff_replay_is_deterministic_and_exact_resume_does_not_respend_e(
    manifest: Manifest,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "experiment-stage"
    stage.mkdir()
    resumed_requests: list[RunRequest] = []

    async def child_stream(_project_id, _kind, request, execution):
        assert isinstance(request, RunRequest)
        if execution.continuation == "fresh":
            record_launched_experiment_turn(execution.store, execution.operation_id)
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="child-experiment-session"))
            yield _sse(AgentEvent(event="error", text="Transient network failure."))
            return
        resumed_requests.append(request)
        yield _sse(AgentEvent(event="done"))

    _, store, _, coordinator, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        child_stream=child_stream,
    )
    goal = "Verify the bounded runtime repair."
    digest = hashlib.sha256(goal.encode()).hexdigest()
    child_id = CHILD_REPLAY
    admission_id = "admission-replay"
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=child_id,
        admission_id=admission_id,
    )
    parameters = {
        "auto_research_episode_id": parent_id,
        "parent_operation_id": root_id,
        "child_episode_id": child_id,
        "node_id": EXPERIMENT_ID,
        "goal": goal,
        "goal_sha256": digest,
        "invocation_limit": None,
        "admission_id": admission_id,
    }

    created = coordinator.kick_off(**parameters)
    assert created.operation_id is not None
    failed = wait_for_task(store, created.operation_id, expect="failed")
    replayed = coordinator.kick_off(**parameters)

    assert replayed.disposition == "existing"
    assert replayed.operation_id == created.operation_id
    assert store.auto_research_experiment_allowance(parent_id).used == 1

    resume_operation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:auto_research:{parent_id}:episode:resume-child-replay",
        )
    )
    resumed = coordinator.resume(
        parent_id,
        child_id,
        operation_id=resume_operation_id,
    )
    assert resumed.disposition == "resumed"
    assert resumed.operation_id is not None
    recovered = wait_for_task(store, resumed.operation_id, expect="succeeded")
    assert recovered.parent_operation_id == failed.operation_id
    assert recovered.native_session_id == "child-experiment-session"
    assert resumed_requests[0].session_id == "child-experiment-session"
    assert store.auto_research_experiment_allowance(parent_id).used == 1

    # A same-key retry after the recovery row committed returns that exact row;
    # it neither creates another attempt nor spends another E unit.
    replayed_resume = coordinator.resume(
        parent_id,
        child_id,
        operation_id=resume_operation_id,
    )
    assert replayed_resume.operation_id == resume_operation_id
    assert len(resumed_requests) == 1
    assert store.auto_research_experiment_allowance(parent_id).used == 1
    assert [
        task.operation_id
        for task in store.episode_tasks(child_id)
        if task.parent_operation_id == failed.operation_id
    ] == [resume_operation_id]

    # A crash can happen after the monotonic episode Stop intent commits but
    # before the command exit is recorded. Reissuing Stop settles that same
    # episode and remains safe on another same-key replay.
    store.request_episode_stop(child_id)
    stopped = coordinator.stop(parent_id, child_id)
    replayed_stop = coordinator.stop(parent_id, child_id)
    assert stopped.disposition in {"stopping", "stopped"}
    assert replayed_stop.disposition in {"stopping", "stopped"}
    child_episode = store.episode(child_id)
    assert child_episode is not None and child_episode.stop_requested_at is not None


def test_experiment_resume_recovers_a_row_committed_before_process_spawn(
    manifest: Manifest,
    tmp_path: Path,
    monkeypatch,
) -> None:
    stage = tmp_path / "experiment-stage"
    stage.mkdir()
    resume_executions = 0

    async def child_stream(_project_id, _kind, _request, execution):
        nonlocal resume_executions
        if execution.continuation == "fresh":
            record_launched_experiment_turn(execution.store, execution.operation_id)
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="child-experiment-session"))
            yield _sse(AgentEvent(event="error", text="Transient network failure."))
            return
        resume_executions += 1
        yield _sse(AgentEvent(event="done"))

    _, store, background, coordinator, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        child_stream=child_stream,
    )
    child_id = CHILD_REPLAY
    admission_id = "admission-resume-crash"
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=child_id,
        admission_id=admission_id,
    )
    created = coordinator.kick_off(
        auto_research_episode_id=parent_id,
        parent_operation_id=root_id,
        child_episode_id=child_id,
        node_id=EXPERIMENT_ID,
        goal=None,
        goal_sha256=None,
        invocation_limit=None,
        admission_id=admission_id,
    )
    assert created.operation_id is not None
    failed = wait_for_task(store, created.operation_id, expect="failed")
    resume_operation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:auto_research:{parent_id}:episode:resume-before-spawn",
        )
    )
    real_spawn_record = background._spawn_record

    def crash_before_spawn(*_args, **_kwargs):
        raise RuntimeError("simulated crash after Experiment recovery commit")

    monkeypatch.setattr(background, "_spawn_record", crash_before_spawn)
    with pytest.raises(RuntimeError, match="after Experiment recovery commit"):
        coordinator.resume(
            parent_id,
            child_id,
            operation_id=resume_operation_id,
        )

    committed = store.agent_task(resume_operation_id)
    assert committed is not None and committed.status == "queued"
    assert committed.parent_operation_id == failed.operation_id
    assert store.agent_task_continuation_cause(resume_operation_id) == "resume"
    assert store.auto_research_experiment_allowance(parent_id).used == 1
    monkeypatch.setattr(background, "_spawn_record", real_spawn_record)

    replayed = coordinator.resume(
        parent_id,
        child_id,
        operation_id=resume_operation_id,
    )
    assert replayed.operation_id == resume_operation_id
    wait_for_task(store, resume_operation_id, expect="succeeded")
    replayed_again = coordinator.resume(
        parent_id,
        child_id,
        operation_id=resume_operation_id,
    )
    assert replayed_again.operation_id == resume_operation_id
    assert resume_executions == 1
    assert store.auto_research_experiment_allowance(parent_id).used == 1
    assert [
        task.operation_id
        for task in store.episode_tasks(child_id)
        if task.parent_operation_id == failed.operation_id
    ] == [resume_operation_id]


def test_experiment_kickoff_replay_dispatches_invocation_one_committed_before_launch(
    manifest: Manifest,
    tmp_path: Path,
    monkeypatch,
) -> None:
    fresh_executions = 0

    async def child_stream(_project_id, _kind, _request, execution):
        nonlocal fresh_executions
        assert execution.continuation == "fresh"
        fresh_executions += 1
        yield _sse(AgentEvent(event="done"))

    _, store, background, coordinator, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        child_stream=child_stream,
    )
    child_id = "00000000-0000-4000-8000-000000000415"
    admission_id = "admission-fresh-experiment-crash"
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=child_id,
        admission_id=admission_id,
    )
    parameters = {
        "auto_research_episode_id": parent_id,
        "parent_operation_id": root_id,
        "child_episode_id": child_id,
        "node_id": EXPERIMENT_ID,
        "goal": None,
        "goal_sha256": None,
        "invocation_limit": None,
        "admission_id": admission_id,
    }
    real_spawn_record = background._spawn_record

    def crash_before_spawn(*_args, **_kwargs):
        raise RuntimeError("simulated crash after fresh Experiment commit")

    monkeypatch.setattr(background, "_spawn_record", crash_before_spawn)
    with pytest.raises(RuntimeError, match="after fresh Experiment commit"):
        coordinator.kick_off(**parameters)

    child = store.episode(child_id)
    assert child is not None and child.root_operation_id is not None
    committed = store.agent_task(child.root_operation_id)
    assert committed is not None and committed.status == "queued"
    assert fresh_executions == 0
    monkeypatch.setattr(background, "_spawn_record", real_spawn_record)

    replayed = coordinator.kick_off(**parameters)
    assert replayed.disposition == "existing"
    assert replayed.operation_id == committed.operation_id
    wait_for_task(store, committed.operation_id, expect="succeeded")
    replayed_again = coordinator.kick_off(**parameters)
    assert replayed_again.operation_id == committed.operation_id
    assert fresh_executions == 1
    assert store.auto_research_experiment_allowance(parent_id).used == 1


def test_transient_experiment_kickoff_failure_keeps_admission_for_exact_recovery(
    manifest: Manifest,
    tmp_path: Path,
    monkeypatch,
) -> None:
    fresh_executions = 0

    async def child_stream(_project_id, _kind, _request, execution):
        nonlocal fresh_executions
        assert execution.continuation == "fresh"
        fresh_executions += 1
        yield _sse(AgentEvent(event="done"))

    _, store, background, coordinator, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        child_stream=child_stream,
    )
    child_id = "00000000-0000-4000-8000-000000000417"
    admission_id = "admission-transient-experiment-kickoff"
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=child_id,
        admission_id=admission_id,
    )
    parameters = {
        "auto_research_episode_id": parent_id,
        "parent_operation_id": root_id,
        "child_episode_id": child_id,
        "node_id": EXPERIMENT_ID,
        "goal": "Run the exact bounded goal after infrastructure recovers.",
        "goal_sha256": hashlib.sha256(
            b"Run the exact bounded goal after infrastructure recovers."
        ).hexdigest(),
        "invocation_limit": None,
        "admission_id": admission_id,
    }
    real_start = start_auto_research_child_experiment

    def unavailable(*_args, **_kwargs):
        raise OSError("canonical state is temporarily unavailable")

    before = store.auto_research_experiment_allowance(parent_id)
    monkeypatch.setattr(
        "rcp.runs.auto_research_experiments.start_auto_research_child_experiment", unavailable
    )
    with pytest.raises(OSError, match="temporarily unavailable"):
        coordinator.kick_off(**parameters)

    admission = store.auto_research_child_admission(admission_id)
    assert admission is not None and admission.state == "accepted"
    assert store.auto_research_child_experiment(child_id) is None
    assert store.auto_research_experiment_allowance(parent_id) == before

    monkeypatch.setattr(
        "rcp.runs.auto_research_experiments.start_auto_research_child_experiment", real_start
    )
    recovered = coordinator.kick_off(**parameters)

    assert recovered.disposition == "created"
    assert recovered.operation_id is not None
    wait_for_task(store, recovered.operation_id, expect="succeeded")
    admission = store.auto_research_child_admission(admission_id)
    assert admission is not None and admission.state == "reflected"
    assert store.auto_research_experiment_allowance(parent_id).used == before.used + 1
    assert fresh_executions == 1


def test_terminal_experiment_kickoff_replay_returns_existing_without_redispatch(
    manifest: Manifest,
    tmp_path: Path,
) -> None:
    fresh_executions = 0

    async def child_stream(_project_id, _kind, _request, execution):
        nonlocal fresh_executions
        assert execution.continuation == "fresh"
        fresh_executions += 1
        yield _sse(AgentEvent(event="done"))

    _, store, _, coordinator, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        child_stream=child_stream,
    )
    child_id = "00000000-0000-4000-8000-000000000416"
    admission_id = "admission-terminal-experiment-replay"
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=child_id,
        admission_id=admission_id,
    )
    parameters = {
        "auto_research_episode_id": parent_id,
        "parent_operation_id": root_id,
        "child_episode_id": child_id,
        "node_id": EXPERIMENT_ID,
        "goal": None,
        "goal_sha256": None,
        "invocation_limit": None,
        "admission_id": admission_id,
    }
    created = coordinator.kick_off(**parameters)
    assert created.operation_id is not None
    wait_for_task(store, created.operation_id, expect="succeeded")
    terminal = store.terminalize_auto_research_child_experiment(
        child_id,
        diagnostic="The bounded child Experiment completed.",
    )
    assert terminal.state == "terminal"

    replayed = coordinator.kick_off(**parameters)

    assert replayed.disposition == "existing"
    assert replayed.operation_id == created.operation_id
    assert replayed.status == "terminal"
    assert fresh_executions == 1
    assert store.auto_research_experiment_allowance(parent_id).used == 1


def _human_experiment_request(service: ProjectService, episode_id: str) -> RunRequest:
    state = service.history.state()
    node = state.nodes[EXPERIMENT_ID]
    assert isinstance(node, Experiment)
    from rcp.control import derive_experiment_control_state

    return fresh_experiment_run_request(
        service,
        RunRequest(
            chat_scope="node",
            chat_id=episode_id,
            node_id=EXPERIMENT_ID,
            message="Run the predecessor Experiment.",
            mode="work",
            trigger="experiment_run",
            patch_kind="experiment_loop",
        ),
        node=node,
        state_revision=state.revision,
        control=derive_experiment_control_state(state, EXPERIMENT_ID),
        episode_id=episode_id,
        trigger="experiment_run",
    )


@pytest.mark.parametrize("main_first", [True, False])
def test_kickoff_coexists_with_main_and_stop_is_owned(
    manifest: Manifest,
    tmp_path: Path,
    main_first: bool,
) -> None:
    release = threading.Event()

    async def child_stream(_project_id, _kind, _request, _execution):
        await async_wait_until(release.is_set)
        yield _sse(AgentEvent(event="done"))

    service, store, background, coordinator, parent_id, root_id = _setup(
        manifest,
        tmp_path,
        child_stream=child_stream,
    )
    parent = store.episode(parent_id)
    assert parent is not None and parent.authorized_by is not None
    _admit(
        store,
        parent_episode_id=parent_id,
        child_episode_id=CHILD_EXPLICIT,
        admission_id="coexisting-child",
    )

    def start_main():
        return background.start(
            PROJECT_ID,
            "node_chat",
            _human_experiment_request(service, HUMAN_PREDECESSOR),
            authorized_by=parent.authorized_by,
        )

    def start_child():
        return coordinator.kick_off(
            auto_research_episode_id=parent_id,
            parent_operation_id=root_id,
            child_episode_id=CHILD_EXPLICIT,
            node_id=EXPERIMENT_ID,
            goal=None,
            goal_sha256=None,
            invocation_limit=None,
            admission_id="coexisting-child",
        )

    try:
        if main_first:
            main = start_main()
            child = start_child()
        else:
            child = start_child()
            main = start_main()
        assert child.disposition == "created"
        assert child.operation_id is not None
        main_episode = store.episode(HUMAN_PREDECESSOR)
        child_episode = store.episode(CHILD_EXPLICIT)
        assert main_episode is not None and child_episode is not None
        assert main_episode.graph_target != child_episode.graph_target
        assert main_episode.stop_requested_at is None
        assert child_episode.stop_requested_at is None
        [child_row] = other_branch_loops(
            store, PROJECT_ID, graph_target=main_episode.graph_target, node_id=EXPERIMENT_ID
        )
        assert child_row.episode_id == CHILD_EXPLICIT
        assert child_row.started_by.kind == "auto_research"
        assert child_row.started_by.auto_research_episode_id == parent_id
        assert child_row.started_by.human is None
        assert child_row.auto_research_parent_episode_id == parent_id
        child_response = serialize_episode(
            store, PROJECT_ID, child_episode, include_graph_branch=False
        )
        assert child_response.started_by == child_row.started_by
        assert child_response.authorized_by == parent.authorized_by
        [human_row] = other_branch_loops(
            store, PROJECT_ID, graph_target=child_episode.graph_target, node_id=EXPERIMENT_ID
        )
        assert human_row.episode_id == HUMAN_PREDECESSOR
        assert human_row.started_by.kind == "human"
        assert human_row.started_by.human == parent.authorized_by
        assert human_row.auto_research_parent_episode_id is None
        with pytest.raises(ValueError, match="outside"):
            coordinator.stop(parent_id, HUMAN_PREDECESSOR, operation_id=root_id)
        coordinator.stop(parent_id, CHILD_EXPLICIT, operation_id=root_id)
        stopped_child = store.episode(CHILD_EXPLICIT)
        untouched_main = store.episode(HUMAN_PREDECESSOR)
        assert stopped_child is not None and untouched_main is not None
        assert untouched_main.stop_requested_at is None
        assert stopped_child.stop_requested_at is not None
        assert not any(
            notice.source_kind == "experiment_replacement"
            for notice in store.auto_research_lifecycle_notices(parent_id)
        )
    finally:
        release.set()
    wait_for_task(store, main.operation_id, expect="succeeded")
    wait_for_task(store, child.operation_id, expect="succeeded")


def test_same_target_kickoff_refuses_without_stopping_existing_child(
    manifest: Manifest,
    tmp_path: Path,
) -> None:
    _service, store, _background, coordinator, parent_id, root_id = _setup(manifest, tmp_path)

    def kickoff(child_id: str):
        _admit(store, parent_episode_id=parent_id, child_episode_id=child_id, admission_id=child_id)
        return coordinator.kick_off(
            auto_research_episode_id=parent_id,
            parent_operation_id=root_id,
            child_episode_id=child_id,
            node_id=EXPERIMENT_ID,
            goal=None,
            goal_sha256=None,
            invocation_limit=None,
            admission_id=child_id,
        )

    first = kickoff(CHILD_EXPLICIT)
    assert first.operation_id is not None
    wait_for_task(store, first.operation_id, expect="succeeded")
    with pytest.raises(ValueError):
        kickoff(CHILD_FALLBACK)
    admitted = store.episode(CHILD_EXPLICIT)
    assert admitted is not None and admitted.stop_requested_at is None
    assert store.episode(CHILD_FALLBACK) is None
    assert store.auto_research_child_experiment(CHILD_FALLBACK) is None
    assert store.auto_research_experiment_allowance(parent_id).used == 1
