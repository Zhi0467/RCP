from __future__ import annotations

import shutil
import sqlite3

import pytest

from rcp.storage import AppStore
from rcp.storage.models import MACHINE_LOGIN_ID, LoginKey
from tests.storage_helpers import downgrade_resource_watermarks


def test_default_login_is_synthetic_signed_in(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    state = store.provider_login_state(LoginKey("codex", "", MACHINE_LOGIN_ID))
    assert state.state == "signed_in"
    assert state.generation == 0
    assert store.provider_login_states() == []


def test_failed_login_is_durable_and_detail_is_bounded(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = AppStore(path)
    state = store.mark_provider_login_failed(
        LoginKey("codex", "", MACHINE_LOGIN_ID),
        generation=0,
        detail="  expired\n  " + "x" * 600,
        source="turn",
    )
    assert state.state == "signed_out"
    assert state.generation == 0
    assert state.detail is not None
    assert len(state.detail) == 500
    assert state.detail.startswith("expired ")
    assert AppStore(path).provider_login_states() == [state]


def test_verification_bumps_generation_and_ignores_late_failure(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    store.mark_provider_login_failed(
        LoginKey("codex", "", MACHINE_LOGIN_ID), generation=0, detail="expired", source="turn"
    )
    verified = store.mark_provider_login_verified(
        LoginKey("codex", "", MACHINE_LOGIN_ID), member_id="member", detail="verified"
    )
    assert verified.state == "signed_in"
    assert verified.generation == 1
    assert verified.changed_by == "member"
    assert verified.source == "verify"
    assert (
        store.mark_provider_login_failed(
            LoginKey("codex", "", MACHINE_LOGIN_ID), generation=0, detail="late", source="report"
        )
        == verified
    )
    assert store.provider_login_state(LoginKey("codex", "", MACHINE_LOGIN_ID)) == verified
    failed = store.mark_provider_login_failed(
        LoginKey("codex", "", MACHINE_LOGIN_ID), generation=1, detail="expired", source="probe"
    )
    assert failed.state == "signed_out"
    assert failed.generation == 1
    assert (
        store.mark_provider_login_verified(
            LoginKey("codex", "", MACHINE_LOGIN_ID), member_id="other", detail="verified"
        ).generation
        == 2
    )


def test_migration_18_upgrades_a_copy_of_version_17(tmp_path):
    fixture = tmp_path / "version17.sqlite3"
    AppStore(fixture)
    with sqlite3.connect(fixture) as connection:
        downgrade_resource_watermarks(connection)
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
    repaired = store.mark_provider_login_verified(
        LoginKey("codex", "", MACHINE_LOGIN_ID), member_id="member", detail="ok"
    )
    snapshot = ProviderReadinessSnapshotRecord(
        provider="codex",
        host="",
        binary="/fake/provider",
        version="1",
        readiness_json="{}",
        probed_at=store.now(),
    )
    store.save_provider_readiness_snapshot(LoginKey("codex", "", MACHINE_LOGIN_ID), snapshot)
    store.mark_provider_login_failed(
        LoginKey("codex", "", MACHINE_LOGIN_ID), generation=0, detail="old failure", source="probe"
    )
    assert (
        store.provider_readiness_snapshot(LoginKey("codex", "", MACHINE_LOGIN_ID), snapshot.binary)
        == snapshot
    )
    store.mark_provider_login_failed(
        LoginKey("codex", "", MACHINE_LOGIN_ID),
        generation=repaired.generation,
        detail="new failure",
        source="turn",
    )
    assert (
        store.provider_readiness_snapshot(LoginKey("codex", "", MACHINE_LOGIN_ID), snapshot.binary)
        is None
    )


def test_limit_marks_require_a_later_provider_start(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "state.sqlite3")
    key = LoginKey("claude", "", MACHINE_LOGIN_ID)
    monkeypatch.setattr(store, "now", lambda: "2026-01-01T03:00:00+00:00")
    assert store.set_provider_login_limit(
        key, provider_started_at="2026-01-01T02:00:00+00:00", limited=True
    )
    for limited in (False, True):
        assert not store.set_provider_login_limit(
            key, provider_started_at="2026-01-01T01:00:00+00:00", limited=limited
        )
    assert store.provider_login_assignment(key).limited_at == store.now()
    assert not store.provider_login_limited(key, now="2026-01-01T09:00:00+00:00")
    assert store.provider_login_limited(key, now="2026-01-01T09:00:00+00:00", continued=True)
    assert store.set_provider_login_limit(
        key, provider_started_at="2026-01-01T04:00:00+00:00", limited=False
    )
    assert store.provider_login_assignment(key).limited_at is None


def test_removed_login_cannot_be_revived_and_restore_keeps_ownership(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    login = store.add_provider_login("claude", "", member_id="member", added_by="member")
    key = LoginKey(login.provider, login.host, login.login_id)
    with pytest.raises(ValueError):
        store.add_provider_login("claude", "", member_id="member", added_by="member")
    store.mark_provider_login_verified(key, member_id="member", detail="ok")
    with store.connection() as connection:
        store.detach_provider_logins_for_restore(connection, now=store.now())
    restored = store.provider_login_state(key)
    assert (restored.member_id, restored.login_id, restored.state, restored.generation) == (
        "member",
        login.login_id,
        "signed_out",
        2,
    )
    store.set_provider_login_draining(key)
    removed = store.finish_provider_login_removal(key)
    assert removed.removed_at is not None
    assert store.provider_login_states() == []
    with pytest.raises(ValueError):
        store.mark_provider_login_verified(key, member_id="member", detail="late")


def test_old_0435_writer_migrates_login_and_session_rows(tmp_path):
    import subprocess
    import types

    from rcp.providers import PROVIDER_IDS

    # Load the release's schema writer; fixture rows never pass through current models.
    source = subprocess.run(
        ["git", "show", "v0.4.35:src/rcp/storage/base.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    old = types.ModuleType("rcp.storage.release_0435_base")
    exec(compile(source, "release_0435_base.py", "exec"), old.__dict__)
    path = tmp_path / "old.sqlite3"
    old_store = type("ReleaseStore", (*AppStore.__bases__[:-1], old.AppStoreBase), {})
    old_store(path, space_kind="team")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO space_users(user_id,identity_kind,display_name,created_at,updated_at) VALUES('member','team_member','Member','2026-01-01','2026-01-01')"
        )
        for host in ("", "worker"):
            connection.execute(
                "INSERT INTO provider_login_states(provider,host,state,generation,changed_at) VALUES('codex',?,'signed_in',3,'2026-01-01')",
                (host,),
            )
            connection.execute(
                "INSERT INTO provider_readiness_snapshots VALUES('codex',?,'codex','1','{}','2026-01-01')",
                (host,),
            )
        for task, status, session in (
            ("queued", "queued", None),
            ("session", "succeeded", "native"),
        ):
            connection.execute(
                "INSERT INTO graph_runs(operation_id,project_id,kind,status,request_json,created_at,updated_at,status_message,native_session_id,runtime_id) VALUES(?,'project','project_chat',?,'{}','2026-01-01','2026-01-01','',?,'codex')",
                (task, status, session),
            )
    upgraded = AppStore(path)
    assert [
        (row.host, row.login_id, row.generation) for row in upgraded.provider_login_states()
    ] == [("", "", 3), ("worker", "", 3)]
    with upgraded.connection() as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT operation_id,login_id,native_session_id FROM graph_runs ORDER BY operation_id"
            )
        ] == [("queued", "", None), ("session", "", "native")]
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT provider,host,setting FROM provider_login_settings WHERE user_id='member' ORDER BY provider"
            )
        ] == [(provider, "*", "own_then_shared") for provider in sorted(PROVIDER_IDS)]
        assert (
            connection.execute("SELECT COUNT(*) FROM provider_readiness_snapshots").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM resource_deadlines WHERE owner LIKE 'provider_readiness_snapshots:%'"
            ).fetchone()[0]
            == 0
        )


def test_assignment_clock_does_not_invalidate_settings(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    key = LoginKey("codex", "", MACHINE_LOGIN_ID)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO graph_runs(operation_id,project_id,kind,status,request_json,created_at,updated_at,status_message) VALUES('task','project','project_chat','queued','{}','2026-01-01','2026-01-01','')"
        )
    store.assign_provider_login("task", lambda connection: key)
    with store.connection() as connection:
        before = connection.execute(
            "SELECT generation FROM resource_versions WHERE scope='' AND resource='settings' AND entity=''"
        ).fetchone()[0]
    store.assign_provider_login("task", lambda connection: key)
    with store.connection() as connection:
        after = connection.execute(
            "SELECT generation FROM resource_versions WHERE scope='' AND resource='settings' AND entity=''"
        ).fetchone()[0]
    assert before == after
    assert store.agent_task_login_id("task") == MACHINE_LOGIN_ID


@pytest.mark.parametrize(("kind", "default"), [("team", "own"), ("personal", "shared")])
def test_setting_lookup_prefers_host_then_fallback_then_space_default(tmp_path, kind, default):
    store = AppStore(tmp_path / "state.sqlite3", space_kind=kind)
    assert store.provider_login_setting("member", "claude", "worker").setting == default
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO provider_login_settings VALUES('member','claude','*','own_then_shared','2026-01-01')"
        )
    assert store.provider_login_setting("member", "claude", "worker").setting == "own_then_shared"
    store.set_provider_login_setting("member", "claude", "worker", "shared_then_own")
    assert store.provider_login_setting("member", "claude", "worker").setting == "shared_then_own"
    with pytest.raises(ValueError):
        store.set_provider_login_setting("member", "claude", "*", "own")
