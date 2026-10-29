from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from rcp.agents.provider_accounts import LoginRefusal, ProviderAccounts
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.storage import AppStore
from rcp.storage.models import LoginKey


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    monkeypatch.setattr(store, "now", lambda: now.isoformat())
    credentials = ProviderCredentialStore.for_data_dir(tmp_path)
    return ProviderAccounts(store, credentials)


def _login(accounts, provider, *, own=False, machine=False):
    store = accounts.store
    login_id = (
        ""
        if machine
        else store.add_provider_login(
            provider, "", member_id="member" if own else None, added_by="member"
        ).login_id
    )
    key = LoginKey(provider, "", login_id)
    store.mark_provider_login_verified(key, member_id="member", detail="ok")
    if provider == "claude":
        accounts.credentials.store_token(
            "claude", "", "test-token", member_id="member", now=store.now(), login_id=login_id
        )
    return key


def _limit(accounts, key):
    start = datetime.fromisoformat(accounts.store.now()) - timedelta(seconds=1)
    assert accounts.store.set_provider_login_limit(
        key, provider_started_at=start.isoformat(), limited=True
    )


def _task(
    store, operation_id, key, *, started=None, member="member", session: str | None = "session"
):
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO graph_runs(operation_id,project_id,kind,status,request_json,created_at,"
            "updated_at,status_message,native_session_id,stage_host,login_id,authorized_user_id) "
            "VALUES(?, 'project', 'refresh', 'queued', ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                operation_id,
                json.dumps({"provider": key.provider}),
                store.now(),
                store.now(),
                session,
                key.host,
                key.login_id,
                member,
            ),
        )
        if started:
            connection.execute(
                "INSERT INTO graph_run_receipts(operation_id,created_at,tier,category,payload_json) "
                "VALUES(?, ?, 'summary', 'provider_runtime_selected', '{}')",
                (operation_id, started),
            )


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize(
    "setting,expected",
    [
        ("own", "own"),
        ("own_then_shared", "own"),
        ("shared_then_own", "shared"),
        ("shared", "shared"),
    ],
)
def test_new_session_uses_first_available_source(accounts, provider, setting, expected):
    own = _login(accounts, provider, own=True)
    shared = _login(accounts, provider, machine=True)
    accounts.store.set_provider_login_setting("member", provider, "", setting)
    assert accounts.resolve_login(provider, "", "member") == (
        {"own": own, "shared": shared}[expected],
        None,
    )


@pytest.mark.parametrize(
    "setting,first_source",
    [
        ("own", "own"),
        ("own_then_shared", "own"),
        ("shared_then_own", "shared"),
        ("shared", "shared"),
    ],
)
def test_refusal_names_first_source(accounts, setting, first_source):
    accounts.store.set_provider_login_setting("member", "claude", "", setting)
    refusal, reason = accounts.resolve_login("claude", "", "member")
    assert isinstance(refusal, LoginRefusal)
    assert refusal.source == first_source
    assert reason is None


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("condition", ["limited", "signed_out", "draining"])
def test_continued_session_moves_or_stays_bound(accounts, provider, condition):
    own = _login(accounts, provider, own=True)
    shared = _login(accounts, provider, machine=True)
    accounts.store.set_provider_login_setting("member", provider, "", "own_then_shared")
    if condition == "limited":
        _limit(accounts, own)
    elif condition == "signed_out":
        accounts.store.mark_provider_login_signed_out(
            own, member_id="member", source="sign_out", detail="out"
        )
    else:
        accounts.store.set_provider_login_draining(own)
    result, reason = accounts.resolve_login(
        provider,
        "",
        "member",
        previous_login=own,
        bound_login=own,
        previous_authorized_by="member",
        previous_provider_started_at=accounts.store.now(),
    )
    if provider == "codex":
        assert reason is None
        if condition == "signed_out":
            assert isinstance(result, LoginRefusal)
            assert result.login == own
        else:
            assert result == own
    else:
        assert result == shared
        assert reason == ("limited" if condition == "limited" else "unhealthy")


@pytest.mark.parametrize("setting", ["own", "own_then_shared", "shared_then_own", "shared"])
def test_every_allowed_limited_keeps_continued_session(accounts, setting):
    own = _login(accounts, "claude", own=True)
    shared = _login(accounts, "claude", machine=True)
    _limit(accounts, own)
    _limit(accounts, shared)
    accounts.store.set_provider_login_setting("member", "claude", "", setting)
    previous = own if setting == "own" else shared
    assert accounts.resolve_login("claude", "", "member", previous_login=previous) == (
        previous,
        None,
    )


def test_lapse_reorders_fresh_sessions_but_evidence_moves_continued(accounts, monkeypatch):
    own = _login(accounts, "claude", own=True)
    shared = _login(accounts, "claude", machine=True)
    accounts.store.set_provider_login_setting("member", "claude", "", "own_then_shared")
    _limit(accounts, own)
    previous_start = accounts.store.now()
    later = datetime.fromisoformat(previous_start) + timedelta(hours=6)
    monkeypatch.setattr(accounts.store, "now", lambda: later.isoformat())
    assert accounts.resolve_login("claude", "", "member") == (own, None)
    continuation = dict(
        previous_login=shared,
        previous_authorized_by="member",
        previous_provider_started_at=previous_start,
    )
    assert accounts.resolve_login("claude", "", "member", **continuation) == (shared, None)
    accounts.store.mark_provider_login_verified(own, member_id="member", detail="ok")
    assert accounts.resolve_login("claude", "", "member", **continuation) == (
        own,
        "preferred_source_available",
    )


@pytest.mark.parametrize(
    "previous_member,change_setting,expected",
    [
        ("other", True, "sender_setting"),
        ("member", True, "setting_changed"),
        ("member", False, "preferred_source_available"),
    ],
)
def test_switch_reason_precedence(accounts, monkeypatch, previous_member, change_setting, expected):
    own = _login(accounts, "claude", own=True)
    shared = _login(accounts, "claude", machine=True)
    accounts.store.set_provider_login_setting("member", "claude", "", "own_then_shared")
    previous_start = accounts.store.now()
    if change_setting:
        later = datetime.fromisoformat(previous_start) + timedelta(seconds=1)
        monkeypatch.setattr(accounts.store, "now", lambda: later.isoformat())
        accounts.store.set_provider_login_setting("member", "claude", "", "own_then_shared")
    assert accounts.resolve_login(
        "claude",
        "",
        "member",
        previous_login=shared,
        previous_authorized_by=previous_member,
        previous_provider_started_at=previous_start,
    ) == (own, expected)


def test_lru_ties_machine_then_added_time_and_sticky_reuse(accounts, monkeypatch):
    machine = _login(accounts, "codex", machine=True)
    first = _login(accounts, "codex")
    later = datetime.fromisoformat(accounts.store.now()) + timedelta(seconds=1)
    monkeypatch.setattr(accounts.store, "now", lambda: later.isoformat())
    second = _login(accounts, "codex")
    for i in range(3):
        _task(accounts.store, str(i), machine, session=None)
    for i, expected in enumerate((machine, first, second)):
        assert accounts.resolve_login(
            "codex", "", "member", dry_run=False, operation_id=str(i)
        ) == (expected, None)
        assert accounts.store.agent_task_login_id(str(i)) == expected.login_id
    assert accounts.resolve_login("codex", "", "member", previous_login=second) == (second, None)


def test_concurrent_assignments_choose_different_logins(accounts):
    machine = _login(accounts, "codex", machine=True)
    shared = _login(accounts, "codex")
    for name in ("first", "second"):
        _task(accounts.store, name, machine, session=None)
    barrier = Barrier(2)

    def assign(name):
        barrier.wait()
        return accounts.resolve_login("codex", "", "member", dry_run=False, operation_id=name)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(assign, ("first", "second")))
    assert set(results) == {machine, shared}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_lineage_ignores_setting_and_drain_but_refuses_removed(accounts, provider):
    shared = _login(accounts, provider)
    accounts.store.set_provider_login_setting("member", provider, "", "own")
    accounts.store.set_provider_login_draining(shared)
    assert accounts.resolve_login(provider, "", "member", lineage_login=shared) == (shared, None)
    accounts.store.finish_provider_login_removal(shared, member_id="member")
    result, reason = accounts.resolve_login(provider, "", "member", lineage_login=shared)
    assert isinstance(result, LoginRefusal)
    assert result.failure_kind == "provider_auth"
    assert reason is None
    if provider == "codex":
        bound, reason = accounts.resolve_login(provider, "", "member", bound_login=shared)
        assert isinstance(bound, LoginRefusal)
        assert bound.failure_kind == "session_refused"
        assert reason is None


def test_opencode_and_legacy_authorizer_use_machine(accounts):
    accounts.store.set_provider_login_setting("member", "opencode", "", "own")
    assert accounts.resolve_login("opencode", "", "member") == (LoginKey("opencode", "", ""), None)
    accounts.store.set_provider_login_setting("", "codex", "", "own")
    assert accounts.resolve_login("codex", "", None) == (LoginKey("codex", "", ""), None)


def test_session_helpers_only_use_provider_started_matching_turns(accounts):
    first = _login(accounts, "codex", machine=True)
    last = _login(accounts, "codex")
    start = datetime.fromisoformat(accounts.store.now())
    _task(accounts.store, "first", first, started=start.isoformat(), member="first-member")
    _task(accounts.store, "last", last, started=(start + timedelta(seconds=1)).isoformat())
    _task(accounts.store, "unstarted", first)
    _task(
        accounts.store,
        "other-host",
        LoginKey("codex", "other", ""),
        started=(start + timedelta(seconds=2)).isoformat(),
    )
    assert accounts.bound_login("codex", "", "session") == first
    previous = accounts.previous_login("codex", "", "session", exclude_operation_id="unstarted")
    assert previous is not None
    assert previous.login == last
    assert previous.authorized_by == "member"
    assert (
        accounts.previous_login("codex", "", "session", exclude_operation_id="last").login == first
    )


@pytest.mark.parametrize(
    "setting,limited_source,expected",
    [
        ("own", "own", "own"),
        ("own_then_shared", "own", "shared"),
        ("shared_then_own", "shared", "own"),
        ("shared", "shared", "shared"),
        ("own_then_shared", "both", "own"),
        ("shared_then_own", "both", "shared"),
    ],
)
def test_fresh_session_prefers_free_source_and_uses_limited_when_necessary(
    accounts, setting, limited_source, expected
):
    keys = {
        "own": _login(accounts, "claude", own=True),
        "shared": _login(accounts, "claude", machine=True),
    }
    accounts.store.set_provider_login_setting("member", "claude", "", setting)
    for source, key in keys.items():
        if limited_source in {source, "both"}:
            _limit(accounts, key)
    assert accounts.resolve_login("claude", "", "member") == (keys[expected], None)


def test_limited_shared_session_moves_to_free_shared_login(accounts):
    previous = _login(accounts, "claude", machine=True)
    alternative = _login(accounts, "claude")
    _limit(accounts, previous)
    assert accounts.resolve_login(
        "claude", "", "member", previous_login=previous, previous_authorized_by="member"
    ) == (alternative, "limited")


def test_codex_binding_survives_receipt_retention(accounts):
    from rcp.limits import AGENT_TASK_RECEIPT_RETENTION_COUNTS

    key = _login(accounts, "codex")
    _task(accounts.store, "bound", key, started=accounts.store.now())
    for index in range(AGENT_TASK_RECEIPT_RETENTION_COUNTS["summary"] + 5):
        accounts.store.record_agent_task_receipt("bound", f"summary-{index}", {}, tier="summary")
    assert accounts.bound_login("codex", "", "session") == key
