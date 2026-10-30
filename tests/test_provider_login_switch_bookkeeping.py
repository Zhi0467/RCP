"""Turn accounting must follow the credential selected after a session moves."""

from __future__ import annotations

import pytest

from rcp.agents import AgentLauncher
from rcp.background import BackgroundAgentTasks
from rcp.providers.base import ProviderUsage
from rcp.runs.provider_login import bound_running_count, remove_shared_login, settle_provider_login
from rcp.service import RunRequest

from .test_provider_login_launch import MEMBER, _launch, _started_task
from .test_provider_login_resolver import _login
from .test_provider_login_resolver import accounts as accounts


async def _unused_stream(*_args):
    yield ""


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("repaired", [False, True])
async def test_switched_turn_revokes_only_captured_login_generation(
    accounts, tmp_path, provider, repaired
):
    old = _login(accounts, provider, own=True, member=MEMBER)
    current = _login(accounts, provider, machine=True)
    accounts.store.set_provider_login_setting(MEMBER, provider, "", "own_then_shared")
    _started_task(accounts, "previous", old)
    accounts.store.mark_provider_login_signed_out(
        old, member_id=MEMBER, source="sign_out", detail="expired"
    )
    old_state = accounts.store.provider_login_state(old)
    execution, selected, _, _ = await _launch(accounts, tmp_path, old)
    assert selected == [current]
    captured_generation = execution.login_generation
    if repaired:
        accounts.store.mark_provider_login_verified(current, member_id=MEMBER, detail="verified")
    background = BackgroundAgentTasks(accounts.store, _unused_stream)
    error = "refresh_token_reused" if provider == "codex" else "401 authentication OAuth failed"
    kind = background._failure_kind("current", RunRequest(provider=provider), execution, error)
    assert kind == (None if repaired else "provider_auth")
    assert accounts.store.provider_login_state(old) == old_state
    state = accounts.store.provider_login_state(current)
    assert state.state == ("signed_in" if repaired else "signed_out")
    assert state.generation == captured_generation + int(repaired)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_switched_turn_accounts_limits_usage_and_drain_on_its_login(
    accounts, tmp_path, provider
):
    store = accounts.store
    old = _login(accounts, provider)
    current = _login(accounts, provider, machine=True)
    store.set_provider_login_setting(MEMBER, provider, "", "shared")
    _started_task(accounts, "previous", old)
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='previous'")
    store.mark_provider_login_signed_out(old, member_id=MEMBER, source="sign_out", detail="expired")
    old_assignment = store.provider_login_assignment(old)
    _, selected, _, _ = await _launch(accounts, tmp_path, old)
    assert selected == [current]
    assert store.provider_login_assignment(old) == old_assignment
    assert store.provider_login_assignment(current).last_assigned_at is not None
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='running' WHERE operation_id='current'")
    assert bound_running_count(store, old) == 0
    assert bound_running_count(store, current) == 1
    removed = remove_shared_login(store, AgentLauncher(), old, cancel_sign_in=lambda _: None)
    assert removed.removed_at is not None
    assert store.agent_task("current").status == "running"
    usage = ProviderUsage(
        provider_profile=provider,
        provider_event_type="turn.completed",
        dedupe_key="same-session-event",
        processed_input_tokens=12,
        generated_tokens=3,
    )
    # Accounting is per operation, so a session moving credentials neither loses
    # a new turn's usage nor recounts replay of an already recorded event.
    first = store.record_agent_usage("previous", usage)
    second = store.record_agent_usage("current", usage)
    replay = store.record_agent_usage("current", usage, idempotent=True)
    assert first.counted and second.counted
    assert replay.usage_id == second.usage_id
    assert len(store.agent_usage("project")) == 2
    store.record_agent_task_receipt("current", "provider_runtime_selected", {})
    store.record_agent_task_receipt(
        "current", "provider_terminal_error", {"classification": "session_limit"}
    )
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='failed' WHERE operation_id='current'")
    settle_provider_login(BackgroundAgentTasks(store, _unused_stream), store.agent_task("current"))
    assert store.provider_login_assignment(current).limited_at is not None
    assert store.provider_login_assignment(old).limited_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_personal_session_survives_team_auth_failure_on_next_team_login(
    accounts, tmp_path, provider
):
    from .helpers import wait_for_task, wait_until
    from .provider_login_switch_helpers import PROJECT, switch_background, switched_session

    stage = tmp_path / "stage"
    old, team_a, member, request = switched_session(accounts, stage, provider, "personal_to_team")
    team_b = _login(accounts, provider)
    # The first continuation really selects team A, then its provider reports
    # revoked credentials. Failure evidence must not be charged to personal A.
    execution, selected, _, _ = await _launch(accounts, stage, old)
    assert selected == [team_a]
    store = accounts.store
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET project_id=?, kind='project_chat', request_json=?, "
            "stage_root=?, authorized_user_id=? WHERE operation_id='current'",
            (PROJECT, request.model_dump_json(), str(stage), member.user_id),
        )
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='previous'")
    store.record_agent_task_receipt("current", "provider_runtime_selected", {})
    selected = []
    background = switch_background(store, stage, selected)
    evidence = "refresh_token_reused" if provider == "codex" else "401 authentication OAuth failed"
    kind = background._failure_kind("current", request, execution, evidence)
    assert kind == "provider_auth"
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='failed', failure_kind=? WHERE operation_id='current'",
            (kind,),
        )
    settle_provider_login(background, store.agent_task("current"))
    child = wait_until(
        lambda: next(
            (
                row
                for row in store.all_project_agent_tasks(PROJECT)
                if row.parent_operation_id == "current"
            ),
            None,
        )
    )
    finished = wait_for_task(store, child.operation_id, expect="succeeded")
    assert selected == [(team_b, "session", None)]
    assert finished.login_id == team_b.login_id
    assert store.provider_login_state(old).state == "signed_out"
    assert store.provider_login_state(team_a).state == "signed_out"
    assert store.provider_login_state(team_b).state == "signed_in"
    assert store.agent_task_has_receipt("current", "provider_auth_auto_retry")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_lineage_gate_recheck_reselects_after_credential_expires(
    accounts, tmp_path, provider
):
    own = _login(accounts, provider, own=True, member=MEMBER)
    shared = _login(accounts, provider, machine=True)
    accounts.store.set_provider_login_setting(MEMBER, provider, "", "own_then_shared")
    _started_task(accounts, "parent", own)
    execution, selected, outcome, _ = await _launch(
        accounts, tmp_path, own, parent="parent", lineage=True, gate_failure=True
    )
    assert selected == [own, shared]
    assert execution.login_key == shared
    assert not outcome.failed
