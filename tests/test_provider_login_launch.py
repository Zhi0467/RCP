from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from rcp.agents import AgentEvent, AgentLauncher, AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.shared import _ProviderOutcome, _stream_agent_events
from rcp.service import RunRequest

from .test_provider_login_resolver import _limit, _login, _task
from .test_provider_login_resolver import accounts as accounts

MEMBER = "00000000-0000-4000-8000-000000000001"
SPACE = "00000000-0000-4000-8000-000000000002"


def _started_task(
    accounts, operation_id, key, *, started=True, session: str | None = "session", parent=None
):
    _task(
        accounts.store,
        operation_id,
        key,
        started=accounts.store.now() if started else None,
        session=session,
        member=MEMBER,
    )
    with accounts.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_space_id='00000000-0000-4000-8000-000000000002', authorized_display_name='Member', "
            "parent_operation_id=? WHERE operation_id=?",
            (parent, operation_id),
        )


async def _launch(
    accounts,
    tmp_path,
    key,
    *,
    session: str | None = "session",
    parent=None,
    fork=False,
    gate_failure=False,
    lineage=False,
):
    _started_task(accounts, "current", key, started=False, session=session, parent=parent)
    with accounts.store.connection() as connection:
        connection.execute("UPDATE graph_runs SET login_id=NULL WHERE operation_id='current'")
    if lineage:
        accounts.store.record_agent_task_receipt("current", "provider_login_lineage", {})
    execution = AgentTaskExecution("current", accounts.store, AgentProcessControl())
    outcome = _ProviderOutcome()
    selected = []

    async def stream(_provider, _prompt, **kwargs):
        selected.append(kwargs["login_key"])
        if gate_failure:
            accounts.store.mark_provider_login_signed_out(
                selected[-1], member_id="member", source="sign_out", detail="out"
            )
            replacement = await kwargs["login_unavailable"](selected[-1])
            if replacement is None:
                yield AgentEvent(event="error", text="unavailable")
                return
            selected.append(replacement)
        await kwargs["before_start"]()
        yield AgentEvent(event="done")

    request = RunRequest(provider=key.provider, mode="discuss", message="hello")
    if fork:
        request = request.model_copy(
            update={
                "fork_session_id": "session",
                "fork_source_host": "",
                "fork_source_cwd": str(tmp_path),
            }
        )
    frames = [
        frame
        async for frame in _stream_agent_events(
            cast(AgentLauncher, SimpleNamespace(stream=stream)),
            request,
            "hello",
            workspace=tmp_path,
            session_id=session,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            execution_host="",
            execution=execution,
            remote_stage=None,
            capability="discuss",
            outcome=outcome,
            binary=None,
        )
    ]
    return execution, selected, outcome, frames


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("event", ["limited", "unhealthy", "draining"])
async def test_real_launch_moves_claude_and_binds_codex(accounts, tmp_path, provider, event):
    own = _login(accounts, provider, own=True, member=MEMBER)
    shared = _login(accounts, provider, machine=True)
    accounts.store.set_provider_login_setting(MEMBER, provider, "", "own_then_shared")
    _started_task(accounts, "previous", own)
    if event == "limited":
        _limit(accounts, own)
    elif event == "unhealthy":
        accounts.store.mark_provider_login_signed_out(
            own, member_id="member", source="sign_out", detail="out"
        )
    else:
        accounts.store.set_provider_login_draining(own)
    execution, selected, outcome, _ = await _launch(accounts, tmp_path, own)
    receipts = [
        r
        for r in accounts.store.agent_task_receipts("current")
        if r.category == "provider_login_switched"
    ]
    if provider == "claude":
        assert selected == [shared]
        assert accounts.store.agent_task("current").login_id == shared.login_id
        assert receipts[0].payload == {
            "from": own.login_id,
            "to": shared.login_id,
            "reason": "limited" if event == "limited" else "unhealthy",
        }
    elif event == "unhealthy":
        assert selected == []
        assert outcome.failed
        assert execution.provider_auth_refused == own
        assert accounts.store.agent_task("current").login_id == own.login_id
        assert accounts.store.provider_login_assignment(own).last_assigned_at is None
        assert receipts == []
    else:
        assert selected == [own]
        assert accounts.store.agent_task("current").login_id == own.login_id
        assert receipts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_gate_recheck_falls_back_only_for_unbound_work(accounts, tmp_path, provider):
    own = _login(accounts, provider, own=True, member=MEMBER)
    shared = _login(accounts, provider, machine=True)
    accounts.store.set_provider_login_setting(MEMBER, provider, "", "own_then_shared")
    _started_task(accounts, "previous", own)
    execution, selected, outcome, _ = await _launch(accounts, tmp_path, own, gate_failure=True)
    if provider == "claude":
        assert selected == [own, shared]
        assert execution.login_key == shared
        assert accounts.store.agent_task("current").login_id == shared.login_id
        assert not outcome.failed
    else:
        assert selected == [own]
        assert execution.provider_auth_refused == own
        assert outcome.failed


@pytest.mark.asyncio
@pytest.mark.parametrize("lineage", ["transport_auto_retry", "provider_login_lineage"])
async def test_lineage_keeps_parent_login(accounts, tmp_path, lineage):
    own = _login(accounts, "claude", own=True, member=MEMBER)
    shared = _login(accounts, "claude", machine=True)
    accounts.store.set_provider_login_setting(MEMBER, "claude", "", "shared")
    _started_task(accounts, "parent", own)
    if lineage == "transport_auto_retry":
        accounts.store.record_agent_task_receipt("parent", lineage, {})
    _, selected, _, _ = await _launch(
        accounts, tmp_path, shared, parent="parent", lineage=lineage == "provider_login_lineage"
    )
    assert selected == [own]
    assert accounts.store.agent_task("current").login_id == own.login_id


@pytest.mark.asyncio
async def test_pre_provider_failure_does_not_supply_previous_login(accounts, tmp_path):
    own = _login(accounts, "claude", own=True, member=MEMBER)
    shared = _login(accounts, "claude", machine=True)
    accounts.store.set_provider_login_setting(MEMBER, "claude", "", "shared")
    _started_task(accounts, "never_started", own, started=False)
    _, selected, _, _ = await _launch(accounts, tmp_path, shared)
    assert selected == [shared]
    assert not any(
        r.category == "provider_login_switched"
        for r in accounts.store.agent_task_receipts("current")
    )


@pytest.mark.asyncio
async def test_removed_codex_binding_is_session_refused(accounts, tmp_path):
    own = _login(accounts, "codex", own=True, member=MEMBER)
    _started_task(accounts, "previous", own)
    accounts.store.finish_provider_login_removal(own)
    execution, selected, outcome, _ = await _launch(accounts, tmp_path, own)
    assert selected == []
    assert outcome.failed and execution.launch_refused
    assert execution.provider_auth_refused is None


@pytest.mark.asyncio
async def test_codex_fork_uses_source_login(accounts, tmp_path):
    own = _login(accounts, "codex", own=True, member=MEMBER)
    shared = _login(accounts, "codex", machine=True)
    accounts.store.set_provider_login_setting(MEMBER, "codex", "", "shared")
    _started_task(accounts, "source", own)
    execution, selected, _, _ = await _launch(accounts, tmp_path, shared, session=None, fork=True)
    assert selected == [own]
    assert execution.login_key == own
    assert accounts.store.agent_task("current").login_id == own.login_id


@pytest.mark.asyncio
async def test_launch_capture_fences_only_its_login(accounts, tmp_path):
    from rcp.background import BackgroundAgentTasks

    own = _login(accounts, "codex", own=True, member=MEMBER)
    shared = _login(accounts, "codex", machine=True)
    accounts.store.set_provider_login_setting(MEMBER, "codex", "", "own")
    execution, selected, _, _ = await _launch(accounts, tmp_path, own, session=None)

    async def unused_stream(*_args):
        yield ""

    background = BackgroundAgentTasks(accounts.store, unused_stream)
    kind = background._failure_kind(
        "current", RunRequest(provider="codex"), execution, "refresh_token_reused"
    )
    assert selected == [own]
    assert kind == "provider_auth"
    assert accounts.store.provider_login_state(own).state == "signed_out"
    assert accounts.store.provider_login_state(shared).state == "signed_in"


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_launcher_releases_failed_gate_before_reselection(
    accounts, tmp_path, monkeypatch, bound
):
    import asyncio

    from rcp.agents import ProviderReadiness
    from rcp.agents.provider_environment import ProviderProcessEnvironment

    own = _login(accounts, "codex", own=True, member=MEMBER)
    other = _login(accounts, "codex", machine=True)
    launcher = AgentLauncher(accounts=accounts)
    held = []
    released = []
    environments = []

    async def hold(key):
        held.append(key)
        if key == own:
            accounts.store.mark_provider_login_signed_out(
                key, member_id=MEMBER, source="sign_out", detail="out"
            )
        return SimpleNamespace(release=lambda: released.append(key))

    async def reselect(key):
        assert key in released
        return None if bound else other

    def environment(key):
        environments.append(key)
        return ProviderProcessEnvironment()

    async def spawn(*_args, **_kwargs):
        raise RuntimeError("process boundary reached")

    monkeypatch.setattr(launcher.credential_gate, "hold", hold)
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda *_, **__: ProviderReadiness(
            provider="codex",
            label="Codex",
            installed=True,
            authenticated=True,
        ),
    )
    monkeypatch.setattr(launcher, "process_environment", environment)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ValueError if bound else RuntimeError):
        async for _ in launcher.stream(
            "codex",
            "hello",
            cwd=tmp_path,
            capability="discuss",
            login_key=own,
            login_unavailable=reselect,
        ):
            pass
    assert held == ([own] if bound else [own, other])
    assert released == held
    assert environments == ([] if bound else [other])


def test_readiness_cache_and_invalidation_are_per_login(accounts, monkeypatch):
    from rcp.agents import ProviderReadiness

    own = _login(accounts, "codex", own=True, member=MEMBER)
    other = _login(accounts, "codex", machine=True)
    launcher = AgentLauncher(accounts=accounts)
    probes = []

    def probe(provider, *, host, binary, refresh, login_id=""):
        probes.append(login_id)
        return ProviderReadiness(
            provider=provider,
            label="Codex",
            installed=True,
            authenticated=True,
            version=login_id or "machine",
        )

    monkeypatch.setattr(launcher, "_readiness_uncached", probe)
    for key in (own, other, own, other):
        assert launcher.readiness("codex", login_id=key.login_id).version == (
            key.login_id or "machine"
        )
    assert probes == [own.login_id, other.login_id]
    launcher.invalidate_readiness("codex", login_id=own.login_id)
    assert launcher.cached_readiness("codex", login_id=own.login_id) is None
    assert launcher.cached_readiness("codex", login_id=other.login_id) is not None


@pytest.mark.asyncio
async def test_claude_fork_is_a_fresh_login_choice(accounts, tmp_path):
    source = _login(accounts, "claude")
    shared = _login(accounts, "claude", machine=True)
    accounts.store.set_provider_login_setting(MEMBER, "claude", "", "shared")
    _started_task(accounts, "source", source)
    _, selected, _, _ = await _launch(accounts, tmp_path, source, session=None, fork=True)
    assert selected == [shared]
    assert not any(
        receipt.category == "provider_login_switched"
        for receipt in accounts.store.agent_task_receipts("current")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize(
    "reason", ["sender_setting", "setting_changed", "preferred_source_available"]
)
async def test_turn_boundary_records_setting_move_reason(
    accounts, tmp_path, monkeypatch, provider, reason
):
    from datetime import datetime, timedelta

    own = _login(accounts, provider, own=True, member=MEMBER)
    shared = _login(accounts, provider, machine=True)
    previous = shared if reason == "preferred_source_available" else own
    expected = own if reason == "preferred_source_available" else shared
    accounts.store.set_provider_login_setting(MEMBER, provider, "", "own_then_shared")
    _started_task(accounts, "previous", previous)
    if reason == "sender_setting":
        with accounts.store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET authorized_user_id=? WHERE operation_id='previous'", (SPACE,)
            )
        accounts.store.set_provider_login_setting(MEMBER, provider, "", "shared")
    elif reason == "setting_changed":
        later = (datetime.fromisoformat(accounts.store.now()) + timedelta(seconds=1)).isoformat()
        monkeypatch.setattr(accounts.store, "now", lambda: later)
        accounts.store.set_provider_login_setting(MEMBER, provider, "", "shared")
    _, selected, _, _ = await _launch(accounts, tmp_path, previous)
    receipts = [
        r
        for r in accounts.store.agent_task_receipts("current")
        if r.category == "provider_login_switched"
    ]
    assert selected == ([expected] if provider == "claude" else [previous])
    assert accounts.store.agent_task("current").login_id == selected[0].login_id
    assert [r.payload["reason"] for r in receipts] == ([reason] if provider == "claude" else [])
