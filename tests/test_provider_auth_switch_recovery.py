from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from rcp.agents import AgentEvent, AgentLauncher
from rcp.background import BackgroundAgentTasks
from rcp.runs.provider_login import resume_provider_account, settle_provider_login
from rcp.runs.shared import _ProviderOutcome, _stream_agent_events
from rcp.service import RunRequest

from .helpers import fabricated_authorizer, wait_for_task, wait_until
from .test_provider_login_lifecycle import _project, _turn
from .test_provider_login_resolver import _login
from .test_provider_login_resolver import accounts as accounts


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("available", [True, False])
def test_auth_failed_work_retries_on_usable_login(accounts, tmp_path, provider, available):
    store = accounts.store
    _project(store)
    member = fabricated_authorizer().user_id
    old = _login(accounts, provider, own=True, member=member)
    next_login = _login(accounts, provider, machine=True)
    store.set_provider_login_setting(member, provider, "", "own_then_shared")
    request = RunRequest(
        provider=provider, mode="work", message="continue", chat_id="chat", chat_scope="project"
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    original = _turn(accounts, old)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_user_id=? WHERE operation_id=?",
            (member, original.operation_id),
        )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='project_chat', request_json=?, "
            "stage_root=?, failure_kind='provider_auth' WHERE operation_id=?",
            (request.model_dump_json(), str(stage), original.operation_id),
        )
    store.mark_provider_login_signed_out(old, member_id=member, source="sign_out", detail="expired")
    if not available:
        store.mark_provider_login_signed_out(
            next_login, member_id=member, source="sign_out", detail="expired"
        )
    selected = []

    async def launch(_provider, _prompt, **kwargs):
        selected.append((kwargs["login_key"], kwargs["session_id"]))
        await kwargs["before_start"]()
        yield AgentEvent(event="done")

    async def stream(_project, _kind, request, execution):
        async for frame in _stream_agent_events(
            cast(AgentLauncher, SimpleNamespace(stream=launch)),
            request,
            "continue",
            workspace=stage,
            session_id=request.session_id,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            execution_host="",
            execution=execution,
            remote_stage=None,
            capability="work",
            outcome=_ProviderOutcome(),
            binary=None,
        ):
            yield frame

    background = BackgroundAgentTasks(store, stream)
    settle_provider_login(background, original)
    if not available:
        assert selected == []
        assert not store.agent_task_has_continuation(original.operation_id)
        store.mark_provider_login_verified(next_login, member_id=member, detail="verified")
        resume_provider_account(background, next_login)
    wait_until(lambda: store.agent_task_has_continuation(original.operation_id))
    child = next(task for task in store.agent_tasks("project") if task.parent_operation_id)
    wait_for_task(store, child.operation_id, expect="succeeded")
    wait_until(lambda: child.operation_id not in background._workers)
    assert selected == [(next_login, original.native_session_id)]
    assert store.agent_task(child.operation_id).login_id == next_login.login_id
    assert (
        store.agent_task_has_receipt(original.operation_id, "provider_auth_auto_retry") == available
    )
    settle_provider_login(background, original)
    assert len(selected) == 1


def test_immediate_auth_retry_is_bounded_but_verify_releases_again(accounts, monkeypatch):
    store = accounts.store
    _project(store)
    member = fabricated_authorizer().user_id
    old = _login(accounts, "codex", own=True, member=member)
    team = _login(accounts, "codex", machine=True)
    store.set_provider_login_setting(member, "codex", "", "own_then_shared")
    original = _turn(accounts, old)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_user_id=? WHERE operation_id=?",
            (member, original.operation_id),
        )
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET failure_kind='provider_auth'")
    store.mark_provider_login_signed_out(old, member_id=member, source="sign_out", detail="expired")
    calls = []

    async def unused(*args):
        yield ""

    background = BackgroundAgentTasks(store, unused)
    monkeypatch.setattr(background, "retry", lambda operation_id, **_: calls.append(operation_id))
    settle_provider_login(background, original)
    child = _turn(accounts, team, operation_id="child")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_user_id=? WHERE operation_id=?",
            (member, child.operation_id),
        )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET parent_operation_id=?, failure_kind='provider_auth' "
            "WHERE operation_id=?",
            (original.operation_id, child.operation_id),
        )
    store.mark_provider_login_signed_out(
        team, member_id=member, source="sign_out", detail="expired"
    )
    store.mark_provider_login_verified(old, member_id=member, detail="verified")
    settle_provider_login(background, child)
    assert calls == [original.operation_id]
    resume_provider_account(background, old)
    assert calls == [original.operation_id, child.operation_id]
    receipt = next(
        r
        for r in store.agent_task_receipts(original.operation_id)
        if r.category == "provider_auth_auto_retry"
    )
    assert receipt.payload == {"from": old.login_id, "to": team.login_id}


@pytest.mark.parametrize("holder_status", ["succeeded", "failed", "paused"])
def test_busy_auth_retry_wakes_when_session_holder_settles(accounts, monkeypatch, holder_status):
    store = accounts.store
    _project(store)
    old = _login(accounts, "codex", machine=True)
    _login(accounts, "codex")
    original = _turn(accounts, old)
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET failure_kind='provider_auth'")
    store.mark_provider_login_signed_out(old, member_id=None, source="sign_out", detail="expired")
    calls = []

    async def unused(*args):
        yield ""

    def retry(operation_id, **kwargs):
        calls.append(operation_id)
        if len(calls) == 1:
            raise ValueError("session occupied")

    background = BackgroundAgentTasks(store, unused)
    monkeypatch.setattr(background, "retry", retry)
    settle_provider_login(background, original)
    assert calls == [original.operation_id]
    holder = _turn(accounts, old, status=holder_status, operation_id="holder")
    settle_provider_login(background, holder)
    assert calls == [original.operation_id, original.operation_id]
    assert store.agent_task_has_receipt(original.operation_id, "provider_auth_retry_deferred")


def test_auth_failure_superseded_by_later_chat_is_not_replayed(accounts, monkeypatch):
    store = accounts.store
    _project(store)
    old = _login(accounts, "codex", machine=True)
    usable = _login(accounts, "codex")
    original = _turn(accounts, old)
    _turn(accounts, usable, status="succeeded", operation_id="later")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json, '$.chat_id', 'chat')"
        )
        connection.execute(
            "UPDATE graph_runs SET failure_kind='provider_auth' WHERE operation_id=?",
            (original.operation_id,),
        )
    store.mark_provider_login_signed_out(old, member_id=None, source="sign_out", detail="expired")
    calls = []

    async def unused(*args):
        yield ""

    background = BackgroundAgentTasks(store, unused)
    monkeypatch.setattr(background, "retry", lambda operation_id, **_: calls.append(operation_id))
    settle_provider_login(background, original)
    resume_provider_account(background, usable)
    assert calls == []


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("direction", ["personal_to_team", "team_to_personal"])
@pytest.mark.parametrize("operation", ["resume", "retry"])
@pytest.mark.parametrize("release", ["verify", "setting"])
def test_requested_recovery_waits_for_allowed_login(
    accounts, tmp_path, provider, direction, operation, release
):
    from unittest.mock import Mock

    from rcp.runs.provider_login import provider_login_changed
    from rcp.watchers import WatcherDelivery

    from .provider_login_switch_helpers import switch_background, switched_session

    stage = tmp_path / "stage"
    _, usable, member, _ = switched_session(
        accounts,
        stage,
        provider,
        direction,
        status="paused" if operation == "resume" else "failed",
    )
    store = accounts.store
    if release == "verify":
        store.mark_provider_login_signed_out(
            usable, member_id=member.user_id, source="sign_out", detail="expired"
        )
    else:
        store.set_provider_login_setting(
            member.user_id, provider, "", "own" if direction == "personal_to_team" else "shared"
        )
    selected = []
    background = switch_background(store, stage, selected)
    child = getattr(background, operation)("previous", authorized_by=member)
    assert child.status == "queued"
    assert selected == []
    if release == "verify":
        store.mark_provider_login_verified(usable, member_id=member.user_id, detail="verified")
        resume_provider_account(background, usable)
    else:
        store.set_provider_login_setting(member.user_id, provider, "", "own_then_shared")
        provider_login_changed(
            background,
            member_id=member.user_id,
            provider=provider,
            host="",
            reconcile_episodes=lambda: 0,
            watcher_delivery=Mock(spec=WatcherDelivery),
        )
    wait_for_task(store, child.operation_id, expect="succeeded")
    wait_until(lambda: child.operation_id not in background._workers)
    assert selected == [(usable, "session", None)]


@pytest.mark.parametrize("deferred", [False, True])
def test_auth_retry_bound_survives_receipt_retention(accounts, monkeypatch, deferred):
    from rcp.limits import AGENT_TASK_RECEIPT_RETENTION_COUNTS

    store = accounts.store
    _project(store)
    old = _login(accounts, "codex", machine=True)
    _login(accounts, "codex")
    original = _turn(accounts, old)
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET failure_kind='provider_auth'")
    store.mark_provider_login_signed_out(old, member_id=None, source="sign_out", detail="expired")
    calls = []

    async def unused(*args):
        yield ""

    def retry(operation_id, **kwargs):
        calls.append(operation_id)
        if deferred and len(calls) == 1:
            raise ValueError("session occupied")

    background = BackgroundAgentTasks(store, unused)
    monkeypatch.setattr(background, "retry", retry)
    settle_provider_login(background, original)
    for tier in ("summary", "diagnostic"):
        for _ in range(AGENT_TASK_RECEIPT_RETENTION_COUNTS[tier] + 1):
            store.record_agent_task_receipt(original.operation_id, "observation", {}, tier=tier)
    settle_provider_login(background, original)
    assert len(calls) == (2 if deferred else 1)
