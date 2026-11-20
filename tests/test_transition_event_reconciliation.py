from __future__ import annotations

from dataclasses import replace

import pytest

from rcp.core.models import Blocker, GraphState, Patch
from rcp.core.transition_models import (
    GraphHeadRef,
    GraphTargetRef,
    TransitionCauseRef,
    TransitionEvent,
    TransitionInitiatingGroup,
    TransitionTrace,
)
from rcp.runs.transition_event_reconciliation import (
    AcceptedGraphBoundary,
    reconcile_accepted_graph_boundaries,
)
from rcp.storage import AppStore, NodeStatusGraphCondition
from rcp.storage.models import WatcherCheckRecord, WatcherRecord

from .storage_helpers import downgrade_artifacts

_CREATED_AT = "2026-08-18T00:00:00+00:00"


def _blocker(status: str = "open") -> Blocker:
    return Blocker(
        id="blk/capacity",
        type="blocker",
        title="Capacity unavailable",
        description="The required capacity is unavailable.",
        status=status,
    )


def _state(revision: int, *, blocker_status: str | None = "open") -> GraphState:
    nodes = {}
    if blocker_status is not None:
        blocker = _blocker(blocker_status)
        nodes[blocker.id] = blocker
    return GraphState(revision=revision, nodes=nodes)


def _arm(
    store: AppStore,
    watcher_id: str,
    *,
    armed_head: int = 1,
    status_in: list[str] | None = None,
    graph_target: GraphTargetRef | None = None,
) -> WatcherRecord:
    return store.arm_watcher(
        WatcherRecord(
            watcher_id=watcher_id,
            agent_id="chat",
            project_id="project",
            purpose="Blocker resolution",
            origin_turn_id="origin",
            created_at=_CREATED_AT,
            checks=[
                WatcherCheckRecord(
                    check_id=watcher_id,
                    watcher_id=watcher_id,
                    machine_id="backend",
                    definition="graph",
                    condition=NodeStatusGraphCondition(
                        node_id="blk/capacity", status_in=status_in or ["resolved"]
                    ),
                    graph_target=graph_target or GraphTargetRef(),
                    armed_head=armed_head,
                    created_at=_CREATED_AT,
                    last_checked_at=_CREATED_AT,
                )
            ],
        )
    )


def _completed_at(store: AppStore, watcher_id: str) -> str | None:
    watcher = store.watcher(watcher_id)
    assert watcher is not None and watcher.cancelled_at is None
    return watcher.checks[0].completed_at


def _boundary(
    revision: int,
    *,
    target: GraphTargetRef | None = None,
    transition_id: str | None = None,
    blocker_status: str | None = None,
    events: tuple[TransitionEvent, ...] = (),
) -> AcceptedGraphBoundary:
    return AcceptedGraphBoundary(
        target=target or GraphTargetRef(),
        revision=revision,
        transition_id=transition_id or f"{revision:x}" * 64,
        state=_state(revision, blocker_status=blocker_status),
        lifecycle_events=events,
    )


def _reconcile(store: AppStore, *boundaries: AcceptedGraphBoundary) -> None:
    reconcile_accepted_graph_boundaries(
        store,
        "project",
        boundaries,
        current_head=GraphHeadRef(target=boundaries[-1].target, revision=boundaries[-1].revision),
    )


def _status_event(
    *,
    event_id: str = "a" * 64,
    after: str = "resolved",
) -> TransitionEvent:
    return TransitionEvent(
        event_id=event_id,
        event_type="node_status_changed",
        cause=TransitionCauseRef(kind="action", action_index=0),
        node_id="blk/capacity",
        field="status",
        before="open",
        after=after,
    )


def _accepted_patch(*, revision: int = 2, admission: str = "accepted") -> Patch:
    operation = {
        "op": "update_nodes",
        "nodes": [
            {
                "id": "blk/capacity",
                "base_updated_rev": revision - 1,
                "changes": {"status": "resolved"},
            }
        ],
    }
    transition = TransitionTrace(
        transition_id="b" * 64,
        pre_head=GraphHeadRef(target=GraphTargetRef(), revision=revision - 1),
        ruleset_tag="rcp.lifecycle.v1",
        initiating_groups=[
            TransitionInitiatingGroup(
                group_id="group-1",
                operation_indexes=[0],
                summary="Resolve the capacity blocker.",
            )
        ],
        lifecycle_events=[_status_event()],
        expanded_ops_sha256="c" * 64,
    )
    return Patch(
        revision=revision,
        kind="work",
        author="agent",
        producer="agent",
        summary="Resolve the capacity blocker.",
        ops=[operation],
        admission=admission,
        transition=transition,
    )


def test_status_event_completes_a_check_whose_node_the_same_transition_removed(
    tmp_path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _arm(store, "resolved-watcher")

    _reconcile(store, _boundary(2, events=(_status_event(),)))

    assert _completed_at(store, "resolved-watcher") is not None
    assert [watcher.watcher_id for watcher in store.ready_watchers()] == ["resolved-watcher"]


def test_same_revision_watcher_does_not_retroactively_consume_event(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _arm(store, "later-watcher", armed_head=2)

    _reconcile(store, _boundary(2, events=(_status_event(),)))

    assert _completed_at(store, "later-watcher") is None
    assert store.ready_watchers() == []


def test_removed_target_without_matching_event_cancels_the_watcher(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _arm(store, "closed-watcher")

    _reconcile(store, _boundary(2, events=(_status_event(after="closed"),)))

    stored = store.watcher("closed-watcher")
    assert stored is not None
    assert stored.cancelled_by == "rcp"
    assert stored.checks[0].completed_at is None
    assert store.ready_watchers() == []
    assert store.graph_watcher_project_ids() == []


def test_replay_boundary_reconciles_post_commit_crash_window_idempotently(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _arm(store, "crash-window-watcher")
    boundary = AcceptedGraphBoundary.from_replay(
        _state(1), _accepted_patch(), _state(2, blocker_status=None)
    )

    reconcile_accepted_graph_boundaries(store, "project", [boundary])
    completed_at = _completed_at(store, "crash-window-watcher")
    assert completed_at is not None

    reconcile_accepted_graph_boundaries(store, "project", [boundary])
    assert _completed_at(store, "crash-window-watcher") == completed_at
    assert [watcher.watcher_id for watcher in store.ready_watchers()] == ["crash-window-watcher"]


def test_boundary_construction_rejects_noncanonical_inputs() -> None:
    pre_state = _state(1)
    post_state = _state(2, blocker_status=None)

    with pytest.raises(ValueError, match="rejected Patch"):
        AcceptedGraphBoundary.from_replay(
            pre_state, _accepted_patch(admission="rejected"), post_state
        )
    with pytest.raises(ValueError, match="state does not match"):
        AcceptedGraphBoundary.from_replay(pre_state, _accepted_patch(), _state(3))


def test_reconciliation_rejects_target_local_reordering(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    first = AcceptedGraphBoundary.from_replay(
        _state(1), _accepted_patch(), _state(2, blocker_status="resolved")
    )

    with pytest.raises(ValueError, match="canonical order"):
        reconcile_accepted_graph_boundaries(store, "project", [first, first])


def test_reconciliation_heads_are_durable_and_target_isolated(tmp_path) -> None:
    path = tmp_path / "rcp.sqlite3"
    store = AppStore(path)
    branch = GraphTargetRef(kind="branch", branch_id="episode/one")
    _arm(store, "main-watcher")
    _arm(store, "branch-watcher", graph_target=branch)

    _reconcile(
        store,
        _boundary(2, transition_id="1" * 64, events=(_status_event(event_id="2" * 64),)),
    )
    assert _completed_at(store, "main-watcher") is not None
    assert _completed_at(store, "branch-watcher") is None

    _reconcile(store, _boundary(2, target=branch, transition_id="3" * 64, blocker_status="open"))
    reopened = AppStore(path)
    main_head = reopened.graph_watcher_reconciliation_head("project", GraphTargetRef())
    branch_head = reopened.graph_watcher_reconciliation_head("project", branch)
    assert main_head is not None and main_head.revision == 2
    assert main_head.transition_id == "1" * 64
    assert branch_head is not None and branch_head.revision == 2
    assert branch_head.transition_id == "3" * 64
    assert _completed_at(reopened, "branch-watcher") is None


def test_branch_boundary_never_consumes_a_main_check(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    branch = GraphTargetRef(kind="branch", branch_id="episode/one")
    _arm(store, "main-watcher")

    _reconcile(store, _boundary(2, target=branch, events=(_status_event(),)))

    assert _completed_at(store, "main-watcher") is None
    assert store.ready_watchers() == []
    assert store.graph_watcher_reconciliation_head("project", GraphTargetRef()) is None


def test_restart_reconciliation_evaluates_only_boundaries_after_durable_head(
    tmp_path,
    monkeypatch,
) -> None:
    import rcp.runs.transition_event_reconciliation as reconciliation

    path = tmp_path / "rcp.sqlite3"
    store = AppStore(path)
    _arm(store, "restart-watcher", armed_head=0)
    boundaries = [_boundary(revision, blocker_status="open") for revision in (1, 2, 3)]
    _reconcile(store, *boundaries[:2])

    reopened = AppStore(path)
    evaluated_revisions: list[int] = []
    original = reconciliation.graph_check_boundary_result

    def observe(check, state, lifecycle_events):
        evaluated_revisions.append(state.revision)
        return original(check, state, lifecycle_events)

    monkeypatch.setattr(reconciliation, "graph_check_boundary_result", observe)
    _reconcile(reopened, *boundaries)

    assert evaluated_revisions == [3]
    head = reopened.graph_watcher_reconciliation_head("project", GraphTargetRef())
    assert head is not None and head.revision == 3

    with pytest.raises(RuntimeError, match="changed identity"):
        _reconcile(reopened, replace(boundaries[-1], transition_id="d" * 64))

    with pytest.raises(ValueError, match="cannot arm behind"):
        _arm(reopened, "stale-arm", armed_head=2)


def test_failed_boundary_evaluation_rolls_back_checks_and_head_for_retry(
    tmp_path,
    monkeypatch,
) -> None:
    import rcp.runs.transition_event_reconciliation as reconciliation

    store = AppStore(tmp_path / "rcp.sqlite3")
    _arm(store, "first-retry-watcher", armed_head=0)
    _arm(store, "second-retry-watcher", armed_head=0)
    boundary = _boundary(2, blocker_status="resolved", events=(_status_event(event_id="5" * 64),))
    original = reconciliation.graph_check_boundary_result
    evaluations = 0

    def fail_second(check, state, lifecycle_events):
        nonlocal evaluations
        evaluations += 1
        if evaluations == 2:
            raise OSError("transient SQLite-adjacent failure")
        return original(check, state, lifecycle_events)

    monkeypatch.setattr(reconciliation, "graph_check_boundary_result", fail_second)
    with pytest.raises(OSError, match="transient"):
        _reconcile(store, boundary)

    assert store.graph_watcher_reconciliation_head("project", GraphTargetRef()) is None
    assert _completed_at(store, "first-retry-watcher") is None
    assert _completed_at(store, "second-retry-watcher") is None

    monkeypatch.setattr(reconciliation, "graph_check_boundary_result", original)
    _reconcile(store, boundary)
    assert _completed_at(store, "first-retry-watcher") is not None
    assert _completed_at(store, "second-retry-watcher") is not None
    head = store.graph_watcher_reconciliation_head("project", GraphTargetRef())
    assert head is not None and head.revision == 2


def test_legacy_database_adds_graph_watcher_reconciliation_table(tmp_path) -> None:
    path = tmp_path / "rcp.sqlite3"
    store = AppStore(path)
    with store.connection() as connection:
        connection.execute("DROP TABLE graph_watcher_reconciliation")
        downgrade_artifacts(connection)
        connection.execute(
            "DELETE FROM storage_schema_migrations WHERE migration_version IN (5, 6)"
        )

    reopened = AppStore(path)
    assert reopened.graph_watcher_reconciliation_head("project", GraphTargetRef()) is None
    with reopened.connection() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'graph_watcher_reconciliation'"
        ).fetchone()
    assert table is not None
