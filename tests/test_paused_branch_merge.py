"""Merge is decided on branch facts; the episode's turns are neither a condition nor a casualty.

A paused, waiting, failed, or interrupted turn is not writing to the branch, so it
does not block a merge, and the merge does not end its episode. Only a queued,
running, or pausing graph-capable task is a writer, and it is named.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.runs import branch_merge as merge_module
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.runs.shared import _sse
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AutoResearchChildAdmissionRecord

from .helpers import wait_for_task
from .test_auto_research_children_storage import (
    _experiment_route,
    _experiment_task,
    _insert_cancelled_legacy_route,
    _work_pair,
)
from .test_background import _done_stream
from .test_branch_merge import _field, _Run
from .test_branch_target_storage import (
    _create_auto_episode,
    _merge_task,
    _ordinary_branch_chat,
    _store,
    _watcher,
    _worker_authority,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_merge_over_a_paused_orchestrator_leaves_the_episode_exactly_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_fails: bool
) -> None:
    store, episode, root, _recovery = _paused_store(tmp_path)
    stage = Path(root.stage_root)
    retained = stage / "patch.json"
    retained.write_bytes(b"retained unfinished provider output")
    watcher = _watcher(store, episode, root, watcher_id="paused-watch")
    store.create_watchers([watcher])
    before_episode = store.episode(episode.episode_id)
    before_root = store.agent_task(root.operation_id)
    before_budget = store.episode_budget_meter(episode.episode_id)
    before_watcher = store.watcher(watcher.watcher_id)
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),) if provider_fails else ())
    if provider_fails:

        def failed_provider(*args, **kwargs):
            outcome, frames = run.provider(*args, **kwargs)
            outcome.failed = True
            return outcome, frames

        monkeypatch.setattr(merge_module, "_provider_turn", failed_provider)

    async def stream(_project_id, _kind, _request, _execution):
        for frame in await run.run():
            yield frame
        yield _sse(AgentEvent(event="done"))

    request = BranchMergeRunRequest(
        episode_id=episode.episode_id,
        mode="work",
        chat_scope="project",
        provider="codex",
        model="",
        reasoning="medium",
        run_on="local",
        run_truth_scope=["repo-a"],
    )
    merge = store.create_branch_merge_task(
        _merge_task(store, episode, str(uuid.uuid4())).model_copy(
            update={"request": request.model_dump(mode="json")}
        )
    )
    tasks = BackgroundAgentTasks(store, stream)
    tasks.launch_admitted(merge.operation_id)
    await asyncio.to_thread(
        wait_for_task,
        store,
        merge.operation_id,
        expect="failed" if provider_fails else "succeeded",
    )

    assert run.outcome.status == ("rejected" if provider_fails else "committed")
    assert len(run.commits) == (0 if provider_fails else 1)
    assert store.episode(episode.episode_id) == before_episode
    assert store.agent_task(root.operation_id) == before_root
    assert store.episode_budget_meter(episode.episode_id) == before_budget
    assert store.watcher(watcher.watcher_id) == before_watcher
    assert retained.read_bytes() == b"retained unfinished provider output"


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


def test_discuss_does_not_block_merge_or_gain_episode_lineage(tmp_path: Path) -> None:
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
    merged = store.create_branch_merge_task(_merge_task(store, episode, str(uuid.uuid4())))
    assert merged.graph_target == episode.graph_target
    assert store.episode(episode.episode_id).ending is None
    assert store.agent_task(chat.operation_id).episode_id is None
    assert store.agent_task(chat.operation_id).status == "queued"
    BackgroundAgentTasks(store, _done_stream).launch_admitted(chat.operation_id)
    assert wait_for_task(store, chat.operation_id).status == "succeeded"


def test_a_resumed_turn_becomes_a_named_writer_that_blocks_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume and merge admissions serialize on the store; the resumed turn wins."""

    store, episode, root, recovery = _paused_store(tmp_path)
    merge = _merge_task(store, episode, str(uuid.uuid4()))
    entered = threading.Event()
    release = threading.Event()
    original_insert = store._insert_agent_task

    def hold_resume(connection, record, **kwargs):
        if record.operation_id == recovery.operation_id:
            entered.set()
            assert release.wait(10), "Concurrent admission never released its transaction."
        return original_insert(connection, record, **kwargs)

    monkeypatch.setattr(store, "_insert_agent_task", hold_resume)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.create_auto_research_recovery_task, recovery)
        try:
            assert entered.wait(10), "Resume admission did not enter its write transaction."
            second = pool.submit(store.create_branch_merge_task, merge)
        finally:
            release.set()
        assert first.result().operation_id == recovery.operation_id
        with pytest.raises(ValueError, match="merge_source_has_live_writer"):
            second.result()
    assert store.agent_task(merge.operation_id) is None
    assert store.agent_task(recovery.operation_id).status == "queued"
    assert store.episode(episode.episode_id).ending is None


@pytest.mark.parametrize("child_kind", ["work", "experiment"])
@pytest.mark.parametrize("status", ["queued", "running", "pausing", "paused"])
def test_only_a_live_child_turn_blocks_the_merge(
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
    merge = _merge_task(store, episode, str(uuid.uuid4()))
    if status == "paused":
        assert store.create_branch_merge_task(merge).kind == "branch_merge"
    else:
        with pytest.raises(ValueError, match="merge_source_has_live_writer"):
            store.create_branch_merge_task(merge)
        assert store.agent_task(merge.operation_id) is None
    assert store.episode(episode.episode_id).ending is None
    assert store.agent_task(task.operation_id).status == status


def test_a_waiting_orchestrator_does_not_block_the_merge(tmp_path: Path) -> None:
    store = _store(tmp_path)
    episode, root = _create_auto_episode(store)
    store.mark_agent_task_running(root.operation_id)
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    assert store.auto_research_is_quiescent(episode.episode_id)
    merged = store.create_branch_merge_task(_merge_task(store, episode, str(uuid.uuid4())))
    assert merged.graph_target == episode.graph_target
    stored = store.episode(episode.episode_id)
    assert stored.ending is None
    assert stored.status == episode.status


@pytest.mark.parametrize("worker_status", ["paused", "failed", "interrupted"])
def test_a_settled_worker_turn_neither_blocks_nor_is_retired_by_the_merge(
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
    before = store.agent_task(worker.operation_id)
    store.create_branch_merge_task(_merge_task(store, episode, str(uuid.uuid4())))
    assert store.episode(episode.episode_id).ending is None
    after = store.agent_task(worker.operation_id)
    assert after.status == worker_status
    assert after.can_resume == before.can_resume
    assert after.can_retry == before.can_retry
    assert store.agent_task(root.operation_id).can_resume


@pytest.mark.parametrize("pending_kind", ["admission", "cancelled_legacy_route"])
def test_a_pending_child_without_a_task_is_not_a_writer(tmp_path: Path, pending_kind: str) -> None:
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
        _insert_cancelled_legacy_route(
            store,
            _experiment_route(
                store, episode, root, task, state="pending", replaces_episode_id=str(uuid.uuid4())
            ),
        )
    store.create_branch_merge_task(_merge_task(store, episode, str(uuid.uuid4())))
    assert store.episode(episode.episode_id).ending is None
    assert store.agent_task(root.operation_id).can_resume
