from __future__ import annotations

import shutil
import sqlite3

from rcp.storage import AppStore


def test_default_login_is_synthetic_signed_in(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    state = store.provider_login_state("codex", "")
    assert state.state == "signed_in"
    assert state.generation == 0
    assert store.provider_login_states() == []


def test_failed_login_is_durable_and_detail_is_bounded(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = AppStore(path)
    state = store.mark_provider_login_failed(
        "codex", "", generation=0, detail="  expired\n  " + "x" * 600, source="turn"
    )
    assert state.state == "signed_out"
    assert state.generation == 0
    assert len(state.detail) == 500
    assert state.detail.startswith("expired ")
    assert AppStore(path).provider_login_states() == [state]


def test_verification_bumps_generation_and_ignores_late_failure(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    store.mark_provider_login_failed("codex", "", generation=0, detail="expired", source="turn")
    verified = store.mark_provider_login_verified(
        "codex", "", member_id="member", detail="verified"
    )
    assert verified.state == "signed_in"
    assert verified.generation == 1
    assert verified.changed_by == "member"
    assert verified.source == "verify"
    assert (
        store.mark_provider_login_failed("codex", "", generation=0, detail="late", source="report")
        == verified
    )
    assert store.provider_login_state("codex", "") == verified
    failed = store.mark_provider_login_failed(
        "codex", "", generation=1, detail="expired", source="probe"
    )
    assert failed.state == "signed_out"
    assert failed.generation == 1
    assert (
        store.mark_provider_login_verified(
            "codex", "", member_id="other", detail="verified"
        ).generation
        == 2
    )


def test_migration_18_upgrades_a_copy_of_version_17(tmp_path):
    fixture = tmp_path / "version17.sqlite3"
    AppStore(fixture)
    with sqlite3.connect(fixture) as connection:
        connection.execute("DROP TABLE provider_login_states")
        connection.execute("DROP TABLE provider_readiness_snapshots")
        connection.execute(
            "ALTER TABLE auto_research_lifecycle_notices DROP COLUMN acknowledged_operation_id"
        )
        # Later migrations only add columns; dropping their ledger rows too keeps
        # the fixture at version 17 as more migrations land.
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version > 17")
    copied = tmp_path / "copied.sqlite3"
    shutil.copy2(fixture, copied)
    upgraded = AppStore(copied)
    assert upgraded.storage_schema_ledger_head() >= 18
    assert upgraded.provider_login_states() == []
    with sqlite3.connect(fixture) as connection:
        assert (
            connection.execute(
                "SELECT MAX(migration_version) FROM storage_schema_migrations"
            ).fetchone()[0]
            == 17
        )


def test_failure_atomically_invalidates_only_its_generation_readiness(tmp_path):
    from rcp.storage.models import ProviderReadinessSnapshotRecord

    store = AppStore(tmp_path / "state.sqlite3")
    repaired = store.mark_provider_login_verified("codex", "", member_id="member", detail="ok")
    snapshot = ProviderReadinessSnapshotRecord(
        provider="codex",
        host="",
        binary="/fake/provider",
        version="1",
        readiness_json="{}",
        probed_at=store.now(),
    )
    store.save_provider_readiness_snapshot(snapshot)
    store.mark_provider_login_failed(
        "codex", "", generation=0, detail="old failure", source="probe"
    )
    assert store.provider_readiness_snapshot("codex", "", snapshot.binary) == snapshot
    store.mark_provider_login_failed(
        "codex", "", generation=repaired.generation, detail="new failure", source="turn"
    )
    assert store.provider_readiness_snapshot("codex", "", snapshot.binary) is None
