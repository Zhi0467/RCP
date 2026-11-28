from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rcp.agents import AgentProcessControl
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.runs.provider_limits import record_provider_limit_decision
from rcp.runs.provider_login import settle_provider_login
from rcp.storage import AppStore

from .test_provider_login_lifecycle import _turn, _unused_stream
from .test_provider_login_resolver import _login
from .test_provider_login_resolver import accounts as accounts


def _settle(background, record, *, execution=None):
    current = background.store.agent_task(record.operation_id) or record
    if current.status == "failed":
        record_provider_limit_decision(
            background.store,
            background.launcher,
            current,
            snapshot=execution.rate_limit if execution else None,
        )
    settle_provider_login(background, record)


@pytest.fixture(autouse=True)
def no_installed_provider_probe(monkeypatch):
    monkeypatch.setattr("rcp.agents.launcher._discover_local_provider", lambda _: None)


def test_snapshot_decision_supplies_login_reset(accounts):
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
    _settle(background, record, execution=execution)
    after = store.agent_task_receipts(record.operation_id)
    added = [receipt for receipt in after if receipt not in before]
    assert len(added) == 1
    decision = added[0]
    assert decision.category == "provider_terminal_error"
    assert decision.payload["source"] == "event"
    assert decision.payload["snapshot"] == snapshot
    assert decision.payload["estimated"] is False
    assert datetime.fromisoformat(str(decision.payload["resets_at"])) == resets_at
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
    _settle(BackgroundAgentTasks(store, _unused_stream), record)
    receipt = next(
        receipt
        for receipt in store.agent_task_receipts(record.operation_id)
        if receipt.category == "provider_terminal_error"
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
    _settle(background, record, execution=execution)
    original = store.agent_task_receipts(record.operation_id)
    _settle(background, record)
    reopened = AppStore(store.path)
    _settle(BackgroundAgentTasks(reopened, _unused_stream), record)
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
    _settle(background, failed)
    receipts = store.agent_task_receipts(failed.operation_id)
    later = datetime.fromisoformat(store.now()) + timedelta(minutes=2)
    monkeypatch.setattr(store, "now", lambda: later.isoformat())
    succeeded = _turn(accounts, key, status="succeeded", operation_id="success")
    assert succeeded is not None
    _settle(background, succeeded)
    assignment = store.provider_login_assignment(key)
    assert assignment.limited_at is None
    assert assignment.limit_resets_at is None
    assert store.agent_task_receipts(failed.operation_id) == receipts


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_recorded_limit_snapshot_reaches_failure_settlement(accounts, tmp_path, provider):
    import asyncio
    import json

    from rcp.background import TaskFailed
    from rcp.runs.recorded_settlement import absorb_recorded_events, provider_turn_request
    from rcp.runs.recorded_turn import RecordedProviderTurn, decode_recorded_turn
    from rcp.runs.shared import _ProviderOutcome
    from rcp.service import RunRequest

    store = accounts.store
    key = _login(accounts, provider)
    record = _turn(accounts, key)
    assert record is not None
    resets_at = datetime.fromisoformat(store.now()).replace(microsecond=0) + timedelta(hours=2)
    snapshot = {
        "unifiedWindows": {
            "five_hour": {
                "status": "rejected",
                "resetsAt": int(resets_at.timestamp()),
            }
        }
    }
    runtime_id = "claude.stream-json.v1"
    events = [{"type": "rate_limit_event", "rate_limit_info": snapshot}]
    if provider == "codex":
        runtime_id = "codex.app-server-stdio.v1"
        snapshot = {"primary": {"usedPercent": 100, "resetsAt": int(resets_at.timestamp())}}
        events = [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"config": {}}},
            {
                "id": 3,
                "result": {
                    "thread": {"id": record.native_session_id},
                    "approvalPolicy": "never",
                    "sandbox": {"type": "dangerFullAccess"},
                },
            },
            {"id": 4, "result": {"turn": {"id": "recorded-turn"}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": snapshot}},
        ]
    recorded = RecordedProviderTurn(
        pid_file="/tmp/pass.pid",
        provider=provider,
        runtime_id=runtime_id,
        provider_version=None,
        outcome={"journal_complete": True},
        accepted=True,
        events="".join(json.dumps(event) + "\n" for event in events),
        stderr="",
        patch=None,
    )
    verdict = decode_recorded_turn(
        recorded, provider_turn_request(tmp_path, recorded, capability="discuss")
    )
    outcome = _ProviderOutcome()
    frames = absorb_recorded_events(outcome, verdict)
    from rcp.agents import AgentEvent

    frames.append(
        f"data: {AgentEvent(event='error', text='usage limit reached').model_dump_json()}\n\n"
    )

    async def source():
        for frame in frames:
            yield frame

    background = BackgroundAgentTasks(store, _unused_stream)
    execution = AgentTaskExecution(record.operation_id, store, AgentProcessControl())
    with pytest.raises(TaskFailed):
        asyncio.run(
            background._consume(
                record.project_id,
                record.kind,
                RunRequest(provider=provider),
                execution,
                source=source(),
                recorded=True,
            )
        )
    store.record_agent_task_receipt(
        record.operation_id, "provider_terminal_error", {"classification": "session_limit"}
    )
    _settle(background, record, execution=execution)
    reset = store.agent_task_receipts_by_category(record.operation_id, "provider_terminal_error")[
        -1
    ]
    assert reset.payload["snapshot"] == snapshot
    assert reset.payload["estimated"] is False
    assert datetime.fromisoformat(reset.payload["resets_at"]) == resets_at


@pytest.mark.parametrize("provider,exhausted", [("agy", True), ("agy", False), ("codex", True)])
def test_structured_probe_decides_limit(accounts, tmp_path, monkeypatch, provider, exhausted):
    import json
    from pathlib import Path

    from rcp.agents import launcher as launcher_module
    from rcp.agents.launcher import AgentLauncher

    store = accounts.store
    key = _login(accounts, provider)
    record = _turn(accounts, key)
    reset_at = datetime.fromisoformat(store.now()) + timedelta(hours=2)
    if provider == "agy":
        payload = json.loads(
            (Path(__file__).parent / "fixtures/provider_usage_limits/agy.json").read_text()
        )
        if not exhausted:
            for group in payload["command"]["data"]["groups"]:
                for bucket in group["buckets"]:
                    bucket["remaining_fraction"] = 0.5
        else:
            reset_at = max(
                datetime.fromisoformat(bucket["reset_time"])
                for group in payload["command"]["data"]["groups"]
                for bucket in group["buckets"]
                if bucket["remaining_fraction"] <= 0
            )
        output = json.dumps(payload)
        check = "assert sys.argv[1:] == ['-p', '/usage', '--output-format', 'json']"
    else:
        output = (
            json.dumps({"id": 1, "result": {}})
            + "\n"
            + json.dumps(
                {
                    "id": 2,
                    "result": {
                        "rateLimits": {
                            "primary": {"usedPercent": 100, "resetsAt": int(reset_at.timestamp())}
                        }
                    },
                }
            )
        )
        check = (
            "assert sys.argv[1:] == ['app-server', '--stdio']\n"
            "messages = [json.loads(line) for line in sys.stdin]\n"
            "assert [m['method'] for m in messages] == "
            "['initialize', 'initialized', 'account/rateLimits/read']"
        )
    binary = tmp_path / "fake-provider"
    binary.write_text(
        "#!/usr/bin/env python3\nimport sys, json\n" + check + "\nprint(" + repr(output) + ")\n"
    )
    binary.chmod(0o755)
    monkeypatch.setattr(launcher_module, "_discover_local_provider", lambda _: str(binary))
    if not exhausted:
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET error=? WHERE operation_id=?",
                ("usage limit reached", record.operation_id),
            )
    _settle(
        BackgroundAgentTasks(store, _unused_stream, launcher=AgentLauncher(accounts=accounts)),
        record,
    )
    receipts = store.agent_task_receipts_by_category(record.operation_id, "provider_terminal_error")
    assignment = store.provider_login_assignment(key)
    if exhausted:
        assert len(receipts) == 1
        assert receipts[0].payload["source"] == "probe"
        assert receipts[0].payload["estimated"] is False
        assert datetime.fromisoformat(receipts[0].payload["resets_at"]) == reset_at
        assert datetime.fromisoformat(assignment.limit_resets_at) == reset_at
    else:
        assert receipts[-1].payload["classification"] == "provider_error"
        assert assignment.limited_at is None


@pytest.mark.parametrize("detail_size", [1, 40000])
def test_probe_failure_records_text_fallback(accounts, tmp_path, monkeypatch, detail_size):
    from rcp.agents import launcher as launcher_module
    from rcp.agents.launcher import AgentLauncher

    store = accounts.store
    key = _login(accounts, "agy")
    record = _turn(accounts, key)
    binary = tmp_path / "fake-provider"
    binary.write_text("#!/bin/sh\necho unavailable" + "x" * detail_size + " >&2\nexit 1\n")
    binary.chmod(0o755)
    monkeypatch.setattr(launcher_module, "_discover_local_provider", lambda _: str(binary))
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET error=? WHERE operation_id=?",
            ("usage limit reached", record.operation_id),
        )
    _settle(
        BackgroundAgentTasks(store, _unused_stream, launcher=AgentLauncher(accounts=accounts)),
        record,
    )
    receipt = store.agent_task_receipts_by_category(record.operation_id, "provider_terminal_error")[
        0
    ]
    assert receipt.payload["source"] == "text"
    assert "unavailable" in receipt.payload["probe_failure"]
    assert receipt.payload["estimated"] is True


def test_claude_nonexhausted_event_overrides_phrase(accounts):
    store = accounts.store
    key = _login(accounts, "claude")
    record = _turn(accounts, key)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET error=? WHERE operation_id=?",
            ("usage limit reached", record.operation_id),
        )
    execution = AgentTaskExecution(
        record.operation_id,
        store,
        AgentProcessControl(),
        rate_limit={
            "status": "allowed",
            "unifiedWindows": {"five_hour": {"utilization": 0.5, "status": "allowed"}},
        },
    )
    _settle(BackgroundAgentTasks(store, _unused_stream), record, execution=execution)
    assert (
        store.agent_task_receipts_by_category(record.operation_id, "provider_terminal_error")[
            -1
        ].payload["classification"]
        == "provider_error"
    )
    assert store.provider_login_assignment(key).limited_at is None


def test_recorded_negative_decision_is_authoritative_after_restart(accounts):
    store = accounts.store
    key = _login(accounts, "claude")
    record = _turn(accounts, key)
    decision = record_provider_limit_decision(
        store, None, record, snapshot={"status": "allowed"}, error="usage limit reached"
    )
    assert decision["classification"] == "provider_error"
    reopened = AppStore(store.path)
    assert (
        reopened.failure_classification(record.operation_id, "usage limit reached")
        == "provider_error"
    )
    assert (
        record_provider_limit_decision(
            reopened, None, record, snapshot={"status": "rejected"}, error="usage limit reached"
        )
        == decision
    )
    assert (
        len(
            reopened.agent_task_receipts_by_category(record.operation_id, "provider_terminal_error")
        )
        == 1
    )


def test_failure_classification_keeps_old_history_read_only(accounts):
    record = _turn(accounts, _login(accounts, "claude"))
    assert (
        accounts.store.failure_classification(record.operation_id, "usage limit reached")
        == "session_limit"
    )
    assert not accounts.store.agent_task_receipts_by_category(
        record.operation_id, "provider_terminal_error"
    )


def test_failed_settlement_records_before_recovery_callback(accounts):
    store = accounts.store
    key = _login(accounts, "claude")
    record = _turn(accounts, key)
    seen = []

    def callback(*args):
        seen.extend(
            store.agent_task_receipts_by_category(record.operation_id, "provider_terminal_error")
        )

    background = BackgroundAgentTasks(store, _unused_stream, on_task_settled=callback)
    execution = AgentTaskExecution(
        record.operation_id, store, AgentProcessControl(), rate_limit={"status": "allowed"}
    )
    from rcp.service import RunRequest

    background._task_settled(record, RunRequest(provider="claude"), execution)
    assert len(seen) == 1
    assert seen[0].payload["classification"] == "provider_error"
    assert seen[0].payload["source"] == "event"


def test_missing_machine_falls_back_without_losing_failure(accounts):
    from rcp.agents.launcher import AgentLauncher

    store = accounts.store
    record = _turn(accounts, _login(accounts, "agy"))
    record = record.model_copy(update={"stage_machine_id": "removed-machine"})
    decision = record_provider_limit_decision(
        store, AgentLauncher(accounts=accounts), record, error="usage limit reached"
    )
    assert decision["classification"] == "session_limit"
    assert decision["source"] == "text"
    assert "removed-machine" in decision["probe_failure"]
    assert decision["estimated"] is True


def test_large_snapshot_does_not_erase_decision(accounts):
    store = accounts.store
    record = _turn(accounts, _login(accounts, "claude"))
    decision = record_provider_limit_decision(
        store, None, record, snapshot={"status": "rejected", "detail": "x" * 40000}
    )
    stored = store.agent_task_receipts_by_category(record.operation_id, "provider_terminal_error")[
        -1
    ].payload
    assert stored == decision
    assert stored["classification"] == "session_limit"
    assert stored["snapshot_omitted"] is True
    assert stored["resets_at"]
