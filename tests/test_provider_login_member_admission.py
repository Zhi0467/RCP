from __future__ import annotations

import pytest

from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.runs.provider_login import ProviderSignedOut
from rcp.service import RunRequest
from rcp.storage import AppStore
from rcp.storage.models import MACHINE_LOGIN_ID, LoginKey

from .helpers import fabricated_authorizer


async def _unused_stream(*args):
    yield ""


def test_admission_resolves_member_before_allocation(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    human = fabricated_authorizer()
    store.set_provider_login_setting(human.user_id, "codex", "", "own")
    tasks = BackgroundAgentTasks(store, _unused_stream)
    request = RunRequest(provider="codex")
    with pytest.raises(ProviderSignedOut):
        tasks.admit_provider_task("project", request, authorized_by=human)
    own = store.add_provider_login("codex", "", member_id=human.user_id, added_by=human.user_id)
    key = LoginKey("codex", "", own.login_id)
    store.mark_provider_login_verified(key, member_id=human.user_id, detail="verified")
    tasks.admit_provider_task("project", request, authorized_by=human)
    assert store.provider_login_assignment(key).last_assigned_at is None
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == 0


def test_typed_gate_refusal_does_not_depend_on_error_text(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    tasks = BackgroundAgentTasks(store, _unused_stream)
    key = LoginKey("codex", "", "")
    execution = AgentTaskExecution(
        "absent", store, AgentProcessControl(), provider_auth_refused=key
    )
    assert (
        tasks._failure_kind("absent", RunRequest(provider="codex"), execution, "opaque")
        == "provider_auth"
    )
    execution.launch_refused = True
    assert (
        tasks._failure_kind("absent", RunRequest(provider="codex"), execution, "opaque")
        == "session_refused"
    )


@pytest.mark.parametrize("mode", ["auto_research", "experiment_loop"])
@pytest.mark.parametrize("switch_provider", [False, True])
def test_recovery_admits_actual_session_or_fresh_target(tmp_path, mode, switch_provider):
    from rcp.agents import AgentEvent
    from rcp.agents.provider_environment import ProviderCredentialStore

    from .helpers import record_launched_experiment_turn, wait_for_task, wait_until
    from .test_auto_research_recovery import (
        _install_recovery_callback,
        _sse,
        _start,
        _store,
        _wait_for_recovery,
    )
    from .test_background import _experiment_request

    store = _store(tmp_path)
    human = fabricated_authorizer()
    own = store.add_provider_login("codex", "", member_id=human.user_id, added_by=human.user_id)
    key = LoginKey("codex", "", own.login_id)
    store.mark_provider_login_verified(key, member_id=human.user_id, detail="verified")
    store.set_provider_login_setting(human.user_id, "codex", "", "own")
    stage = tmp_path / "stage"
    stage.mkdir()
    seen = []

    async def stream(_project, _kind, request, execution):
        seen.append((request.provider, request.session_id))
        execution.checkpoint_stage("", str(stage))
        if execution.continuation == "fresh":
            store.assign_provider_login(execution.operation_id, lambda _: key)
            store.record_agent_task_receipt(execution.operation_id, "provider_runtime_selected", {})
            if mode == "experiment_loop":
                record_launched_experiment_turn(store, execution.operation_id)
        yield _sse(AgentEvent(event="session", session_id=request.session_id or "session"))
        if execution.continuation == "fresh":
            yield _sse(AgentEvent(event="error", text="temporary provider failure"))
        else:
            yield _sse(AgentEvent(event="done"))

    tasks = BackgroundAgentTasks(store, stream)
    if mode == "auto_research":
        _install_recovery_callback(tasks)
        _, root = _start(tasks)
    else:
        root = tasks.start("project", "node_chat", _experiment_request(), authorized_by=human)
    failed = wait_for_task(store, root.operation_id, expect="failed")
    wait_until(lambda: root.operation_id not in tasks._workers)
    assert failed.native_session_id == "session"
    assert failed.request.get("session_id") is None
    if switch_provider:
        store.mark_provider_login_signed_out(
            key, member_id=human.user_id, source="sign_out", detail="out"
        )
        ProviderCredentialStore.for_data_dir(store.path.parent).store_token(
            "claude",
            "",
            "test-token",
            member_id=human.user_id,
            now=store.now(),
            login_id=MACHINE_LOGIN_ID,
        )
        store.set_provider_login_setting(human.user_id, "claude", "", "shared")
        retried = tasks.retry(root.operation_id, provider="claude")
    else:
        # New Codex sessions have no eligible login. The existing session remains
        # bound to its healthy own login even though Shared is now the only source.
        store.set_provider_login_setting(human.user_id, "codex", "", "shared")
        store.mark_provider_login_signed_out(
            LoginKey("codex", "", ""), member_id=human.user_id, source="sign_out", detail="out"
        )
        if mode == "auto_research":
            from rcp.runs.auto_research_recovery import reconcile_due_auto_research_recoveries

            recovery = _wait_for_recovery(store, f"task:{root.operation_id}")
            assert (
                reconcile_due_auto_research_recoveries(tasks, as_of=recovery.next_attempt_at) == 1
            )
            recovery = store.auto_research_recovery(recovery.recovery_id)
            assert recovery is not None and recovery.admitted_operation_id is not None
            retried = store.agent_task(recovery.admitted_operation_id)
            assert retried is not None
        else:
            retried = tasks.retry(root.operation_id)
    wait_for_task(store, retried.operation_id, expect="succeeded")
    assert seen[-1] == (("claude", None) if switch_provider else ("codex", "session"))
