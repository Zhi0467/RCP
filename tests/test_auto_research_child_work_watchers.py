from __future__ import annotations

import hashlib
import json

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.runs.auto_research import project_auto_research_episode, request_auto_research_stop
from rcp.runs.auto_research_admission import (
    reconcile_committed_auto_research_dispatches,
    start_auto_research_child_work,
)
from rcp.runs.auto_research_delivery import (
    deliver_auto_research_watchers,
    deliver_pending_auto_research_mail,
    record_auto_research_message,
)
from rcp.service import RunRequest

from .helpers import wait_for_task
from .test_auto_research_child_work_watchers_storage import armed_watcher
from .test_auto_research_delivery import _sse, _start_auto_research, _store


def _waiting_child(tmp_path, *, ceiling=6, browser_requested=False):
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
        execution.checkpoint_stage("backend", str(stage))
        yield _sse(AgentEvent(event="session", session_id=request.session_id or kind + "-session"))
        yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    episode, root = _start_auto_research(
        tasks, invocation_ceiling=ceiling, browser_requested=browser_requested
    )
    worker_id = "00000000-0000-4000-8000-000000000451"
    instruction = "Inspect the result."
    child = start_auto_research_child_work(
        tasks,
        episode.episode_id,
        RunRequest(
            browser_requested=not browser_requested,
            provider="codex",
            run_on="backend",
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
    watchers = [
        armed_watcher(
            store,
            watcher_id,
            agent_id=worker_id,
            origin_turn_id=child.operation_id,
            project_id=episode.project_id,
            log_path=f"/tmp/{watcher_id}.log",
        )
        for watcher_id in ("child-shell", "child-helper")
    ]
    return tasks, episode, child, watchers, seen


def _deliver(tasks, episode, watchers):
    store = tasks.store
    current = [store.watcher(item.watcher_id) for item in watchers]
    deliver_auto_research_watchers(tasks, current[0].agent_id, episode, current, None)
    wake_id = store.watcher(current[0].watcher_id).wake_turn_id
    return store.agent_task(wake_id) if wake_id is not None else None


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
        store.auto_research_child_work(watchers[0].agent_id).current_operation_id
        == wake.operation_id
    )
    assert wake.request["watcher_ids"] == [item.watcher_id for item in watchers]
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before
    payload = wake.request["message"]
    for watcher in watchers:
        assert watcher.watcher_id in payload
        assert watcher.checks[0].log_path in payload
    deliver_auto_research_watchers(tasks, watchers[0].agent_id, episode, watchers, None)
    assert store.auto_research_child_work(watchers[0].agent_id).current_operation_id == (
        wake.operation_id
    )
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before
    assert [item[1:3] for item in seen] == [
        ("auto_research", "fresh"),
        ("node_chat", "fresh"),
        ("node_chat", "watcher_wake"),
    ]


@pytest.mark.parametrize("fence", ["route_stop", "episode_stop"])
def test_child_watcher_completion_stays_unclaimed_when_fenced(tmp_path, fence):
    tasks, episode, child, watchers, _seen = _waiting_child(tmp_path, ceiling=6)
    store = tasks.store
    before = store.episode_budget_meter(episode.episode_id).invocations_used
    if fence == "route_stop":
        store.request_auto_research_child_work_stop(watchers[0].agent_id)
    elif fence == "episode_stop":
        request_auto_research_stop(store, episode.episode_id)
    for watcher in watchers:
        assert store.watcher(watcher.watcher_id).state == "cancelled"
    deliver_auto_research_watchers(tasks, watchers[0].agent_id, episode, watchers, None)
    assert store.episode_budget_meter(episode.episode_id).invocations_used == before
    assert (
        store.auto_research_child_work(watchers[0].agent_id).current_operation_id
        == child.operation_id
    )


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
    wake_id = tasks.store.watcher(watchers[0].watcher_id).wake_turn_id
    assert reconcile_committed_auto_research_dispatches(tasks, episode_id=episode.episode_id) == [
        wake_id
    ]
    wake = wait_for_task(tasks.store, wake_id, expect="succeeded")
    assert wake.native_session_id == child.native_session_id
    assert tasks.store.episode_budget_meter(episode.episode_id).invocations_used == before
    assert sum(item[0] == wake_id for item in seen) == 1


@pytest.mark.parametrize("wake_kind", ["watcher", "message"])
def test_child_automatic_wakes_do_not_reinvoke_unavailable_old_writer_skills(
    manifest, tmp_path, monkeypatch, wake_kind
):
    from rcp.skill_registry import SkillDefaults
    from tests.helpers import create_named_app
    from tests.test_api import _persist_skill_defaults

    tasks, episode, child, watchers, seen = _waiting_child(tmp_path)
    service = create_named_app(str(manifest.path), data_dir=tmp_path / "catalog").state.service
    _persist_skill_defaults(service, SkillDefaults(skill_ids=["evidence-triage"]))
    invocations = {
        "invoked_workflow_ids": ["research-graph-audit"],
        "invoked_skill_ids": ["graph-audit"],
        "invoked_provider_skill_names": ["native-review"],
        "resolved_provider_skills": [
            {
                "provider": "codex",
                "machine": "backend",
                "provider_version": "old",
                "inventory_hash": "old",
                "name": "native-review",
                "label": "Native review",
                "description": "Earlier turn only",
            }
        ],
    }
    old_request = {**child.request, **invocations}
    with tasks.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id=?",
            (json.dumps(old_request), child.operation_id),
        )
    with pytest.raises(ValueError):
        service.resolve_skill_request(RunRequest.model_validate(old_request))
    admitted = []

    def admit(_project, request, **kwargs):
        admitted.append(service.resolve_skill_request(request))

    monkeypatch.setattr(tasks, "admit_provider_task", admit)
    if wake_kind == "watcher":
        wake = _deliver(tasks, episode, watchers)
        assert wake is not None
        wake_id = wake.operation_id
    else:
        record_auto_research_message(
            tasks.store,
            episode_id=episode.episode_id,
            sender_role="orchestrator",
            sender_task_id=episode.root_operation_id,
            authorized_by=None,
            recipient_task_id=watchers[0].agent_id,
            body="Inspect the current result.",
        )
        wake_id = deliver_pending_auto_research_mail(
            tasks, episode_id=episode.episode_id, recipient_task_id=watchers[0].agent_id
        )
    assert wake_id is not None
    wake = wait_for_task(tasks.store, wake_id, expect="succeeded")
    assert wake.native_session_id == child.native_session_id
    assert wake.stage_root == child.stage_root and wake.graph_target == child.graph_target
    assert wake.dispatch_authority == child.dispatch_authority
    for field, value in invocations.items():
        assert wake.request[field] == []
        assert getattr(admitted[0], field) == []
        assert tasks.store.agent_task(child.operation_id).request[field] == value
    assert admitted[0].skill_ids == ["evidence-triage"]
    assert sum(item[0] == wake_id for item in seen) == 1


def test_targeted_worker_status_reports_its_watcher_wait(tmp_path):
    from rcp.runs.auto_research_effects import _worker_status

    tasks, episode, child, watchers, _seen = _waiting_child(tmp_path)
    store = tasks.store
    route = store.auto_research_child_works(episode.episode_id)[0]
    leaf = store.agent_task(route.current_operation_id)
    assert leaf.status == "succeeded"
    result = _worker_status(store, route, leaf)
    assert result["status"] == "waiting"
