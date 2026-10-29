from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from rcp.agents import AgentLauncher
from rcp.agents.provider_accounts import LoginRefusal, ProviderAccounts
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.background import BackgroundAgentTasks
from rcp.runs.provider_login import (
    bound_running_count,
    reconcile_login_removals,
    remove_shared_login,
    resolved_task_login,
    settle_provider_login,
)
from rcp.storage import AppStore
from rcp.storage.models import EpisodeMode, EpisodeRecord, LoginKey

from .helpers import fabricated_authorizer, fake_launch_admitted
from .test_provider_login_resolver import _login, _task


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    monkeypatch.setattr(store, "now", lambda: now.isoformat())
    return ProviderAccounts(store, ProviderCredentialStore.for_data_dir(tmp_path))


def _turn(accounts, key, *, status="failed", episode_id=None, operation_id="turn"):
    store = accounts.store
    authorizer = fabricated_authorizer()
    _task(
        store,
        operation_id,
        key,
        started=(datetime.fromisoformat(store.now()) - timedelta(seconds=1)).isoformat(),
        member=authorizer.user_id,
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status=?, episode_id=?, authorized_space_id=?, authorized_display_name=? WHERE operation_id=?",
            (status, episode_id, authorizer.space_id, authorizer.display_name, operation_id),
        )
    return store.agent_task(operation_id)


def _episode(store, *, mode: EpisodeMode = "experiment_loop"):
    episode = store.create_episode(
        EpisodeRecord(
            episode_id="episode",
            project_id="project",
            mode=mode,
            control_node_id="experiment" if mode == "experiment_loop" else None,
            authorized_by=fabricated_authorizer(),
            status="queued",
            invocation_ceiling=5,
            created_at=store.now(),
            updated_at=store.now(),
        )
    )

    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status='running' WHERE episode_id=?", (episode.episode_id,)
        )
    return store.episode(episode.episode_id)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_drain_preserves_bound_codex_and_removes_after_episode_ends(accounts, provider):
    store = accounts.store
    key = _login(accounts, provider)
    _episode(store)
    task = _turn(accounts, key, status="succeeded", episode_id="episode")
    launcher = AgentLauncher()
    cancelled = []
    credentials = accounts.credentials
    home = credentials.account_dir(provider, "", key.login_id) / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text("credential")
    (home / "session.jsonl").write_text("session")
    state = remove_shared_login(store, launcher, key, cancel_sign_in=cancelled.append)
    assert cancelled == [key]
    assert state.draining_since is not None
    assert (state.removed_at is None) == (provider == "codex")
    if provider == "codex":
        assert bound_running_count(store, key) == 1
        assert resolved_task_login(store, task) == key
        with store.connection() as connection:
            connection.execute("UPDATE episodes SET status='completed' WHERE episode_id='episode'")
        reconcile_login_removals(store, launcher)
        assert store.provider_login_state(key).removed_at is not None
        assert not (home / "auth.json").exists()
        assert (home / "session.jsonl").exists()
        result, _ = accounts.resolve_login(
            provider, "", fabricated_authorizer().user_id, bound_login=key
        )
        assert isinstance(result, LoginRefusal)
        assert result.failure_kind == "session_refused"


def test_paused_codex_episode_does_not_hold_drain(accounts):
    key = _login(accounts, "codex")
    _episode(accounts.store)
    _turn(accounts, key, status="paused", episode_id="episode")
    assert bound_running_count(accounts.store, key) == 0


def test_remote_credential_deletion_retries_after_restart(accounts, monkeypatch):
    store = accounts.store
    state = store.add_provider_login("codex", "remote", added_by="member")
    key = LoginKey("codex", "remote", state.login_id)
    store.mark_provider_login_verified(key, member_id="member", detail="ok")
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 255, "", "unreachable")

    monkeypatch.setattr("rcp.runs.provider_login.subprocess.run", fail)
    remove_shared_login(store, AgentLauncher(), key, cancel_sign_in=lambda _: None)
    assert store.provider_login_state(key).removed_at is not None
    assert len(calls) == 1

    def succeed(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("rcp.runs.provider_login.subprocess.run", succeed)
    reopened = AppStore(store.path)
    reconcile_login_removals(reopened, AgentLauncher())
    reconcile_login_removals(reopened, AgentLauncher())
    assert len(calls) == 2


async def _unused_stream(*args):
    yield ""


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("alternative_lapsed", [False, True])
def test_limit_retry_is_once_per_session_login_and_not_rearmed(
    accounts, monkeypatch, provider, alternative_lapsed
):
    store = accounts.store
    own = _login(accounts, provider, own=True)
    shared = _login(accounts, provider, machine=True)
    authorizer = fabricated_authorizer()
    with store.connection() as connection:
        connection.execute(
            "UPDATE provider_login_states SET member_id=? WHERE login_id=?",
            (authorizer.user_id, own.login_id),
        )
    store.set_provider_login_setting(authorizer.user_id, provider, "", "own_then_shared")
    _episode(store, mode="experiment_loop")
    record = _turn(accounts, own, episode_id="episode")
    store.record_agent_task_receipt(
        record.operation_id, "provider_terminal_error", {"classification": "session_limit"}
    )
    if alternative_lapsed:
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO provider_login_assignments(provider,host,login_id,limited_at) VALUES(?,?,?,?)",
                (*shared, (datetime.fromisoformat(store.now()) - timedelta(days=1)).isoformat()),
            )
    calls = []
    background = BackgroundAgentTasks(store, _unused_stream)
    monkeypatch.setattr(background, "retry", calls.append)
    settle_provider_login(background, record)
    settle_provider_login(background, record)
    reopened = AppStore(store.path)
    restarted = BackgroundAgentTasks(reopened, _unused_stream)
    monkeypatch.setattr(restarted, "retry", calls.append)
    reloaded = reopened.agent_task(record.operation_id)
    assert reloaded is not None
    settle_provider_login(restarted, reloaded)
    assert calls == (
        [record.operation_id] if provider == "claude" and not alternative_lapsed else []
    )
    assert store.provider_login_assignment(own).limited_at is not None


def test_completed_turn_clears_limit_only_when_started_after_mark(accounts):
    store = accounts.store
    key = _login(accounts, "codex")
    record = _turn(accounts, key, status="succeeded")
    store.set_provider_login_limit(key, provider_started_at=record.created_at, limited=True)
    background = BackgroundAgentTasks(store, _unused_stream)
    settle_provider_login(background, record)
    assert store.provider_login_assignment(key).limited_at is not None
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_run_receipts SET created_at=? WHERE operation_id=? AND category='provider_runtime_selected'",
            (
                (datetime.fromisoformat(store.now()) + timedelta(seconds=1)).isoformat(),
                record.operation_id,
            ),
        )
    settle_provider_login(background, record)
    assert store.provider_login_assignment(key).limited_at is None


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_settlement_releases_parked_recovery_only_when_session_can_move(
    accounts, monkeypatch, provider
):
    from rcp.storage import ProjectRecord

    store = accounts.store
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="unused",
            name="project",
            state_location="unused",
            state_remote=False,
            added_at=store.now(),
        )
    )
    own = _login(accounts, provider, own=True)
    _login(accounts, provider, machine=True)
    member = fabricated_authorizer().user_id
    with store.connection() as connection:
        connection.execute(
            "UPDATE provider_login_states SET member_id=? WHERE login_id=?", (member, own.login_id)
        )
    store.set_provider_login_setting(member, provider, "", "own_then_shared")
    _episode(store)
    record = _turn(accounts, own, episode_id="episode")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET failure_kind='provider_auth' WHERE operation_id=?",
            (record.operation_id,),
        )
        connection.execute(
            "INSERT INTO auto_research_recoveries(recovery_id,episode_id,operation_id,failure_kind,retry_mode,attempts,max_attempts,status,diagnostic,created_at,updated_at) VALUES('recovery','episode',?,'provider_auth','blocked',0,3,'blocked','auth',?,?)",
            (record.operation_id, store.now(), store.now()),
        )
    store.mark_provider_login_signed_out(
        own, member_id=member, source="sign_out", detail="signed out"
    )
    background = BackgroundAgentTasks(store, _unused_stream)
    settle_provider_login(background, record)
    with store.connection() as connection:
        status = connection.execute(
            "SELECT status FROM auto_research_recoveries WHERE recovery_id='recovery'"
        ).fetchone()[0]
    assert status == ("pending" if provider == "claude" else "blocked")


def test_verify_shared_releases_unbound_work_even_when_resolver_prefers_own(accounts, monkeypatch):
    from rcp.runs.provider_login import resume_provider_account
    from rcp.storage import ProjectRecord

    store = accounts.store
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="unused",
            name="project",
            state_location="unused",
            state_remote=False,
            added_at=store.now(),
        )
    )
    own = _login(accounts, "claude", own=True)
    shared = _login(accounts, "claude", machine=True)
    member = fabricated_authorizer().user_id
    with store.connection() as connection:
        connection.execute(
            "UPDATE provider_login_states SET member_id=? WHERE login_id=?", (member, own.login_id)
        )
    store.set_provider_login_setting(member, "claude", "", "own_then_shared")
    record = _turn(accounts, own, status="queued")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET stage_host='' WHERE operation_id=?", (record.operation_id,)
        )
    calls = []
    background = BackgroundAgentTasks(store, _unused_stream)
    fake_launch_admitted(monkeypatch, background, calls.append)
    resume_provider_account(background, shared)
    assert calls == [record.operation_id]


def test_unhealthy_bound_login_stays_discoverable_for_release(accounts):
    key = _login(accounts, "codex")
    record = _turn(accounts, key)
    accounts.store.mark_provider_login_signed_out(
        key, member_id="member", source="sign_out", detail="signed out"
    )
    assert resolved_task_login(accounts.store, record) == key


@pytest.mark.parametrize("event", ["setting", "verify_own", "verify_shared"])
def test_null_login_park_re_resolves_setting_on_release(accounts, monkeypatch, event):
    from rcp.runs.provider_login import provider_login_changed
    from rcp.storage import AgentTaskRecord, ProjectRecord
    from rcp.watchers import WatcherDelivery

    watcher_delivery = Mock(spec=WatcherDelivery)
    store = accounts.store
    member = fabricated_authorizer()
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="unused",
            name="project",
            state_location="unused",
            state_remote=False,
            added_at=store.now(),
        )
    )
    store.set_provider_login_setting(member.user_id, "codex", "", "own")
    task = AgentTaskRecord(
        operation_id="parked",
        project_id="project",
        kind="project_chat",
        status="queued",
        request={"provider": "codex", "run_on": "local"},
        login_id=None,
        authorized_by=member,
        stage_host="",
        stage_root="unused",
        created_at=store.now(),
        updated_at=store.now(),
        status_message="",
    )
    store.create_agent_task(task)
    assert resolved_task_login(store, task) is None
    background = BackgroundAgentTasks(store, _unused_stream)
    launched = []
    fake_launch_admitted(monkeypatch, background, launched.append)
    if event == "verify_own":
        row = store.add_provider_login(
            "codex", "", member_id=member.user_id, added_by=member.user_id
        )
        key = LoginKey("codex", "", row.login_id)
    else:
        key = LoginKey("codex", "", "")
    store.mark_provider_login_verified(key, member_id=member.user_id, detail="verified")
    if event != "verify_own":
        # A shared verify cannot release work while only Own is enabled.
        provider_login_changed(
            background, key=key, reconcile_episodes=lambda: 0, watcher_delivery=watcher_delivery
        )
        assert launched == []
        store.set_provider_login_setting(member.user_id, "codex", "", "shared")
    if event == "setting":
        provider_login_changed(
            background,
            member_id=member.user_id,
            provider="codex",
            host="",
            reconcile_episodes=lambda: 0,
            watcher_delivery=watcher_delivery,
        )
    else:
        provider_login_changed(
            background, key=key, reconcile_episodes=lambda: 0, watcher_delivery=watcher_delivery
        )
    assert launched == ["parked"]
    assert resolved_task_login(store, store.agent_task("parked")) == key


@pytest.mark.parametrize("kind", ["shared", "own", "machine"])
def test_row_reports_bound_count_before_removal(accounts, kind):
    from rcp.provider_login_rows import provider_login_row

    key = _login(accounts, "codex", own=kind == "own", machine=kind == "machine")
    _turn(accounts, key, status="running")
    row = provider_login_row(accounts.store, accounts.credentials, None, key, viewer_id="member")
    assert row["draining_since"] is None
    assert row["bound_running"] == (1 if kind == "shared" else 0)
