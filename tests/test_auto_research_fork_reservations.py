from __future__ import annotations

import hashlib
import uuid

import pytest

from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.storage import (
    AutoResearchCommandFileRecord,
    AutoResearchExperimentAllowanceReached,
    EpisodeInvocationCeilingReached,
)
from rcp.storage.models import GraphBranchRecord
from tests.test_auto_research_children_storage import (
    _admission,
    _experiment_route,
    _experiment_task,
    _setup_parent,
    _work_pair,
)


def _reservation(store, parent, root):
    return GraphBranchRecord(
        project_id=parent.project_id,
        branch_id=str(uuid.uuid4()),
        owner_episode_id=parent.episode_id,
        parent_head=GraphHeadRef(target=parent.graph_target, revision=0),
        name="reserved-fork",
        created_by=root.operation_id,
        created_at=store.now(),
        auto_archive=True,
    )


def _start(store, parent, root, reservation, *, command_id="spawn", **kwargs):
    return store.start_agent_command(
        operation_id=root.operation_id,
        command_id=command_id,
        episode_id=parent.episode_id,
        verb="spawn",
        idempotency_key="spawn-key",
        payload={
            "resolved_graph_target": GraphTargetRef(
                kind="branch", branch_id=reservation.branch_id
            ).model_dump(mode="json"),
            **kwargs.pop("payload", {}),
        },
        child_admission=_admission(
            store, parent, admission_id=command_id, child_kind="work", child_id="worker"
        ),
        **kwargs,
    )


@pytest.mark.parametrize("changed", ["archived", "owner"])
def test_command_capture_rechecks_branch_admission_inside_transaction(tmp_path, changed):
    store, parent, root = _setup_parent(tmp_path)
    reservation = _reservation(store, parent, root)
    store.create_graph_branch(
        **reservation.model_dump(exclude={"archived", "parent_head"}),
        parent_head=reservation.parent_head,
    )
    with store.connection() as connection:
        column, value = ("archived", 1) if changed == "archived" else ("owner_episode_id", None)
        connection.execute(
            f"UPDATE graph_branches SET {column}=? WHERE branch_id=?",
            (value, reservation.branch_id),
        )
    with pytest.raises(ValueError):
        _start(store, parent, root, reservation)
    assert store.agent_command("spawn") is None
    assert store.pending_auto_research_child_admissions() == []


@pytest.mark.parametrize("exhausted", [False, True])
def test_fork_seed_command_and_admission_commit_or_rollback_together(tmp_path, exhausted):
    store, parent, root = _setup_parent(tmp_path, ceiling=1 if exhausted else 2)
    reservation = _reservation(store, parent, root)
    seed = AutoResearchCommandFileRecord(
        command_id="seed",
        episode_id=parent.episode_id,
        operation_id=root.operation_id,
        kind="apply",
        filename="seed.json",
        content="{}",
        sha256=hashlib.sha256(b"{}").hexdigest(),
        created_at=store.now(),
    )
    kwargs = {
        "fork_reservation": reservation,
        "seed_snapshot": seed,
        "payload": {
            "fork_seed": {
                "snapshot_id": seed.command_id,
                "filename": seed.filename,
                "sha256": seed.sha256,
            }
        },
    }
    if exhausted:
        with pytest.raises(EpisodeInvocationCeilingReached):
            _start(store, parent, root, reservation, **kwargs)
        assert store.agent_command("spawn") is None
        assert store.auto_research_command_file("seed") is None
        assert store.graph_branch(parent.project_id, reservation.branch_id) is None
    else:
        started = _start(store, parent, root, reservation, **kwargs)
        replay = _start(store, parent, root, reservation, command_id="replay", **kwargs)
        assert started == replay
        assert store.auto_research_command_file("seed") == seed
        assert store.graph_branch(parent.project_id, reservation.branch_id) == reservation
        assert len(store.pending_auto_research_child_admissions()) == 1
        route, task = _work_pair(store, parent, root, worker_id="worker")
        task = task.model_copy(
            update={"graph_target": GraphTargetRef(kind="branch", branch_id=reservation.branch_id)}
        )
        store.create_auto_research_child_work(route, task, admission_id="spawn")
        assert store.episode_budget_meter(parent.episode_id).invocations_used == 2


def test_pending_worker_reserves_last_budget_slot_until_reflection(tmp_path):
    store, parent, root = _setup_parent(tmp_path, ceiling=2)
    admission = _admission(
        store, parent, admission_id="reserved", child_kind="work", child_id="reserved-worker"
    )
    store.record_auto_research_child_admission(admission)
    with pytest.raises(EpisodeInvocationCeilingReached):
        store.create_auto_research_agent_task(
            root.model_copy(update={"operation_id": str(uuid.uuid4())}), role="orchestrator"
        )
    unrelated, task = _work_pair(store, parent, root, worker_id="other-worker")
    with pytest.raises(EpisodeInvocationCeilingReached):
        store.create_auto_research_child_work(unrelated, task)
    route, task = _work_pair(store, parent, root, worker_id=admission.child_id)
    store.create_auto_research_child_work(route, task, admission_id=admission.admission_id)
    assert store.episode_budget_meter(parent.episode_id).invocations_used == 2


def test_pending_experiments_reserve_e_and_reflect_without_double_spend(tmp_path):
    store, parent, root = _setup_parent(tmp_path, ceiling=1)
    admissions = [
        _admission(
            store,
            parent,
            admission_id=str(uuid.uuid4()),
            child_kind="experiment",
            child_id=str(uuid.uuid4()),
        )
        for _ in range(5)
    ]
    for admission in admissions:
        store.record_auto_research_child_admission(admission)
    unrelated = _experiment_task(
        store, str(uuid.uuid4()), parent.authorized_by, node_id="exp/other"
    )
    with pytest.raises(AutoResearchExperimentAllowanceReached):
        store.create_experiment_episode_with_invocation(
            unrelated, auto_research_route=_experiment_route(store, parent, root, unrelated)
        )
    for index, admission in enumerate(admissions):
        task = _experiment_task(
            store, admission.child_id, parent.authorized_by, node_id=f"exp/{index}"
        )
        store.create_experiment_episode_with_invocation(
            task,
            auto_research_route=_experiment_route(store, parent, root, task),
            auto_research_admission_id=admission.admission_id,
        )
    assert store.auto_research_experiment_allowance(parent.episode_id).used == 5
