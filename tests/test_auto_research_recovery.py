from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

import pytest

from rcp import background as background_module
from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.auto_research import (
    AutoResearchRunRequest,
    AutoResearchStartRequest,
    settle_auto_research_stop,
)
from rcp.runs.auto_research_admission import (
    start_auto_research,
    start_auto_research_turn,
)
from rcp.runs.auto_research_recovery import (
    AutoResearchOrchestratorTerminalFailure,
    reconcile_auto_research_task_settlement,
    reconcile_due_auto_research_recoveries,
    reconcile_orphaned_auto_research_failures,
)
from rcp.runs.turn_collection import CollectedTurn, CollectionPending
from rcp.storage import AppStore, ProjectRecord

from .helpers import fabricated_authorizer, wait_for_task, wait_until, write_local_test_manifest


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _store(tmp_path: Path) -> AppStore:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(write_local_test_manifest(tmp_path)),
            name="project",
            state_location="/tmp/project/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )
    return store


def _start(tasks: BackgroundAgentTasks, *, operation_id: str = "root", provider: str = "codex"):
    episode, root = start_auto_research(
        tasks,
        "project",
        AutoResearchStartRequest(invocation_ceiling=4, run_truth_scope=["repo"], provider=provider),
        authorized_by=fabricated_authorizer(),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto_research",
        operation_id=operation_id,
    )
    assert episode.graph_target.kind == "branch"
    assert episode.graph_target.branch_id == episode.episode_id
    assert episode.graph_base_head == GraphHeadRef(revision=0)
    assert root.graph_target == episode.graph_target
    return episode, root


def _install_recovery_callback(tasks: BackgroundAgentTasks) -> None:
    """Stand in for the app's one settlement callback, Auto-research half only."""

    def settled(_project_id, _kind, request, execution) -> None:
        if not isinstance(request, AutoResearchRunRequest):
            return
        episode = tasks.store.episode(request.episode_id)
        if episode is None:
            return
        if episode.stop_requested_at is not None:
            episode = settle_auto_research_stop(tasks.store, episode.episode_id) or episode
        reconcile_auto_research_task_settlement(tasks, episode, request, execution)

    tasks.on_task_settled = settled


def _wait_for_recovery(store: AppStore, recovery_id: str):
    return wait_until(
        lambda: store.auto_research_recovery(recovery_id),
        detail=f"auto_research recovery did not appear: {recovery_id}",
    )


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
    auto_research, root = _start(tasks)
    root = wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    assert recovery.retry_mode == "exact"
    assert store.episode_budget_meter(auto_research.episode_id).invocations_used == 1

    assert (
        reconcile_due_auto_research_recoveries(
            tasks,
            as_of=recovery.next_attempt_at,
        )
        == 1
    )
    admitted = store.auto_research_recovery("task:root")
    assert admitted is not None and admitted.admitted_operation_id is not None
    child = wait_for_task(store, admitted.admitted_operation_id, expect="succeeded")
    assert child.native_session_id == root.native_session_id == "session-1"
    assert child.stage_root == root.stage_root == str(stage)
    assert store.episode_budget_meter(auto_research.episode_id).invocations_used == 1
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
        reconcile_due_auto_research_recoveries(
            tasks,
            as_of=recovery.next_attempt_at,
        )
        == 1
    )
    admitted = store.auto_research_recovery("task:root")
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
            reconcile_due_auto_research_recoveries(
                tasks,
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

    admitted = store.auto_research_recovery("task:root")
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
    auto_research, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    pending = _wait_for_recovery(store, "task:root")
    assert pending.retry_mode == "clean"

    restarted = BackgroundAgentTasks(AppStore(store.path), stream)
    assert len(restarted.store.due_auto_research_recoveries(as_of=pending.next_attempt_at)) == 1
    assert (
        reconcile_due_auto_research_recoveries(
            restarted,
            as_of=pending.next_attempt_at,
        )
        == 1
    )
    admitted = restarted.store.auto_research_recovery("task:root")
    assert admitted is not None and admitted.admitted_operation_id is not None
    child = wait_for_task(restarted.store, admitted.admitted_operation_id, expect="succeeded")
    assert child.native_session_id == "clean-session"
    assert observed == [None, None]
    assert restarted.store.episode_budget_meter(auto_research.episode_id).invocations_used == 1


def test_worker_failure_never_becomes_auto_research_verdict(tmp_path: Path) -> None:
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
    auto_research, root = _start(tasks)
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    worker = start_auto_research_turn(
        tasks,
        auto_research.episode_id,
        AutoResearchRunRequest(
            provider="codex",
            episode_id=auto_research.episode_id,
            role="worker",
            control_node_id="exp/check",
        ),
        parent_operation_id=root.operation_id,
        operation_id="worker",
    )
    wait_for_task(store, worker.operation_id, expect="failed")
    current = store.episode(auto_research.episode_id)
    assert current is not None and current.status == "running" and current.ending is None
    assert store.auto_research_recovery("task:worker") is None


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


def test_repeated_provider_failures_share_one_bounded_allocation_recovery(
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
    auto_research, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    assert recovery.attempts == 0
    assert _recovery_delay_seconds(recovery) == 120

    complete_recovery = store.complete_auto_research_recovery

    def complete_after_child_settles(
        recovery_id: str,
        *,
        admitted_operation_id: str | None = None,
        expected_operation_id: str | None = None,
    ):
        if admitted_operation_id is not None:

            def child_settled() -> bool:
                child = store.agent_task(admitted_operation_id)
                current = store.auto_research_recovery(recovery_id)
                return bool(
                    child is not None
                    and child.status == "failed"
                    and current is not None
                    and current.operation_id == admitted_operation_id
                )

            wait_until(
                child_settled,
                detail="recovery child did not settle before admission checkpoint",
            )
        return complete_recovery(
            recovery_id,
            admitted_operation_id=admitted_operation_id,
            expected_operation_id=expected_operation_id,
        )

    monkeypatch.setattr(store, "complete_auto_research_recovery", complete_after_child_settles)

    for expected_attempt, expected_consumed, expected_delay in (
        (2, 1, 240),
        (3, 2, 480),
        (4, 3, None),
    ):
        expected_status = "exhausted" if expected_delay is None else "pending"
        reconcile_due_auto_research_recoveries(
            tasks,
            as_of=recovery.next_attempt_at,
        )

        def failed_recovery(
            expected_attempt=expected_attempt,
            expected_consumed=expected_consumed,
            expected_status=expected_status,
        ):
            candidate = store.auto_research_recovery("task:root")
            assert candidate is not None
            current = store.agent_task(candidate.operation_id or "")
            if (
                candidate.attempts == expected_consumed
                and candidate.status == expected_status
                and current is not None
                and current.attempt == expected_attempt
                and current.status == "failed"
            ):
                return candidate
            return None

        recovery = wait_until(
            failed_recovery,
            detail=f"auto_research recovery attempt {expected_attempt} did not fail",
        )
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
    current_auto_research = store.episode(auto_research.episode_id)
    assert current_auto_research is not None
    assert current_auto_research.status == "running"
    assert current_auto_research.ending is None
    assert store.episode_budget_meter(auto_research.episode_id).invocations_used == 1


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
    reconcile_due_auto_research_recoveries(
        tasks,
        as_of=recovery.next_attempt_at,
    )
    recovery = store.auto_research_recovery("task:root")
    assert recovery is not None
    assert recovery.attempts == 1
    assert recovery.status == "pending"
    assert _recovery_delay_seconds(recovery) == 240

    monkeypatch.setattr(tasks, "retry", retry)
    restarted = BackgroundAgentTasks(AppStore(store.path), stream)
    _install_recovery_callback(restarted)
    assert len(restarted.store.due_auto_research_recoveries(as_of=recovery.next_attempt_at)) == 1
    reconcile_due_auto_research_recoveries(
        restarted,
        as_of=recovery.next_attempt_at,
    )

    def failed_recovery():
        candidate = restarted.store.auto_research_recovery("task:root")
        assert candidate is not None
        current = restarted.store.agent_task(candidate.operation_id or "")
        if (
            candidate.attempts == 2
            and candidate.status == "pending"
            and current is not None
            and current.status == "failed"
        ):
            return candidate
        return None

    recovery = wait_until(
        failed_recovery,
        detail="mixed recovery provider attempt did not fail",
    )
    assert recovery.status == "pending"
    assert _recovery_delay_seconds(recovery) == 480

    durable_store = AppStore(store.path)
    durable = durable_store.auto_research_recovery("task:root")
    assert durable is not None
    assert durable.attempts == 2
    assert durable.status == "pending"
    assert durable.next_attempt_at == recovery.next_attempt_at

    durable_tasks = BackgroundAgentTasks(durable_store, stream)
    monkeypatch.setattr(durable_tasks, "retry", fail_admission)
    reconcile_due_auto_research_recoveries(
        durable_tasks,
        as_of=durable.next_attempt_at,
    )
    exhausted = AppStore(store.path).auto_research_recovery("task:root")
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
        yield _sse(AgentEvent(event="session", session_id="orchestrator-session"))
        raise AutoResearchOrchestratorTerminalFailure(
            "typed structural failure",
        )

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    auto_research, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    fenced = wait_until(
        lambda: (
            candidate
            if (candidate := store.episode(auto_research.episode_id)) is not None
            and candidate.status == "wrapping_up"
            else None
        ),
        detail="typed failure did not fence the auto_research",
    )
    assert fenced.status == "wrapping_up"
    assert fenced.ending == "failed"
    assert fenced.ending_diagnostic == "typed structural failure"
    assert store.auto_research_recovery("task:root") is None

    receipts = store.agent_task_receipts(root.operation_id)
    typed = [item for item in receipts if item.category == "auto_research_orchestrator_failure"]
    assert len(typed) == 1
    assert typed[0].payload["classification"] == "structural_unrecoverable"
    assert typed[0].payload["recoverable"] is False


def test_recovery_admission_whose_launch_fails_records_a_durable_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="session-1"))
        yield _sse(AgentEvent(event="error", text="provider unavailable"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    _, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")

    def refuse_launch(_operation_id):
        raise RuntimeError("simulated launch refusal")

    monkeypatch.setattr(tasks, "launch_admitted", refuse_launch)
    reconcile_due_auto_research_recoveries(tasks, as_of=recovery.next_attempt_at)

    child = store.auto_research_task_recovery_child(root.operation_id)
    assert child is not None
    assert child.status == "queued"
    receipts = {
        receipt.category: receipt.payload
        for receipt in store.agent_task_receipts(child.operation_id)
    }
    assert (
        receipts["operation_launch_failed_after_admission"]["detail"] == "simulated launch refusal"
    )
    admitted = store.auto_research_recovery("task:root")
    assert admitted is not None
    assert admitted.status == "admitted"
    assert admitted.admitted_operation_id == child.operation_id


def test_a_vanished_session_retries_clean_rather_than_resuming_it(tmp_path: Path) -> None:
    """A thread the provider no longer has cannot be resumed, so an exact retry
    would fail the same way and spend the allocation proving it."""

    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="gone-session"))
        yield _sse(
            AgentEvent(
                event="error",
                text="collab spawn failed: no thread with id: 01a0976e-c283-7622-b2d6-43bf9d992198",
            )
        )

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    _, root = _start(tasks)
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")

    assert recovery.failure_kind == "stale_session"
    assert recovery.retry_mode == "clean"


def test_a_revoked_login_is_parked_until_verified(tmp_path: Path) -> None:
    """The way back is a verified sign-in, never a spaced automatic retry."""

    store = _store(tmp_path)
    stage = tmp_path / "orchestrator-stage"
    stage.mkdir()

    async def stream(_project_id, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="revoked-session"))
        yield _sse(
            AgentEvent(
                event="error",
                text='stream error: unexpected status 401 Unauthorized: {"error":'
                '{"type":"token_revoked","message":"Your session has ended."}}',
            )
        )

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    _, root = _start(tasks, provider="codex")
    wait_for_task(store, root.operation_id, expect="failed")

    assert store.agent_task(root.operation_id).failure_kind == "provider_auth"
    recovery = wait_until(
        lambda: store.auto_research_recovery("task:root"),
        detail="settlement never scheduled the recovery",
    )
    assert recovery.status == "blocked"
    assert recovery.next_attempt_at is None
    assert reconcile_due_auto_research_recoveries(tasks) == 0
    store.mark_provider_login_verified("codex", "", member_id="member", detail="verified")
    assert store.release_provider_auth_recoveries([(recovery.episode_id, root.operation_id)]) == [
        recovery.recovery_id
    ]
    assert store.release_provider_auth_recoveries([(recovery.episode_id, root.operation_id)]) == []
    calls = []
    original_retry = tasks.retry

    def retry(operation_id):
        calls.append(operation_id)
        return original_retry(operation_id)

    tasks.retry = retry
    assert reconcile_due_auto_research_recoveries(tasks) == 1
    assert reconcile_due_auto_research_recoveries(tasks) == 0
    assert calls == [root.operation_id]


def test_pending_recovery_is_not_claimed_while_account_signed_out(tmp_path):
    store = _store(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()

    async def stream(_project, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="saved-session"))
        yield _sse(AgentEvent(event="error", text="Temporary provider failure"))

    tasks = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(tasks)
    _, root = _start(tasks, provider="codex")
    wait_for_task(store, root.operation_id, expect="failed")
    recovery = _wait_for_recovery(store, "task:root")
    assert recovery.status == "pending"
    store.mark_provider_login_failed("codex", "", generation=0, detail="expired", source="turn")
    with store.connection() as connection:
        before = connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    budget = store.episode(root.episode_id).invocations_used
    with pytest.raises(ValueError, match="signed out"):
        tasks.retry(root.operation_id)
    assert reconcile_due_auto_research_recoveries(tasks, as_of=recovery.next_attempt_at) == 0
    assert store.auto_research_recovery(recovery.recovery_id) == recovery
    assert store.episode(root.episode_id).invocations_used == budget
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == before


@pytest.mark.parametrize("session_checkpointed", [True, False])
@pytest.mark.parametrize("protocol_complete", [True, False])
@pytest.mark.parametrize("unreachable", [True, False])
def test_auto_research_collection_admission_preserves_actor_and_waits_without_retry_spend(
    tmp_path, monkeypatch, session_checkpointed, protocol_complete, unreachable
) -> None:
    store = _store(tmp_path)
    manifest_path = Path(store.project("project").locator)
    manifest_path.write_text(manifest_path.read_text().replace('host = ""', 'host = "test-host"'))
    stage = tmp_path / "original-stage"
    stage.mkdir()
    pid_file = str(stage / "original.pid")
    observed = []

    async def stream(_project_id, _kind, request, execution):
        observed.append((execution.continuation, request.session_id, request.actor_operation_id))
        if execution.continuation == "fresh":
            execution.checkpoint_stage("test-host", str(stage))
            store.begin_remote_provider_pass(
                execution.operation_id, "test-host", str(stage), pid_file, journaled=True
            )
            if session_checkpointed:
                yield _sse(AgentEvent(event="session", session_id="original-session"))
            yield _sse(AgentEvent(event="error", text="SSH delivery was interrupted"))
            return
        assert execution.continuation == "collect", "paid work must not be retried"
        yield _sse(AgentEvent(event="answer", text="The existing remote work completed."))
        yield _sse(AgentEvent(event="done"))

    pending = True

    def collected(_store, _record):
        if pending:
            if unreachable:
                from rcp.transport import RemoteStageUnreachable

                raise RemoteStageUnreachable("The host cannot be reached.")
            raise CollectionPending("The original provider is still running.")
        return CollectedTurn(
            "root",
            pid_file,
            {
                "protocol_complete": protocol_complete,
                "journal_complete": True,
                "root_thread_id": "original-session",
            },
            "",
            None,
        )

    monkeypatch.setattr(background_module, "read_collected_turn", collected)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args: True)
    tasks = BackgroundAgentTasks(store, stream)
    monkeypatch.setattr(tasks, "_schedule_transport_retry", lambda *_args, **_kwargs: None)
    _install_recovery_callback(tasks)
    episode, root = _start(tasks)
    original = wait_for_task(store, root.operation_id, expect="failed")
    assert bool(original.native_session_id) == session_checkpointed
    recovery = _wait_for_recovery(store, "task:root")
    initial_attempts = recovery.attempts
    initial_budget = store.episode_budget_meter(episode.episode_id)
    store.mark_provider_login_failed(
        "codex", "test-host", generation=0, detail="expired after execution", source="turn"
    )
    for _ in range(recovery.max_attempts + 1):
        assert reconcile_due_auto_research_recoveries(tasks, as_of=recovery.next_attempt_at) == 1
        recovery = store.auto_research_recovery("task:root")
        assert recovery.status == "pending"
        assert recovery.attempts == initial_attempts
        assert store.auto_research_task_recovery_child(root.operation_id) is None
        assert len(observed) == 1

    pending = False
    assert reconcile_due_auto_research_recoveries(tasks, as_of=recovery.next_attempt_at) == 1
    recovery = store.auto_research_recovery("task:root")
    child = wait_for_task(
        store, recovery.admitted_operation_id, expect="succeeded" if protocol_complete else "failed"
    )
    assert store.agent_task(root.operation_id).native_session_id == "original-session"
    assert child.native_session_id == "original-session"
    assert child.stage_host == original.stage_host
    assert child.stage_root == original.stage_root
    assert child.authorized_by == original.authorized_by
    assert child.dispatch_authority == original.dispatch_authority
    assert child.runtime_id == original.runtime_id
    assert child.graph_target == original.graph_target
    assert child.request["actor_operation_id"] == root.operation_id
    binding = store.auto_research_actor_binding(child.operation_id)
    assert binding.actor_operation_id == root.operation_id
    assert binding.current_operation_id == child.operation_id
    assert store.episode_budget_meter(episode.episode_id) == initial_budget
    if protocol_complete:
        assert observed[-1] == ("collect", "original-session", root.operation_id)
    else:
        assert store.agent_task_has_receipt(child.operation_id, "provider_collection_incomplete")
        wait_until(lambda: child.operation_id not in tasks._workers)
        assert store.auto_research_recovery("task:root").status == "admitted"
        assert reconcile_orphaned_auto_research_failures(tasks) == []
        assert store.auto_research_recovery("task:root").status == "admitted"
        assert len(observed) == 1
    with pytest.raises(ValueError, match="no undelivered"):
        tasks.collect(root.operation_id)


@pytest.mark.parametrize("report_started", [False, True])
def test_auto_research_collect_finishes_delivery_behind_its_durable_ending(
    tmp_path, monkeypatch, report_started
):
    from rcp.runs.auto_research import auto_research_completion_signal, auto_research_wrapup_spec
    from rcp.runs.episodes.wrapup import begin_episode_report_wrapup

    store = _store(tmp_path)
    manifest_path = Path(store.project("project").locator)
    manifest_path.write_text(manifest_path.read_text().replace('host = ""', 'host = "test-host"'))
    stage = tmp_path / "original-stage"
    stage.mkdir()
    pid_file = str(stage / "original.pid")
    launched = []

    async def stream(_project_id, _kind, request, execution):
        launched.append(execution.continuation)
        if execution.continuation == "fresh":
            execution.checkpoint_stage("test-host", str(stage))
            store.begin_remote_provider_pass(
                execution.operation_id, "test-host", str(stage), pid_file, journaled=True
            )
            yield _sse(AgentEvent(event="session", session_id="original-session"))
            yield _sse(AgentEvent(event="error", text="Delivery interrupted after ending."))
            return
        assert execution.continuation == "collect"
        assert request.actor_operation_id == "root"
        yield _sse(AgentEvent(event="answer", text="Completed original work."))
        yield _sse(AgentEvent(event="done"))

    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args: True)
    monkeypatch.setattr(
        background_module,
        "read_collected_turn",
        lambda *_args: CollectedTurn(
            "root",
            pid_file,
            {
                "protocol_complete": True,
                "journal_complete": True,
                "root_thread_id": "original-session",
            },
            "",
            None,
        ),
    )
    tasks = BackgroundAgentTasks(store, stream)
    monkeypatch.setattr(tasks, "_schedule_transport_retry", lambda *_args, **_kwargs: None)
    episode, root = start_auto_research(
        tasks,
        "project",
        AutoResearchStartRequest(
            invocation_ceiling=4,
            run_truth_scope=["repo"],
            provider="codex",
            model="gpt-5",
            reasoning="medium",
            run_on="laptop",
        ),
        authorized_by=fabricated_authorizer(),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _episode: None,
        episode_id="auto_research",
        operation_id="root",
    )
    source = wait_for_task(store, root.operation_id, expect="failed")
    signal = auto_research_completion_signal(store, episode.episode_id)
    if report_started:
        begin_episode_report_wrapup(store, auto_research_wrapup_spec(store, signal))
        store.allocate_episode_report_attempt(episode.episode_id)
    ended = store.episode(episode.episode_id)
    assert ended.ending == "completed"
    assert ended.wrapup_state == ("running" if report_started else "not_started")
    meter = store.episode_budget_meter(episode.episode_id)
    wrapup = store.episode_wrapup(episode.episode_id)

    if report_started:
        # A newer report owns the stage now; the old turn must not overwrite it.
        with pytest.raises(ValueError, match="no undelivered"):
            tasks.collect(root.operation_id)
        assert launched == ["fresh"]
    else:
        child = tasks.collect(root.operation_id)
        child = wait_for_task(store, child.operation_id, expect="succeeded")
        assert launched == ["fresh", "collect"]
        assert child.authorized_by == source.authorized_by
        assert child.native_session_id == source.native_session_id
        assert child.stage_root == source.stage_root
    assert store.episode_budget_meter(episode.episode_id) == meter
    assert store.episode(episode.episode_id).ending == ended.ending
    assert store.episode(episode.episode_id).wrapup_state == ended.wrapup_state
    assert store.episode_wrapup(episode.episode_id) == wrapup
    with pytest.raises(ValueError, match="no undelivered"):
        tasks.collect(root.operation_id)
