from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from rcp.limits import AGENT_TASK_RECEIPT_RETENTION_COUNTS
from rcp.storage import AgentTaskAdmissionConflict, AgentTaskRecord, AppStore


def _store(tmp_path) -> AppStore:
    store = AppStore(tmp_path / "state.sqlite3")
    for operation_id in ("first", "second"):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=operation_id,
                kind="seed",
                status="queued",
                request={},
                created_at=store.now(),
                updated_at=store.now(),
                status_message="Queued",
            )
        )
    return store


def test_remote_pass_reserves_stage_across_task_status_and_project(tmp_path):
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/pass-one.pid")
    store.fail_agent_task("first", "SSH disconnected")
    with pytest.raises(AgentTaskAdmissionConflict, match="exit is unconfirmed"):
        store.begin_remote_provider_pass("second", "remote", "/stage", "/stage/pass-two.pid")
    assert store.unresolved_remote_provider_passes("remote", "/stage") == [
        ("first", "/stage/pass-one.pid")
    ]
    store.begin_remote_provider_pass("second", "other-host", "/stage", "/stage/other-host.pid")
    store.begin_remote_provider_pass("second", "remote", "/other-stage", "/other-stage/pass.pid")
    store.finish_remote_provider_pass("first", "/stage/pass-one.pid")
    store.begin_remote_provider_pass("second", "remote", "/stage", "/stage/pass-two.pid")


def test_settlement_requires_exact_start_and_cannot_clear_new_pass(tmp_path):
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid")
    for operation_id, pid in (("second", "/stage/one.pid"), ("first", "/stage/wrong.pid")):
        with pytest.raises(ValueError, match="matching start"):
            store.finish_remote_provider_pass(operation_id, pid)
    store.finish_remote_provider_pass("first", "/stage/one.pid")
    with pytest.raises(ValueError, match="new pidfile"):
        store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid")
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/two.pid")
    store.finish_remote_provider_pass("first", "/stage/one.pid")
    assert store.unresolved_remote_provider_passes("remote", "/stage") == [
        ("first", "/stage/two.pid")
    ]


@pytest.mark.parametrize("category", ["remote_provider_started", "remote_provider_stopped"])
def test_remote_pass_receipts_cannot_be_forged_through_public_writer(tmp_path, category):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="reserved"):
        store.record_agent_task_receipt("first", category, {})


def test_remote_pass_receipts_survive_count_and_age_retention(tmp_path):
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid")
    store.finish_remote_provider_pass("first", "/stage/one.pid")
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/two.pid")
    for index in range(AGENT_TASK_RECEIPT_RETENTION_COUNTS["summary"] + 1):
        store.record_agent_task_receipt("first", "ordinary", {"index": index})
    store.fail_agent_task("first", "disconnected")
    store.prune_operational_storage(now=datetime.now(UTC) + timedelta(days=3650))
    assert store.unresolved_remote_provider_passes("remote", "/stage") == [
        ("first", "/stage/two.pid")
    ]
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM graph_run_receipts WHERE category LIKE 'remote_provider_%'"
            ).fetchone()[0]
            == 3
        )


def test_remote_pass_reservation_is_atomic(tmp_path):
    store = _store(tmp_path)
    barrier = Barrier(2)

    def reserve(operation_id):
        barrier.wait()
        try:
            store.begin_remote_provider_pass(
                operation_id, "remote", "/stage", f"/stage/{operation_id}.pid"
            )
            return True
        except AgentTaskAdmissionConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ("first", "second"))) == [False, True]
    assert len(store.unresolved_remote_provider_passes("remote", "/stage")) == 1
