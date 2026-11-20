from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.core.materialize import MaterializationResult
from rcp.core.models import (
    AuthorizedHuman,
    Blocker,
    GatedCard,
    GraphState,
    Hypothesis,
    Patch,
    Proposal,
)
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.runs.shared import _record_patch_applied_receipt
from rcp.runs.watcher_admission import start_plain_watcher_wake
from rcp.service import RunRequest
from rcp.storage import (
    ACTIVE_AGENT_TASK_STATUSES,
    AgentTaskRecord,
    AppStore,
    NodeStatusGraphCondition,
    ProposalResolvedGraphCondition,
    WatcherCheckRecord,
    WatcherRecord,
)
from rcp.transport import StateUnavailable
from rcp.watchers import (
    AgentWatchSpec,
    WatcherBinding,
    WatcherCheckResult,
    WatcherInitialCheckError,
    WatcherPoller,
    WatcherRetryGeneration,
    WatcherRetryWorker,
    WatchSpec,
    arm_watchers,
    graph_check_boundary_result,
    graph_condition_result,
)
from tests.helpers import signed_in_client

from .helpers import append_fixture_patch, async_wait_until, wait_until
from .helpers import create_named_app as create_app
from .test_branch_target_storage import _store as _project_store

_CREATED_AT = "2026-08-12T00:00:00+00:00"
_RESOLVED = NodeStatusGraphCondition(node_id="blk/foo", status_in=["resolved"])
_PROPOSAL = ProposalResolvedGraphCondition(node_id="hyp/foo", proposal_resolved=True)
_SPEC = AgentWatchSpec(
    purpose="job finishes", check_command="still-running", log_path="/tmp/run.log", cwd="/tmp"
)


def _blocker(status: str = "open") -> Blocker:
    return Blocker(
        id="blk/foo",
        type="blocker",
        title="Waiting on a canonical result",
        description="The result is not ready yet.",
        status=status,
    )


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="hyp/foo",
        type="hypothesis",
        title="The intervention works",
        statement="The intervention improves the measured outcome.",
    )


def _proposal(status: str = "pending") -> Proposal:
    return Proposal(
        id="prop/foo",
        title="Resolve the hypothesis",
        card=GatedCard(),
        ops=[],
        related_node_ids=["hyp/foo"],
        base_rev=1,
        status=status,
        resolved_rev=2 if status != "pending" else None,
    )


def _state(
    *,
    blocker_status: str = "open",
    proposal_status: str = "pending",
    revision: int = 1,
    replay_status: str = "complete",
    include_blocker: bool = True,
) -> GraphState:
    nodes = {}
    if include_blocker:
        blocker = _blocker(blocker_status)
        nodes[blocker.id] = blocker
    hypothesis = _hypothesis()
    nodes[hypothesis.id] = hypothesis
    return GraphState(
        revision=revision,
        nodes=nodes,
        proposals={"prop/foo": _proposal(proposal_status)},
        replay_status=replay_status,
    )


def _canonical_fixture_patch(*, blocker_status: str) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Created the graph-condition watcher fixture.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "blk/foo",
                        "type": "blocker",
                        "title": "Waiting on a canonical result",
                        "description": "The result is not ready yet.",
                        "status": blocker_status,
                    },
                ],
            }
        ],
    )


def _blocker_status_patch(status: str) -> Patch:
    return Patch(
        kind="work",
        author="agent",
        summary=f"Set the blocker status to {status}.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "update_nodes",
                "nodes": [{"id": "blk/foo", "changes": {"status": status}}],
            }
        ],
    )


def _test_authorizer(store: AppStore) -> AuthorizedHuman:
    owner = store.local_owner
    assert owner is not None
    return AuthorizedHuman(
        space_id=store.space_id,
        user_id=owner.user_id,
        display_name=owner.display_name or "Test researcher",
    )


def _wait_for_terminal_task(store: AppStore, operation_id: str) -> AgentTaskRecord:
    def settled() -> AgentTaskRecord | None:
        task = store.agent_task(operation_id)
        assert task is not None
        if task.status not in ACTIVE_AGENT_TASK_STATUSES:
            return task
        return None

    return wait_until(settled, detail=f"watcher wake {operation_id} did not settle")


@pytest.fixture
def store(tmp_path) -> AppStore:
    return AppStore(tmp_path / "rcp.sqlite3")


def _binding(origin: str = "origin", agent_id: str = "chat") -> WatcherBinding:
    return WatcherBinding(
        project_id="project",
        agent_id=agent_id,
        origin_turn_id=origin,
        machine_id=BACKEND_MACHINE_ID,
    )


def _active_check(_spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
    return WatcherCheckResult(state="active", checked_at=_CREATED_AT, exit_code=1)


def _graph_check(
    check_id: str,
    condition: NodeStatusGraphCondition | ProposalResolvedGraphCondition = _RESOLVED,
    *,
    armed_head: int = 1,
    completed: bool = False,
    graph_target: GraphTargetRef | None = None,
) -> WatcherCheckRecord:
    return WatcherCheckRecord(
        check_id=check_id,
        watcher_id=check_id,
        machine_id=BACKEND_MACHINE_ID,
        definition="graph",
        condition=condition,
        graph_target=graph_target or GraphTargetRef(),
        armed_head=armed_head,
        created_at=_CREATED_AT,
        completed_at=_CREATED_AT if completed else None,
    )


def _external_check(check_id: str, *, completed: bool = False) -> WatcherCheckRecord:
    return WatcherCheckRecord(
        check_id=check_id,
        watcher_id=check_id,
        machine_id=BACKEND_MACHINE_ID,
        definition="external",
        check_command=f"test -f /tmp/{check_id}.done",
        log_path=f"/tmp/{check_id}.log",
        cwd="/tmp",
        created_at=_CREATED_AT,
        completed_at=_CREATED_AT if completed else None,
    )


def _arm(
    store: AppStore,
    watcher_id: str,
    *checks: WatcherCheckRecord,
    project_id: str = "project",
    origin: str = "origin",
) -> WatcherRecord:
    return store.arm_watcher(
        WatcherRecord(
            watcher_id=watcher_id,
            agent_id="chat",
            project_id=project_id,
            purpose="Wait for the result",
            origin_turn_id=origin,
            created_at=_CREATED_AT,
            checks=list(checks) or [_graph_check(watcher_id)],
        )
    )


def _check(store: AppStore, watcher_id: str) -> WatcherCheckRecord:
    watcher = store.watcher(watcher_id)
    assert watcher is not None
    return watcher.checks[0]


def _chat_turn(store: AppStore, operation_id: str, *, project_id: str = "project"):
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=project_id,
        kind="node_chat",
        status="queued",
        request={
            "chat_id": "chat",
            "node_id": "exp/one",
            "provider": "codex",
            "model": "gpt-5",
            "reasoning": "medium",
            "run_on": BACKEND_MACHINE_ID,
            "run_truth_scope": ["repo-a"],
            "mode": "work",
            "trigger": "human",
            "patch_kind": "work",
            "workflow_ids": [],
            "skill_ids": [],
            "invoked_workflow_ids": [],
            "invoked_skill_ids": [],
            "resolved_skill_packages": [],
            "watcher_ids": [],
        },
        created_at=now,
        updated_at=now,
        status_message="Queued chat turn.",
        authorized_by=_test_authorizer(store),
    )


def _completed_origin(store: AppStore, project_id: str, operation_id: str) -> AgentTaskRecord:
    origin = store.create_agent_task(_chat_turn(store, operation_id, project_id=project_id))
    store.complete_agent_task(operation_id, applied_revision=None, result={})
    return origin


def _watcher_app(manifest, tmp_path):
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    return app, app.state.service, app.state.background_tasks.store, project_id


def _record_deliveries(monkeypatch, app) -> list[list[str]]:
    """Record each delivery batch without waking anyone."""

    delivered: list[list[str]] = []
    monkeypatch.setattr(
        app.state.services.watcher_delivery,
        "deliver_ready",
        lambda _agent_id, ready, **_kwargs: delivered.append([w.watcher_id for w in ready]),
    )
    return delivered


def _claiming_wakes(monkeypatch, store: AppStore) -> list[list[str]]:
    """Replace the plain wake with a bare durable claim through its fence."""

    claimed: list[list[str]] = []

    def claim_wake(_tasks, agent_id, ready, _authorized_by, fence=None):
        ids = [watcher.watcher_id for watcher in ready]

        def claim() -> None:
            with store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                assert store.claim_watcher_wake(connection, agent_id, ids, str(uuid.uuid4()))
            claimed.append(ids)

        if fence is None:
            claim()
        else:
            fence(claim)

    monkeypatch.setattr("rcp.api.app.start_plain_watcher_wake", claim_wake)
    return claimed


def _settle(app, project_id: str, operation_id: str, **execution) -> None:
    callback = app.state.background_tasks.on_task_settled
    assert callback is not None
    callback(
        project_id,
        "node_chat",
        RunRequest(chat_scope="node", chat_id="chat", node_id="exp/one", mode="work"),
        AgentTaskExecution(
            operation_id=operation_id,
            store=app.state.background_tasks.store,
            control=AgentProcessControl(),
            **execution,
        ),
    )


async def _done_wake(_project_id, _kind, _request, _execution):
    yield f"data: {AgentEvent(event='answer', text='Observed.').model_dump_json()}\n\n"
    yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"


def _current() -> WatcherRetryGeneration:
    return WatcherRetryGeneration(lambda: True, lambda callback: (callback(), True)[1])


def test_watch_validation_is_all_or_none_across_external_and_graph(store) -> None:
    invalid = NodeStatusGraphCondition(node_id="blk/foo", status_in=["completed"])
    checked: list[str] = []

    def active_check(candidate: WatchSpec, host: str, timeout: float) -> WatcherCheckResult:
        checked.append(candidate.check_command)
        return _active_check(candidate, host, timeout)

    with pytest.raises(ValueError, match="invalid statuses"):
        arm_watchers(
            store,
            [_SPEC],
            _binding(),
            graph_conditions=[invalid],
            state=_state(),
            check_runner=active_check,
        )
    assert checked == []
    assert store.project_watchers("project") == []

    def failed_check(_candidate: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        return WatcherCheckResult(
            state="error", checked_at=_CREATED_AT, exit_code=2, error="scheduler unavailable"
        )

    with pytest.raises(WatcherInitialCheckError):
        arm_watchers(
            store,
            [_SPEC],
            _binding(),
            graph_conditions=[_RESOLVED],
            state=_state(),
            check_runner=failed_check,
        )
    assert store.project_watchers("project") == []

    armed = arm_watchers(
        store,
        [_SPEC],
        _binding(),
        graph_conditions=[_RESOLVED],
        state=_state(revision=4),
        watcher_ids=["external-id", "graph-id"],
        check_runner=active_check,
    )
    assert [watcher.watcher_id for watcher in armed] == ["external-id", "graph-id"]
    assert [watcher.checks[0].definition for watcher in armed] == ["external", "graph"]
    assert armed[1].checks[0].armed_head == 4

    def unexpected_check(*_args):
        raise AssertionError("an already-persisted handoff must not run its check again")

    replayed = arm_watchers(
        store,
        [_SPEC],
        _binding(),
        graph_conditions=[_RESOLVED],
        state=_state(revision=4),
        watcher_ids=["external-id", "graph-id"],
        check_runner=unexpected_check,
    )
    assert replayed == armed
    assert len(store.project_watchers("project")) == 2


@pytest.mark.parametrize(
    "watcher_ids",
    [[], ["only-one"], ["same", "same"], ["external", "  "]],
)
def test_invalid_supplied_watcher_ids_fail_before_checks_or_insertion(
    store,
    watcher_ids: list[str],
) -> None:
    def unexpected_check(*_args):
        raise AssertionError("invalid ids must fail before any check runs")

    with pytest.raises(ValueError, match="watcher_ids"):
        arm_watchers(
            store,
            [_SPEC],
            _binding(),
            graph_conditions=[_RESOLVED],
            state=_state(),
            watcher_ids=watcher_ids,
            check_runner=unexpected_check,
        )
    assert store.project_watchers("project") == []


def test_a_later_turn_rearming_the_same_conditions_reuses_the_agents_open_watchers(
    store,
) -> None:
    def arm(origin: str, agent_id: str = "chat", *, reuse_live: bool = True) -> list[str]:
        armed = arm_watchers(
            store,
            [_SPEC],
            _binding(origin, agent_id),
            graph_conditions=[_RESOLVED],
            state=_state(),
            watcher_ids=[f"{origin}-{agent_id}-0", f"{origin}-{agent_id}-1"],
            reuse_live=reuse_live,
            check_runner=_active_check,
        )
        return [watcher.watcher_id for watcher in armed]

    first = arm("turn-1")
    assert arm("turn-2") == first
    assert arm("turn-2") == first
    assert len(store.project_watchers("project")) == 2

    keyed = arm("command-1", reuse_live=False)
    assert keyed == ["command-1-chat-0", "command-1-chat-1"]
    store.cancel_watchers(keyed, by="agent")

    other_agent = arm("turn-3", agent_id="other")
    assert set(other_agent).isdisjoint(first)
    assert len(store.project_watchers("project")) == 6

    store.cancel_watchers(first, by="agent")
    rearmed = arm("turn-4")
    assert set(rearmed).isdisjoint(first)
    assert len(store.agent_watchers("chat")) == 6


def test_initially_satisfied_graph_condition_is_immediately_ready(store) -> None:
    armed = arm_watchers(
        store,
        [],
        _binding(),
        graph_conditions=[_RESOLVED],
        state=_state(blocker_status="resolved"),
    )

    assert len(armed) == 1
    assert armed[0].checks[0].completed_at is not None
    assert [watcher.watcher_id for watcher in store.ready_watchers()] == [armed[0].watcher_id]


def test_status_and_proposal_conditions_use_the_closed_vocabulary() -> None:
    assert graph_condition_result(_RESOLVED, _state(), armed_revision=1) == "active"
    assert (
        graph_condition_result(_RESOLVED, _state(blocker_status="resolved"), armed_revision=1)
        == "completed"
    )
    assert graph_condition_result(_PROPOSAL, _state(), armed_revision=1) == "active"
    assert (
        graph_condition_result(
            _PROPOSAL, _state(proposal_status="approved", revision=2), armed_revision=1
        )
        == "completed"
    )


def test_degraded_replay_never_fires_or_removes_a_graph_check() -> None:
    degraded = _state(
        blocker_status="resolved", proposal_status="approved", replay_status="degraded"
    )
    missing = NodeStatusGraphCondition(node_id="blk/missing", status_in=["resolved"])

    for condition in (_RESOLVED, _PROPOSAL, missing):
        check = _graph_check("check", condition, armed_head=0)
        assert graph_check_boundary_result(check, degraded, ()) == "active"


def test_proposal_resolution_must_happen_after_the_watcher_was_armed(store) -> None:
    _arm(store, "proposal-after-arm", _graph_check("proposal-after-arm", _PROPOSAL, armed_head=2))
    old_resolved = _proposal("approved").model_copy(
        update={"id": "prop/old", "base_rev": 0, "resolved_rev": 1}
    )
    new_pending = _proposal().model_copy(update={"id": "prop/new", "base_rev": 2})
    unrelated = _state(revision=3).model_copy(
        update={"proposals": {old_resolved.id: old_resolved, new_pending.id: new_pending}}
    )
    newly_resolved = new_pending.model_copy(update={"status": "approved", "resolved_rev": 4})
    resolved = unrelated.model_copy(
        update={
            "revision": 4,
            "proposals": {old_resolved.id: old_resolved, newly_resolved.id: newly_resolved},
        }
    )

    def consume(state: GraphState) -> None:
        store.consume_graph_watcher_boundary(
            "project",
            GraphHeadRef(revision=state.revision),
            evaluate=lambda check: graph_check_boundary_result(check, state, ()),
        )

    consume(unrelated)
    check = _check(store, "proposal-after-arm")
    assert check.completed_at is None and check.last_checked_at is not None
    assert store.ready_watchers() == []

    consume(resolved)
    assert _check(store, "proposal-after-arm").completed_at is not None
    assert [watcher.watcher_id for watcher in store.ready_watchers()] == ["proposal-after-arm"]


def test_human_sync_boundary_wakes_the_arming_agent(manifest, tmp_path) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    append_fixture_patch(service, _canonical_fixture_patch(blocker_status="open"))
    origin = _completed_origin(store, project_id, "sync-origin")
    _arm(
        store,
        "sync-graph",
        _graph_check("sync-graph", armed_head=service.history.state().revision),
        project_id=project_id,
        origin=origin.operation_id,
    )
    app.state.background_tasks.stream = _done_wake
    blocker = service.history.state().nodes["blk/foo"]
    client = signed_in_client(app)
    try:
        response = client.post(
            f"/api/projects/{project_id}/sync",
            json={
                "base_revision": service.history.state().revision,
                "nodes": [
                    {
                        "node_id": blocker.id,
                        "base_updated_rev": blocker.updated_rev,
                        "changes": {"status": "resolved"},
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        stored = wait_until(
            lambda: (watcher := store.watcher("sync-graph")) and watcher.wake_turn_id and watcher,
            detail="sync boundary did not claim a wake",
        )
        wake = _wait_for_terminal_task(store, stored.wake_turn_id)
        assert wake.status == "succeeded"
        assert wake.request["trigger"] == "watcher"
        assert wake.request["watcher_ids"] == ["sync-graph"]
    finally:
        client.close()
        app.state.background_tasks.shutdown()


def test_agent_settlement_evaluates_the_exact_applied_revision_boundary(
    manifest, tmp_path, monkeypatch
) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    append_fixture_patch(service, _canonical_fixture_patch(blocker_status="open"))
    origin = _completed_origin(store, project_id, "agent-boundary-origin")
    _arm(
        store,
        "agent-boundary",
        _graph_check("agent-boundary", armed_head=service.history.state().revision),
        project_id=project_id,
        origin=origin.operation_id,
    )
    applied, applied_result = append_fixture_patch(service, _blocker_status_patch("resolved"))
    append_fixture_patch(service, _blocker_status_patch("open"))
    claimed = _claiming_wakes(monkeypatch, store)

    _settle(
        app,
        project_id,
        origin.operation_id,
        applied_revision=applied.revision,
        applied_graph_state=applied_result.state,
    )

    assert claimed == [["agent-boundary"]]
    assert _check(store, "agent-boundary").completed_at is not None
    app.state.background_tasks.shutdown()


@pytest.mark.parametrize(
    ("historical", "armed"),
    [
        (_state(blocker_status="resolved", revision=2), _state(revision=3)),
        (_state(include_blocker=False, revision=1), _state(revision=2)),
    ],
    ids=["past-satisfaction", "before-target-creation"],
)
def test_historical_callback_cannot_change_a_newer_watcher(
    manifest, tmp_path, monkeypatch, historical, armed
) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    _arm(
        store,
        "newer-watcher",
        _graph_check("newer-watcher", armed_head=armed.revision),
        project_id=project_id,
    )
    monkeypatch.setattr(
        service.history,
        "accepted_boundary_states",
        lambda: (MaterializationResult(state=armed), [historical, armed]),
    )

    _settle(
        app,
        project_id,
        "historical-callback",
        applied_revision=historical.revision,
        applied_graph_state=historical,
    )

    stored = store.watcher("newer-watcher")
    assert stored is not None and stored.open
    assert stored.checks[0].completed_at is None
    app.state.background_tasks.shutdown()


def test_no_patch_arming_settlement_catches_revision_between_validation_and_insert(
    manifest, tmp_path, monkeypatch
) -> None:
    """Validation saw R1 and R2 committed before the durable insert."""

    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    _initial, initial_result = append_fixture_patch(
        service, _canonical_fixture_patch(blocker_status="open")
    )
    append_fixture_patch(service, _blocker_status_patch("resolved"))
    _arm(
        store,
        "insert-race",
        _graph_check("insert-race", armed_head=initial_result.state.revision),
        project_id=project_id,
    )
    delivered = _record_deliveries(monkeypatch, app)

    _settle(app, project_id, "arming-without-patch", armed_graph_watchers=True)

    assert _check(store, "insert-race").completed_at is not None
    assert delivered == [["insert-race"]]
    app.state.background_tasks.shutdown()


@pytest.mark.parametrize(
    ("boundaries", "completes"),
    [
        (
            [
                _state(blocker_status="resolved", revision=1),
                _state(include_blocker=False, revision=2),
            ],
            True,
        ),
        (
            [
                _state(include_blocker=False, revision=1),
                _state(blocker_status="resolved", revision=2),
            ],
            False,
        ),
    ],
    ids=["target-then-removal", "removal-then-recreation"],
)
def test_reversed_task_settlement_uses_canonical_boundary_order(
    manifest, tmp_path, monkeypatch, boundaries, completes
) -> None:
    """The later task settles first; reconciliation still visits R1 before R2."""

    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    _arm(
        store,
        "canonical-order",
        _graph_check("canonical-order", armed_head=0),
        project_id=project_id,
    )
    monkeypatch.setattr(
        service.history,
        "accepted_boundary_states",
        lambda: (MaterializationResult(state=boundaries[-1]), boundaries),
    )
    _record_deliveries(monkeypatch, app)

    _settle(
        app,
        project_id,
        "settled-r2-first",
        applied_revision=2,
        applied_graph_state=boundaries[-1],
    )

    stored = store.watcher("canonical-order")
    assert stored is not None
    assert (stored.checks[0].completed_at is not None) is completes
    assert stored.cancelled_by == (None if completes else "rcp")
    app.state.background_tasks.shutdown()


def test_repeated_task_settlement_skips_transition_traces_before_durable_cursor(
    manifest, tmp_path, monkeypatch
) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    _arm(
        store, "durable-cursor", _graph_check("durable-cursor", armed_head=0), project_id=project_id
    )
    boundaries = [_state(revision=revision) for revision in (1, 2, 3)]
    monkeypatch.setattr(
        service.history,
        "accepted_boundary_states",
        lambda: (MaterializationResult(state=boundaries[-1]), boundaries),
    )
    traced_revisions: list[int] = []

    def observe_trace(revision: int):
        traced_revisions.append(revision)
        return None

    monkeypatch.setattr(service.history, "transition_trace_at_revision", observe_trace)

    for _ in range(2):
        _settle(
            app,
            project_id,
            "durable-cursor-signal",
            applied_revision=3,
            applied_graph_state=boundaries[-1],
        )

    assert traced_revisions == [1, 2, 3, 3]
    consumed = store.graph_watcher_reconciliation_head(project_id, GraphTargetRef())
    assert consumed is not None and consumed.revision == 3
    assert consumed.transition_id is None
    app.state.background_tasks.shutdown()


def test_ready_only_graph_project_skips_canonical_boundary_replay(
    manifest, tmp_path, monkeypatch
) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    _arm(
        store,
        "ready-fast-path",
        _graph_check("ready-fast-path", completed=True),
        project_id=project_id,
    )

    def unexpected_replay():
        raise AssertionError("ready-only delivery must not replay canonical history")

    monkeypatch.setattr(service.history, "accepted_boundary_states", unexpected_replay)
    delivered = _record_deliveries(monkeypatch, app)

    _settle(app, project_id, "ready-fast-path", applied_revision=1)

    assert delivered == [["ready-fast-path"]]
    app.state.background_tasks.shutdown()


def test_degraded_final_replay_applies_no_satisfying_prefix_transition(
    manifest, tmp_path, monkeypatch
) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    satisfying_prefix = _state(blocker_status="resolved", revision=2)
    degraded_head = _state(blocker_status="resolved", revision=3, replay_status="degraded")
    _arm(store, "degraded-prefix", project_id=project_id)
    monkeypatch.setattr(
        service.history,
        "accepted_boundary_states",
        lambda: (MaterializationResult(state=degraded_head), [satisfying_prefix]),
    )

    _settle(
        app,
        project_id,
        "degraded-r3",
        applied_revision=degraded_head.revision,
        applied_graph_state=degraded_head,
    )

    check = _check(store, "degraded-prefix")
    assert check.completed_at is None and check.last_checked_at is None
    assert store.graph_watcher_reconciliation_head(project_id, GraphTargetRef()) is None
    app.state.background_tasks.shutdown()


@pytest.mark.parametrize("refresh_failure", ["exception", "false-return"])
def test_remote_refresh_failure_never_evaluates_the_stale_graph_mirror(
    manifest, tmp_path, monkeypatch, refresh_failure
) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    append_fixture_patch(service, _canonical_fixture_patch(blocker_status="open"))
    armed_revision = service.history.state().revision
    append_fixture_patch(service, _blocker_status_patch("resolved"))
    _arm(
        store,
        "stale-remote",
        _graph_check("stale-remote", armed_head=armed_revision),
        project_id=project_id,
    )

    def unavailable() -> bool:
        if refresh_failure == "exception":
            raise StateUnavailable("canonical remote is unavailable")
        return False

    monkeypatch.setattr(service.history.workspace, "refresh_if_stale", unavailable)

    _settle(app, project_id, "stale-remote-trigger", applied_revision=armed_revision + 1)

    check = _check(store, "stale-remote")
    assert check.completed_at is None and check.last_checked_at is None
    app.state.background_tasks.shutdown()


@pytest.mark.parametrize("failure", ["degraded", "unavailable", "invalid"])
def test_startup_reconciles_healthy_target_after_another_target_fails(
    manifest, tmp_path, monkeypatch, failure
) -> None:
    """A successful main-only callback must not erase the branch retry."""

    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    delivery = app.state.services.watcher_delivery
    _, initial = append_fixture_patch(service, _canonical_fixture_patch(blocker_status="open"))
    append_fixture_patch(service, _blocker_status_patch("resolved"))
    branch = GraphTargetRef(kind="branch", branch_id="branch-owner")
    for watcher_id, target in (("a-branch", branch), ("z-main", GraphTargetRef())):
        _arm(
            store,
            watcher_id,
            _graph_check(watcher_id, armed_head=initial.state.revision, graph_target=target),
            project_id=project_id,
        )
    failed = True

    def branch_boundaries():
        if failed:
            if failure == "unavailable":
                raise StateUnavailable("temporary failure")
            if failure == "invalid":
                raise ValueError("invalid branch metadata")
            return SimpleNamespace(
                state=initial.state.model_copy(update={"replay_status": "degraded"})
            ), []
        resolved = initial.state.model_copy(
            update={
                "revision": initial.state.revision + 1,
                "nodes": {**initial.state.nodes, "blk/foo": _blocker("resolved")},
            }
        )
        return SimpleNamespace(state=resolved), [resolved]

    branch_service = SimpleNamespace(
        history=SimpleNamespace(
            accepted_boundary_states=branch_boundaries,
            transition_trace_at_revision=lambda _revision: None,
        )
    )
    monkeypatch.setattr(
        delivery,
        "_graph_project_service",
        lambda _pid, target: branch_service if target == branch else service,
    )
    delivered = _record_deliveries(monkeypatch, app)
    try:
        delivery.request_startup_sweep()
        delivery.retry_graph_wakes_after_poll(_current())
        assert _check(store, "z-main").completed_at is not None
        assert _check(store, "a-branch").completed_at is None
        assert delivered == [["z-main"]]
        assert store.graph_watcher_reconciliation_head(project_id, branch) is None
        if failure == "unavailable":
            delivery.evaluate_graph_wake_boundary(
                project_id, None, graph_target=GraphTargetRef(), source="main callback"
            )
            failed = False
            delivery.retry_graph_wakes_after_poll(_current())
            assert _check(store, "a-branch").completed_at is not None
    finally:
        app.state.background_tasks.shutdown()


@pytest.mark.parametrize(
    "transient_failure",
    [
        StateUnavailable("canonical remote is temporarily unavailable"),
        sqlite3.OperationalError("database is locked"),
    ],
)
def test_transient_reconciliation_failure_retries_without_a_new_revision(
    manifest, tmp_path, monkeypatch, transient_failure
) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    append_fixture_patch(service, _canonical_fixture_patch(blocker_status="open"))
    armed_revision = service.history.state().revision
    append_fixture_patch(service, _blocker_status_patch("resolved"))
    origin = _completed_origin(store, project_id, "retry-origin")
    _arm(
        store,
        "reconcile-retry",
        _graph_check("reconcile-retry", armed_head=armed_revision),
        project_id=project_id,
        origin=origin.operation_id,
    )
    original_replay = service.history.accepted_boundary_states
    replay_calls = 0

    def unavailable_once():
        nonlocal replay_calls
        replay_calls += 1
        if replay_calls == 1:
            raise transient_failure
        return original_replay()

    monkeypatch.setattr(service.history, "accepted_boundary_states", unavailable_once)
    claimed = _claiming_wakes(monkeypatch, store)
    revision = service.history.state().revision

    _settle(app, project_id, "failed-reconciliation", applied_revision=revision)
    assert _check(store, "reconcile-retry").completed_at is None

    app.state.graph_watcher_retry_worker.start()
    try:
        app.state.graph_watcher_retry_worker.signal()
        wait_until(lambda: bool(claimed), detail="reconciliation retry did not wake")
        assert replay_calls == 2
        assert claimed == [["reconcile-retry"]]
        assert service.history.state().revision == revision

        app.state.graph_watcher_retry_worker.signal()
        time.sleep(0.05)
        assert claimed == [["reconcile-retry"]]
    finally:
        app.state.graph_watcher_retry_worker.stop()
        app.state.background_tasks.shutdown()


def test_completed_reconciliation_cannot_clear_a_later_transient_failure(
    manifest, tmp_path, monkeypatch
) -> None:
    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    _, initial = append_fixture_patch(service, _canonical_fixture_patch(blocker_status="open"))
    delivery = app.state.services.watcher_delivery
    _arm(
        store,
        "overlapping-reconciliation",
        _graph_check("overlapping-reconciliation", armed_head=initial.state.revision),
        project_id=project_id,
    )
    delivered = _record_deliveries(monkeypatch, app)
    project_lock = delivery._retry.lock_for(project_id)
    first_released_lock = threading.Event()
    release_first_pass = threading.Event()

    @contextmanager
    def controlled_lock(_project_id):
        with project_lock:
            yield
        if threading.current_thread() is first_pass:
            first_released_lock.set()
            assert release_first_pass.wait(5)

    monkeypatch.setattr(delivery._retry, "lock_for", controlled_lock)
    first_pass = threading.Thread(
        target=lambda: delivery.evaluate_graph_wake_boundary(
            project_id, None, source="reconciliation retry"
        ),
        name="successful-all-target-reconciliation",
    )
    original_replay = service.history.accepted_boundary_states
    failed = False

    def unavailable_once():
        nonlocal failed
        if not failed:
            failed = True
            raise StateUnavailable("canonical remote is temporarily unavailable")
        return original_replay()

    try:
        first_pass.start()
        assert first_released_lock.wait(5)
        append_fixture_patch(service, _blocker_status_patch("resolved"))
        monkeypatch.setattr(service.history, "accepted_boundary_states", unavailable_once)
        delivery.evaluate_graph_wake_boundary(
            project_id, None, graph_target=GraphTargetRef(), source="task settlement"
        )
        release_first_pass.set()
        first_pass.join(timeout=5)
        assert not first_pass.is_alive()
        assert _check(store, "overlapping-reconciliation").completed_at is None

        delivery.retry_graph_wakes_after_poll(_current())

        assert _check(store, "overlapping-reconciliation").completed_at is not None
        assert delivered == [["overlapping-reconciliation"]]
    finally:
        release_first_pass.set()
        first_pass.join(timeout=5)
        app.state.background_tasks.shutdown()


def test_due_reconciliation_survives_retry_worker_stop_then_start(
    manifest, tmp_path, monkeypatch
) -> None:
    """The stale generation selects the due project; the replacement must still process it."""

    app, service, store, project_id = _watcher_app(manifest, tmp_path)
    append_fixture_patch(service, _canonical_fixture_patch(blocker_status="open"))
    armed_revision = service.history.state().revision
    append_fixture_patch(service, _blocker_status_patch("resolved"))
    origin = _completed_origin(store, project_id, "generation-retry-origin")
    _arm(
        store,
        "generation-reconcile-retry",
        _graph_check("generation-reconcile-retry", armed_head=armed_revision),
        project_id=project_id,
        origin=origin.operation_id,
    )
    original_replay = service.history.accepted_boundary_states
    replay_calls = 0

    def unavailable_once():
        nonlocal replay_calls
        replay_calls += 1
        if replay_calls == 1:
            raise StateUnavailable("canonical remote is temporarily unavailable")
        return original_replay()

    monkeypatch.setattr(service.history, "accepted_boundary_states", unavailable_once)
    claimed = _claiming_wakes(monkeypatch, store)
    _settle(
        app,
        project_id,
        "schedule-generation-retry",
        applied_revision=service.history.state().revision,
    )
    assert replay_calls == 1

    worker = app.state.graph_watcher_retry_worker
    retry_callback = worker.callback
    first_started = threading.Event()
    replacement_started = threading.Event()
    release_first = threading.Event()
    first_finished = threading.Event()
    call_count = 0
    call_count_lock = threading.Lock()

    def controlled_retry(generation) -> None:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
            call = call_count
        if call == 1:
            first_started.set()
            release_first.wait()
            retry_callback(generation)
            first_finished.set()
            return
        replacement_started.set()
        first_finished.wait()
        retry_callback(generation)

    worker.callback = controlled_retry
    worker.start()
    try:
        worker.signal()
        assert first_started.wait(1), "first retry generation did not start"
        worker.stop(timeout=0.01)
        worker.start()
        worker.signal()
        assert replacement_started.wait(1), "replacement retry generation did not start"

        release_first.set()
        wait_until(lambda: bool(claimed), detail="replacement generation lost due retry")
        assert replay_calls == 2
        assert claimed == [["generation-reconcile-retry"]]
    finally:
        release_first.set()
        worker.stop()
        app.state.background_tasks.shutdown()


def _restarted_app_with_condition_met_while_down(manifest, tmp_path, monkeypatch):
    data_dir = tmp_path / "startup-data"
    first = create_app(str(manifest.path), data_dir=data_dir)
    service = first.state.service
    append_fixture_patch(service, _canonical_fixture_patch(blocker_status="open"))
    project_id = first.state.default_project_id
    assert project_id is not None
    first_store = first.state.background_tasks.store
    origin = _completed_origin(first_store, project_id, "startup-origin")
    _arm(
        first_store,
        "startup-hook",
        _graph_check("startup-hook", armed_head=service.history.state().revision),
        project_id=project_id,
        origin=origin.operation_id,
    )
    first.state.background_tasks.shutdown()
    append_fixture_patch(service, _blocker_status_patch("resolved"))

    reopened = create_app(str(manifest.path), data_dir=data_dir)
    claimed = _claiming_wakes(monkeypatch, reopened.state.background_tasks.store)
    return reopened, project_id, origin, claimed


def test_app_lifespan_evaluates_conditions_satisfied_before_restart(
    manifest, tmp_path, monkeypatch
) -> None:
    reopened, project_id, origin, claimed = _restarted_app_with_condition_met_while_down(
        manifest, tmp_path, monkeypatch
    )
    store = reopened.state.background_tasks.store
    _arm(
        store,
        "startup-retry",
        _graph_check("startup-retry", completed=True),
        project_id=project_id,
        origin=origin.operation_id,
    )

    with signed_in_client(reopened) as client:
        assert client.get("/api/health").status_code == 200
        wait_until(
            lambda: len({item for batch in claimed for item in batch}) == 2 or None,
            detail="startup graph watcher delivery did not run",
        )

    assert sorted(item for batch in claimed for item in batch) == ["startup-hook", "startup-retry"]
    assert _check(store, "startup-hook").completed_at is not None


def test_app_startup_serves_while_the_graph_sweep_read_is_blocked(
    manifest, tmp_path, monkeypatch
) -> None:
    reopened, _project_id, _origin, claimed = _restarted_app_with_condition_met_while_down(
        manifest, tmp_path, monkeypatch
    )
    delivery = reopened.state.services.watcher_delivery
    open_service = delivery._graph_project_service
    entered = threading.Event()
    release = threading.Event()
    serving = threading.Event()

    def slow_service(identifier, target):
        entered.set()
        assert release.wait(10), "graph read was not released"
        return open_service(identifier, target)

    monkeypatch.setattr(delivery, "_graph_project_service", slow_service)

    def serve():
        with signed_in_client(reopened) as client:
            health = client.get("/api/health")
            serving.set()
            assert release.wait(10), "test did not finish checking startup"
            wait_until(lambda: claimed or None, detail="startup graph watcher delivery did not run")
            return health

    with ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(serve)
        try:
            assert entered.wait(5), "startup did not begin the graph sweep"
            assert serving.wait(5), "API startup waited for the graph sweep read"
        finally:
            release.set()
        health = request.result(timeout=15)

    assert health.status_code == 200
    assert claimed == [["startup-hook"]]


def test_discuss_settlement_retries_a_wake_blocked_by_same_chat_overlap(manifest, tmp_path) -> None:
    app, _service, store, project_id = _watcher_app(manifest, tmp_path)
    origin = _chat_turn(store, "overlap-origin", project_id=project_id)
    origin.request["browser_requested"] = True
    store.create_agent_task(origin)
    store.complete_agent_task(origin.operation_id, applied_revision=None, result={})
    store.set_chat_browser_requested(project_id, "chat", browser_requested=False)
    _arm(
        store,
        "overlap-graph",
        _graph_check("overlap-graph", completed=True),
        project_id=project_id,
        origin=origin.operation_id,
    )
    overlap = _chat_turn(store, "active-discuss", project_id=project_id)
    overlap.request["mode"] = "discuss"
    store.create_agent_task(overlap)
    store.mark_agent_task_running(overlap.operation_id)
    callback = app.state.background_tasks.on_task_settled
    assert callback is not None
    app.state.background_tasks.stream = _done_wake
    try:
        callback(
            project_id,
            "project_chat",
            RunRequest(chat_scope="project", chat_id="other-chat", mode="discuss"),
            AgentTaskExecution(
                operation_id="other-discuss", store=store, control=AgentProcessControl()
            ),
        )
        assert store.watcher("overlap-graph").wake_turn_id is None

        store.complete_agent_task(overlap.operation_id, applied_revision=None, result={})
        callback(
            project_id,
            "node_chat",
            RunRequest.model_validate(overlap.request),
            AgentTaskExecution(
                operation_id=overlap.operation_id, store=store, control=AgentProcessControl()
            ),
        )

        delivered = store.watcher("overlap-graph")
        assert delivered is not None and delivered.wake_turn_id is not None
        wake = _wait_for_terminal_task(store, delivered.wake_turn_id)
        assert wake.status == "succeeded"
        assert wake.request["mode"] == "work"
        assert wake.request["trigger"] == "watcher"
        assert wake.request["watcher_ids"] == ["overlap-graph"]
        assert wake.request["browser_requested"] is False
    finally:
        app.state.background_tasks.shutdown()


def test_graph_checks_never_enter_the_watcher_poller(store) -> None:
    _arm(store, "external-solo", _external_check("external-solo"))
    _arm(
        store,
        "mixed",
        _external_check("mixed-external"),
        _graph_check("mixed-graph"),
    )
    checked: list[str] = []

    def complete_external(spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        checked.append(spec.log_path)
        return WatcherCheckResult(state="complete", checked_at=_CREATED_AT, exit_code=0)

    WatcherPoller(
        store, check_runner=complete_external, clock=lambda: "2100-01-01T00:00:00+00:00"
    ).poll_once()

    assert sorted(checked) == ["/tmp/external-solo.log", "/tmp/mixed-external.log"]
    mixed = store.watcher("mixed")
    assert mixed is not None
    assert [check.completed_at is not None for check in mixed.checks] == [True, False]
    assert mixed.checks[1].last_checked_at is None
    assert [watcher.watcher_id for watcher in store.ready_watchers()] == ["external-solo"]


def test_periodic_poll_delivers_a_mixed_watcher_after_its_external_check_completes(
    manifest, tmp_path, monkeypatch
) -> None:
    app, _service, store, project_id = _watcher_app(manifest, tmp_path)
    origin = _completed_origin(store, project_id, "periodic-mixed-origin")
    _arm(
        store,
        "periodic-mixed",
        _external_check("periodic-external"),
        _graph_check("periodic-graph", completed=True),
        project_id=project_id,
        origin=origin.operation_id,
    )
    claimed = _claiming_wakes(monkeypatch, store)

    def complete_external(_spec, _host, _timeout):
        return WatcherCheckResult(state="complete", checked_at=_CREATED_AT, exit_code=0)

    app.state.watcher_poller.check_runner = complete_external
    app.state.watcher_poller.clock = lambda: "2100-01-01T00:00:00+00:00"
    try:
        app.state.watcher_poller.poll_once()
        assert claimed == [["periodic-mixed"]]
        app.state.watcher_poller.poll_once()
        assert claimed == [["periodic-mixed"]]
    finally:
        app.state.background_tasks.shutdown()


def test_periodic_poll_retries_a_graph_delivery_after_transient_failure(
    manifest, tmp_path, monkeypatch
) -> None:
    app, _service, store, project_id = _watcher_app(manifest, tmp_path)
    origin = _completed_origin(store, project_id, "periodic-retry-origin")
    _arm(
        store,
        "periodic-retry-graph",
        _graph_check("periodic-retry-graph", completed=True),
        project_id=project_id,
        origin=origin.operation_id,
    )
    claimed = _claiming_wakes(monkeypatch, store)
    from rcp.api import app as app_module

    claiming = app_module.start_plain_watcher_wake
    attempts = 0

    def transient_then_claim(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient delivery failure")
        return claiming(*args, **kwargs)

    monkeypatch.setattr(app_module, "start_plain_watcher_wake", transient_then_claim)
    try:
        app.state.watcher_poller.poll_once()
        assert attempts == 1 and claimed == []
        assert store.watcher("periodic-retry-graph").open

        app.state.watcher_poller.poll_once()
        app.state.watcher_poller.poll_once()
        assert attempts == 2
        assert claimed == [["periodic-retry-graph"]]
    finally:
        app.state.background_tasks.shutdown()


def test_retry_worker_keeps_the_watcher_poller_nonblocking(store) -> None:
    retry_started = threading.Event()
    release_retry = threading.Event()
    second_poll_finished = threading.Event()

    def blocked_retry(_generation) -> None:
        retry_started.set()
        release_retry.wait()

    worker = WatcherRetryWorker(blocked_retry)
    poller = WatcherPoller(store, on_poll_completed=worker.signal)
    worker.start()
    second_poll = threading.Thread(
        target=lambda: (poller.poll_once(), second_poll_finished.set()),
        name="second-watcher-poll",
    )
    try:
        poller.poll_once()
        assert retry_started.wait(1), "retry worker did not start"
        second_poll.start()
        assert second_poll_finished.wait(1), "retry callback blocked the poller thread"
    finally:
        release_retry.set()
        second_poll.join(timeout=1)
        worker.stop()


def test_retry_worker_lifecycle_stays_bounded_during_reconciliation(
    manifest, tmp_path, monkeypatch
) -> None:
    app, _service, store, project_id = _watcher_app(manifest, tmp_path)
    delivery = app.state.services.watcher_delivery
    _arm(
        store,
        "slow-reconciliation",
        _graph_check("slow-reconciliation", armed_head=0),
        project_id=project_id,
    )
    delivery._retry.schedule(project_id)
    original_reconcile = delivery._reconcile_graph_boundaries
    reconciliation_started = threading.Event()
    release_reconciliation = threading.Event()

    def blocked_reconciliation(*args, **kwargs):
        reconciliation_started.set()
        assert release_reconciliation.wait(timeout=3)
        return original_reconcile(*args, **kwargs)

    monkeypatch.setattr(delivery, "_reconcile_graph_boundaries", blocked_reconciliation)
    worker = app.state.graph_watcher_retry_worker
    worker.start()
    worker.signal()
    assert reconciliation_started.wait(1), "retry reconciliation did not start"

    signal_finished = threading.Event()
    stop_finished = threading.Event()
    signal_thread = threading.Thread(
        target=lambda: (worker.signal(), signal_finished.set()),
        name="signal-during-watcher-reconciliation",
    )
    stop_thread = threading.Thread(
        target=lambda: (worker.stop(timeout=0.01), stop_finished.set()),
        name="stop-during-watcher-reconciliation",
    )
    signal_thread.start()
    stop_thread.start()
    try:
        signal_was_bounded = signal_finished.wait(0.2)
        stop_was_bounded = stop_finished.wait(0.2)
    finally:
        release_reconciliation.set()
        signal_thread.join(timeout=1)
        stop_thread.join(timeout=1)
        worker.stop()
        app.state.background_tasks.shutdown()

    assert signal_was_bounded, "signal waited for graph reconciliation"
    assert stop_was_bounded, "stop(timeout=) waited for graph reconciliation"


def test_retry_worker_stop_then_start_invalidates_the_slow_prior_generation() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    first_finished = threading.Event()
    stale_delivery = threading.Event()
    replacement_delivery = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def retry(generation) -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            first_started.set()
            release_first.wait()
            generation.run_if_current(stale_delivery.set)
            first_finished.set()
            return
        generation.run_if_current(replacement_delivery.set)

    worker = WatcherRetryWorker(retry)
    worker.start()
    worker.signal()
    assert first_started.wait(1), "first retry generation did not start"

    worker.stop(timeout=0.01)
    worker.start()
    worker.signal()
    assert replacement_delivery.wait(1), "replacement retry generation did not run"

    release_first.set()
    assert first_finished.wait(1), "stale retry generation did not finish"
    assert not stale_delivery.is_set()
    worker.stop()


def _plain_wake_store(tmp_path, *watcher_ids: str) -> AppStore:
    store = _project_store(tmp_path)
    owner = store.local_owner
    assert owner is not None
    store.rename_space_user(owner.user_id, "Test researcher")
    _completed_origin(store, "project", "origin")
    for watcher_id in watcher_ids:
        _arm(store, watcher_id, _external_check(watcher_id, completed=True))
    return store


def _ready(store: AppStore, watcher_id: str) -> list[WatcherRecord]:
    watcher = store.watcher(watcher_id)
    assert watcher is not None and watcher.ready
    return [watcher]


def test_plain_wake_fence_owns_claim_and_launch(tmp_path, monkeypatch) -> None:
    store = _plain_wake_store(tmp_path, "fenced-claim")
    tasks = BackgroundAgentTasks(store, _done_wake)
    fence_active = False
    original_claim = store.create_watcher_wake_task

    def observed_claim(record, agent_id, watcher_ids):
        assert fence_active, "durable watcher claim escaped the generation fence"
        return original_claim(record, agent_id, watcher_ids)

    def observed_launch(operation_id):
        assert fence_active, "watcher task launch escaped the generation fence"
        stored = store.agent_task(operation_id)
        assert stored is not None
        return stored

    def fence(callback) -> bool:
        nonlocal fence_active
        fence_active = True
        try:
            callback()
        finally:
            fence_active = False
        return True

    monkeypatch.setattr(store, "create_watcher_wake_task", observed_claim)
    monkeypatch.setattr(tasks, "launch_admitted", observed_launch)

    started = start_plain_watcher_wake(
        tasks, "chat", _ready(store, "fenced-claim"), _test_authorizer(store), fence
    )

    assert started is not None
    assert store.watcher("fenced-claim").wake_turn_id == started.operation_id
    tasks.shutdown()


def test_stale_retry_generation_cannot_make_the_final_watcher_claim(tmp_path) -> None:
    store = _plain_wake_store(tmp_path, "stale-final-claim")
    tasks = BackgroundAgentTasks(store, _done_wake)
    retry_started = threading.Event()
    release_retry = threading.Event()
    retry_finished = threading.Event()
    results: list[AgentTaskRecord | None] = []

    def delayed_retry(generation) -> None:
        retry_started.set()
        release_retry.wait()
        results.append(
            start_plain_watcher_wake(
                tasks,
                "chat",
                _ready(store, "stale-final-claim"),
                _test_authorizer(store),
                generation.run_if_current,
            )
        )
        retry_finished.set()

    worker = WatcherRetryWorker(delayed_retry)
    worker.start()
    try:
        worker.signal()
        assert retry_started.wait(1), "retry generation did not reach final admission"
        worker.stop(timeout=0.01)
        worker.start()
        release_retry.set()
        assert retry_finished.wait(1), "stale retry generation did not finish"

        assert results == [None]
        assert _ready(store, "stale-final-claim")
    finally:
        release_retry.set()
        worker.stop()
        tasks.shutdown()


def test_shutdown_serializes_plain_wake_admission_with_worker_snapshot(
    tmp_path, monkeypatch
) -> None:
    store = _plain_wake_store(tmp_path, "pre-close", "post-close")
    authorized_by = _test_authorizer(store)
    launch_entered = threading.Event()
    release_launch = threading.Event()
    shutdown_finished = threading.Event()
    delivered: list[AgentTaskRecord | None] = []
    errors: list[BaseException] = []

    async def wait_for_shutdown(_project_id, _kind, _request, execution):
        await async_wait_until(execution.control.pause_requested.is_set)
        yield f"data: {AgentEvent(event='paused', text='Server shutdown.').model_dump_json()}\n\n"

    tasks = BackgroundAgentTasks(store, wait_for_shutdown)
    original_launch = tasks.launch_admitted

    def blocked_launch(operation_id):
        launch_entered.set()
        if not release_launch.wait(2):
            raise RuntimeError("test did not release watcher launch")
        return original_launch(operation_id)

    monkeypatch.setattr(tasks, "launch_admitted", blocked_launch)

    def deliver_before_close() -> None:
        try:
            delivered.append(
                start_plain_watcher_wake(tasks, "chat", _ready(store, "pre-close"), authorized_by)
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def shut_down() -> None:
        try:
            tasks.shutdown(timeout=2)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            shutdown_finished.set()

    delivery_thread = threading.Thread(target=deliver_before_close, name="pre-close-delivery")
    shutdown_thread = threading.Thread(target=shut_down, name="watcher-shutdown")
    try:
        delivery_thread.start()
        assert launch_entered.wait(1), "delivery did not enter its serialized launch"
        shutdown_thread.start()
        assert not shutdown_finished.wait(0.05), "shutdown bypassed watcher admission"
        release_launch.set()
        delivery_thread.join(timeout=3)
        shutdown_thread.join(timeout=3)

        assert not delivery_thread.is_alive()
        assert not shutdown_thread.is_alive()
        assert errors == []
        assert len(delivered) == 1 and delivered[0] is not None
        settled = store.agent_task(delivered[0].operation_id)
        assert settled is not None and settled.status == "paused"

        assert (
            start_plain_watcher_wake(tasks, "chat", _ready(store, "post-close"), authorized_by)
            is None
        )
        assert _ready(store, "post-close")
    finally:
        release_launch.set()
        delivery_thread.join(timeout=1)
        shutdown_thread.join(timeout=1)
        tasks.shutdown(timeout=1)


def test_pre_start_pause_still_runs_settlement_hook_without_stream_close(tmp_path) -> None:
    store = AppStore(tmp_path / "pre-start-pause.sqlite3")
    record = _chat_turn(store, "pre-start-pause")
    streamed: list[str] = []
    stream_closed: list[str] = []
    settled: list[tuple[str, str]] = []

    async def stream(_project_id, _kind, _request, _execution):
        streamed.append("called")
        yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"

    def closed(_project_id, _kind, _request, execution) -> None:
        stream_closed.append(execution.operation_id)

    def after_settlement(_project_id, _kind, _request, execution) -> None:
        task = store.agent_task(execution.operation_id)
        assert task is not None
        settled.append((execution.operation_id, task.status))

    tasks = BackgroundAgentTasks(
        store, stream, on_stream_closed=closed, on_task_settled=after_settlement
    )
    store.create_agent_task(record)
    control = AgentProcessControl()
    control.pause_requested.set()

    tasks._run(record, RunRequest.model_validate(record.request), control, "fresh")

    paused = store.agent_task(record.operation_id)
    assert paused is not None and paused.status == "paused"
    assert settled == [(record.operation_id, "paused")]
    assert streamed == []
    assert stream_closed == []


@pytest.mark.parametrize(
    ("terminal_event", "expected_status"),
    [("error", "failed"), ("paused", "paused")],
)
def test_post_settlement_hook_observes_verdict_and_revision_without_replacing_it(
    tmp_path, terminal_event, expected_status
) -> None:
    from .test_auto_research_recovery import _store

    store = _store(tmp_path)
    boundary = _state(blocker_status="resolved", revision=7)
    observed: list[tuple[str, int | None, bool]] = []

    async def stream(_project_id, _kind, _request, execution):
        _record_patch_applied_receipt(execution, boundary)
        yield f"data: {AgentEvent(event=terminal_event, text='provider stopped').model_dump_json()}\n\n"

    def settled(_project_id, _kind, _request, execution) -> None:
        task = store.agent_task(execution.operation_id)
        assert task is not None
        observed.append(
            (task.status, execution.applied_revision, execution.applied_graph_state is boundary)
        )
        raise RuntimeError("observer failure")

    tasks = BackgroundAgentTasks(store, stream, on_task_settled=settled)
    request = RunRequest(
        provider="codex",
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Apply a graph change, then stop.",
        mode="work",
    )
    owner = store.local_owner
    assert owner is not None
    owner = store.rename_space_user(owner.user_id, "Test researcher")
    assert owner.display_name is not None
    started = tasks.start(
        "project",
        "project_chat",
        request,
        authorized_by=AuthorizedHuman(
            space_id=store.space_id, user_id=owner.user_id, display_name=owner.display_name
        ),
    )
    try:
        terminal = _wait_for_terminal_task(store, started.operation_id)
    finally:
        tasks.shutdown()

    assert terminal.status == expected_status
    assert observed == [(expected_status, 7, True)]
    receipt_categories = {
        receipt.category for receipt in store.agent_task_receipts(started.operation_id)
    }
    assert {"patch_applied", "task_settled_callback_failed"} <= receipt_categories
