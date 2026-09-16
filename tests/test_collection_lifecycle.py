from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rcp.background import BackgroundAgentTasks
from rcp.limits import AGENT_TASK_RECEIPT_RETENTION_COUNTS
from rcp.runs.provider_process import require_remote_provider_quiescence
from rcp.runs.turn_collection import can_collect

from .test_turn_collection import _failed_turn


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
