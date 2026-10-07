from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.runs.experiment_loop import experiment_watcher_output_name
from rcp.storage import AgentTaskRecord, AppStore, WatcherContinuation, WatcherRecord

_PROJECT_ID = "project"
_CONTROL_NODE_ID = "exp/shared"


def _identity(store: AppStore) -> AuthorizedHuman:
    owner = store.local_owner
    assert owner is not None
    return AuthorizedHuman(
        space_id=store.space_id,
        user_id=owner.user_id,
        display_name=owner.display_name or "Test researcher",
    )


def _loop_task(
    store: AppStore,
    operation_id: str,
    episode_id: str,
    *,
    invocation: int = 1,
    watcher_ids: list[str] | None = None,
    graph_target: GraphTargetRef | None = None,
) -> AgentTaskRecord:
    target = graph_target or GraphTargetRef()
    now = store.now()
    ids = list(watcher_ids or [])
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=_PROJECT_ID,
        episode_id=episode_id,
        graph_target=target,
        kind="node_chat",
        status="queued",
        request={
            "chat_id": f"chat-{episode_id}",
            "node_id": _CONTROL_NODE_ID,
            "provider": "codex",
            "model": "gpt-5",
            "reasoning": "medium",
            "run_on": "laptop",
            "run_truth_scope": ["state"],
            "mode": "work",
            "trigger": "watcher" if ids else "experiment_run",
            "patch_kind": "experiment_loop",
            "control_node_id": _CONTROL_NODE_ID,
            "control_revision": 0,
            "control_episode_id": episode_id,
            "control_invocation": invocation,
            "control_invocation_ceiling": 3,
            "control_decision_bundle": [],
            "control_completion_criteria": [],
            "workflow_ids": [],
            "skill_ids": [],
            "invoked_workflow_ids": [],
            "invoked_skill_ids": [],
            "resolved_skill_packages": [],
            "watcher_ids": ids,
        },
        created_at=now,
        updated_at=now,
        status_message="Queued loop invocation.",
        authorized_by=_identity(store),
    )


def _create_episode(
    store: AppStore,
    operation_id: str,
    graph_target: GraphTargetRef,
) -> tuple[str, AgentTaskRecord]:
    owns_branch = (
        graph_target.kind == "branch" and store.episode(graph_target.branch_id or "") is None
    )
    episode_id = graph_target.branch_id if owns_branch else str(uuid.uuid4())
    assert episode_id is not None
    root = _loop_task(store, operation_id, episode_id, graph_target=graph_target)
    if owns_branch:
        root = root.model_copy(update={"request": {**root.request, "graph_isolation": True}})
    stored = store.create_experiment_episode_with_invocation(
        root, graph_base_head=GraphHeadRef(revision=0) if owns_branch else None
    )
    assert stored.graph_target == graph_target
    return episode_id, stored


def _watcher(
    store: AppStore,
    watcher_id: str,
    root: AgentTaskRecord,
    *,
    status: str = "completed",
) -> WatcherRecord:
    episode_id = root.episode_id
    assert episode_id is not None
    continuation = WatcherContinuation(
        provider="codex",
        model="gpt-5",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["state"],
        patch_kind="experiment_loop",
        control_node_id=_CONTROL_NODE_ID,
        control_revision=0,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=3,
        control_decision_bundle=[],
        control_completion_criteria=[],
    )
    created_at = store.now()
    return WatcherRecord(
        watcher_id=watcher_id,
        project_id=_PROJECT_ID,
        origin_operation_id=root.operation_id,
        origin_task_kind="node_chat",
        chat_id=str(root.request["chat_id"]),
        node_id=_CONTROL_NODE_ID,
        episode_id=episode_id,
        graph_target=root.graph_target,
        execution_host="",
        check_command="true",
        log_path=f"/tmp/{watcher_id}.log",
        cwd="/tmp",
        continuation=continuation,
        status=status,
        created_at=created_at,
        completed_at=created_at if status == "completed" else None,
    )


def _watcher_snapshot(store: AppStore, graph_target: GraphTargetRef) -> str:
    with store.connection() as connection:
        return store._experiment_watcher_snapshot_token(  # noqa: SLF001
            connection,
            _PROJECT_ID,
            _CONTROL_NODE_ID,
            graph_target,
        )


def test_same_node_watcher_snapshot_and_file_identity_are_target_local(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    main = GraphTargetRef()
    branch = GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))
    main_root = _loop_task(store, "main-root", str(uuid.uuid4()), graph_target=main)
    _branch_episode_id, branch_root = _create_episode(store, "branch-root", branch)

    empty_main = _watcher_snapshot(store, main)
    empty_branch = _watcher_snapshot(store, branch)
    assert empty_main == empty_branch

    store.create_watchers([_watcher(store, "main-watcher", main_root, status="active")])
    main_only = _watcher_snapshot(store, main)
    assert main_only != empty_main
    assert _watcher_snapshot(store, branch) == empty_branch

    store.create_watchers([_watcher(store, "branch-watcher", branch_root, status="active")])
    assert _watcher_snapshot(store, main) == main_only
    assert _watcher_snapshot(store, branch) != empty_branch
    assert experiment_watcher_output_name(_CONTROL_NODE_ID, main) != experiment_watcher_output_name(
        _CONTROL_NODE_ID,
        branch,
    )


@pytest.mark.parametrize("watcher_target_kind", ["main", "branch"])
def test_newer_other_target_episode_cannot_claim_or_adopt_completed_watcher(
    tmp_path,
    watcher_target_kind: str,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    branch = GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))
    watcher_target = GraphTargetRef() if watcher_target_kind == "main" else branch
    newer_target = branch if watcher_target_kind == "main" else GraphTargetRef()

    old_episode_id, old_root = _create_episode(store, "old-root", watcher_target)
    store.complete_agent_task(old_root.operation_id, applied_revision=None, result={})
    pending = _watcher(store, "pending-old-target", old_root)
    store.create_watchers([pending])

    newer_episode_id, newer_root = _create_episode(store, "newer-root", newer_target)
    assert (
        store.experiment_loop_runtime(
            _PROJECT_ID,
            _CONTROL_NODE_ID,
            graph_target=newer_target,
        ).episode_id
        == newer_episode_id
    )
    exact_old = store.experiment_loop_runtime(
        _PROJECT_ID,
        _CONTROL_NODE_ID,
        graph_target=watcher_target,
    )
    exact_new = store.experiment_loop_runtime(
        _PROJECT_ID,
        _CONTROL_NODE_ID,
        graph_target=newer_target,
    )
    assert exact_old.episode_id == old_episode_id
    assert exact_old.watcher_completion_pending is True
    assert exact_new.episode_id == newer_episode_id
    assert exact_new.watcher_completion_pending is False
    group = store.completed_experiment_watcher_group(
        _PROJECT_ID,
        _CONTROL_NODE_ID,
        graph_target=watcher_target,
    )
    assert group is not None and [item.watcher_id for item in group] == [pending.watcher_id]
    assert (
        store.completed_experiment_watcher_group(
            _PROJECT_ID,
            _CONTROL_NODE_ID,
            graph_target=newer_target,
        )
        is None
    )

    forged = _loop_task(
        store,
        "cross-target-wake",
        newer_episode_id,
        invocation=2,
        watcher_ids=[pending.watcher_id],
        graph_target=newer_target,
    )
    with pytest.raises(ValueError, match="different graph targets"):
        store.create_experiment_watcher_invocation(forged, [pending.watcher_id])

    assert store.agent_task(forged.operation_id) is None
    assert store.watcher(pending.watcher_id).notified is False
    assert store.episode(newer_episode_id).invocations_used == 1
    assert store.agent_task(newer_root.operation_id).applied_revision is None


@pytest.mark.parametrize("stop_branch", [False, True])
def test_exact_target_stop_mutates_only_selected_episode_and_target_watchers(
    tmp_path, stop_branch
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    branch = GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))
    selected_target = branch if stop_branch else GraphTargetRef()
    other_target = GraphTargetRef() if stop_branch else branch
    selected_episode_id, selected_root = _create_episode(store, "branch-root", selected_target)
    store.complete_agent_task(selected_root.operation_id, applied_revision=None, result={})
    selected_watcher = _watcher(store, "branch-watcher", selected_root, status="active")
    store.create_watchers([selected_watcher])

    other_episode_id, other_root = _create_episode(store, "newer-main-root", other_target)
    other_watcher = _watcher(store, "main-watcher", other_root, status="active")
    store.create_watchers([other_watcher])
    stopped = store.request_experiment_loop_stop(
        _PROJECT_ID,
        _CONTROL_NODE_ID,
        episode_id=selected_episode_id,
        graph_target=selected_target,
    )

    assert stopped is not None and stopped.episode_id == selected_episode_id
    assert stopped.graph_target == selected_target
    assert stopped.stop_requested_at is not None
    assert stopped.stop_settled_at is not None
    other = store.episode(other_episode_id)
    assert other is not None and other.status == "running"
    assert other.stop_requested_at is None and other.stop_settled_at is None
    assert store.watcher(selected_watcher.watcher_id).status == "stopped"
    assert store.watcher(other_watcher.watcher_id).status == "active"
    assert store.agent_task(other_root.operation_id).status == "queued"


@pytest.mark.parametrize("branch_first", [False, True])
def test_same_node_targets_recover_independently_after_reopen(tmp_path, branch_first) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    targets = [GraphTargetRef(), GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))]
    if branch_first:
        targets.reverse()
    roots = [
        _create_episode(store, f"root-{index}", target)[1] for index, target in enumerate(targets)
    ]
    for root in roots:
        store.fail_agent_task(root.operation_id, "Provider failed")
    store = AppStore(store.path)
    for root in roots:
        assert root.episode_id is not None
        retry = root.model_copy(
            update={
                "operation_id": f"retry-{root.operation_id}",
                "parent_operation_id": root.operation_id,
                "attempt": 2,
            }
        )
        recovered = store.create_experiment_recovery_task(retry)
        assert recovered.graph_target == root.graph_target
        episode = store.episode(root.episode_id)
        assert episode is not None and episode.invocations_used == 1
        assert (
            store.experiment_loop_runtime(
                _PROJECT_ID, _CONTROL_NODE_ID, graph_target=root.graph_target
            ).current_operation_id
            == recovered.operation_id
        )
    snapshots = store.project_experiment_control_projection_snapshots(_PROJECT_ID)
    assert set(snapshots) == {(_CONTROL_NODE_ID, target.key) for target in targets}
    assert all(row.episode is not None for row in snapshots.values())
    assert {
        row.episode.episode.episode_id for row in snapshots.values() if row.episode is not None
    } == {root.episode_id for root in roots}


@pytest.mark.parametrize("branch_first", [False, True])
def test_same_node_targets_claim_graph_repairs_independently(tmp_path, branch_first) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    targets = [GraphTargetRef(), GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))]
    if branch_first:
        targets.reverse()
    roots = [
        _create_episode(store, f"root-{index}", target)[1] for index, target in enumerate(targets)
    ]
    for root in roots:
        store.checkpoint_agent_task(
            root.operation_id,
            native_session_id=f"session-{root.operation_id}",
            stage_root=str(tmp_path / root.operation_id),
        )
        store.complete_agent_task(
            root.operation_id,
            applied_revision=None,
            result={"graph_update": {"status": "rejected", "repairable": True}},
        )
    for root in roots:
        claimed = store.claim_agent_task_graph_repair(root.operation_id)
        assert claimed.operation_id == root.operation_id
        assert claimed.graph_target == root.graph_target


@pytest.mark.parametrize("branch_first", [False, True])
def test_same_node_targets_deliver_completed_watcher_groups_independently(
    tmp_path, branch_first
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    targets = [GraphTargetRef(), GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))]
    if branch_first:
        targets.reverse()
    roots = [
        _create_episode(store, f"root-{index}", target)[1] for index, target in enumerate(targets)
    ]
    for root in roots:
        assert root.episode_id is not None
        store.complete_agent_task(root.operation_id, applied_revision=None, result={})
        store.commit_experiment_episode_turn(
            episode_id=root.episode_id,
            project_id=_PROJECT_ID,
            control_node_id=_CONTROL_NODE_ID,
            provider="codex",
            execution_machine="laptop",
            execution_host="",
            native_session_id=f"session-{root.operation_id}",
            stage_host=None,
            stage_root=str(tmp_path / root.operation_id),
            chat_id=str(root.request["chat_id"]),
            operation_id=root.operation_id,
            invocation=1,
            graph_result="no graph change",
            watcher_ids=[],
            context_baseline={"ontology": {"sha256": "abc"}},
        )
        store.create_watchers([_watcher(store, f"watcher-{root.operation_id}", root)])
    groups = store.completed_watcher_groups()
    assert {group[0].graph_target.key for group in groups} == {target.key for target in targets}
    for root in roots:
        assert root.episode_id is not None
        watcher_id = f"watcher-{root.operation_id}"
        wake = _loop_task(
            store,
            f"wake-{root.operation_id}",
            root.episode_id,
            invocation=2,
            watcher_ids=[watcher_id],
            graph_target=root.graph_target,
        ).model_copy(
            update={
                "native_session_id": f"session-{root.operation_id}",
                "stage_root": str(tmp_path / root.operation_id),
            }
        )
        wake.request.update(
            session_id=f"session-{root.operation_id}",
            code_worktree=root.request.get("code_worktree", False),
            graph_isolation=root.request.get("graph_isolation", False),
        )
        admitted = store.create_experiment_watcher_invocation(wake, [watcher_id])
        assert admitted is not None
        assert admitted.graph_target == root.graph_target
        episode = store.episode(root.episode_id)
        assert episode is not None and episode.invocations_used == 2
        watcher = store.watcher(watcher_id)
        assert watcher is not None and watcher.notification_operation_id == wake.operation_id
    assert store.completed_watcher_groups() == []


def test_stop_requires_an_exact_identity(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    with pytest.raises(ValueError, match="exact episode or graph target"):
        store.request_experiment_loop_stop(_PROJECT_ID, _CONTROL_NODE_ID)
    with pytest.raises(ValueError, match="exact episode or graph target"):
        store.settle_experiment_loop_stop(_PROJECT_ID, _CONTROL_NODE_ID)


def _failed_remote_loop(store: AppStore) -> tuple[str, AgentTaskRecord]:
    episode_id, root = _create_episode(store, "remote-root", GraphTargetRef())
    store.checkpoint_agent_task(
        root.operation_id,
        native_session_id="remote-session",
        stage_host="worker.example",
        stage_root="/remote/rcp-stage",
    )
    store.fail_agent_task(root.operation_id, "interrupted fixture", status="interrupted")
    store.request_episode_stop(episode_id)
    return episode_id, root


def test_other_target_watcher_writes_while_stop_probes_remote_workspace(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id, _root = _failed_remote_loop(store)
    branch = GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))
    _branch_id, branch_root = _create_episode(store, "branch-root", branch)
    store.create_watchers([_watcher(store, "branch-watcher", branch_root, status="active")])
    probing, release = Event(), Event()

    def slow_probe(_stage, _path):
        probing.set()
        assert release.wait(timeout=5)
        return False

    monkeypatch.setattr("rcp.transport.RemoteRunStage.directory_exists", slow_probe)
    with ThreadPoolExecutor(max_workers=2) as executor:
        stop = executor.submit(
            store.settle_experiment_loop_stop,
            _PROJECT_ID,
            _CONTROL_NODE_ID,
            episode_id=episode_id,
        )
        try:
            assert probing.wait(timeout=2)
            write = executor.submit(
                store.record_watcher_check,
                "branch-watcher",
                status="completed",
                exit_code=0,
                error=None,
            )
            watcher = write.result(timeout=2)
            assert watcher.status == "completed" and watcher.graph_target == branch
            assert not stop.done()
        finally:
            release.set()
        settled = stop.result(timeout=2)
    assert settled is not None and settled.stop_settled_at is not None
    watcher = store.watcher("branch-watcher")
    assert watcher is not None and watcher.status == "completed" and not watcher.notified


@pytest.mark.parametrize("field", ["native_session_id", "stage_host", "stage_root"])
def test_stop_discards_workspace_probe_when_saved_binding_changes(tmp_path, monkeypatch, field):
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id, root = _failed_remote_loop(store)

    def rebind_during_probe(_stage, _path):
        store.checkpoint_agent_task(root.operation_id, **{field: "replacement-binding"})
        return False

    monkeypatch.setattr("rcp.transport.RemoteRunStage.directory_exists", rebind_during_probe)
    pending = store.settle_experiment_loop_stop(
        _PROJECT_ID, _CONTROL_NODE_ID, episode_id=episode_id
    )
    assert pending is not None and pending.stop_settled_at is None
    assert pending.session_diagnostic is None
    assert "experiment_recovery_abandoned" not in {
        receipt.category for receipt in store.agent_task_receipts(root.operation_id)
    }
    monkeypatch.setattr("rcp.transport.RemoteRunStage.directory_exists", lambda *_: False)
    settled = store.settle_experiment_loop_stop(
        _PROJECT_ID, _CONTROL_NODE_ID, episode_id=episode_id
    )
    assert settled is not None and settled.stop_settled_at is not None
    assert "experiment_recovery_abandoned" in {
        receipt.category for receipt in store.agent_task_receipts(root.operation_id)
    }


def test_stop_settles_on_broken_local_task_before_unreachable_remote_task(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id, local = _create_episode(store, "local-task-a", GraphTargetRef())
    store.checkpoint_agent_task(
        local.operation_id,
        native_session_id="local-session",
        stage_root=str(tmp_path / "missing-stage"),
    )
    store.fail_agent_task(local.operation_id, "interrupted fixture", status="interrupted")
    # Seed retained history with two unresolved turns on different hosts.
    # Neither turn supersedes the other; Stop must inspect both in order.
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO graph_runs (
                operation_id, project_id, episode_id, kind, status, request_json,
                created_at, updated_at, status_message, native_session_id, stage_host, stage_root
            )
            SELECT 'remote-task-b', project_id, episode_id, kind, status,
                json_set(request_json, '$.control_invocation', 2, '$.trigger', 'watcher'),
                created_at, updated_at, status_message, 'remote-session',
                'worker.example', '/remote/rcp-stage'
            FROM graph_runs WHERE operation_id = ?
            """,
            (local.operation_id,),
        )
    store.request_episode_stop(episode_id)
    probes = []

    def unreachable(_stage, path):
        probes.append(path)
        raise OSError("fixture host unreachable")

    monkeypatch.setattr("rcp.transport.RemoteRunStage.directory_exists", unreachable)
    settled = store.settle_experiment_loop_stop(
        _PROJECT_ID, _CONTROL_NODE_ID, episode_id=episode_id
    )
    assert settled is not None and settled.stop_settled_at is not None
    assert settled.session_diagnostic is not None
    assert probes == []
    for operation_id in (local.operation_id, "remote-task-b"):
        assert "experiment_recovery_abandoned" in {
            receipt.category for receipt in store.agent_task_receipts(operation_id)
        }
