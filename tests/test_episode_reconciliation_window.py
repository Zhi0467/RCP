from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent
from rcp.runs.auto_research import auto_research_exhaustion_signal
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, EpisodeRecord
from rcp.watchers import WatcherPoller

from .helpers import create_named_app, wait_for_task
from .test_auto_research_children_storage import _experiment_route
from .test_auto_research_delivery import _sse
from .test_episode_lifecycle_acceptance import _add_worker_seat


def _deferred_parent_outside_recent_window(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data", acceptance_agent=True)
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    _add_worker_seat(
        app,
        node_id="exp/deferred",
        title="Deferred child",
        objective="Settle the child before the parent report starts.",
    )

    async def root_stream(_project_id, _kind, request, execution):
        execution.checkpoint_stage("", str(tmp_path))
        yield _sse(AgentEvent(event="session", session_id=request.session_id or "root-session"))
        yield _sse(AgentEvent(event="done"))

    app.state.background_tasks.stream = root_stream
    # Shut down the real root's runtime before arranging the deferred recovery.
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/episodes",
            json={"mode": "auto_research", "invocation_ceiling": 6},
        )
        assert response.status_code == 202, response.text
        parent = store.episode(response.json()["episode_id"])
        root = wait_for_task(store, parent.root_operation_id, expect="succeeded")
    child_id = str(uuid.uuid4())
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_scope="node",
        chat_id=child_id,
        node_id="exp/deferred",
        message="Finish the already-admitted child.",
        mode="work",
        trigger="orchestrator",
        patch_kind="experiment_loop",
        control_node_id="exp/deferred",
        control_revision=1,
        control_episode_id=child_id,
        control_invocation=1,
        control_invocation_ceiling=10,
        control_completion_criteria=["The bounded child finishes."],
        session_id="child-session",
    )
    now = store.now()
    child = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=project_id,
        episode_id=child_id,
        graph_target=parent.graph_target,
        kind="node_chat",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="Saved child turn.",
        native_session_id=request.session_id,
        stage_root=str(tmp_path),
        authorized_by=parent.authorized_by,
    )
    store.create_experiment_episode_with_invocation(
        child, auto_research_route=_experiment_route(store, parent, root, child)
    )
    store.record_agent_task_contract(
        child.operation_id,
        "experiment_episode_context_candidate",
        "{}",
        hashlib.sha256(b"{}").hexdigest(),
    )
    store.pause_agent_task(child.operation_id, detail="Saved child checkpoint.")
    auto_research_exhaustion_signal(store, parent.episode_id)
    for index in range(51):
        now = store.now()
        newer = EpisodeRecord(
            episode_id=str(uuid.uuid4()),
            project_id=project_id,
            mode="experiment_loop",
            control_node_id=f"exp/newer-{index}",
            status="queued",
            invocation_ceiling=1,
            authorized_by=parent.authorized_by,
            created_at=now,
            updated_at=now,
        )
        store.create_episode(newer)
        store.end_episode_without_report(
            newer.episode_id, ending="failed", diagnostic="No provider session was started."
        )
    assert parent.episode_id not in {item.episode_id for item in store.episodes(project_id)}
    assert len(store.episodes(project_id)) == 50
    assert len(store.episodes(project_id, limit=None)) == 53
    assert store.episode_wrapup(parent.episode_id) is None
    return app, parent, child


def _capture_report_launches(app, monkeypatch):
    launches = []

    def launch(operation_id):
        task = app.state.background_tasks.store.agent_task(operation_id)
        assert task.kind == "episode_report" and not task.visible
        launches.append(operation_id)
        return task

    monkeypatch.setattr(app.state.background_tasks, "launch_admitted", launch)
    return launches


def _finish_child_recovery(store, child):
    now = store.now()
    recovery = child.model_copy(
        update={
            "operation_id": str(uuid.uuid4()),
            "parent_operation_id": child.operation_id,
            "attempt": 2,
            "created_at": now,
            "updated_at": now,
        }
    )
    store.create_experiment_recovery_task(recovery)
    store.complete_agent_task(recovery.operation_id, applied_revision=None, result={})


def _assert_report_allocated(store, parent, launches):
    wrapup = store.episode_wrapup(parent.episode_id)
    assert wrapup is not None and wrapup.state == "pending"
    assert store.episode(parent.episode_id).wrapup_state == "pending"
    task = store.agent_task(wrapup.allocation_operation_id)
    assert task.kind == "episode_report" and not task.visible
    assert task.native_session_id == "root-session"
    assert launches == [task.operation_id]


def test_watcher_poll_reconciles_deferred_parent_older_than_recent_window(
    manifest, tmp_path, monkeypatch
):
    app, parent, child = _deferred_parent_outside_recent_window(manifest, tmp_path)
    store = app.state.background_tasks.store
    launches = _capture_report_launches(app, monkeypatch)

    app.state.watcher_poller.poll_once()
    assert store.episode_wrapup(parent.episode_id) is None
    assert not launches
    _finish_child_recovery(store, child)

    app.state.watcher_poller.poll_once()

    _assert_report_allocated(store, parent, launches)


@pytest.mark.parametrize("settled_before_restart", [False, True])
def test_startup_reconciles_deferred_parent_older_than_recent_window(
    manifest, tmp_path, monkeypatch, settled_before_restart
):
    first_app, parent, child = _deferred_parent_outside_recent_window(manifest, tmp_path)
    store = first_app.state.background_tasks.store
    if settled_before_restart:
        _finish_child_recovery(store, child)
    restarted = create_named_app(
        str(manifest.path), data_dir=tmp_path / "data", acceptance_agent=True
    )
    launches = _capture_report_launches(restarted, monkeypatch)
    # A background poll must not hide a missed startup reconciliation pass.
    monkeypatch.setattr(WatcherPoller, "start", lambda _self: None)

    with TestClient(restarted):
        if not settled_before_restart:
            assert store.episode_wrapup(parent.episode_id) is None
            assert not launches
            _finish_child_recovery(store, child)
            restarted.state.watcher_poller.poll_once()
        _assert_report_allocated(store, parent, launches)
