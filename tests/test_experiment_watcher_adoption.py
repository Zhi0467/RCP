from __future__ import annotations

import uuid

import pytest

from rcp.core.models import GraphBranchMetadata
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.runs.experiment_loop import preflight_episode_wake

from .helpers import create_named_app as create_app
from .test_experiment_stop import EXPERIMENT_ID, _Loop
from .test_experiment_watcher_targets import _retarget_episode


def _adopted_loop(manifest, tmp_path, *, branch: bool, old_status: str = "completed"):
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    loop = _Loop(app, invocation_ceiling=1)
    target = GraphTargetRef()
    if branch:
        branch_id = str(uuid.uuid4())
        target = GraphTargetRef(kind="branch", branch_id=branch_id)
        base = loop.service.history.head_ref()
        loop.service.history.create_auto_research_branch(
            GraphBranchMetadata(
                branch_id=branch_id,
                episode_id=branch_id,
                project_id=loop.project_id,
                base_head=base,
                head=GraphHeadRef(
                    target=target, revision=base.revision, transition_id=base.transition_id
                ),
                authorized_by=loop.authorizer,
            )
        )
    loop.start_episode(operation_id="loop-root")
    _retarget_episode(loop.store, loop.episode_id, target)
    old_episode = loop.episode_id
    loop.arm_watcher(
        "old-watcher", status=old_status, origin_operation_id="loop-root", graph_target=target
    )
    loop.settle_exhausted_ending()
    loop.episode_id = str(uuid.uuid4())
    loop.chat_id = str(uuid.uuid4())
    loop.invocation_ceiling = 8
    loop.start_episode(operation_id="current-root")
    _retarget_episode(loop.store, loop.episode_id, target)
    loop.bind_session(tmp_path / "current-stage", operation_id="current-root")
    loop.arm_watcher(
        "current-watcher",
        status="completed",
        origin_operation_id="current-root",
        graph_target=target,
    )
    return loop, target, old_episode


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("origin_authorizer", ["different", "missing"])
def test_adopted_completions_wake_current_episode_once_with_current_authority(
    manifest, tmp_path, monkeypatch, branch, origin_authorizer
):
    loop, target, old_episode = _adopted_loop(manifest, tmp_path, branch=branch)
    # Model retained historical attribution from another human or a legacy
    # origin. The current episode still has its validated authorizer.
    with loop.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_user_id = ?, authorized_space_id = ?, "
            "authorized_display_name = ? WHERE operation_id = 'loop-root'",
            (
                str(uuid.uuid4()) if origin_authorizer == "different" else None,
                loop.store.space_id if origin_authorizer == "different" else None,
                "Prior researcher" if origin_authorizer == "different" else None,
            ),
        )
    launched = []
    monkeypatch.setattr(
        loop.app.state.background_tasks,
        "launch_admitted",
        lambda operation_id: launched.append(operation_id) or loop.store.agent_task(operation_id),
    )
    origin = loop.store.watcher("old-watcher")
    groups = loop.store.completed_watcher_groups()
    assert len(groups) == 1
    assert {item.watcher_id for item in groups[0]} == {"old-watcher", "current-watcher"}
    loop.app.state.watcher_poller.on_completed(groups[0])
    assert loop.store.completed_watcher_groups() == []
    assert len(launched) == 1
    task = loop.store.agent_task(launched[0])
    assert task.episode_id == loop.episode_id
    assert task.graph_target == target
    assert task.authorized_by == loop.store.episode(loop.episode_id).authorized_by
    assert task.native_session_id == "native-session-abc"
    assert task.stage_root == str(tmp_path / "current-stage")
    assert task.request["control_invocation"] == 2
    assert loop.store.episode(loop.episode_id).invocations_used == 2
    assert loop.store.episode(old_episode).invocations_used == 1
    retained = loop.store.watcher("old-watcher")
    assert retained.episode_id == origin.episode_id == old_episode
    assert retained.continuation == origin.continuation
    assert retained.notification_operation_id == task.operation_id
    assert loop.store.watcher("current-watcher").notification_operation_id == task.operation_id


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("old_status", ["active", "completed"])
def test_stop_retires_adopted_watchers_without_touching_later_episode(
    manifest, tmp_path, branch, old_status
):
    loop, target, old_episode = _adopted_loop(
        manifest, tmp_path, branch=branch, old_status=old_status
    )
    stopped_episode = loop.episode_id
    loop.store.request_experiment_loop_stop(
        loop.project_id, EXPERIMENT_ID, episode_id=stopped_episode, graph_target=target
    )
    assert loop.store.episode(stopped_episode).stop_settled_at is not None
    for watcher_id in ("old-watcher", "current-watcher"):
        watcher = loop.store.watcher(watcher_id)
        assert watcher.status == "stopped" and watcher.notified
        assert watcher.notification_operation_id is None
    assert loop.store.watcher("old-watcher").episode_id == old_episode
    loop.episode_id = str(uuid.uuid4())
    loop.chat_id = str(uuid.uuid4())
    loop.start_episode(operation_id="new-root")
    _retarget_episode(loop.store, loop.episode_id, target)
    assert loop.store.completed_watcher_groups() == []
    loop.arm_watcher("new-watcher", origin_operation_id="new-root", graph_target=target)
    loop.store.settle_experiment_loop_stop(
        loop.project_id, EXPERIMENT_ID, episode_id=stopped_episode, graph_target=target
    )
    assert loop.store.watcher("new-watcher").status == "active"
    assert not loop.store.watcher("new-watcher").notified


@pytest.mark.parametrize("mismatch", ["graph_target", "execution_host", "node_id"])
def test_adoption_keeps_target_and_host_preflight_checks(manifest, tmp_path, mismatch):
    loop, target, _ = _adopted_loop(manifest, tmp_path, branch=False)
    runtime = loop.store.experiment_loop_runtime_for_target(loop.project_id, EXPERIMENT_ID, target)
    episode = loop.store.experiment_episode(loop.episode_id)
    watcher = loop.store.watcher("old-watcher")
    assert preflight_episode_wake(runtime, episode, [watcher]).readiness == "ready"
    wrong = {
        "graph_target": GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4())),
        "execution_host": "another-machine.example",
        "node_id": "exp/another",
    }[mismatch]
    watcher = watcher.model_copy(update={mismatch: wrong})
    assert preflight_episode_wake(runtime, episode, [watcher]).readiness == "incompatible"


def test_stop_preserves_an_already_delivered_older_completion(manifest, tmp_path):
    loop, target, _ = _adopted_loop(manifest, tmp_path, branch=False)
    # A historical receipt already consumed by its originating episode is not
    # an observation adopted by the current loop.
    with loop.store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET notified = 1, notification_operation_id = 'loop-root' "
            "WHERE watcher_id = 'old-watcher'"
        )
    before = loop.store.watcher("old-watcher")
    loop.store.request_experiment_loop_stop(
        loop.project_id, EXPERIMENT_ID, episode_id=loop.episode_id, graph_target=target
    )
    assert loop.store.watcher("old-watcher") == before
    assert loop.store.watcher("current-watcher").status == "stopped"
