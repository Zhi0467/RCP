from __future__ import annotations

from datetime import datetime, timedelta

from rcp.agents import AgentProcessControl
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.runs.provider_login import settle_provider_login
from rcp.storage import AppStore

from .test_provider_login_lifecycle import _turn, _unused_stream
from .test_provider_login_resolver import _login
from .test_provider_login_resolver import accounts as accounts


def test_snapshot_reset_is_copied_to_login_and_only_stored_in_reset_receipt(accounts):
    store = accounts.store
    key = _login(accounts, "codex")
    record = _turn(accounts, key)
    assert record is not None
    failed_at = datetime.fromisoformat(store.now())
    resets_at = failed_at + timedelta(hours=2)
    snapshot: dict[str, object] = {
        "primary": {"usedPercent": 100, "resetsAt": int(resets_at.timestamp())},
        "secondary": {"usedPercent": 15, "resetsAt": int(resets_at.timestamp()) + 3600},
    }
    store.record_agent_task_receipt(
        record.operation_id, "provider_terminal_error", {"classification": "session_limit"}
    )
    before = store.agent_task_receipts(record.operation_id)
    execution = AgentTaskExecution(
        record.operation_id, store, AgentProcessControl(), rate_limit=snapshot
    )
    background = BackgroundAgentTasks(store, _unused_stream)
    settle_provider_login(background, record, execution=execution)
    after = store.agent_task_receipts(record.operation_id)
    added = [receipt for receipt in after if receipt not in before]
    assert len(added) == 1
    assert added[0].category == "provider_limit_reset"
    assert added[0].payload["snapshot"] == snapshot
    assert added[0].payload["estimated"] is False
    assert datetime.fromisoformat(str(added[0].payload["resets_at"])) == resets_at
    assert (
        datetime.fromisoformat(store.provider_login_assignment(key).limit_resets_at or "")
        == resets_at
    )
    stored = store.agent_task(record.operation_id)
    assert stored is not None and "rate_limit" not in stored.model_dump()


def test_error_duration_uses_failure_clock(accounts):
    store = accounts.store
    key = _login(accounts, "agy")
    record = _turn(accounts, key)
    assert record is not None
    failed_at = datetime.fromisoformat(store.now()) - timedelta(minutes=7)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET finished_at=?, error=? WHERE operation_id=?",
            (
                failed_at.isoformat(),
                "Individual quota reached. Resets in 2h57m1s.",
                record.operation_id,
            ),
        )
    settle_provider_login(BackgroundAgentTasks(store, _unused_stream), record)
    receipt = next(
        receipt
        for receipt in store.agent_task_receipts(record.operation_id)
        if receipt.category == "provider_limit_reset"
    )
    assert datetime.fromisoformat(str(receipt.payload["resets_at"])) == failed_at + timedelta(
        hours=2, minutes=57, seconds=1
    )
    assert receipt.payload["estimated"] is False
    assert receipt.payload["snapshot"] is None


def test_settlement_is_idempotent_across_restart_without_memory_snapshot(accounts):
    store = accounts.store
    key = _login(accounts, "codex")
    record = _turn(accounts, key)
    assert record is not None
    resets_at = datetime.fromisoformat(store.now()) + timedelta(hours=1)
    execution = AgentTaskExecution(
        record.operation_id,
        store,
        AgentProcessControl(),
        rate_limit={"primary": {"usedPercent": 100, "resetsAt": int(resets_at.timestamp())}},
    )
    store.record_agent_task_receipt(
        record.operation_id, "provider_terminal_error", {"classification": "session_limit"}
    )
    background = BackgroundAgentTasks(store, _unused_stream)
    settle_provider_login(background, record, execution=execution)
    original = store.agent_task_receipts(record.operation_id)
    settle_provider_login(background, record)
    reopened = AppStore(store.path)
    settle_provider_login(BackgroundAgentTasks(reopened, _unused_stream), record)
    assert reopened.agent_task_receipts(record.operation_id) == original
    assert (
        datetime.fromisoformat(reopened.provider_login_assignment(key).limit_resets_at or "")
        == resets_at
    )


def test_later_success_clears_login_but_preserves_failed_turn_reset(accounts, monkeypatch):
    store = accounts.store
    key = _login(accounts, "codex")
    failed = _turn(accounts, key)
    assert failed is not None
    store.record_agent_task_receipt(
        failed.operation_id, "provider_terminal_error", {"classification": "session_limit"}
    )
    background = BackgroundAgentTasks(store, _unused_stream)
    settle_provider_login(background, failed)
    receipts = store.agent_task_receipts(failed.operation_id)
    later = datetime.fromisoformat(store.now()) + timedelta(minutes=2)
    monkeypatch.setattr(store, "now", lambda: later.isoformat())
    succeeded = _turn(accounts, key, status="succeeded", operation_id="success")
    assert succeeded is not None
    settle_provider_login(background, succeeded)
    assignment = store.provider_login_assignment(key)
    assert assignment.limited_at is None
    assert assignment.limit_resets_at is None
    assert store.agent_task_receipts(failed.operation_id) == receipts
