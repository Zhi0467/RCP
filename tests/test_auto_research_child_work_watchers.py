from __future__ import annotations

import hashlib

import pytest

from rcp.agents import AgentEvent
from rcp.api.app import _generic_watcher_delivery_request
from rcp.background import BackgroundAgentTasks
from rcp.runs.auto_research import project_auto_research_episode, request_auto_research_stop
from rcp.runs.auto_research_admission import (
    reconcile_committed_auto_research_dispatches,
    start_auto_research_child_work,
)
from rcp.runs.auto_research_delivery import (
    deliver_pending_auto_research_mail,
    record_auto_research_message,
)
from rcp.runs.watcher_admission import start_watcher_notification
from rcp.service import RunRequest
from rcp.storage import WatcherContinuation

from .helpers import wait_for_task
from .test_auto_research_delivery import _sse, _start_auto_research, _store
from .test_compute_jobs_storage import job_record
from .test_watchers import _record


def _waiting_child(tmp_path, *, ceiling=6):
    store = _store(tmp_path)
    seen = []

    async def stream(_project_id, kind, request, execution):
        seen.append(
            (
                execution.operation_id,
                kind,
                execution.continuation,
                getattr(request, "message", None),
            )
        )
        stage = tmp_path / kind
        stage.mkdir(exist_ok=True)
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id=request.session_id or kind + "-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    episode, root = _start_auto_research(tasks, invocation_ceiling=ceiling)
    worker_id = "00000000-0000-4000-8000-000000000451"
    instruction = "Inspect the result."
    child = start_auto_research_child_work(
        tasks,
        episode.episode_id,
        RunRequest(
            provider="codex",
            run_on="local",
            run_truth_scope=["repo-a"],
            chat_scope="node",
            node_id="blk/result",
            chat_id=worker_id,
            message=instruction,
            mode="work",
            trigger="orchestrator",
            patch_kind="work",
        ),
        admitted_by_operation_id=root.operation_id,
        worker_id=worker_id,
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    child = wait_for_task(store, child.operation_id, expect="succeeded")
    continuation_data = {
        key: child.request[key] for key in WatcherContinuation.model_fields if key in child.request
    }
    for key in ("workflow_ids", "skill_ids", "resolved_skill_packages"):
        continuation_data[key] = continuation_data.get(key) or []
    continuation = WatcherContinuation.model_validate(continuation_data)
    shell = _record("child-shell", origin=child.operation_id, status="completed").model_copy(
        update={
            "episode_id": episode.episode_id,
            "continuation": continuation,
            "worker_id": worker_id,
            "graph_target": episode.graph_target,
            "chat_id": worker_id,
            "node_id": "blk/result",
        }
    )
    job = job_record(
        origin_operation_id=child.operation_id,
        episode_id=episode.episode_id,
        status="exited",
        exit_status=3,
        started_at="2026-09-06T00:00:00Z",
        ended_at="2026-09-06T00:00:05Z",
    )
    store.create_compute_job(job)
    observer = shell.model_copy(
        update={
            "watcher_id": "child-job",
            "job_id": job.job_id,
            "check_command": None,
            "log_path": None,
            "cwd": None,
        }
    )
    watchers = store.create_watchers([shell, observer])
    return tasks, episode, child, watchers, seen


def _deliver(tasks, episode, watchers):
    request = _generic_watcher_delivery_request(watchers, store=tasks.store)
    return start_watcher_notification(
        tasks,
        episode.project_id,
        "node_chat",
        request,
        [item.watcher_id for item in watchers],
        authorized_by=episode.authorized_by,
    )


def test_child_watcher_wake_preserves_route_session_payload_and_spends_once(tmp_path):
    tasks, episode, child, watchers, seen = _waiting_child(tmp_path)
    store = tasks.store
    before = store.episode_budget_meter(episode.episode_id).invocations_used
    assert project_auto_research_episode(store, episode.episode_id).work[0]["status"] == "waiting"

    wake = _deliver(tasks, episode, watchers)
    assert wake is not None
    wake = wait_for_task(store, wake.operation_id, expect="succeeded")
    assert wake.native_session_id == child.native_session_id
    assert wake.stage_root == child.stage_root
    assert wake.parent_operation_id == child.operation_id
    assert wake.graph_target == child.graph_target
    assert wake.request["trigger"] == "orchestrator"
    assert store.agent_task_continuation_cause(wake.operation_id) == "watcher_wake"
    assert (
        store.auto_research_child_work(watchers[0].worker_id).current_operation_id
        == wake.operation_id
    )
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before + 1
    payload = wake.request["message"]
    for field in (
        '"job_id": "job-1"',
        '"exit_status": 3',
        '"duration_seconds": 5.0',
        '"log_path": "/jobs/job-1/log"',
        '"backend_id": "systemd_user"',
        '"started_at"',
        '"ended_at"',
        "/tmp/child-shell.log",
    ):
        assert field in payload
    assert _deliver(tasks, episode, watchers) is None
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before + 1
    assert [item[1:3] for item in seen] == [
        ("auto_research", "fresh"),
        ("node_chat", "fresh"),
        ("node_chat", "watcher_wake"),
    ]


@pytest.mark.parametrize("fence", ["budget", "route_stop", "episode_stop"])
def test_child_watcher_completion_stays_unclaimed_when_fenced(tmp_path, fence):
    tasks, episode, child, watchers, _seen = _waiting_child(
        tmp_path, ceiling=2 if fence == "budget" else 6
    )
    store = tasks.store
    before = store.episode_budget_meter(episode.episode_id).invocations_used
    if fence == "route_stop":
        store.request_auto_research_child_work_stop(watchers[0].worker_id)
    elif fence == "episode_stop":
        request_auto_research_stop(store, episode.episode_id)
    assert _deliver(tasks, episode, watchers) is None
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before
    assert (
        store.auto_research_child_work(watchers[0].worker_id).current_operation_id
        == child.operation_id
    )
    for watcher in watchers:
        retained = store.watcher(watcher.watcher_id)
        assert retained.notification_operation_id is None
        if fence == "budget":
            assert retained.status == "completed" and not retained.notified
        else:
            assert retained.status == "stopped" and retained.notified
    assert store.compute_job("job-1").cancel_requested_at is None


def test_child_watcher_wake_reconciles_paid_dispatch_after_crash(tmp_path, monkeypatch):
    tasks, episode, child, watchers, seen = _waiting_child(tmp_path)
    before = tasks.store.episode_budget_meter(episode.episode_id).invocations_used
    original = tasks._spawn_record

    def crash(*_args, **_kwargs):
        raise RuntimeError("crash after child watcher claim")

    monkeypatch.setattr(tasks, "_spawn_record", crash)
    with pytest.raises(RuntimeError, match="crash after child watcher claim"):
        _deliver(tasks, episode, watchers)
    monkeypatch.setattr(tasks, "_spawn_record", original)
    wake_id = tasks.store.watcher(watchers[0].watcher_id).notification_operation_id
    assert reconcile_committed_auto_research_dispatches(tasks, episode_id=episode.episode_id) == [
        wake_id
    ]
    wake = wait_for_task(tasks.store, wake_id, expect="succeeded")
    assert wake.native_session_id == child.native_session_id
    assert tasks.store.episode_budget_meter(episode.episode_id).invocations_used == before + 1
    assert sum(item[0] == wake_id for item in seen) == 1


def test_child_watcher_wake_uses_resolved_policy_and_keeps_invoked_skills(tmp_path):
    tasks, episode, _child, watchers, _seen = _waiting_child(tmp_path)
    resolved = watchers[0].continuation.model_copy(
        update={
            "model": "resolved-model",
            "reasoning": "medium",
            "invoked_skill_ids": ["explicit-skill"],
            "invoked_workflow_ids": ["explicit-workflow"],
        }
    )
    # These are the values settlement stores after resolving a turn's defaults.
    with tasks.store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET continuation_json = ?", (resolved.model_dump_json(),)
        )
    watchers = [tasks.store.watcher(item.watcher_id) for item in watchers]
    wake = _deliver(tasks, episode, watchers)
    assert wake is not None
    wake = wait_for_task(tasks.store, wake.operation_id, expect="succeeded")
    assert wake.request["model"] == "resolved-model"
    assert wake.request["reasoning"] == "medium"
    assert wake.request["invoked_skill_ids"] == ["explicit-skill"]
    assert wake.request["invoked_workflow_ids"] == ["explicit-workflow"]

    record_auto_research_message(
        tasks.store,
        episode_id=episode.episode_id,
        sender_role="orchestrator",
        sender_task_id=episode.root_operation_id,
        authorized_by=None,
        recipient_task_id=watchers[0].worker_id,
        body="Continue with the same resolved policy.",
    )
    next_id = deliver_pending_auto_research_mail(
        tasks,
        episode_id=episode.episode_id,
        recipient_task_id=watchers[0].worker_id,
    )
    assert next_id is not None
    next_task = wait_for_task(tasks.store, next_id, expect="succeeded")
    for field in ("model", "reasoning", "invoked_skill_ids", "invoked_workflow_ids"):
        assert next_task.request[field] == wake.request[field]
