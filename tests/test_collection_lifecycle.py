from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.limits import AGENT_TASK_RECEIPT_RETENTION_COUNTS
from rcp.runs.provider_process import require_remote_provider_quiescence
from rcp.runs.turn_collection import can_collect
from rcp.service import RunRequest

from .test_turn_collection import _failed_turn


@pytest.mark.parametrize(
    ("status", "failure_kind"), [("paused", None), ("failed", None), ("failed", "provider_auth")]
)
def test_delivered_provider_failures_and_human_pause_keep_normal_recovery(
    tmp_path, monkeypatch, status, failure_kind
):
    store, original, pid = _failed_turn(tmp_path, status=status, failure_kind=failure_kind)
    store.finish_remote_provider_pass(original.operation_id, str(pid))
    assert not can_collect(store, original)
    assert store.uncollected_remote_task_ids() == []
    require_remote_provider_quiescence(store, original.stage_host, original.stage_root)
    tasks = BackgroundAgentTasks(store, None)
    monkeypatch.setattr(
        tasks, "collect", lambda *_args, **_kw: pytest.fail("Outcome was delivered")
    )
    monkeypatch.setattr(tasks, "_session_is_rcp_owned", lambda _record: True)
    monkeypatch.setattr(
        tasks, "_schedule_transport_retry", lambda *_args, **_kw: pytest.fail("No link loss")
    )
    continuations = []
    monkeypatch.setattr(
        tasks,
        "_create_and_spawn",
        lambda *_args, **kw: continuations.append(kw["continuation"]) or original,
    )
    tasks._auto_retry_transport_loss(original)
    tasks._rearm_owed_transport_retries()
    if status == "paused":
        tasks.resume(original.operation_id)
        assert continuations == ["resume"]
    else:
        tasks.retry(original.operation_id)
        assert continuations == ["retry"]
    tasks.shutdown(timeout=0.1)


def test_restart_interruption_is_collectible_without_a_transport_exit(tmp_path):
    store, original, _pid = _failed_turn(tmp_path, status="interrupted")
    assert can_collect(store, original)
    assert store.uncollected_remote_task_ids() == [original.operation_id]


@pytest.mark.parametrize("delivery_lost", [True, False])
def test_terminal_output_does_not_hide_journal_transport_loss(tmp_path, delivery_lost):
    store, original, _pid = _failed_turn(tmp_path)
    store.record_agent_task_receipt(
        original.operation_id,
        "provider_exit",
        {
            "return_code": 255,
            "explicit_terminal_event": True,
            "event_counts": {},
            "delivery_lost": delivery_lost,
        },
    )
    tasks = BackgroundAgentTasks(store, None)
    execution = AgentTaskExecution(
        original.operation_id, store, AgentProcessControl(), stage_host=original.stage_host
    )
    assert tasks._failure_kind(
        original.operation_id, RunRequest(provider="codex"), execution, "Transport ended"
    ) == ("transport_lost" if delivery_lost else None)
    tasks.shutdown(timeout=0.1)


def test_uncollected_completed_provider_keeps_stage_until_collection(tmp_path):
    store, original, pid = _failed_turn(tmp_path)
    store.finish_remote_provider_pass(original.operation_id, str(pid))
    assert store.unresolved_remote_provider_passes(original.stage_host, original.stage_root) == []
    with pytest.raises(ValueError, match="Collect the prior"):
        require_remote_provider_quiescence(store, original.stage_host, original.stage_root)
    stage = next(
        item for item in store.run_stage_lifecycles() if item.stage_root == str(pid.parent)
    )
    assert stage.must_exist and stage.protect_from_cleanup


def test_restart_collects_interrupted_collector_without_retry_promise(tmp_path, monkeypatch):
    store, original, _pid = _failed_turn(tmp_path)
    child = store.create_agent_task(
        original.model_copy(
            update={
                "operation_id": "collection",
                "parent_operation_id": original.operation_id,
                "status": "queued",
            }
        ),
        continuation_cause="collect",
    )
    store.fail_agent_task(child.operation_id, "Controller restarted")
    child = store.agent_task(child.operation_id)
    assert can_collect(store, child)
    assert store.uncollected_remote_task_ids() == [child.operation_id]
    tasks = BackgroundAgentTasks(store, None)
    scheduled = []
    monkeypatch.setattr(
        tasks,
        "_schedule_transport_retry",
        lambda operation_id, **kw: scheduled.append(operation_id),
    )
    tasks._rearm_owed_transport_retries()
    assert scheduled == [child.operation_id]
    tasks.shutdown(timeout=0.1)


def test_incomplete_collection_verdict_survives_receipt_retention(tmp_path):
    store, original, _pid = _failed_turn(tmp_path)
    store.record_agent_task_receipt(original.operation_id, "provider_collection_incomplete", {})
    for index in range(AGENT_TASK_RECEIPT_RETENTION_COUNTS["summary"] + 1):
        store.record_agent_task_receipt(original.operation_id, "ordinary", {"index": index})
    store.prune_operational_storage(now=datetime.now(UTC) + timedelta(days=3650))
    assert store.agent_task_has_receipt(original.operation_id, "provider_collection_incomplete")
    assert not can_collect(store, store.agent_task(original.operation_id))
    assert store.uncollected_remote_task_ids() == []
