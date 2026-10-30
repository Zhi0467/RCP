from __future__ import annotations

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.auto_research import AutoResearchStartRequest
from rcp.runs.auto_research_admission import start_auto_research
from rcp.runs.provider_login import resume_provider_account

from .helpers import (
    fabricated_authorizer,
    record_launched_experiment_turn,
    wait_for_task,
    wait_until,
)
from .provider_login_switch_helpers import switch_background
from .test_auto_research_recovery import _sse
from .test_background import _experiment_request
from .test_provider_login_lifecycle import _project
from .test_provider_login_resolver import _login
from .test_provider_login_resolver import accounts as accounts


@pytest.mark.parametrize("mode", ["auto_research", "experiment_loop"])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_episode_retry_waits_for_login_without_spending_allocation(
    accounts, tmp_path, mode, provider
):
    store = accounts.store
    _project(store)
    human = fabricated_authorizer()
    original_login = _login(accounts, provider, own=True, member=human.user_id)
    replacement = _login(accounts, provider, machine=True)
    store.set_provider_login_setting(human.user_id, provider, "", "own_then_shared")
    stage = tmp_path / "stage"
    stage.mkdir()

    async def initial_stream(_project, _kind, request, execution):
        execution.checkpoint_stage("", str(stage))
        store.assign_provider_login(execution.operation_id, lambda _: original_login)
        store.record_agent_task_receipt(execution.operation_id, "provider_runtime_selected", {})
        if mode == "experiment_loop":
            record_launched_experiment_turn(store, execution.operation_id)
        yield _sse(AgentEvent(event="session", session_id="session"))
        yield _sse(AgentEvent(event="error", text="temporary provider failure"))

    tasks = BackgroundAgentTasks(store, initial_stream)
    if mode == "auto_research":
        _, root = start_auto_research(
            tasks,
            "project",
            AutoResearchStartRequest(
                invocation_ceiling=4, provider=provider, run_on="local", run_truth_scope=["repo"]
            ),
            authorized_by=human,
            graph_base_head=GraphHeadRef(revision=0),
            ensure_graph_target=lambda _: None,
        )
    else:
        root = tasks.start(
            "project",
            "node_chat",
            _experiment_request().model_copy(update={"provider": provider, "run_on": "local"}),
            authorized_by=human,
        )
    failed = wait_for_task(store, root.operation_id, expect="failed")
    wait_until(lambda: root.operation_id not in tasks._workers)
    before = store.episode(failed.episode_id)
    for key in (original_login, replacement):
        store.mark_provider_login_signed_out(
            key, member_id=human.user_id, source="sign_out", detail="expired"
        )
    selected = []
    tasks = switch_background(store, stage, selected)
    queued = tasks.retry(root.operation_id, authorized_by=human)
    assert queued.status == "queued"
    assert queued.authorized_by == human
    assert queued.native_session_id == failed.native_session_id
    assert store.episode(failed.episode_id).invocations_used == before.invocations_used
    assert selected == []
    store.mark_provider_login_verified(replacement, member_id=human.user_id, detail="verified")
    resume_provider_account(tasks, replacement)
    finished = wait_for_task(store, queued.operation_id, expect="succeeded")
    wait_until(lambda: queued.operation_id not in tasks._workers)
    assert selected == [(replacement, "session", None)]
    assert finished.authorized_by == human
    assert finished.login_id == replacement.login_id
    assert store.episode(failed.episode_id).invocations_used == before.invocations_used
