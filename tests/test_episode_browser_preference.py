from __future__ import annotations

import uuid

import pytest

from rcp.runs.auto_research_admission import resume_auto_research_child_work
from rcp.runs.auto_research_delivery import (
    deliver_pending_auto_research_mail,
    record_auto_research_message,
)
from rcp.runs.experiment_loop import experiment_watcher_delivery_request
from rcp.storage import AppStore

from .helpers import wait_for_task
from .test_auto_research_child_work_watchers import _deliver, _waiting_child
from .test_experiment_episode_storage import _bind, _task


@pytest.mark.parametrize("path", ["watcher", "queued_message", "episode_resume"])
@pytest.mark.parametrize("requested", [False, True])
def test_child_continuation_browser_matrix(tmp_path, path, requested):
    tasks, episode, child, watchers, _ = _waiting_child(tmp_path, browser_requested=requested)
    assert episode.browser_requested is requested
    assert child.request["browser_requested"] is requested
    assert watchers[0].continuation.browser_requested is requested
    if path == "watcher":
        next_task = _deliver(tasks, episode, watchers)
        assert next_task is not None
        operation_id = next_task.operation_id
    elif path == "queued_message":
        record_auto_research_message(
            tasks.store,
            episode_id=episode.episode_id,
            sender_role="orchestrator",
            sender_task_id=episode.root_operation_id,
            authorized_by=None,
            recipient_task_id=watchers[0].worker_id,
            body="Continue the work.",
        )
        operation_id = deliver_pending_auto_research_mail(
            tasks, episode_id=episode.episode_id, recipient_task_id=watchers[0].worker_id
        )
        assert operation_id is not None
    else:
        with tasks.store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status = 'failed', error = 'interrupted' WHERE operation_id = ?",
                (child.operation_id,),
            )
        # A child turn the orchestrator started recovers through its episode
        # route; generic Retry would make it human-owned and is refused.
        resumed = resume_auto_research_child_work(tasks, episode.episode_id, watchers[0].worker_id)
        assert resumed.task is not None, resumed.reason
        operation_id = resumed.task.operation_id
    continued = wait_for_task(tasks.store, operation_id, expect="succeeded")
    assert continued.request["browser_requested"] is requested
    assert continued.stage_root == child.stage_root


def test_experiment_browser_roundtrip_and_bounded_wake(tmp_path):
    from .test_watchers import _record

    store = AppStore(tmp_path / "store.sqlite3")
    episode_id = str(uuid.uuid4())
    root = _task(store, "root", episode_id)
    root = root.model_copy(update={"request": {**root.request, "browser_requested": True}})
    root = store.create_experiment_episode_with_invocation(root)
    _bind(store, episode_id, root.operation_id, invocation=1)
    assert store.episode(episode_id).browser_requested is True
    assert store.experiment_episode(episode_id).browser_requested is True
    from rcp.storage import WatcherContinuation

    continuation = WatcherContinuation.model_validate(
        {
            key: value
            for key, value in root.request.items()
            if key in WatcherContinuation.model_fields
        }
    )
    watcher = _record("watch", origin=root.operation_id).model_copy(
        update={"continuation": continuation}
    )
    request = experiment_watcher_delivery_request(
        [watcher],
        trigger="watcher",
        episode_id=episode_id,
        invocation=2,
        invocation_ceiling=2,
        control_revision=1,
        decision_bundle=[],
        completion_criteria=[],
        session_id="native-session",
    )
    assert request.browser_requested is True
