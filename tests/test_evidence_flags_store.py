from __future__ import annotations

import pytest

from rcp.storage import AppStore
from rcp.storage.evidence_flags import (
    EvidenceFlagError,
    migrate_evidence_flag_origins,
    migrate_evidence_flag_targets,
    migrate_evidence_flags,
)
from rcp.transfer import TRANSFER_EXCLUDED_PROJECT_TABLES

from .test_lessons_store import consolidate
from .test_lessons_store import store as _store_fixture

store = _store_fixture


def flag(store, *, key="flag", **kwargs):
    return store.execute_evidence_flag_command(
        "project",
        "turn",
        key=key,
        evidence_id="evidence",
        kind="overclaim",
        finding=kwargs.pop("finding", "The reported effect is unsupported."),
        validate_evidence=kwargs.pop("validate_evidence", lambda: 4),
        **kwargs,
    )


def test_flags_require_consolidation_and_live_membership(store):
    with pytest.raises(EvidenceFlagError, check=lambda exc: exc.code == "flag_forbidden"):
        flag(store)
    consolidate(store)
    assert flag(store)["flag"]["run_id"] == "run"
    with store.connection() as connection:
        connection.execute("DELETE FROM project_members WHERE project_id='project'")
    with pytest.raises(EvidenceFlagError, check=lambda exc: exc.code == "flag_forbidden"):
        flag(store)


def test_flag_receipts_replay_without_graph_and_dedupe_live_findings(store):
    consolidate(store)
    first = flag(store)

    def unavailable():
        raise EvidenceFlagError("flag_evidence_invalid")

    assert flag(store, validate_evidence=unavailable) == first
    with pytest.raises(EvidenceFlagError, check=lambda exc: exc.code == "flag_key_conflict"):
        flag(store, finding="Changed")
    duplicate = flag(store, key="another")
    assert duplicate == {**first, "code": "flag_exists"}
    store.dismiss_evidence_flag(
        "project", first["flag"]["flag_id"], user_id=store.local_owner.user_id
    )
    assert store.list_evidence_flags("project") == []
    assert flag(store) == first
    assert flag(store, key="new")["flag"]["flag_id"] != first["flag"]["flag_id"]


def test_flag_admission_binding_rolls_back_with_task_transaction(store):
    from rcp.storage.evidence_flags import record_evidence_flag_address

    consolidate(store)
    original = flag(store)["flag"]
    task = store.agent_task("turn")
    task = task.model_copy(
        update={
            "kind": "node_chat",
            "request": {
                **task.request,
                "chat_id": "address-chat",
                "mode": "work",
                "node_id": "evidence",
            },
        }
    )
    with store.evidence_flag_admission(
        "project", [original["flag_id"]], user_id=store.local_owner.user_id
    ):
        with pytest.raises(RuntimeError), store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_evidence_flag_address(store, connection, task)
            raise RuntimeError("rollback")
        assert store.evidence_flag("project", original["flag_id"]) == original
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_evidence_flag_address(store, connection, task)
    result = store.evidence_flag_address_result("project", original["flag_id"])
    assert result["operation_id"] == "turn"
    assert result["chat_id"] == "address-chat"
    assert result["flag"]["state"] == "addressing"
    assert store.list_evidence_flags("project") == [result["flag"]]


def test_flag_migration_preserves_existing_store_and_space_records(store):
    tables = {"evidence_flags", "evidence_flag_command_receipts", "evidence_flag_carries"}
    assert tables <= TRANSFER_EXCLUDED_PROJECT_TABLES
    before = store.agent_task("turn")
    with store.connection() as connection:
        for table in tables:
            connection.execute(f"DROP TABLE {table}")
        migrate_evidence_flags(connection)
        migrate_evidence_flag_targets(connection)
        migrate_evidence_flag_origins(connection)
    path = store.path
    store.close()
    reopened = AppStore(path)
    assert reopened.agent_task("turn") == before
    assert reopened.storage_schema_ledger_head() == AppStore.storage_schema_registry_head()
    consolidate(reopened)
    original = flag(reopened)["flag"]
    reopened.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")
    assert reopened.list_evidence_flags("project") == [original]
    counts = reopened.delete_project_records("project")
    assert all(counts[table] == int(table != "evidence_flag_carries") for table in tables)


def test_origin_migration_preserves_old_flag_rows_and_default(store):
    consolidate(store)
    original = flag(store)["flag"]
    with store.connection() as connection:
        connection.execute("DROP TABLE evidence_flag_carries")
        connection.execute("ALTER TABLE evidence_flags DROP COLUMN origin_flag_id")
    assert store.evidence_flag("project", original["flag_id"]) == original
    with store.connection() as connection:
        migrate_evidence_flag_origins(connection)
    assert store.evidence_flag("project", original["flag_id"]) == original
    assert original["origin_flag_id"] is None


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("finding", "claim\n", "flag_finding_invalid"),
        ("finding", "claim\u2028other", "flag_finding_invalid"),
        ("source", "/source\n", "flag_source_invalid"),
        ("source", "ftp://example.invalid/paper.pdf", "flag_source_invalid"),
        ("source", "results.txt", "flag_source_invalid"),
    ],
)
def test_flag_fields_are_single_line_and_sources_are_paths(store, field, value, code):
    consolidate(store)
    with pytest.raises(EvidenceFlagError, check=lambda exc: exc.code == code):
        flag(store, **{field: value})
    assert store.list_evidence_flags("project") == []


def test_batch_binding_conflict_rolls_back_every_flag_and_receipt(store):
    from rcp.storage.client_requests import record_client_request
    from rcp.storage.evidence_flags import record_evidence_flag_address

    consolidate(store)
    first = flag(store)["flag"]
    second = store.execute_evidence_flag_command(
        "project",
        "turn",
        key="second",
        evidence_id="second",
        kind="underclaim",
        finding="Check source.",
        validate_evidence=lambda: 4,
    )["flag"]
    ids = [first["flag_id"], second["flag_id"]]
    task = store.agent_task("turn").model_copy(
        update={
            "kind": "project_chat",
            "request": {"chat_id": "batch-chat", "mode": "work"},
        }
    )
    with (
        store.client_request_admission("project", store.local_owner.user_id, "batch", "batch"),
        store.evidence_flag_admission("project", ids, user_id=store.local_owner.user_id),
        pytest.raises(EvidenceFlagError, match="flag_address_conflict"),
        store.connection() as connection,
    ):
        connection.execute("BEGIN IMMEDIATE")
        record_client_request(store, connection, task)
        # Force the second compare-and-set to fail after the first update.
        connection.execute("UPDATE evidence_flags SET state='dismissed' WHERE flag_id=?", (ids[1],))
        record_evidence_flag_address(store, connection, task)
    assert store.evidence_flag("project", ids[0]) == first
    assert store.evidence_flag("project", ids[1]) == second
    assert store.client_request("project", "batch") is None
