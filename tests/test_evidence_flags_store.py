from __future__ import annotations

import pytest

from rcp.storage import AppStore
from rcp.storage.evidence_flags import EvidenceFlagError
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
        "project", original["flag_id"], user_id=store.local_owner.user_id, request_id="address"
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
    tables = {"evidence_flags", "evidence_flag_command_receipts"}
    assert tables <= TRANSFER_EXCLUDED_PROJECT_TABLES
    before = store.agent_task("turn")
    with store.connection() as connection:
        for table in tables:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=51")
    path = store.path
    store.close()
    reopened = AppStore(path)
    assert reopened.agent_task("turn") == before
    assert reopened.storage_schema_ledger_head() == 51
    consolidate(reopened)
    original = flag(reopened)["flag"]
    reopened.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")
    assert reopened.list_evidence_flags("project") == [original]
    counts = reopened.delete_project_records("project")
    assert all(counts[table] == 1 for table in tables)


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
