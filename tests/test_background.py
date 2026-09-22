from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import rcp.background as background_module
from rcp.agents import AgentEvent, AgentProcessControl
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.limits import AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS, AGENT_TRANSPORT_RETRY_LIMIT
from rcp.runs.auto_research import AutoResearchRunRequest, AutoResearchStartRequest
from rcp.runs.auto_research_admission import (
    auto_research_child_work_task,
    ensure_auto_research_child_work_spawned,
    pause_auto_research_child_work,
    reconcile_committed_auto_research_dispatches,
    reconcile_reserved_auto_research_roots,
    reserve_auto_research,
    resume_auto_research_child_experiment,
    resume_auto_research_child_work,
    start_auto_research,
    start_auto_research_child_experiment,
    start_auto_research_child_work,
    start_auto_research_child_work_message_wake,
    start_auto_research_turn,
    stop_auto_research,
    stop_auto_research_child_work,
)
from rcp.runs.episodes.report import start_episode_report
from rcp.runs.episodes.wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup
from rcp.runs.recorded_turn import RecordedProviderTurn
from rcp.runs.remote_reconciliation import Reconciliation
from rcp.runs.tasks.episode_report import EpisodeReportRunRequest
from rcp.runs.watcher_admission import start_watcher_notification
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import (
    AgentTaskAlreadyContinued,
    AgentTaskRecord,
    AppStore,
    AutoResearchChildExperimentRecord,
    AutoResearchMessageRecord,
    EpisodeInvocationCeilingReached,
    EpisodeRecord,
    EpisodeReportRecord,
    ProjectRecord,
    WatcherContinuation,
    WatcherRecord,
)

from .helpers import (
    fabricated_authorizer,
    record_launched_experiment_turn,
    wait_for_task,
    wait_until,
    write_local_test_manifest,
)

_EXPERIMENT_ID = "exp/background-admission"
_EXPERIMENT_EPISODE_ID = "00000000-0000-4000-8000-000000000101"


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _store(tmp_path: Path) -> AppStore:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(write_local_test_manifest(tmp_path)),
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


def _spawned_work_request(worker_id: str, instruction: str) -> RunRequest:
    return RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_id=worker_id,
        chat_scope="node",
        node_id="blk/spawned-work",
        message=instruction,
        mode="work",
        trigger="orchestrator",
        patch_kind="work",
    )


def _child_experiment_request(episode_id: str, goal: str) -> RunRequest:
    return RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_id="00000000-0000-4000-8000-000000000302",
        chat_scope="node",
        node_id="exp/child",
        message=goal,
        mode="work",
        trigger="orchestrator",
        patch_kind="experiment_loop",
        control_node_id="exp/child",
        control_revision=1,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=2,
        control_completion_criteria=["The bounded child comparison is analyzed."],
    )


def _admitted_launch_task(
    store: AppStore,
    *,
    operation_id: str,
    request: RunRequest | None = None,
    parent_operation_id: str | None = None,
    record_updates: dict[str, object] | None = None,
    continuation_cause: str = "fresh",
) -> AgentTaskRecord:
    request = request or RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="project",
        chat_id=f"launch-{operation_id}",
        message="Exercise the admitted launch boundary.",
        mode="work",
        patch_kind="work",
    )
    authority = resolve_dispatch_authority("project_chat", request)
    assert authority is not None
    now = store.now()
    record = AgentTaskRecord(
        operation_id=operation_id,
        project_id="project",
        kind="project_chat",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="Queued",
        attempt=2 if parent_operation_id is not None else 1,
        parent_operation_id=parent_operation_id,
        phase="queued",
        last_activity_at=now,
        authorized_by=fabricated_authorizer("Researcher"),
        dispatch_authority=authority,
    )
    return store.create_agent_task(
        record.model_copy(update=record_updates or {}), continuation_cause=continuation_cause
    )


async def _done_stream(_project_id, _kind, _request, _execution):
    yield _sse(AgentEvent(event="done"))


def test_construction_leaves_recovery_undone_until_startup_asks_for_it(
    tmp_path: Path,
) -> None:
    """Constructing the engine must not write to the store.

    Startup recovery is an explicit call so that constructing this object — which
    358 sites do, most of them tests — cannot silently interrupt live work.
    """

    store = _store(tmp_path)
    task = _admitted_launch_task(store, operation_id="survives-construction")
    store.mark_agent_task_running(task.operation_id)

    tasks = BackgroundAgentTasks(store, _done_stream)

    untouched = store.agent_task(task.operation_id)
    assert untouched is not None and untouched.status == "running"

    tasks.recover_at_startup()

    interrupted = store.agent_task(task.operation_id)
    assert interrupted is not None and interrupted.status == "interrupted"


def test_launch_admitted_missing_operation_is_read_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)

    with pytest.raises(KeyError, match="missing-launch"):
        tasks.launch_admitted("missing-launch")

    assert store.agent_task("missing-launch") is None
    assert store.agent_task_receipts("missing-launch") == []


def test_launch_admitted_valid_task_uses_persisted_cause_and_receipt_order(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    task = _admitted_launch_task(store, operation_id="valid-launch")

    launched = tasks.launch_admitted(task.operation_id)
    finished = wait_for_task(store, launched.operation_id, expect="succeeded")

    assert finished.operation_id == task.operation_id
    assert [
        item.category
        for item in store.agent_task_receipts(task.operation_id)
        if item.category
        in {
            "operation_admitted",
            "operation_dispatch_attempt",
            "operation_created",
            "operation_dispatch_started",
        }
    ] == [
        "operation_admitted",
        "operation_dispatch_attempt",
        "operation_created",
        "operation_dispatch_started",
    ]


def test_runtime_is_checkpointed_before_the_provider_session(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(AgentEvent(event="runtime", text="codex.app-server-stdio.v1"))
        yield _sse(AgentEvent(event="session", session_id="app-server-thread"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    task = _admitted_launch_task(store, operation_id="runtime-checkpoint")
    launched = tasks.launch_admitted(task.operation_id)
    finished = wait_for_task(store, launched.operation_id, expect="succeeded")

    assert finished.runtime_id == "codex.app-server-stdio.v1"
    receipt = next(
        item
        for item in store.agent_task_receipts(task.operation_id)
        if item.category == "provider_runtime_selected"
    )
    assert receipt.payload == {
        "provider": "codex",
        "runtime_id": "codex.app-server-stdio.v1",
    }


def test_a_passed_over_runtime_is_recorded_as_a_diagnostic(tmp_path: Path) -> None:
    """The fallback is silent to the human, so the reason must survive somewhere."""

    store = _store(tmp_path)

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(
            AgentEvent(
                event="runtime_fallback",
                text=json.dumps(
                    {"runtime_id": "codex.app-server-stdio.v1", "detail": "codex 0.140.0 is old"}
                ),
            )
        )
        yield _sse(AgentEvent(event="runtime", text="codex.exec-json.v1"))
        yield _sse(AgentEvent(event="session", session_id="exec-thread"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    task = _admitted_launch_task(store, operation_id="runtime-fallback")
    launched = tasks.launch_admitted(task.operation_id)
    finished = wait_for_task(store, launched.operation_id, expect="succeeded")

    assert finished.runtime_id == "codex.exec-json.v1"
    receipt = next(
        item
        for item in store.agent_task_receipts(task.operation_id)
        if item.category == "provider_runtime_fallback"
    )
    assert receipt.payload == {
        "runtime_id": "codex.app-server-stdio.v1",
        "detail": "codex 0.140.0 is old",
    }


def test_launch_admitted_is_idempotent_for_live_and_terminal_duplicates(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    stage = tmp_path / "duplicate-launch-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="duplicate-launch-session"))
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    task = _admitted_launch_task(store, operation_id="duplicate-launch")
    tasks.launch_admitted(task.operation_id)
    assert entered.wait(timeout=2)

    live_duplicate = tasks.launch_admitted(task.operation_id)
    assert live_duplicate.operation_id == task.operation_id
    assert live_duplicate.native_session_id == "duplicate-launch-session"
    release.set()
    wait_for_task(store, task.operation_id, expect="succeeded")

    terminal_duplicate = tasks.launch_admitted(task.operation_id)
    assert terminal_duplicate.status == "succeeded"
    receipts = store.agent_task_receipts(task.operation_id)
    assert sum(item.category == "operation_created" for item in receipts) == 1
    assert sum(item.category == "operation_dispatch_attempt" for item in receipts) == 1


@pytest.mark.parametrize("already_settled", [False, True])
def test_member_pause_before_running_claim_never_dispatches_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_settled: bool,
) -> None:
    store = _store(tmp_path)
    claiming = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    dispatched = threading.Event()
    mark_running = store.mark_agent_task_running

    def gated_mark_running(operation_id):
        claiming.set()
        assert release.wait(timeout=5)
        return mark_running(operation_id)

    monkeypatch.setattr(store, "mark_agent_task_running", gated_mark_running)

    async def stream(*_args):
        dispatched.set()
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(
        store,
        stream,
        on_task_settled=lambda *_args: settled.set(),
    )
    task = _admitted_launch_task(store, operation_id="member-pause-before-running")
    tasks.launch_admitted(task.operation_id)
    try:
        assert claiming.wait(timeout=5)
        pausing = tasks.request_member_removal_pause(task.operation_id)
        assert pausing.status == "pausing"
        if already_settled:
            store.pause_agent_task(task.operation_id, detail="Member removal settled the task.")
        release.set()
        assert settled.wait(timeout=5)
        assert not dispatched.is_set()
        paused = store.agent_task(task.operation_id)
        assert paused is not None and paused.status == "paused"
        if already_settled:
            assert paused.status_message == "Member removal settled the task."
    finally:
        release.set()
        tasks.shutdown()


def test_shutdown_fences_a_launch_that_has_not_claimed_a_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    claiming = threading.Event()
    release = threading.Event()
    dispatched = threading.Event()

    async def stream(*_args):
        dispatched.set()
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    task = _admitted_launch_task(store, operation_id="shutdown-before-worker-claim")
    spawn = tasks._spawn_record

    def gated_spawn(*args, **kwargs):
        claiming.set()
        assert release.wait(timeout=5)
        return spawn(*args, **kwargs)

    monkeypatch.setattr(tasks, "_spawn_record", gated_spawn)
    with ThreadPoolExecutor(max_workers=1) as callers:
        launch = callers.submit(tasks.launch_admitted, task.operation_id)
        try:
            assert claiming.wait(timeout=5)
            tasks.shutdown()
            release.set()
            deferred = launch.result(timeout=5)
            assert deferred.status == "queued"
            assert tasks.runtime_is_idle()
            assert not dispatched.is_set()
            assert store.agent_task_dispatch_was_proven_not_started(task.operation_id)
        finally:
            release.set()
            tasks.shutdown()

    tasks.recover_at_startup()
    recovered = store.agent_task(task.operation_id)
    assert recovered is not None and recovered.status == "interrupted"
    assert recovered.can_retry
    assert not dispatched.is_set()


@pytest.mark.parametrize("intent_state", ["missing", "malformed"])
def test_launch_admitted_rejects_missing_or_malformed_intent_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intent_state: str,
) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    task = _admitted_launch_task(store, operation_id=f"bad-intent-{intent_state}")
    before = store.agent_task_receipts(task.operation_id)
    if intent_state == "missing":
        monkeypatch.setattr(store, "agent_task_admission_intent", lambda _operation_id: None)
    else:
        monkeypatch.setattr(
            store,
            "agent_task_admission_intent",
            lambda _operation_id: (_ for _ in ()).throw(ValueError("malformed intent")),
        )

    with pytest.raises(ValueError, match="intent"):
        tasks.launch_admitted(task.operation_id)

    assert store.agent_task(task.operation_id).status == "queued"  # type: ignore[union-attr]
    assert store.agent_task_receipts(task.operation_id) == before
    assert task.operation_id not in tasks._workers


def test_launch_admitted_rejects_unknown_dispatch_attempt_before_new_receipts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    task = _admitted_launch_task(store, operation_id="unknown-dispatch-attempt")
    store.record_agent_task_receipt(
        task.operation_id,
        "operation_dispatch_attempt",
        {"dispatch_attempt_id": "unknown-attempt"},
        tier="diagnostic",
    )
    before = store.agent_task_receipts(task.operation_id)

    with pytest.raises(ValueError, match="ambiguous|already-started"):
        tasks.launch_admitted(task.operation_id)

    assert store.agent_task_receipts(task.operation_id) == before
    assert task.operation_id not in tasks._workers


@pytest.mark.parametrize(
    ("record_updates", "message"),
    [
        ({"dispatch_authority": None}, "dispatch authority"),
        ({"native_session_id": "changed-session"}, "native session"),
        ({"stage_host": "remote"}, "stage binding"),
        ({"write_scope_fingerprint": "a" * 64}, "write-scope"),
    ],
)
def test_launch_admitted_rejects_invalid_launch_bindings_before_dispatch(
    tmp_path: Path,
    record_updates: dict[str, object],
    message: str,
) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    task = _admitted_launch_task(
        store,
        operation_id=f"invalid-binding-{message.replace(' ', '-')}",
        record_updates=record_updates,
    )
    before = store.agent_task_receipts(task.operation_id)

    with pytest.raises(ValueError, match=message):
        tasks.launch_admitted(task.operation_id)

    assert store.agent_task_receipts(task.operation_id) == before
    assert task.operation_id not in tasks._workers


def test_launch_admitted_retries_proven_prestart_failure_without_duplicate_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    task = _admitted_launch_task(store, operation_id="prestart-retry")
    original_start = threading.Thread.start
    failed = True

    def fail_once(worker: threading.Thread) -> None:
        nonlocal failed
        if failed:
            failed = False
            raise RuntimeError("thread start failed before provider launch")
        original_start(worker)

    monkeypatch.setattr(threading.Thread, "start", fail_once)
    with pytest.raises(RuntimeError, match="before provider launch"):
        tasks.launch_admitted(task.operation_id)

    first = store.agent_task_receipts(task.operation_id)
    assert sum(item.category == "operation_created" for item in first) == 1
    assert sum(item.category == "operation_dispatch_attempt" for item in first) == 1
    assert sum(item.category == "operation_dispatch_failed_before_start" for item in first) == 1

    launched = tasks.launch_admitted(task.operation_id)
    wait_for_task(store, launched.operation_id, expect="succeeded")
    second = store.agent_task_receipts(task.operation_id)
    assert sum(item.category == "operation_created" for item in second) == 1
    assert sum(item.category == "operation_dispatch_attempt" for item in second) == 2
    assert sum(item.category == "operation_dispatch_failed_before_start" for item in second) == 1


def test_validated_spawn_record_rejects_both_parent_presence_directions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    parent = _admitted_launch_task(store, operation_id="parent-binding")
    store.mark_agent_task_running(parent.operation_id)
    store.complete_agent_task(parent.operation_id, applied_revision=None, result={})
    child = _admitted_launch_task(
        store,
        operation_id="child-binding",
        request=RunRequest.model_validate(parent.request),
        parent_operation_id=parent.operation_id,
    )
    tasks = BackgroundAgentTasks(store, _done_stream)
    request = BackgroundAgentTasks._request_from_record(child)

    monkeypatch.setattr(
        store,
        "agent_task",
        lambda operation_id: (
            child.model_copy(update={"parent_operation_id": parent.operation_id})
            if operation_id == child.operation_id
            else parent
        ),
    )
    with pytest.raises(ValueError, match="changed before background dispatch"):
        tasks._validated_spawn_record(
            child.model_copy(update={"parent_operation_id": None}), request, parent=None
        )

    monkeypatch.setattr(
        store,
        "agent_task",
        lambda operation_id: (
            child.model_copy(update={"parent_operation_id": None})
            if operation_id == child.operation_id
            else parent
        ),
    )
    with pytest.raises(ValueError, match="changed before background dispatch"):
        tasks._validated_spawn_record(child, request, parent=parent)


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
    episode, root = start_auto_research(
        tasks,
        "project",
        _auto_start(starting_instruction="  Begin with the disputed claim.  "),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-episode",
        operation_id="auto-root",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")

    assert episode.mode == "auto_research"
    assert episode.graph_target.kind == "branch"
    assert episode.graph_target.branch_id == episode.episode_id
    assert episode.graph_base_head == GraphHeadRef(revision=0)
    assert root.kind == "auto_research"
    assert root.graph_target == episode.graph_target
    assert root.episode_id == episode.episode_id
    assert root.request["episode_id"] == episode.episode_id
    assert root.request["instruction"] == "Begin with the disputed claim."
    assert "campaign_id" not in root.request
    assert isinstance(tasks._request_from_record(root), AutoResearchRunRequest)
    assert store.episode_budget_meter(episode.episode_id).invocations_used == 1
    assert store.episode_tasks(episode.episode_id) == [root]


def test_reserved_auto_research_root_reconciles_branch_before_provider_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)

    async def forbidden_stream(*_args, **_kwargs):
        raise AssertionError("the reservation must not launch before branch reconciliation")
        yield  # pragma: no cover

    tasks = BackgroundAgentTasks(store, forbidden_stream)
    episode, root, _request = reserve_auto_research(
        tasks,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        episode_id="reserved-auto-episode",
        operation_id="reserved-auto-root",
    )
    assert store.agent_task(root.operation_id).status == "queued"  # type: ignore[union-attr]

    restarted = BackgroundAgentTasks(store, forbidden_stream)
    order: list[str] = []

    def held_spawn(record, _request, **_kwargs):
        order.append("spawn")
        return record

    monkeypatch.setattr(restarted, "_spawn_record", held_spawn)
    started = reconcile_reserved_auto_research_roots(
        restarted, lambda reserved: order.append(f"branch:{reserved.episode_id}")
    )

    assert started == [root.operation_id]
    assert order == [f"branch:{episode.episode_id}", "spawn"]
    assert store.agent_task(root.operation_id).status == "queued"  # type: ignore[union-attr]


def test_spawned_child_uses_ordinary_node_work_and_atomically_spends_b(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seen: list[tuple[str, object]] = []

    async def stream(_project_id, kind, request, _execution):
        seen.append((kind, request))
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-work",
        operation_id="auto-child-work-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000301"
    instruction = "Resolve the runtime blocker, then report the bounded evidence."
    request = _spawned_work_request(worker_id, instruction)

    child = start_auto_research_child_work(
        background,
        episode.episode_id,
        request,
        admitted_by_operation_id=root.operation_id,
        worker_id=worker_id,
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    child = wait_for_task(store, child.operation_id, expect="succeeded")

    route, current = auto_research_child_work_task(
        background,
        episode.episode_id,
        worker_id,
    )
    assert route.current_operation_id == child.operation_id == worker_id
    assert route.instruction == instruction
    assert current.kind == "node_chat"
    assert current.graph_target == episode.graph_target
    assert current.request["mode"] == "work"
    assert current.request["trigger"] == "orchestrator"
    assert current.dispatch_authority is not None
    assert current.dispatch_authority.profile == "ordinary"
    assert current.dispatch_authority.task_contract == "work_auto"
    assert store.episode_budget_meter(episode.episode_id).invocations_used == 2
    assert isinstance(seen[-1][1], RunRequest)
    assert seen[-1][0] == "node_chat"


def test_committed_child_dispatch_is_claimed_once_under_concurrent_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    executions = 0

    async def stream(_project_id, kind, _request, _execution):
        nonlocal executions
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        executions += 1
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-dispatch-claim",
        operation_id="auto-child-dispatch-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000319"
    instruction = "Dispatch this committed worker once."
    real_spawn_record = background._spawn_record

    def crash_before_spawn(*_args, **_kwargs):
        raise RuntimeError("simulated crash after child commit")

    monkeypatch.setattr(background, "_spawn_record", crash_before_spawn)
    with pytest.raises(RuntimeError, match="after child commit"):
        start_auto_research_child_work(
            background,
            episode.episode_id,
            _spawned_work_request(worker_id, instruction),
            admitted_by_operation_id=root.operation_id,
            worker_id=worker_id,
            instruction=instruction,
            instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
        )
    monkeypatch.setattr(background, "_spawn_record", real_spawn_record)

    def ensure() -> str:
        return ensure_auto_research_child_work_spawned(
            background,
            episode.episode_id,
            worker_id,
            operation_id=worker_id,
            continuation="fresh",
        ).operation_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: ensure(), range(2)))

    assert results == [worker_id, worker_id]
    assert entered.wait(timeout=2)
    assert executions == 1
    release.set()
    wait_for_task(store, worker_id, expect="succeeded")


def test_restart_dispatches_committed_fresh_child_work_without_respending_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    executions: list[str] = []

    async def stream(_project_id, kind, _request, execution):
        if kind != "auto_research":
            executions.append(execution.continuation)
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-fresh-restart",
        operation_id="auto-child-fresh-restart-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000351"
    instruction = "Run the exact committed fresh Work allocation after restart."

    def crash_after_commit(*_args, **_kwargs):
        raise RuntimeError("simulated process loss before Work dispatch")

    monkeypatch.setattr(background, "_spawn_record", crash_after_commit)
    with pytest.raises(RuntimeError, match="before Work dispatch"):
        start_auto_research_child_work(
            background,
            episode.episode_id,
            _spawned_work_request(worker_id, instruction),
            admitted_by_operation_id=root.operation_id,
            worker_id=worker_id,
            instruction=instruction,
            instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
        )
    before = store.episode_budget_meter(episode.episode_id)

    restarted = BackgroundAgentTasks(store, stream)
    queued = store.agent_task(worker_id)
    assert queued is not None and queued.status == "queued"
    projects = AppStore.projects

    def fail_global_project_scan(_store):
        pytest.fail("episode-scoped dispatch reconciliation performed a global scan")

    monkeypatch.setattr(AppStore, "projects", fail_global_project_scan)
    assert reconcile_committed_auto_research_dispatches(
        restarted,
        episode_id=episode.episode_id,
    ) == [worker_id]
    monkeypatch.setattr(AppStore, "projects", projects)
    wait_for_task(store, worker_id, expect="succeeded")

    assert executions == ["fresh"]
    assert store.episode_budget_meter(episode.episode_id) == before


def test_restart_dispatches_committed_child_work_resume_without_respending_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "restart-work-resume-stage"
    stage.mkdir()
    resumed: list[str] = []

    async def stream(_project_id, kind, _request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="restart-work-session"))
            yield _sse(AgentEvent(event="error", text="Transient network failure."))
            return
        resumed.append(execution.continuation)
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-resume-restart",
        operation_id="auto-child-resume-restart-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000352"
    instruction = "Resume this exact paid child allocation after restart."
    failed = start_auto_research_child_work(
        background,
        episode.episode_id,
        _spawned_work_request(worker_id, instruction),
        admitted_by_operation_id=root.operation_id,
        worker_id=worker_id,
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    wait_for_task(store, failed.operation_id, expect="failed")
    resume_id = "00000000-0000-4000-8000-000000000353"
    store.start_agent_command(
        operation_id=root.operation_id,
        command_id="restart-work-resume-command",
        episode_id=episode.episode_id,
        verb="resume",
        idempotency_key="restart-work-resume",
        payload={
            "request_id": "1" * 32,
            "arguments": {"worker_id": worker_id},
            "planned_resume_operation_id": resume_id,
        },
    )

    def crash_after_commit(*_args, **_kwargs):
        raise RuntimeError("simulated process loss before Work Resume dispatch")

    monkeypatch.setattr(background, "_spawn_record", crash_after_commit)
    with pytest.raises(RuntimeError, match="before Work Resume dispatch"):
        resume_auto_research_child_work(
            background,
            episode.episode_id,
            worker_id,
            operation_id=resume_id,
        )
    before = store.episode_budget_meter(episode.episode_id)

    restarted = BackgroundAgentTasks(store, stream)
    queued = store.agent_task(resume_id)
    assert queued is not None and queued.status == "queued"
    assert reconcile_committed_auto_research_dispatches(
        restarted,
    ) == [resume_id]
    completed = wait_for_task(store, resume_id, expect="succeeded")

    assert completed.native_session_id == "restart-work-session"
    assert resumed == ["resume"]
    assert store.episode_budget_meter(episode.episode_id) == before


def test_spawned_child_message_wake_reuses_exact_work_session_and_spends_b(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "spawned-work-mail-stage"
    stage.mkdir()
    seen_continuations: list[str] = []

    async def stream(_project_id, kind, request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        assert isinstance(request, RunRequest)
        seen_continuations.append(execution.continuation)
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="spawned-work-mail-session"))
        else:
            assert execution.continuation == "message_wake"
            assert request.message is None
            assert request.session_id == "spawned-work-mail-session"
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-mail",
        operation_id="auto-child-mail-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000321"
    instruction = "Inspect the bounded result and report it."
    child = start_auto_research_child_work(
        background,
        episode.episode_id,
        _spawned_work_request(worker_id, instruction),
        admitted_by_operation_id=root.operation_id,
        worker_id=worker_id,
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    child = wait_for_task(store, child.operation_id, expect="succeeded")
    message_id = "00000000-0000-4000-8000-000000000322"
    store.record_auto_research_message(
        AutoResearchMessageRecord(
            message_id=message_id,
            episode_id=episode.episode_id,
            sender_role="orchestrator",
            sender_task_id=root.operation_id,
            recipient_task_id=worker_id,
            control_node_id="blk/spawned-work",
            body="Recheck the new observation.",
            created_at=store.now(),
        )
    )
    wake_id = "00000000-0000-4000-8000-000000000323"

    wake = start_auto_research_child_work_message_wake(
        background,
        episode.episode_id,
        worker_id,
        [message_id],
        operation_id=wake_id,
    )

    assert wake is not None
    wake = wait_for_task(store, wake.operation_id, expect="succeeded")
    route = store.auto_research_child_work(worker_id)
    delivered = store.auto_research_message(message_id)
    assert route is not None and route.current_operation_id == wake_id
    assert wake.parent_operation_id == child.operation_id
    assert wake.attempt == child.attempt + 1 == 2
    assert wake.native_session_id == child.native_session_id == "spawned-work-mail-session"
    assert wake.stage_root == child.stage_root == str(stage)
    assert wake.request["message"] is None
    assert delivered is not None and delivered.delivery_operation_id == wake_id
    assert store.episode_budget_meter(episode.episode_id).invocations_used == 3
    assert seen_continuations == ["fresh", "message_wake"]


def test_failed_spawned_child_exact_resume_reuses_checkpoint_and_b_allocation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "spawned-work-stage"
    stage.mkdir()
    resumed_requests: list[RunRequest] = []

    async def stream(_project_id, kind, request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        assert isinstance(request, RunRequest)
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="spawned-work-session"))
            yield _sse(AgentEvent(event="error", text="Transient network failure."))
            return
        resumed_requests.append(request)
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-resume",
        operation_id="auto-child-resume-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000311"
    instruction = "Diagnose the transient runtime failure."
    failed = start_auto_research_child_work(
        background,
        episode.episode_id,
        _spawned_work_request(worker_id, instruction),
        admitted_by_operation_id=root.operation_id,
        worker_id=worker_id,
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    wait_for_task(store, failed.operation_id, expect="failed")
    before = store.episode_budget_meter(episode.episode_id).invocations_used

    outcome = resume_auto_research_child_work(
        background,
        episode.episode_id,
        worker_id,
        operation_id="00000000-0000-4000-8000-000000000312",
    )
    assert outcome.disposition == "resumed"
    assert outcome.task is not None
    resumed = wait_for_task(store, outcome.task.operation_id, expect="succeeded")

    assert resumed.parent_operation_id == worker_id
    assert resumed.native_session_id == "spawned-work-session"
    assert resumed.stage_root == str(stage)
    assert resumed_requests[0].session_id == "spawned-work-session"
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before
    route = store.auto_research_child_work(worker_id)
    assert route is not None
    assert route.current_operation_id == resumed.operation_id


def test_spawned_child_resume_preserves_recovery_when_remote_stage_probe_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, kind, _request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        execution.checkpoint_stage("worker-host", "/tmp/rcp-run.remote-work")
        yield _sse(AgentEvent(event="session", session_id="remote-work-session"))
        yield _sse(AgentEvent(event="error", text="Transient network failure."))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-remote-resume",
        operation_id="auto-child-remote-resume-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000313"
    instruction = "Resume only if the exact remote workspace can be checked."
    failed = start_auto_research_child_work(
        background,
        episode.episode_id,
        _spawned_work_request(worker_id, instruction),
        admitted_by_operation_id=root.operation_id,
        worker_id=worker_id,
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    wait_for_task(store, failed.operation_id, expect="failed")
    before = store.episode_budget_meter(episode.episode_id).invocations_used
    monkeypatch.setattr(
        "rcp.background.RemoteRunStage.directory_exists",
        lambda _stage, _root: None,
    )

    with pytest.raises(OSError, match="remote infrastructure is unavailable"):
        resume_auto_research_child_work(
            background,
            episode.episode_id,
            worker_id,
            operation_id="00000000-0000-4000-8000-000000000314",
        )

    route = store.auto_research_child_work(worker_id)
    assert route is not None and route.current_operation_id == failed.operation_id
    assert store.agent_task("00000000-0000-4000-8000-000000000314") is None
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before

    monkeypatch.setattr(
        "rcp.background.RemoteRunStage.directory_exists",
        lambda _stage, _root: False,
    )
    unavailable = resume_auto_research_child_work(
        background,
        episode.episode_id,
        worker_id,
        operation_id="00000000-0000-4000-8000-000000000314",
    )
    assert unavailable.disposition == "resume_unavailable"
    assert unavailable.reason == "the saved provider workspace is unavailable"


def test_unusable_spawned_child_resume_names_fresh_spawn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "limited-work-stage"
    stage.mkdir()

    async def stream(_project_id, kind, _request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="limited-session"))
        yield _sse(AgentEvent(event="error", text="You've hit your session limit"))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-unavailable",
        operation_id="auto-child-unavailable-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000321"
    instruction = "Continue the bounded probe."
    failed = start_auto_research_child_work(
        background,
        episode.episode_id,
        _spawned_work_request(worker_id, instruction),
        admitted_by_operation_id=root.operation_id,
        worker_id=worker_id,
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    wait_for_task(store, failed.operation_id, expect="failed")
    before = store.episode_budget_meter(episode.episode_id).invocations_used

    outcome = resume_auto_research_child_work(background, episode.episode_id, worker_id)

    assert outcome.disposition == "resume_unavailable"
    assert outcome.task is None
    assert outcome.replacement_command == "spawn"
    assert outcome.reason == "the saved provider session reached its limit"
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before


def test_routed_worker_pause_and_stop_target_only_its_current_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    child_started = threading.Event()

    async def stream(_project_id, kind, _request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        child_started.set()
        while not execution.control.pause_requested.is_set():
            await asyncio.sleep(0.01)
        yield _sse(AgentEvent(event="paused", text="Paused at the exact child checkpoint."))

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-control",
        operation_id="auto-child-control-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    worker_id = "00000000-0000-4000-8000-000000000325"
    instruction = "Pause this bounded diagnostic when asked."
    child = start_auto_research_child_work(
        background,
        episode.episode_id,
        _spawned_work_request(worker_id, instruction),
        admitted_by_operation_id=root.operation_id,
        worker_id=worker_id,
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    assert child_started.wait(timeout=2)

    pausing = pause_auto_research_child_work(background, episode.episode_id, worker_id)
    assert pausing.operation_id == child.operation_id
    paused = wait_for_task(store, child.operation_id, expect="paused")
    stopped_attempt = stop_auto_research_child_work(
        background,
        episode.episode_id,
        worker_id,
    )

    assert stopped_attempt.operation_id == paused.operation_id
    route = store.auto_research_child_work(worker_id)
    assert route is not None
    assert route.stop_requested_at is not None
    unavailable = resume_auto_research_child_work(background, episode.episode_id, worker_id)
    assert unavailable.disposition == "resume_unavailable"
    assert unavailable.reason == "the worker was stopped"
    assert unavailable.replacement_command == "spawn"


def test_child_experiment_start_and_exact_resume_spend_e_only_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "child-experiment-stage"
    stage.mkdir()
    resumed: list[RunRequest] = []

    async def stream(_project_id, kind, request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        assert isinstance(request, RunRequest)
        if execution.continuation == "fresh":
            record_launched_experiment_turn(store, execution.operation_id)
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="child-experiment-session"))
            yield _sse(AgentEvent(event="error", text="Transient network failure."))
            return
        resumed.append(request)
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    parent, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-experiment-parent",
        operation_id="auto-child-experiment-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    child_episode_id = "00000000-0000-4000-8000-000000000331"
    goal = "Determine whether the repaired runtime survives the bounded probe."
    request = _child_experiment_request(child_episode_id, goal)
    now = store.now()
    route = AutoResearchChildExperimentRecord(
        child_episode_id=child_episode_id,
        auto_research_episode_id=parent.episode_id,
        project_id="project",
        control_node_id="exp/child",
        state="running",
        request={"goal": goal, "invocation_limit": 2},
        goal_sha256=hashlib.sha256(goal.encode()).hexdigest(),
        parent_operation_id=root.operation_id,
        created_at=now,
        updated_at=now,
    )

    failed = start_auto_research_child_experiment(background, route, request)
    wait_for_task(store, failed.operation_id, expect="failed")
    child_episode = store.episode(child_episode_id)
    assert child_episode is not None
    assert child_episode.graph_target == parent.graph_target
    assert child_episode.graph_base_head == parent.graph_base_head
    assert failed.graph_target == parent.graph_target
    allowance = store.auto_research_experiment_allowance(parent.episode_id)
    assert allowance.used == 1

    outcome = resume_auto_research_child_experiment(
        background,
        parent.episode_id,
        child_episode_id,
        operation_id="00000000-0000-4000-8000-000000000332",
    )
    assert outcome.disposition == "resumed"
    assert outcome.task is not None
    resumed_task = wait_for_task(store, outcome.task.operation_id, expect="succeeded")

    assert resumed_task.parent_operation_id == failed.operation_id
    assert resumed_task.graph_target == parent.graph_target
    assert resumed_task.native_session_id == "child-experiment-session"
    assert resumed[0].session_id == "child-experiment-session"
    assert store.auto_research_experiment_allowance(parent.episode_id).used == 1


def test_restart_dispatches_committed_fresh_child_experiment_without_respending_e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    executions: list[str] = []

    async def stream(_project_id, kind, _request, execution):
        if kind != "auto_research":
            executions.append(execution.continuation)
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    parent, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-experiment-fresh-restart",
        operation_id="auto-child-experiment-fresh-restart-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    child_id = "00000000-0000-4000-8000-000000000354"
    goal = "Run the exact committed child Experiment after restart."
    now = store.now()
    route = AutoResearchChildExperimentRecord(
        child_episode_id=child_id,
        auto_research_episode_id=parent.episode_id,
        project_id="project",
        control_node_id="exp/child",
        state="running",
        request={"goal": goal, "invocation_limit": 2},
        goal_sha256=hashlib.sha256(goal.encode()).hexdigest(),
        parent_operation_id=root.operation_id,
        created_at=now,
        updated_at=now,
    )

    def crash_after_commit(*_args, **_kwargs):
        raise RuntimeError("simulated process loss before Experiment dispatch")

    monkeypatch.setattr(background, "_spawn_record", crash_after_commit)
    with pytest.raises(RuntimeError, match="before Experiment dispatch"):
        start_auto_research_child_experiment(
            background,
            route,
            _child_experiment_request(child_id, goal),
        )
    child = store.episode(child_id)
    assert child is not None and child.root_operation_id is not None
    operation_id = child.root_operation_id
    before = store.auto_research_experiment_allowance(parent.episode_id)

    restarted = BackgroundAgentTasks(store, stream)
    queued = store.agent_task(operation_id)
    assert queued is not None and queued.status == "queued"
    assert reconcile_committed_auto_research_dispatches(
        restarted,
    ) == [operation_id]
    wait_for_task(store, operation_id, expect="succeeded")

    assert executions == ["fresh"]
    assert store.auto_research_experiment_allowance(parent.episode_id) == before


def test_restart_dispatches_committed_child_experiment_resume_without_respending_e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "restart-experiment-resume-stage"
    stage.mkdir()
    resumed: list[str] = []

    async def stream(_project_id, kind, _request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        if execution.continuation == "fresh":
            record_launched_experiment_turn(store, execution.operation_id)
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="restart-experiment-session"))
            yield _sse(AgentEvent(event="error", text="Transient network failure."))
            return
        resumed.append(execution.continuation)
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    parent, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-experiment-resume-restart",
        operation_id="auto-child-experiment-resume-restart-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    child_id = "00000000-0000-4000-8000-000000000355"
    goal = "Resume the exact child Experiment allocation after restart."
    now = store.now()
    route = AutoResearchChildExperimentRecord(
        child_episode_id=child_id,
        auto_research_episode_id=parent.episode_id,
        project_id="project",
        control_node_id="exp/child",
        state="running",
        request={"goal": goal, "invocation_limit": 2},
        goal_sha256=hashlib.sha256(goal.encode()).hexdigest(),
        parent_operation_id=root.operation_id,
        created_at=now,
        updated_at=now,
    )
    failed = start_auto_research_child_experiment(
        background,
        route,
        _child_experiment_request(child_id, goal),
    )
    wait_for_task(store, failed.operation_id, expect="failed")
    resume_id = "00000000-0000-4000-8000-000000000356"
    store.start_agent_command(
        operation_id=root.operation_id,
        command_id="restart-experiment-resume-command",
        episode_id=parent.episode_id,
        verb="episode",
        idempotency_key="restart-experiment-resume",
        payload={
            "request_id": "2" * 32,
            "arguments": {"action": "resume", "episode_id": child_id},
            "planned_episode_effect_id": resume_id,
        },
    )

    def crash_after_commit(*_args, **_kwargs):
        raise RuntimeError("simulated process loss before Experiment Resume dispatch")

    monkeypatch.setattr(background, "_spawn_record", crash_after_commit)
    with pytest.raises(RuntimeError, match="before Experiment Resume dispatch"):
        resume_auto_research_child_experiment(
            background,
            parent.episode_id,
            child_id,
            operation_id=resume_id,
        )
    before = store.auto_research_experiment_allowance(parent.episode_id)

    restarted = BackgroundAgentTasks(store, stream)
    queued = store.agent_task(resume_id)
    assert queued is not None and queued.status == "queued"
    assert reconcile_committed_auto_research_dispatches(
        restarted,
    ) == [resume_id]
    completed = wait_for_task(store, resume_id, expect="succeeded")

    assert completed.native_session_id == "restart-experiment-session"
    assert resumed == ["resume"]
    assert store.auto_research_experiment_allowance(parent.episode_id) == before


def test_restart_redispatches_committed_child_experiment_graph_repair_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "restart-experiment-graph-repair-stage"
    stage.mkdir()
    continuations: list[str] = []

    async def stream(_project_id, kind, request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        continuations.append(execution.continuation)
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="graph-repair-session"))
            yield _sse(
                AgentEvent(
                    event="message",
                    text=json.dumps(
                        {
                            "graph_update": {
                                "status": "rejected",
                                "validation_messages": ["Patch requires correction."],
                                "repairable": True,
                            }
                        }
                    ),
                )
            )
        else:
            assert execution.continuation == "graph_repair"
            assert isinstance(request, RunRequest) and request.message is None
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    parent, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-experiment-graph-repair-restart",
        operation_id="auto-child-experiment-graph-repair-restart-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    child_id = "00000000-0000-4000-8000-000000000358"
    goal = "Do not turn a graph repair into a full CLI Resume."
    child_request = _child_experiment_request(child_id, goal)
    now = store.now()
    route = AutoResearchChildExperimentRecord(
        child_episode_id=child_id,
        auto_research_episode_id=parent.episode_id,
        project_id="project",
        control_node_id="exp/child",
        state="running",
        request={"goal": goal, "invocation_limit": 2},
        goal_sha256=hashlib.sha256(goal.encode()).hexdigest(),
        parent_operation_id=root.operation_id,
        created_at=now,
        updated_at=now,
    )
    rejected = start_auto_research_child_experiment(background, route, child_request)
    rejected = wait_for_task(store, rejected.operation_id, expect="succeeded")
    assert rejected.native_session_id == "graph-repair-session"
    assert rejected.result is not None
    assert rejected.result["graph_update"]["repairable"] is True
    allowance_before = store.auto_research_experiment_allowance(parent.episode_id)

    def crash_after_commit(*_args, **_kwargs):
        raise RuntimeError("simulated process loss before graph-repair dispatch")

    monkeypatch.setattr(background, "_spawn_record", crash_after_commit)
    with pytest.raises(RuntimeError, match="before graph-repair dispatch"):
        background.repair_graph_update(rejected.operation_id)
    repair_tasks = [
        task
        for task in store.episode_tasks(child_id)
        if task.parent_operation_id == rejected.operation_id
    ]
    assert len(repair_tasks) == 1
    repair_id = repair_tasks[0].operation_id
    assert store.agent_task_continuation_cause(repair_id) == "graph_repair"

    restarted = BackgroundAgentTasks(store, stream)
    queued = store.agent_task(repair_id)
    assert queued is not None and queued.status == "queued"
    assert reconcile_committed_auto_research_dispatches(
        restarted,
    ) == [repair_id]
    wait_for_task(store, repair_id, expect="succeeded")

    assert continuations == ["fresh", "graph_repair"]
    assert store.auto_research_experiment_allowance(parent.episode_id) == allowance_before
    assert [task.operation_id for task in store.episode_tasks(child_id)].count(repair_id) == 1
    created = [
        receipt
        for receipt in store.agent_task_receipts(repair_id)
        if receipt.category == "operation_created"
    ]
    assert len(created) == 1
    assert created[0].payload["continuation_cause"] == "graph_repair"


def test_child_experiment_resume_preserves_recovery_when_remote_stage_probe_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, kind, _request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        record_launched_experiment_turn(store, execution.operation_id)
        execution.checkpoint_stage("experiment-host", "/tmp/rcp-run.remote-experiment")
        yield _sse(AgentEvent(event="session", session_id="remote-experiment-session"))
        yield _sse(AgentEvent(event="error", text="Transient network failure."))

    background = BackgroundAgentTasks(store, stream)
    parent, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-remote-experiment-parent",
        operation_id="auto-child-remote-experiment-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    child_episode_id = "00000000-0000-4000-8000-000000000333"
    goal = "Resume only if the exact remote Experiment workspace can be checked."
    now = store.now()
    route = AutoResearchChildExperimentRecord(
        child_episode_id=child_episode_id,
        auto_research_episode_id=parent.episode_id,
        project_id="project",
        control_node_id="exp/child",
        state="running",
        request={"goal": goal, "invocation_limit": 2},
        goal_sha256=hashlib.sha256(goal.encode()).hexdigest(),
        parent_operation_id=root.operation_id,
        created_at=now,
        updated_at=now,
    )
    failed = start_auto_research_child_experiment(
        background,
        route,
        _child_experiment_request(child_episode_id, goal),
    )
    wait_for_task(store, failed.operation_id, expect="failed")
    allowance_before = store.auto_research_experiment_allowance(parent.episode_id)
    monkeypatch.setattr(
        "rcp.background.RemoteRunStage.directory_exists",
        lambda _stage, _root: None,
    )

    with pytest.raises(OSError, match="remote infrastructure is unavailable"):
        resume_auto_research_child_experiment(
            background,
            parent.episode_id,
            child_episode_id,
            operation_id="00000000-0000-4000-8000-000000000334",
        )

    tasks = store.episode_tasks(child_episode_id)
    assert tasks[-1].operation_id == failed.operation_id
    assert store.agent_task("00000000-0000-4000-8000-000000000334") is None
    assert store.auto_research_experiment_allowance(parent.episode_id) == allowance_before

    monkeypatch.setattr(
        "rcp.background.RemoteRunStage.directory_exists",
        lambda _stage, _root: False,
    )
    unavailable = resume_auto_research_child_experiment(
        background,
        parent.episode_id,
        child_episode_id,
        operation_id="00000000-0000-4000-8000-000000000334",
    )
    assert unavailable.disposition == "resume_unavailable"
    assert unavailable.reason == "the saved provider workspace is unavailable"


def test_task_result_keeps_ordered_graph_updates_and_latest_compatibility_projection(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    in_turn_updates = [
        {
            "status": "applied",
            "applied_revision": revision,
            "change_summary": [f"Applied in-turn revision {revision}."],
        }
        for revision in (2, 3)
    ]
    final_update = {
        "status": "applied",
        "applied_revision": 4,
        "change_summary": ["Applied the final unconsumed Patch."],
    }

    async def stream(_project_id, _kind, _request, _execution):
        yield _sse(
            AgentEvent(
                event="message",
                text=json.dumps(
                    {
                        "applied_revision": 4,
                        "graph_update": final_update,
                        "graph_updates": in_turn_updates,
                    }
                ),
            )
        )
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    task = tasks.start(
        "project",
        "node_chat",
        RunRequest(
            provider="codex",
            model="",
            reasoning="medium",
            run_on="laptop",
            run_truth_scope=["repo"],
            chat_id="graph-update-chat",
            chat_scope="node",
            node_id="exp/result-compatibility",
            message="Apply the bounded updates.",
            mode="work",
        ),
        operation_id="graph-update-task",
        authorized_by=fabricated_authorizer("Researcher"),
    )
    completed = wait_for_task(store, task.operation_id, expect="succeeded")

    assert completed.applied_revision == 4
    assert completed.result is not None
    assert completed.result["graph_update"]["applied_revision"] == 4
    assert [update["applied_revision"] for update in completed.result["graph_updates"]] == [2, 3]
    assert completed.result["messages"] == []


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
    episode, root = start_auto_research(
        tasks,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
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
    episode, root = start_auto_research(
        tasks,
        "project",
        _auto_start(),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-episode",
        operation_id="auto-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")

    stopped = stop_auto_research(tasks, episode.episode_id)

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
    episode, root = start_auto_research(
        tasks,
        "project",
        _auto_start(invocation_ceiling=1),
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
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
            start_auto_research_turn(
                tasks,
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
            record_launched_experiment_turn(store, execution.operation_id)
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

    wake = start_watcher_notification(
        tasks,
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


def test_restart_dispatches_committed_child_experiment_watcher_wake_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "restart-child-experiment-watcher-stage"
    stage.mkdir()
    authorizer = fabricated_authorizer("Researcher")
    watcher_executions: list[str] = []

    async def stream(_project_id, kind, request, execution):
        if kind == "auto_research":
            yield _sse(AgentEvent(event="done"))
            return
        assert isinstance(request, RunRequest)
        if execution.continuation == "fresh":
            execution.checkpoint_stage("", str(stage))
            yield _sse(AgentEvent(event="session", session_id="child-watcher-session"))
        else:
            watcher_executions.append(execution.continuation)
        yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    parent, root = start_auto_research(
        background,
        "project",
        _auto_start(),
        authorized_by=authorizer,
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto-child-experiment-watcher-restart",
        operation_id="auto-child-experiment-watcher-restart-root",
    )
    wait_for_task(store, root.operation_id, expect="succeeded")
    child_id = "00000000-0000-4000-8000-000000000357"
    goal = "Continue the child Experiment when its watcher completes."
    child_request = _child_experiment_request(child_id, goal)
    now = store.now()
    route = AutoResearchChildExperimentRecord(
        child_episode_id=child_id,
        auto_research_episode_id=parent.episode_id,
        project_id="project",
        control_node_id="exp/child",
        state="running",
        request={"goal": goal, "invocation_limit": 2},
        goal_sha256=hashlib.sha256(goal.encode()).hexdigest(),
        parent_operation_id=root.operation_id,
        created_at=now,
        updated_at=now,
    )
    child_root = start_auto_research_child_experiment(background, route, child_request)
    child_root = wait_for_task(store, child_root.operation_id, expect="succeeded")
    store.commit_experiment_episode_turn(
        episode_id=child_id,
        project_id="project",
        control_node_id="exp/child",
        provider="codex",
        execution_machine="laptop",
        execution_host="",
        native_session_id="child-watcher-session",
        stage_host=None,
        stage_root=str(stage),
        chat_id=child_request.chat_id,
        operation_id=child_root.operation_id,
        invocation=1,
        graph_result="applied",
        watcher_ids=[],
        context_baseline={},
    )
    watcher = WatcherRecord(
        watcher_id="child-experiment-completed-watcher",
        project_id="project",
        origin_operation_id=child_root.operation_id,
        origin_task_kind="node_chat",
        chat_id=child_request.chat_id,
        node_id="exp/child",
        episode_id=child_id,
        graph_target=parent.graph_target,
        execution_host="",
        check_command="true",
        log_path="/tmp/child-experiment-completed-watcher.log",
        cwd="/tmp",
        continuation=WatcherContinuation(
            provider="codex",
            model="",
            reasoning="medium",
            run_on="laptop",
            run_truth_scope=["repo"],
            patch_kind="experiment_loop",
            control_node_id="exp/child",
            control_revision=1,
            control_episode_id=child_id,
            control_invocation=1,
            control_invocation_ceiling=2,
            control_decision_bundle=[],
            control_completion_criteria=["The bounded child comparison is analyzed."],
        ),
        status="active",
        created_at=store.now(),
    )
    store.create_watchers([watcher])
    store.record_watcher_check(watcher.watcher_id, status="completed", exit_code=0, error=None)
    wake_request = child_request.model_copy(
        update={
            "trigger": "watcher",
            "control_invocation": 2,
            "watcher_ids": [watcher.watcher_id],
            "session_id": "child-watcher-session",
        }
    )
    real_thread_start = threading.Thread.start

    def fail_before_thread_start(_thread):
        raise RuntimeError("simulated process loss before watcher thread start")

    monkeypatch.setattr(threading.Thread, "start", fail_before_thread_start)
    with pytest.raises(RuntimeError, match="before watcher thread start"):
        start_watcher_notification(
            background,
            "project",
            "node_chat",
            wake_request,
            [watcher.watcher_id],
            authorized_by=authorizer,
            episode_stage_root=str(stage),
        )
    monkeypatch.setattr(threading.Thread, "start", real_thread_start)
    claimed = store.watcher(watcher.watcher_id)
    assert claimed is not None and claimed.notification_operation_id is not None
    operation_id = claimed.notification_operation_id
    allowance_before = store.auto_research_experiment_allowance(parent.episode_id)
    meter_before = store.episode_budget_meter(child_id)

    restarted = BackgroundAgentTasks(store, stream)
    queued = store.agent_task(operation_id)
    assert queued is not None and queued.status == "queued"
    assert reconcile_committed_auto_research_dispatches(
        restarted,
    ) == [operation_id]
    completed = wait_for_task(store, operation_id, expect="succeeded")

    claimed_after = store.watcher(watcher.watcher_id)
    assert completed.native_session_id == "child-watcher-session"
    assert completed.graph_target == parent.graph_target
    assert claimed_after is not None and claimed_after.graph_target == parent.graph_target
    assert watcher_executions == ["watcher_wake"]
    assert claimed_after.notification_operation_id == operation_id
    assert store.auto_research_experiment_allowance(parent.episode_id) == allowance_before
    assert store.episode_budget_meter(child_id) == meter_before


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


def test_shutdown_defers_a_settlement_report_until_startup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started = threading.Event()
    report_started = threading.Event()
    allocated: list[AgentTaskRecord] = []

    async def stream(_project_id, kind, request, execution):
        if kind != "episode_report":
            started.set()
            assert await asyncio.to_thread(execution.control.pause_requested.wait, 5)
            yield _sse(AgentEvent(event="paused"))
            return
        report_started.set()
        attempt = store.allocate_episode_report_attempt(request.episode_id)
        store.mark_episode_report_attempt_running(attempt.attempt_id)
        html = "<html><body><figure>Evidence map</figure></body></html>"
        store.finish_episode_report_ready(
            attempt.attempt_id,
            EpisodeReportRecord(
                report_id="shutdown-report",
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

    def settled(*_args):
        allocated.append(_report_allocation(store, tmp_path))
        start_episode_report(tasks, "report-episode")

    tasks = BackgroundAgentTasks(store, stream, on_task_settled=settled)
    task = _admitted_launch_task(store, operation_id="shutdown-before-report")
    tasks.launch_admitted(task.operation_id)
    try:
        assert started.wait(timeout=5)
        tasks.shutdown()
        assert len(allocated) == 1
        assert not report_started.is_set()
        hidden = store.agent_task(allocated[0].operation_id)
        assert hidden is not None and hidden.status == "queued"
        episode = store.episode("report-episode")
        assert episode is not None and episode.wrapup_state == "pending"
        assert episode.wrapup_error is None
        assert store.agent_task_dispatch_was_proven_not_started(hidden.operation_id)

        tasks.recover_at_startup()
        wait_for_task(store, hidden.operation_id, expect="succeeded")
        assert report_started.is_set()
        recovered_episode = store.episode("report-episode")
        assert recovered_episode is not None and recovered_episode.wrapup_state == "ready"
    finally:
        tasks.shutdown()


@pytest.mark.parametrize("prior_status", ["interrupted", "paused"])
def test_interrupted_hidden_report_restarts_once_and_runner_owns_success(
    tmp_path: Path,
    prior_status: str,
) -> None:
    store = _store(tmp_path)
    hidden = _report_allocation(store, tmp_path)
    store.mark_agent_task_running(hidden.operation_id)
    store.bind_agent_task_write_scope(
        hidden.operation_id,
        project_id="project",
        stage_host="",
        stage_root=str(tmp_path / "report-stage"),
        fingerprint="a" * 64,
        continuation_binding=False,
    )
    store.record_agent_task_receipt(
        hidden.operation_id,
        "operation_dispatch_attempt",
        {"dispatch_attempt_id": "previous-report-dispatch"},
        tier="diagnostic",
    )
    store.record_agent_task_receipt(
        hidden.operation_id,
        "operation_dispatch_started",
        {"dispatch_attempt_id": "previous-report-dispatch"},
        tier="diagnostic",
    )
    if prior_status == "interrupted":
        store.interrupt_active_agent_tasks()
    else:
        store.pause_agent_task(hidden.operation_id, detail="Paused for shutdown")
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
    tasks.recover_at_startup()
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
    assert start_episode_report(tasks, "report-episode") is None
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
    tasks.recover_at_startup()
    assert entered.wait(timeout=2)
    duplicate = start_episode_report(tasks, "report-episode")
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


def test_legacy_experiment_episode_without_authorizer_names_the_fresh_run(tmp_path: Path) -> None:
    """A recorded episode authorizer is required, so say what the human can do.

    Regression: an Experiment episode written before the authorizer snapshot
    existed refused Retry with "A patch-capable agent task requires a human
    authorizer snapshot." That is true and useless. The episode's own human is
    the authority for every turn in it, so a current human cannot stand in --
    but pressing Run starts a fresh episode, and the message must say so.
    """

    store = _store(tmp_path)
    stage = tmp_path / "legacy-experiment-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, request, execution):
        execution.checkpoint_stage("", str(stage))
        record_launched_experiment_turn(store, execution.operation_id)
        yield _sse(AgentEvent(event="session", session_id="experiment-session"))
        yield _sse(AgentEvent(event="error", text="Transient provider failure."))

    tasks = BackgroundAgentTasks(store, stream)
    root = tasks.start(
        "project",
        "node_chat",
        _experiment_request(),
        operation_id="legacy-experiment-root",
        authorized_by=fabricated_authorizer("Researcher"),
    )
    wait_for_task(store, root.operation_id, expect="failed")

    # Age the episode back to before the authorizer snapshot was recorded.
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET authorized_space_id = NULL, authorized_user_id = NULL, "
            "authorized_display_name = NULL WHERE episode_id = ?",
            (_EXPERIMENT_EPISODE_ID,),
        )
    assert store.episode(_EXPERIMENT_EPISODE_ID).authorized_by is None

    with pytest.raises(ValueError) as refusal:
        tasks.retry(root.operation_id, authorized_by=fabricated_authorizer("Someone else"))
    message = str(refusal.value)
    assert "predates the recorded human authorizer" in message
    assert "Press Run on the Experiment to start a fresh episode." in message
    assert store.agent_task(root.operation_id).status == "failed"


def _transport_failed_task(
    store: AppStore,
    *,
    operation_id: str,
    parent_operation_id: str | None = None,
    failure_kind: str = "transport_lost",
    request: RunRequest | None = None,
    record_updates: dict[str, object] | None = None,
) -> AgentTaskRecord:
    task = _admitted_launch_task(
        store,
        operation_id=operation_id,
        parent_operation_id=parent_operation_id,
        request=request,
        record_updates=record_updates,
    )
    store.mark_agent_task_running(task.operation_id)
    store.fail_agent_task(
        task.operation_id,
        "The connection to gpu.example.edu was lost before codex finished.",
        failure_kind=failure_kind,  # type: ignore[arg-type]
    )
    settled = store.agent_task(task.operation_id)
    assert settled is not None
    return settled


def _waiting_remote_work(store: AppStore, operation_id: str) -> AgentTaskRecord:
    from rcp.runs.tasks.work import WORK_FINALIZATION_CONTEXT_ROLE

    _admitted_launch_task(
        store,
        operation_id=operation_id,
        record_updates={"stage_host": "remote", "stage_root": "/stage"},
    )
    assert store.mark_agent_task_running(operation_id)
    store.record_agent_task_contract(
        operation_id, WORK_FINALIZATION_CONTEXT_ROLE, "{}", hashlib.sha256(b"{}").hexdigest()
    )
    store.begin_remote_provider_pass(
        operation_id,
        "remote",
        "/stage",
        f"/stage/{operation_id}.pid",
        supervised=True,
    )
    store.interrupt_active_agent_tasks()
    waiting = store.agent_task(operation_id)
    assert waiting is not None and waiting.phase == "awaiting_remote_result"
    return waiting


def _recorded_turn(pid_file: str) -> RecordedProviderTurn:
    return RecordedProviderTurn(
        pid_file=pid_file,
        provider="codex",
        runtime_id="codex.exec-json.v1",
        provider_version=None,
        outcome={"terminal_event": True, "journal_complete": True},
        events="",
        stderr="",
        patch=None,
        watch=None,
        accepted=True,
    )


def test_dropped_supervised_stream_keeps_the_original_task_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("remote", "/stage")
        store.begin_remote_provider_pass(
            execution.operation_id,
            "remote",
            "/stage",
            "/stage/dropped.pid",
            supervised=True,
        )
        yield _sse(
            AgentEvent(
                event="remote_result_pending",
                text="Waiting for the remote result.",
            )
        )

    tasks = BackgroundAgentTasks(store, stream)
    admitted = _admitted_launch_task(store, operation_id="dropped-supervised")
    tasks.launch_admitted(admitted.operation_id)
    wait_until(lambda: tasks.runtime_is_idle())

    waiting = store.agent_task(admitted.operation_id)
    assert waiting is not None
    assert waiting.status == "running"
    assert waiting.phase == "awaiting_remote_result"
    assert not store.agent_task_has_receipt(admitted.operation_id, "transport_auto_retry")
    assert len(store.agent_tasks("project", include_hidden=True)) == 1


def test_recorded_result_completes_the_same_task_and_pass_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rcp.runs import remote_finalization

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "recorded-original")
    pid_file = "/stage/recorded-original.pid"
    seen: list[str] = []

    async def unused_stream(*_args):
        raise AssertionError("recorded finalization must not launch a provider")
        yield  # pragma: no cover

    async def recorded_stream(_project_id, _kind, _request, execution, _recorded):
        seen.append(execution.operation_id)
        yield _sse(AgentEvent(event="session", session_id="recorded-session"))
        yield _sse(AgentEvent(event="answer", text="Recovered answer."))
        yield _sse(AgentEvent(event="done"))

    monkeypatch.setattr(
        remote_finalization,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(
            "finalize",
            "The provider finished.",
            pid_file,
            _recorded_turn(pid_file),
        ),
    )
    tasks = BackgroundAgentTasks(store, unused_stream, recorded_stream=recorded_stream)

    assert tasks._reconcile_remote_results() is False

    settled = store.agent_task(waiting.operation_id)
    assert settled is not None and settled.status == "succeeded"
    assert settled.native_session_id == "recorded-session"
    assert settled.result == {"messages": ["Recovered answer."]}
    assert seen == [waiting.operation_id]
    assert store.unresolved_remote_provider_passes("remote", "/stage") == []
    assert len(store.agent_tasks("project", include_hidden=True)) == 1


def test_recorded_finalization_releases_its_claim_if_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rcp.runs import remote_finalization

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "recorded-setup-failure")
    pid_file = "/stage/recorded-setup-failure.pid"
    monkeypatch.setattr(
        remote_finalization,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(
            "finalize",
            "The provider finished.",
            pid_file,
            _recorded_turn(pid_file),
        ),
    )
    tasks = BackgroundAgentTasks(store, _done_stream, recorded_stream=_done_stream)

    def fail_request(_record):
        raise RuntimeError("bad retained request")

    monkeypatch.setattr(tasks, "_request_from_record", fail_request)

    with pytest.raises(RuntimeError, match="bad retained request"):
        tasks._reconcile_remote_results()

    released = store.agent_task(waiting.operation_id)
    assert released is not None and released.phase == "awaiting_remote_result"


def test_pause_of_detached_turn_requires_reachable_exact_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rcp.runs import remote_reconciliation

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "pause-detached")
    monkeypatch.setattr(
        AgentProcessControl,
        "remote_process_state",
        staticmethod(lambda *_args: (False, "boot:123")),
    )
    stopped: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        AgentProcessControl,
        "stop_remote_process",
        classmethod(
            lambda _cls, host, pid_file, *, expected_identity: (
                stopped.append((host, pid_file, expected_identity)) or True
            )
        ),
    )
    tasks = BackgroundAgentTasks(store, _done_stream)
    monkeypatch.setattr(
        remote_reconciliation,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation("fail", "Provider interrupted before completion."),
    )

    paused = tasks.pause(waiting.operation_id)

    assert paused.status == "paused"
    assert stopped == [("remote", "/stage/pause-detached.pid", "boot:123")]
    assert store.unresolved_remote_provider_passes("remote", "/stage") == []


def test_pause_of_unreachable_detached_turn_is_a_no_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "unreachable-detached")
    monkeypatch.setattr(
        AgentProcessControl,
        "remote_process_state",
        staticmethod(lambda *_args: (None, None)),
    )
    tasks = BackgroundAgentTasks(store, _done_stream)

    with pytest.raises(ValueError, match="left the remote turn running"):
        tasks.pause(waiting.operation_id)

    unchanged = store.agent_task(waiting.operation_id)
    assert unchanged is not None
    assert unchanged.status == "running"
    assert unchanged.phase == "awaiting_remote_result"
    assert store.unresolved_remote_provider_passes("remote", "/stage")


def test_pause_preserves_a_finished_remote_result_for_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rcp.runs import remote_finalization

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "finished-before-pause")
    pid_file = "/stage/finished-before-pause.pid"
    monkeypatch.setattr(
        AgentProcessControl, "remote_process_state", staticmethod(lambda *_: (True, None))
    )
    monkeypatch.setattr(
        remote_finalization,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(
            "finalize", "Finished", pid_file, _recorded_turn(pid_file)
        ),
    )

    async def recorded_stream(*_args):
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, _done_stream, recorded_stream=recorded_stream)

    with pytest.raises(ValueError, match="already stopped"):
        tasks.pause(waiting.operation_id)

    assert store.agent_task(waiting.operation_id).phase == "awaiting_remote_result"
    assert store.unresolved_remote_provider_passes("remote", "/stage")
    assert tasks._reconcile_remote_results() is False
    assert store.agent_task(waiting.operation_id).status == "succeeded"
    assert not store.unresolved_remote_provider_passes("remote", "/stage")


@pytest.mark.parametrize("journal_action", ["finalize", "wait"])
def test_pause_preserves_a_result_that_finishes_during_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journal_action: str
) -> None:
    from rcp.runs import remote_reconciliation

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "finished-during-stop")
    monkeypatch.setattr(
        AgentProcessControl,
        "remote_process_state",
        staticmethod(lambda *_: (False, "boot:123")),
    )
    monkeypatch.setattr(
        AgentProcessControl,
        "stop_remote_process",
        classmethod(lambda *_args, **_kwargs: True),
    )
    monkeypatch.setattr(
        remote_reconciliation,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(journal_action, "Journal preserved."),
    )
    tasks = BackgroundAgentTasks(store, _done_stream)

    with pytest.raises(ValueError, match="reconcile its recorded result"):
        tasks.pause(waiting.operation_id)

    assert store.agent_task(waiting.operation_id).phase == "awaiting_remote_result"
    assert store.unresolved_remote_provider_passes("remote", "/stage")


def test_pause_rechecks_the_task_after_finalization_claims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "claimed-before-pause")
    tasks = BackgroundAgentTasks(store, _done_stream)
    observed_waiting = threading.Event()
    original_require = tasks._require_operation

    def observe(operation_id):
        record = original_require(operation_id)
        observed_waiting.set()
        return record

    monkeypatch.setattr(tasks, "_require_operation", observe)
    monkeypatch.setattr(
        AgentProcessControl,
        "remote_process_state",
        staticmethod(lambda *_: pytest.fail("a finalizing task must not be probed for Pause")),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with tasks._remote_result_lock:
            paused = pool.submit(tasks.pause, waiting.operation_id)
            assert observed_waiting.wait(2)
            assert store.claim_recorded_finalization(waiting.operation_id)
        with pytest.raises(ValueError, match="already being finalized"):
            paused.result(timeout=2)

    assert store.agent_task(waiting.operation_id).phase == "finalizing_recorded_result"
    assert store.unresolved_remote_provider_passes("remote", "/stage")


def test_reconciliation_is_busy_until_shutdown_has_drained_its_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream, recorded_stream=_done_stream)
    entered = threading.Event()
    release = threading.Event()
    shutdown_entered = threading.Event()

    def reconcile():
        entered.set()
        assert release.wait(2)
        return False

    def shutdown():
        shutdown_entered.set()
        tasks.shutdown(timeout=2)

    monkeypatch.setattr(tasks, "_reconcile_remote_results", reconcile)
    tasks._schedule_remote_reconciliation(delay=0)
    try:
        assert entered.wait(2)
        assert not tasks.runtime_is_idle()
        with ThreadPoolExecutor(max_workers=1) as pool:
            stopped = pool.submit(shutdown)
            assert shutdown_entered.wait(2)
            wait_until(lambda: tasks._shutdown_requested)
            assert not stopped.done()
            release.set()
            stopped.result(timeout=2)
        assert tasks.runtime_is_idle()
    finally:
        release.set()
        tasks.shutdown(timeout=2)


def test_maintenance_fences_a_scheduled_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rcp.server_ops.maintenance import RuntimeAdmissionGate

    gate = RuntimeAdmissionGate(closed=True)
    tasks = BackgroundAgentTasks(
        _store(tmp_path),
        _done_stream,
        recorded_stream=_done_stream,
        runtime_admission_gate=gate,
    )
    checked = threading.Event()
    require_open = gate.require_open

    def require(effect):
        checked.set()
        require_open(effect)

    monkeypatch.setattr(gate, "require_open", require)
    monkeypatch.setattr(
        tasks, "_reconcile_remote_results", lambda: pytest.fail("maintenance must fence mutations")
    )
    tasks._schedule_remote_reconciliation(delay=0)
    try:
        assert checked.wait(2)
        wait_until(tasks.runtime_is_idle)
    finally:
        tasks.shutdown(timeout=2)


def test_waiting_task_arriving_during_reconciliation_gets_another_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rcp.background as background_module

    tasks = BackgroundAgentTasks(_store(tmp_path), _done_stream, recorded_stream=_done_stream)
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def reconcile():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return False

    monkeypatch.setattr(tasks, "_reconcile_remote_results", reconcile)
    monkeypatch.setattr(background_module, "REMOTE_RESULT_RECONCILIATION_INTERVAL_SECONDS", 0)
    tasks._schedule_remote_reconciliation(delay=0)
    try:
        assert entered.wait(2)
        tasks._schedule_remote_reconciliation(delay=0)
        assert len(calls) == 1
        release.set()
        wait_until(lambda: len(calls) == 2 and tasks.runtime_is_idle())
    finally:
        release.set()
        tasks.shutdown(timeout=2)


def test_lost_connection_is_reattempted_without_a_human(tmp_path: Path, monkeypatch) -> None:
    """A network change ends a turn for a reason the turn had no part in.

    That is not the human's failure to diagnose, and this is a chat turn:
    a dropped link is reattempted wherever it lands, not only in an episode.
    """

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    scheduled: list[tuple[str, float]] = []
    monkeypatch.setattr(
        tasks,
        "_schedule_transport_retry",
        lambda operation_id, *, attempt: scheduled.append(
            (operation_id, AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[attempt])
        ),
    )
    failed = _transport_failed_task(store, operation_id="dropped")

    tasks._auto_retry_transport_loss(failed)

    assert scheduled == [("dropped", AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[0])]
    assert store.agent_task_has_receipt("dropped", "transport_auto_retry")


def test_a_revoked_login_is_never_reattempted(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    scheduled: list[tuple[str, float]] = []
    monkeypatch.setattr(
        tasks,
        "_schedule_transport_retry",
        lambda operation_id, *, attempt: scheduled.append(
            (operation_id, AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[attempt])
        ),
    )
    failed = _transport_failed_task(store, operation_id="revoked", failure_kind="provider_auth")

    tasks._auto_retry_transport_loss(failed)

    assert scheduled == []
    assert not store.agent_task_has_receipt("revoked", "transport_auto_retry")


def test_reattempts_stop_at_the_limit_and_say_so(tmp_path: Path, monkeypatch) -> None:
    """A link still down after the last wait is not a transient stall, and a
    loop that never ends would burn an episode's invocations on the network."""

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    scheduled: list[tuple[str, float]] = []
    monkeypatch.setattr(
        tasks,
        "_schedule_transport_retry",
        lambda operation_id, *, attempt: scheduled.append(
            (operation_id, AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[attempt])
        ),
    )

    # One lineage means one dispatch authority, so every attempt carries the
    # same request the human authorized.
    lineage = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="project",
        chat_id="launch-lineage",
        message="Exercise the reattempt budget.",
        mode="work",
        patch_kind="work",
    )
    parent: str | None = None
    for index in range(AGENT_TRANSPORT_RETRY_LIMIT):
        name = f"attempt-{index}"
        failed = _transport_failed_task(
            store, operation_id=name, parent_operation_id=parent, request=lineage
        )
        tasks._auto_retry_transport_loss(failed)
        parent = name

    exhausted = _transport_failed_task(
        store, operation_id="exhausted", parent_operation_id=parent, request=lineage
    )
    tasks._auto_retry_transport_loss(exhausted)

    assert [delay for _operation, delay in scheduled] == list(
        AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[:AGENT_TRANSPORT_RETRY_LIMIT]
    )
    assert not store.agent_task_has_receipt("exhausted", "transport_auto_retry")
    assert store.agent_task_has_receipt("exhausted", "transport_auto_retry_exhausted")


def test_a_reattempt_in_a_short_wake_waits_without_spending_its_attempt(
    tmp_path: Path, monkeypatch
) -> None:
    """A lid-closed laptop wakes for seconds; a launch then loses its link again."""

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    _transport_failed_task(store, operation_id="dropped")
    scheduled: list[tuple[str, int, float | None]] = []
    monkeypatch.setattr(
        tasks,
        "_schedule_transport_retry",
        lambda operation_id, *, attempt, delay=None: scheduled.append(
            (operation_id, attempt, delay)
        ),
    )
    monkeypatch.setattr(background_module, "seconds_until_automatic_launch", lambda: 25.0)
    monkeypatch.setattr(
        tasks, "retry", lambda *_args, **_kwargs: pytest.fail("launched in a short wake")
    )

    tasks._run_transport_retry("dropped", attempt=1)

    assert scheduled == [("dropped", 1, 25.0)]


def test_shutdown_cancels_a_pending_reattempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    _transport_failed_task(store, operation_id="pending")

    tasks._schedule_transport_retry("pending", attempt=2)
    assert tasks._transport_retry_timers

    tasks.shutdown(timeout=0.1)

    assert tasks._transport_retry_timers == []
    assert store.agent_task("pending").status == "failed"


def test_an_episode_turn_is_reattempted_on_the_same_terms_as_a_chat(
    tmp_path: Path, monkeypatch
) -> None:
    """A dropped link is a transport fact, so the surface it landed on is not one.

    An Experiment episode turn is an ordinary node chat, so this covers the
    other half of what a lost link can interrupt.
    """

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    scheduled: list[tuple[str, float]] = []
    monkeypatch.setattr(
        tasks,
        "_schedule_transport_retry",
        lambda operation_id, *, attempt: scheduled.append(
            (operation_id, AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[attempt])
        ),
    )
    failed = _transport_failed_task(
        store,
        operation_id="episode-dropped",
        record_updates={"kind": "node_chat"},
    )

    tasks._auto_retry_transport_loss(failed)

    assert scheduled == [("episode-dropped", AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[0])]


def test_a_reattempt_stands_down_once_a_human_has_recovered_the_turn(
    tmp_path: Path, monkeypatch
) -> None:
    """The wait can outlast the failure it was scheduled for.

    A human pressing Retry, or any recovery owner, leaves a child on the failed
    task. Firing the timer anyway would run the same turn a second time after
    the first already finished, repeating whatever that turn did.
    """

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    # One request for both attempts: a continuation must keep its parent's
    # dispatch authority, which is derived from the request.
    shared = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="project",
        chat_id="launch-supersession",
        message="Exercise the admitted launch boundary.",
        mode="work",
        patch_kind="work",
    )
    failed = _transport_failed_task(store, operation_id="dropped", request=shared)
    retried: list[str] = []
    monkeypatch.setattr(
        tasks, "retry", lambda operation_id, **_kwargs: retried.append(operation_id)
    )

    # Nothing has taken it over yet, so the wait still means something.
    assert not tasks._transport_retry_superseded(failed.operation_id)

    _admitted_launch_task(
        store,
        operation_id="human-retry",
        parent_operation_id=failed.operation_id,
        request=shared,
    )

    assert tasks._transport_retry_superseded(failed.operation_id)
    assert store.agent_task_has_receipt(failed.operation_id, "transport_auto_retry_superseded")
    assert retried == []


@pytest.mark.parametrize(
    ("exit_payload", "expected"),
    [
        ({"return_code": 255, "event_counts": {"raw": 11}}, "transport_lost"),
        (
            {
                "return_code": 255,
                "event_counts": {"answer": 2, "raw": 1},
                "explicit_terminal_event": True,
            },
            None,
        ),
        ({"return_code": -15, "event_counts": {"error": 1}}, None),
    ],
)
def test_a_turn_the_provider_explained_is_never_blamed_on_the_link(
    tmp_path: Path, exit_payload: dict[str, object], expected: str | None
) -> None:
    """One remote host produced both 255s: a stream that simply stopped, and a
    turn that streamed its answers, reached its own terminal event, then exited
    255 saying its thread was gone. Naming the second a lost link would reattempt
    work the provider had already done for a reason no attempt can change."""

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    task = _admitted_launch_task(store, operation_id="explained")
    store.mark_agent_task_running(task.operation_id)
    store.record_agent_task_receipt(
        task.operation_id, "provider_exit", exit_payload, tier="diagnostic"
    )
    execution = SimpleNamespace(stage_host="gpu.example.edu")
    request = tasks._request_from_record(store.agent_task(task.operation_id))

    assert (
        tasks._failure_kind(
            task.operation_id,
            request,
            execution,  # type: ignore[arg-type]
            "codex could not finish.",
        )
        == expected
    )


def test_a_promised_reattempt_survives_a_restart(tmp_path: Path, monkeypatch) -> None:
    """Stopping RCP during the wait must not quietly cancel the reattempt.

    The receipt that promises one is durable and the timer that keeps it is
    not, so without startup re-arming the turn stays failed forever while its
    history says a reattempt is coming.
    """

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    monkeypatch.setattr(tasks, "_schedule_transport_retry", lambda _operation, *, attempt: None)
    failed = _transport_failed_task(store, operation_id="dropped")
    tasks._auto_retry_transport_loss(failed)
    assert store.agent_task_has_receipt("dropped", "transport_auto_retry")

    restarted = BackgroundAgentTasks(store, _done_stream)
    rearmed: list[tuple[str, float]] = []
    monkeypatch.setattr(
        restarted,
        "_schedule_transport_retry",
        lambda operation_id, *, attempt: rearmed.append(
            (operation_id, AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[attempt])
        ),
    )

    restarted.recover_at_startup()

    assert rearmed == [("dropped", AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[0])]


def test_a_restart_resumes_the_waits_a_refused_reattempt_had_reached(
    tmp_path: Path, monkeypatch
) -> None:
    """A refused reattempt admits no child, so the lineage cannot carry it.

    If a restart recomputed the position from lineage alone it would hand the
    turn the first, shortest wait again, and restarts could reattempt a turn
    without bound however many the limit says.
    """

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    failed = _transport_failed_task(store, operation_id="still-down")
    monkeypatch.setattr(tasks, "_schedule_transport_retry", lambda _operation, *, attempt: None)
    tasks._auto_retry_transport_loss(failed)
    # The first wait elapsed and its reattempt was refused, exactly as the
    # timer's own failure path records it.
    store.record_agent_task_receipt(
        failed.operation_id,
        "transport_auto_retry_failed",
        {"exception_type": "ValueError"},
        tier="diagnostic",
    )

    restarted = BackgroundAgentTasks(store, _done_stream)
    rearmed: list[float] = []
    monkeypatch.setattr(
        restarted,
        "_schedule_transport_retry",
        lambda _operation, *, attempt: rearmed.append(
            AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[attempt]
        ),
    )

    restarted.recover_at_startup()

    assert rearmed == [AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[1]]


def test_a_restart_re_arms_nothing_for_a_turn_already_taken_over(
    tmp_path: Path, monkeypatch
) -> None:
    """A restart must not reopen the supersession window the timer closes.

    A human who pressed Retry before the process stopped already has the turn
    running; re-arming would run it a second time.
    """

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    monkeypatch.setattr(tasks, "_schedule_transport_retry", lambda _operation, *, attempt: None)
    # One request for both attempts: a continuation must keep its parent's
    # dispatch authority, which is derived from the request.
    shared = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="project",
        chat_id="launch-restart-supersession",
        message="Exercise the reattempt across a restart.",
        mode="work",
        patch_kind="work",
    )
    failed = _transport_failed_task(store, operation_id="dropped", request=shared)
    tasks._auto_retry_transport_loss(failed)
    _admitted_launch_task(
        store,
        operation_id="human-retry",
        parent_operation_id=failed.operation_id,
        request=shared,
    )

    restarted = BackgroundAgentTasks(store, _done_stream)
    rearmed: list[str] = []
    monkeypatch.setattr(
        restarted,
        "_schedule_transport_retry",
        lambda operation_id, *, attempt: rearmed.append(operation_id),
    )

    restarted.recover_at_startup()

    assert rearmed == []


def test_a_refused_reattempt_keeps_the_remaining_waits(tmp_path: Path, monkeypatch) -> None:
    """A host still rebooting refuses admission, which is what the longer waits
    are for. No child is admitted, so the receipt chain cannot carry the count
    and the sequence has to hand it on itself."""

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    failed = _transport_failed_task(store, operation_id="still-down")

    def refuse(_operation_id, **_kwargs):
        raise ValueError("the execution machine is unavailable")

    monkeypatch.setattr(tasks, "retry", refuse)
    scheduled: list[int] = []
    original = tasks._schedule_transport_retry

    def record(operation_id: str, *, attempt: int) -> None:
        scheduled.append(attempt)
        if len(scheduled) == 1:
            original(operation_id, attempt=attempt)

    monkeypatch.setattr(tasks, "_schedule_transport_retry", record)
    monkeypatch.setattr("rcp.background.AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS", (0.01, 0.01, 0.01))

    tasks._auto_retry_transport_loss(failed)
    wait_until(
        lambda: len(scheduled) >= 2,
        timeout=5,
        detail=lambda: f"the next wait was never scheduled: {scheduled}",
    )
    tasks.shutdown(timeout=0.5)

    assert scheduled[:2] == [0, 1]
    assert store.agent_task_has_receipt(failed.operation_id, "transport_auto_retry_failed")


def test_provider_auth_finalizer_marks_login_before_settlement(tmp_path):
    store = _store(tmp_path)
    observed = []

    async def stream(_project, _kind, _request, execution):
        execution.login_generation = store.provider_login_state("codex", "").generation
        yield _sse(AgentEvent(event="error", text="refresh_token_reused"))

    def settled(_project, _kind, _request, _execution):
        observed.append(store.provider_login_state("codex", "").state)

    tasks = BackgroundAgentTasks(store, stream, on_task_settled=settled)
    task = _admitted_launch_task(store, operation_id="dead-login")
    tasks.launch_admitted(task.operation_id)
    finished = wait_for_task(store, task.operation_id, expect="failed")
    wait_until(lambda: bool(observed))
    assert finished.failure_kind == "provider_auth"
    assert store.provider_login_state("codex", "").source == "turn"
    assert observed == ["signed_out"]


def test_late_provider_auth_finalizer_cannot_undo_verified_login(tmp_path):
    store = _store(tmp_path)

    async def stream(_project, _kind, _request, execution):
        execution.login_generation = store.provider_login_state("codex", "").generation
        store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")
        yield _sse(AgentEvent(event="error", text="refresh_token_reused"))

    tasks = BackgroundAgentTasks(store, stream)
    task = _admitted_launch_task(store, operation_id="late-dead-login")
    tasks.launch_admitted(task.operation_id)
    finished = wait_for_task(store, task.operation_id, expect="failed")
    # The account was repaired after this turn captured its generation, so the
    # stale failure neither fences the login nor parks the task behind sign-in.
    assert finished.failure_kind is None
    state = store.provider_login_state("codex", "")
    assert state.state == "signed_in"
    assert state.generation == 1


def test_only_an_episode_pins_the_machine_its_recovery_runs_on(tmp_path: Path) -> None:
    """An episode owns watchers and a stage on its machine; a lone turn owns neither."""

    store = _store(tmp_path)
    # A second reachable machine, so "may move" is a real move and not a no-op.
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[[repositories]]", '[[machines]]\nalias = "cluster"\nhost = ""\n[[repositories]]', 1
        ),
        encoding="utf-8",
    )
    stage = tmp_path / "retry-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        yield _sse(AgentEvent(event="error", text="Transient provider failure."))

    tasks = BackgroundAgentTasks(store, stream)
    standalone = tasks.start(
        "project",
        "project_chat",
        RunRequest(
            provider="codex",
            model="",
            reasoning="medium",
            run_on="laptop",
            run_truth_scope=[],
            chat_scope="project",
            chat_id="machine-chat",
            message="Exercise the recovery machine boundary.",
            mode="work",
            patch_kind="work",
        ),
        operation_id="standalone-root",
        authorized_by=fabricated_authorizer("Researcher"),
    )
    standalone = wait_for_task(store, standalone.operation_id, expect="failed")
    assert standalone.episode_id is None

    moved = tasks.retry(
        standalone.operation_id,
        run_on="cluster",
        authorized_by=standalone.authorized_by,
    )
    assert moved.request["run_on"] == "cluster"

    episode_turn = tasks.start(
        "project",
        "node_chat",
        _experiment_request(),
        operation_id="episode-root",
        authorized_by=fabricated_authorizer("Researcher"),
    )
    episode_turn = wait_for_task(store, episode_turn.operation_id, expect="failed")
    assert episode_turn.episode_id is not None

    with pytest.raises(ValueError, match="cannot change its pinned execution machine"):
        tasks.retry(
            episode_turn.operation_id,
            run_on="cluster",
            authorized_by=episode_turn.authorized_by,
        )


def test_a_host_that_goes_quiet_mid_finalization_waits_rather_than_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaching the journal proves nothing about reaching it again.

    Every recorded owner reopens its retained stage over SSH, and that second
    reach can fail transiently while the pass itself sits finished and intact on
    the host. Settling that as a failure would discard a completed provider turn
    over a dropped packet.
    """

    from rcp.runs import remote_finalization
    from rcp.transport import StateUnavailable

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "host-went-quiet")
    pid_file = "/stage/host-went-quiet.pid"
    monkeypatch.setattr(
        remote_finalization,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(
            "finalize", "Finished", pid_file, _recorded_turn(pid_file)
        ),
    )

    async def unreachable_stage(*_args):
        raise StateUnavailable(
            "The saved remote staging directory is unavailable; retry this operation instead."
        )
        yield  # pragma: no cover - the raise above ends this generator

    tasks = BackgroundAgentTasks(store, _done_stream, recorded_stream=unreachable_stage)

    assert tasks._reconcile_remote_results() is True

    task = store.agent_task(waiting.operation_id)
    assert task is not None
    assert task.status == "running"
    assert task.phase == "awaiting_remote_result"
    # The claim is released, so the next sweep can try the host again.
    assert store.claim_recorded_finalization(waiting.operation_id)
    assert store.unresolved_remote_provider_passes("remote", "/stage")


def test_a_stage_that_vanishes_mid_settlement_waits_rather_than_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settlement reads the stage again, long after the journal was read.

    Those reads report an unreachable host as an unreadable deliverable, which
    reads exactly like an agent that wrote nonsense. Only the read that failed
    can still tell the two apart, so it says which happened and no verdict is
    drawn about a turn whose stage could not be seen.
    """

    from rcp.runs import remote_finalization

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "stage-vanished-mid-settlement")
    pid_file = "/stage/stage-vanished-mid-settlement.pid"
    monkeypatch.setattr(
        remote_finalization,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(
            "finalize", "Finished", pid_file, _recorded_turn(pid_file)
        ),
    )

    async def unreadable_deliverable(_project_id, _kind, _request, execution, _pass):
        # What settlement does when its stage read could not reach the host.
        execution.stage_unreachable = True
        yield _sse(
            AgentEvent(event="error", text="The agent wrote a patch file that could not be read.")
        )

    tasks = BackgroundAgentTasks(store, _done_stream, recorded_stream=unreadable_deliverable)

    assert tasks._reconcile_remote_results() is True

    task = store.agent_task(waiting.operation_id)
    assert task is not None
    assert task.status == "running"
    assert task.phase == "awaiting_remote_result"
    assert store.claim_recorded_finalization(waiting.operation_id)


def test_a_removed_stage_still_fails_the_turn_it_belonged_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stage that is genuinely gone is an answer, not a silence."""

    from rcp.runs import remote_finalization

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "stage-removed")
    pid_file = "/stage/stage-removed.pid"
    monkeypatch.setattr(
        remote_finalization,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(
            "finalize", "Finished", pid_file, _recorded_turn(pid_file)
        ),
    )

    async def unreadable_deliverable(*_args):
        yield _sse(
            AgentEvent(event="error", text="The agent wrote a patch file that could not be read.")
        )

    tasks = BackgroundAgentTasks(store, _done_stream, recorded_stream=unreadable_deliverable)
    tasks._reconcile_remote_results()

    assert store.agent_task(waiting.operation_id).status == "failed"


def test_a_real_failure_stands_even_if_the_host_leaves_right_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journalled provider error is a verdict; the host's later silence is not.

    Asking the host whether it is still there after the fact cannot tell these
    apart, and answering "wait" to both leaves a decided turn waiting forever
    once that host is decommissioned. Only the read that failed knows, so only
    the read that failed says.
    """

    from rcp.runs import remote_finalization
    from rcp.transport import RemoteRunStage

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "verdict-then-silence")
    pid_file = "/stage/verdict-then-silence.pid"
    monkeypatch.setattr(
        remote_finalization,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(
            "finalize", "Finished", pid_file, _recorded_turn(pid_file)
        ),
    )
    # The host is unreachable by the time anyone could ask, which is exactly the
    # state that used to suppress the verdict below.
    monkeypatch.setattr(RemoteRunStage, "directory_exists", lambda _self, _root: None)

    async def provider_said_it_failed(_project_id, _kind, _request, _execution, _pass):
        # No stage read failed: the journal itself carries the provider's error.
        yield _sse(AgentEvent(event="error", text="The provider refused the task."))

    tasks = BackgroundAgentTasks(store, _done_stream, recorded_stream=provider_said_it_failed)
    tasks._reconcile_remote_results()

    task = store.agent_task(waiting.operation_id)
    assert task is not None and task.status == "failed"
    assert "refused" in (task.error or "")


def test_a_pass_the_live_stream_already_consumed_is_not_owed_reconciliation(
    tmp_path: Path,
) -> None:
    """Parking a task on a remote result it already has would park it forever.

    Reconciliation exists for a pass nobody read. Both the scheduler's query and
    `reconcile_remote_pass` find one the same way: a supervised start with no
    recorded stop. Once the live stream consumed the turn and wrote that stop
    down, no reconciler will ever revisit the task, so settlement that fails
    after that point has to say so rather than wait.
    """

    from rcp.runs import remote_finalization

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "already-consumed")
    pid_file = f"/stage/{waiting.operation_id}.pid"
    assert store.operation_ids_awaiting_remote_result() == [waiting.operation_id]

    store.finish_remote_provider_pass(waiting.operation_id, pid_file)

    assert store.operation_ids_awaiting_remote_result() == []
    assert not store.unresolved_remote_provider_passes("remote", "/stage")
    record = store.agent_task(waiting.operation_id)
    assert record is not None and record.phase == "awaiting_remote_result"
    # Even reached directly, reconciliation has nothing outstanding to act on.
    decision = remote_finalization.reconcile_remote_pass(
        store,
        record,
        stopped=lambda *_args: True,
        read_journal=lambda *_args: None,
    )
    assert decision.action == "settled"


def test_a_stage_the_host_says_is_gone_fails_instead_of_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attaching collapses two answers; only one of them is worth waiting on.

    A host that cannot be asked is silence and retries. A host that answers
    "that stage is not there" has given the turn its verdict, and retrying it
    forever would hide a deleted stage behind a permanent wait.
    """

    from rcp.runs import remote_finalization
    from rcp.transport import StateMissing

    store = _store(tmp_path)
    waiting = _waiting_remote_work(store, "stage-answered-gone")
    pid_file = "/stage/stage-answered-gone.pid"
    monkeypatch.setattr(
        remote_finalization,
        "reconcile_remote_pass",
        lambda *_args, **_kwargs: Reconciliation(
            "finalize", "Finished", pid_file, _recorded_turn(pid_file)
        ),
    )

    async def stage_is_gone(*_args):
        raise StateMissing("The saved remote staging directory is unavailable.")
        yield  # pragma: no cover - the raise above ends this generator

    tasks = BackgroundAgentTasks(store, _done_stream, recorded_stream=stage_is_gone)
    tasks._reconcile_remote_results()

    task = store.agent_task(waiting.operation_id)
    assert task is not None and task.status == "failed"
    assert "staging directory is unavailable" in (task.error or "")


def test_a_link_lost_before_the_provider_is_classified_from_its_typed_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readiness probe, previous-pass check, or input transfer that cannot
    reach the host fails the turn with no `provider_exit` to read. The execution
    carries the probe's own word instead; the error text is never consulted."""

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    task = _admitted_launch_task(store, operation_id="never-started")
    store.mark_agent_task_running(task.operation_id)
    request = tasks._request_from_record(store.agent_task(task.operation_id))
    error = "gpu.example.edu is unreachable, so codex could not be checked."

    def kind(*, stage_unreachable: bool, host: str | None = "gpu.example.edu"):
        execution = SimpleNamespace(
            stage_host=host, stage_unreachable=stage_unreachable, login_generation=0
        )
        return tasks._failure_kind(task.operation_id, request, execution, error)  # type: ignore[arg-type]

    assert kind(stage_unreachable=True) == "transport_lost"
    assert kind(stage_unreachable=False) is None
    assert kind(stage_unreachable=True, host="") is None
    # A stage that never opened checkpointed no host. The turn was still bound
    # to one by its request, and the link lost opening the stage is on it.
    assert kind(stage_unreachable=True, host=None) is None
    monkeypatch.setattr(
        background_module, "provider_login_host", lambda manifest, run_on: "gpu.example.edu"
    )
    assert kind(stage_unreachable=True, host=None) == "transport_lost"


@pytest.mark.parametrize("cause", ["retry", "graph_repair"])
def test_a_second_recovery_of_one_task_is_refused_inside_admission(
    tmp_path: Path, cause: str
) -> None:
    """The window between `_transport_retry_superseded` and `retry` used to admit
    a second child if a human Retry was admitted inside it. The claim now lives
    in the transaction that inserts the child, for a graph-repair recovery too."""

    store = _store(tmp_path)
    # One request throughout: a continuation must keep its parent's dispatch authority.
    request = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="project",
        chat_id="claimed-once",
        message="Exercise the admitted launch boundary.",
        mode="work",
        patch_kind="work",
    )
    failed = _transport_failed_task(store, operation_id="dropped", request=request)
    human = _admitted_launch_task(
        store, operation_id="human-retry", parent_operation_id=failed.operation_id, request=request
    )
    # Settled, so the chat overlap guard has nothing left to refuse.
    store.mark_agent_task_running(human.operation_id)
    store.fail_agent_task(human.operation_id, "also failed")

    with pytest.raises(AgentTaskAlreadyContinued):
        _admitted_launch_task(
            store,
            operation_id="timer-retry",
            parent_operation_id=failed.operation_id,
            request=request,
            continuation_cause=cause,
        )
    assert store.agent_task("timer-retry") is None


def test_a_reattempt_refused_by_the_claim_stands_down(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    _transport_failed_task(store, operation_id="dropped")

    def taken(_operation_id, **_kwargs):
        raise AgentTaskAlreadyContinued("another attempt already continues this task")

    monkeypatch.setattr(tasks, "retry", taken)
    tasks._run_transport_retry("dropped", attempt=0)

    assert store.agent_task_has_receipt("dropped", "transport_auto_retry_superseded")
    assert not store.agent_task_has_receipt("dropped", "transport_auto_retry_failed")
    assert tasks._transport_retry_timers == []


def test_shutdown_waits_for_a_reattempt_that_is_already_admitting(
    tmp_path: Path, monkeypatch
) -> None:
    """A callback that passed its shutdown check is admitting a child. If
    shutdown fenced spawns underneath it, that child would sit queued until a
    startup interrupted it, and the parent would no longer be owed anything."""

    store = _store(tmp_path)
    tasks = BackgroundAgentTasks(store, _done_stream)
    _transport_failed_task(store, operation_id="dropped")
    entered = threading.Event()
    release = threading.Event()
    observed: list[bool] = []

    def slow_retry(operation_id, **_kwargs):
        entered.set()
        assert release.wait(5)
        # Spawns are still open: this is what lets the child get a worker.
        observed.append(tasks._shutdown_requested)

    monkeypatch.setattr(tasks, "retry", slow_retry)
    reattempt = threading.Thread(
        target=tasks._run_transport_retry, args=("dropped",), kwargs={"attempt": 0}
    )
    reattempt.start()
    assert entered.wait(5)
    stopping = threading.Thread(target=tasks.shutdown, kwargs={"timeout": 5})
    stopping.start()
    wait_until(lambda: tasks._transport_retry_closed, timeout=5)
    stopping.join(0.2)
    assert stopping.is_alive() and not tasks._shutdown_requested

    release.set()
    reattempt.join(5)
    stopping.join(5)
    assert not reattempt.is_alive() and not stopping.is_alive()
    assert observed == [False] and tasks._shutdown_requested

    # A reattempt that arrives once shutdown has begun stands down instead.
    tasks._run_transport_retry("dropped", attempt=0)
    assert observed == [False]
