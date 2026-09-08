from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AutoResearchChildAdmissionRecord, EpisodeNotRunning

from .helpers import wait_for_task
from .test_auto_research_children_storage import _experiment_route, _experiment_task, _work_pair
from .test_background import _done_stream
from .test_branch_merge_api import (
    _candidate_for,
    _create_branch_harness,
    _episode_payload,
    _PatchWritingLauncher,
)
from .test_branch_target_storage import (
    _create_auto_episode,
    _merge_task,
    _ordinary_branch_chat,
    _store,
    _watcher,
    _worker_authority,
)


def _paused_branch_harness(manifest, tmp_path: Path):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence", ended=False)
    stage = tmp_path / "paused-orchestrator-stage"
    stage.mkdir()
    (stage / "patch.json").write_text("retained unfinished provider output", encoding="utf-8")
    harness.store.mark_agent_task_running(harness.root.operation_id)
    harness.store.checkpoint_agent_task(
        harness.root.operation_id, native_session_id="paused-session", stage_root=str(stage)
    )
    harness.store.request_agent_task_pause(harness.root.operation_id)
    harness.store.pause_agent_task(harness.root.operation_id, detail="Paused by the human.")
    return harness, stage


@pytest.mark.parametrize("provider_fails", [False, True])
def test_end_and_merge_retires_paused_episode_even_if_merge_fails(
    manifest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_fails: bool
) -> None:
    harness, stage = _paused_branch_harness(manifest, tmp_path)
    payload = _episode_payload(harness)
    assert payload["task_control"] == "resume"
    assert payload["graph_branch"]["merge_eligible"] is True
    assert payload["graph_branch"]["merge_requires_end"] is True
    budget = harness.store.episode_budget_meter(harness.episode.episode_id)
    branch_head = harness.branch.head_ref()
    watcher = _watcher(harness.store, harness.episode, harness.root, watcher_id="paused-watch")
    harness.store.create_watchers([watcher])

    async def fail_provider(*_args, **_kwargs):
        yield AgentEvent(event="error", text="The merge provider is unavailable.")

    launcher = _PatchWritingLauncher(_candidate_for("evidence"))
    monkeypatch.setattr(
        harness.app.state.launcher, "stream", fail_provider if provider_fails else launcher.stream
    )
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    )
    assert response.status_code == 202, response.text
    admitted = response.json()
    assert admitted["ending"] == "stopped"
    assert admitted["task_control"] is None
    assert admitted["graph_branch"]["merge_requires_end"] is False
    operation_id = admitted["graph_branch"]["active_merge_task_id"]
    wait_for_task(harness.store, operation_id, expect="failed" if provider_fails else "succeeded")
    episode = harness.store.episode(harness.episode.episode_id)
    assert episode.ending == "stopped"
    assert episode.wrapup_state == "skipped"
    root = harness.store.agent_task(harness.root.operation_id)
    assert root.status == "paused"
    assert not root.can_resume and not root.can_retry
    assert root.native_session_id == "paused-session"
    assert Path(root.stage_root) == stage
    assert (stage / "patch.json").read_text(
        encoding="utf-8"
    ) == "retained unfinished provider output"
    assert harness.branch.head_ref() == branch_head
    assert harness.store.episode_budget_meter(harness.episode.episode_id) == budget
    assert harness.store.watcher(watcher.watcher_id).status == "stopped"
    for action in ("resume", "retry"):
        refused = harness.client.post(
            f"/api/projects/{harness.project_id}/tasks/{harness.root.operation_id}/{action}"
        )
        assert refused.status_code == 409, refused.text


def _paused_store(tmp_path: Path):
    store = _store(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    episode, root = _create_auto_episode(store, stage_root=str(stage))
    store.mark_agent_task_running(root.operation_id)
    store.pause_agent_task(root.operation_id, detail="Paused by the human.")
    recovery = root.model_copy(
        update={
            "operation_id": str(uuid.uuid4()),
            "parent_operation_id": root.operation_id,
            "attempt": root.attempt + 1,
            "request": {**root.request, "session_id": root.native_session_id},
        }
    )
    return store, episode, root, recovery


def test_discuss_does_not_block_end_and_merge_or_gain_episode_lineage(tmp_path: Path) -> None:
    store, episode, _root, _recovery = _paused_store(tmp_path)
    chat = _ordinary_branch_chat(store, episode, status="queued")
    request = {**chat.request, "mode": "discuss"}
    chat = store.create_agent_task(
        chat.model_copy(
            update={
                "request": request,
                "dispatch_authority": resolve_dispatch_authority(
                    "project_chat", RunRequest.model_validate(request)
                ),
            }
        )
    )
    assert store.auto_research_can_end_for_merge(episode.episode_id)
    merged = store.create_branch_merge_task(_merge_task(store, episode, str(uuid.uuid4())))
    assert merged.graph_target == episode.graph_target
    assert store.agent_task(chat.operation_id).episode_id is None
    assert store.agent_task(chat.operation_id).status == "queued"
    BackgroundAgentTasks(store, _done_stream).launch_admitted(chat.operation_id)
    assert wait_for_task(store, chat.operation_id).status == "succeeded"


@pytest.mark.parametrize("winner", ["merge", "resume"])
def test_end_and_merge_serializes_with_concurrent_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    store, episode, root, recovery = _paused_store(tmp_path)
    merge = _merge_task(store, episode, str(uuid.uuid4()))
    entered = threading.Event()
    release = threading.Event()
    original_insert = store._insert_agent_task
    winner_id = merge.operation_id if winner == "merge" else recovery.operation_id

    def hold_winner(connection, record, **kwargs):
        if record.operation_id == winner_id:
            entered.set()
            assert release.wait(10), "Concurrent admission never released its transaction."
        return original_insert(connection, record, **kwargs)

    monkeypatch.setattr(store, "_insert_agent_task", hold_winner)
    admissions = {
        "merge": lambda: store.create_branch_merge_task(merge),
        "resume": lambda: store.create_auto_research_recovery_task(recovery),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(admissions[winner])
        try:
            assert entered.wait(10), "Winning admission did not enter its write transaction."
            second = pool.submit(admissions["resume" if winner == "merge" else "merge"])
        finally:
            release.set()
        assert first.result().operation_id == winner_id
        with pytest.raises((ValueError, EpisodeNotRunning)):
            second.result()
    assert (store.agent_task(merge.operation_id) is not None) == (winner == "merge")
    assert (store.agent_task(recovery.operation_id) is not None) == (winner == "resume")
    assert store.episode(episode.episode_id).ending == ("stopped" if winner == "merge" else None)
    assert store.agent_task(root.operation_id).can_resume is (winner == "resume")


def test_failed_merge_admission_rolls_back_episode_end(tmp_path: Path, monkeypatch) -> None:
    store, episode, root, _recovery = _paused_store(tmp_path)
    watcher = _watcher(store, episode, root, watcher_id="rollback-watch")
    store.create_watchers([watcher])
    merge = _merge_task(store, episode, str(uuid.uuid4()))
    original_insert = store._insert_agent_task

    def fail_insert(connection, record, **kwargs):
        if record.kind == "branch_merge":
            raise RuntimeError("injected merge admission failure")
        return original_insert(connection, record, **kwargs)

    monkeypatch.setattr(store, "_insert_agent_task", fail_insert)
    with pytest.raises(RuntimeError, match="injected merge admission failure"):
        store.create_branch_merge_task(merge)
    assert store.episode(episode.episode_id).ending is None
    assert store.episode(episode.episode_id).stop_requested_at is None
    assert store.agent_task(root.operation_id).can_resume
    assert store.watcher(watcher.watcher_id).status == "active"
    assert store.agent_task(merge.operation_id) is None
    assert not any(
        receipt.category == "auto_research_recovery_abandoned"
        for receipt in store.agent_task_receipts(root.operation_id)
    )


@pytest.mark.parametrize("child_kind", ["work", "experiment"])
@pytest.mark.parametrize("status", ["queued", "running", "pausing", "paused"])
def test_paused_parent_does_not_end_unsettled_child_work(
    tmp_path: Path, child_kind: str, status: str
) -> None:
    store, episode, root, _recovery = _paused_store(tmp_path)
    if child_kind == "work":
        route, task = _work_pair(store, episode, root, worker_id=str(uuid.uuid4()))
        store.create_auto_research_child_work(route, task)
    else:
        task = _experiment_task(
            store, str(uuid.uuid4()), episode.authorized_by, node_id="exp/child"
        )
        route = _experiment_route(store, episode, root, task)
        store.create_experiment_episode_with_invocation(task, auto_research_route=route)
    if status != "queued":
        store.mark_agent_task_running(task.operation_id)
    if status in {"pausing", "paused"}:
        store.request_agent_task_pause(task.operation_id)
    if status == "paused":
        store.pause_agent_task(task.operation_id, detail="Child has an unfinished turn.")
    assert not store.auto_research_can_end_for_merge(episode.episode_id)
    with pytest.raises(ValueError, match="not paused with all child work settled"):
        store.create_branch_merge_task(_merge_task(store, episode, str(uuid.uuid4())))
    assert store.episode(episode.episode_id).ending is None
    assert store.agent_task(task.operation_id).status == status


def test_sleeping_orchestrator_is_not_a_paused_merge_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)
    store.mark_agent_task_running(root.operation_id)
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    assert store.auto_research_is_quiescent(episode.episode_id)
    assert not store.auto_research_can_end_for_merge(episode.episode_id)
    with pytest.raises(ValueError, match="not paused with all child work settled"):
        store.create_branch_merge_task(_merge_task(store, episode, str(uuid.uuid4())))
    assert store.episode(episode.episode_id).ending is None


@pytest.mark.parametrize("worker_status", ["paused", "failed", "interrupted"])
def test_end_and_merge_retires_only_paused_same_episode_worker_turns(
    tmp_path: Path, worker_status: str
) -> None:
    store, episode, root, _recovery = _paused_store(tmp_path)
    worker_id = str(uuid.uuid4())
    worker = store.create_auto_research_agent_task(
        root.model_copy(
            update={
                "operation_id": worker_id,
                "parent_operation_id": root.operation_id,
                "request": {
                    **root.request,
                    "role": "worker",
                    "actor_operation_id": worker_id,
                    "control_node_id": "exp/worker",
                },
                "dispatch_authority": _worker_authority(episode.episode_id),
            }
        ),
        role="worker",
    )
    store.mark_agent_task_running(worker.operation_id)
    if worker_status == "paused":
        store.pause_agent_task(worker.operation_id, detail="The worker is paused too.")
    else:
        store.fail_agent_task(
            worker.operation_id, "The worker needs recovery.", status=worker_status
        )
    assert store.auto_research_can_end_for_merge(episode.episode_id) is (worker_status == "paused")
    merge = _merge_task(store, episode, str(uuid.uuid4()))
    if worker_status == "paused":
        store.create_branch_merge_task(merge)
        assert store.episode(episode.episode_id).ending == "stopped"
        assert not store.agent_task(worker.operation_id).can_resume
    else:
        with pytest.raises(ValueError, match="not paused with all child work settled"):
            store.create_branch_merge_task(merge)
        assert store.episode(episode.episode_id).ending is None


@pytest.mark.parametrize("pending_kind", ["admission", "replacement"])
def test_paused_parent_cannot_abandon_a_pending_child(tmp_path: Path, pending_kind: str) -> None:
    store, episode, root, _recovery = _paused_store(tmp_path)
    child_id = str(uuid.uuid4())
    if pending_kind == "admission":
        store.record_auto_research_child_admission(
            AutoResearchChildAdmissionRecord(
                admission_id=str(uuid.uuid4()),
                episode_id=episode.episode_id,
                project_id=episode.project_id,
                child_kind="work",
                child_id=child_id,
                state="accepted",
                created_at=store.now(),
                updated_at=store.now(),
            )
        )
    else:
        task = _experiment_task(store, child_id, episode.authorized_by, node_id="exp/replacement")
        store.reserve_auto_research_experiment_replacement(
            _experiment_route(
                store, episode, root, task, state="pending", replaces_episode_id=str(uuid.uuid4())
            )
        )
    assert not store.auto_research_can_end_for_merge(episode.episode_id)
    with pytest.raises(ValueError, match="not paused with all child work settled"):
        store.create_branch_merge_task(_merge_task(store, episode, str(uuid.uuid4())))
    assert store.episode(episode.episode_id).ending is None
    assert store.agent_task(root.operation_id).can_resume
