from __future__ import annotations

import hashlib

import pytest

from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.runs.provider_process import require_remote_provider_quiescence
from rcp.storage import AppStore

from .test_experiment_episode_storage import _admit_root
from .test_remote_provider_receipts import _store


@pytest.mark.parametrize("stopped", [False, None])
def test_remote_checkpoint_refuses_live_or_unknown_before_mutating_stage(
    tmp_path, monkeypatch, stopped
):
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid")
    store.fail_agent_task("first", "SSH disconnected")
    reopened = AppStore(tmp_path / "state.sqlite3")
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args: stopped)
    execution = AgentTaskExecution("second", reopened, AgentProcessControl())

    with pytest.raises(ValueError, match="confirmed stopped"):
        execution.checkpoint_stage("remote", "/stage")

    assert execution.stage_root is None
    assert reopened.agent_task("second").stage_root is None
    assert reopened.unresolved_remote_provider_passes("remote", "/stage") == [
        ("first", "/stage/one.pid")
    ]
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args: True)
    execution.checkpoint_stage("remote", "/stage")
    assert reopened.agent_task("second").stage_root == "/stage"
    assert reopened.unresolved_remote_provider_passes("remote", "/stage") == []


@pytest.mark.parametrize("stopped", [False, None])
@pytest.mark.parametrize("switch", [False, True])
def test_experiment_retry_checks_remote_pass_before_admission(
    tmp_path, monkeypatch, stopped, switch
):
    store = AppStore(tmp_path / "state.sqlite3")
    episode_id, root = _admit_root(store)
    store.checkpoint_agent_task(root.operation_id, stage_host="remote", stage_root="/stage")
    store.record_agent_task_contract(
        root.operation_id,
        "experiment_episode_context_candidate",
        "{}",
        hashlib.sha256(b"{}").hexdigest(),
    )
    store.begin_remote_provider_pass(root.operation_id, "remote", "/stage", "/stage/one.pid")
    store.fail_agent_task(root.operation_id, "session limit")
    reopened = AppStore(tmp_path / "state.sqlite3")

    async def forbidden_stream(*_args):
        raise AssertionError("No provider should launch during this admission check")
        yield

    tasks = BackgroundAgentTasks(reopened, forbidden_stream)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args: stopped)
    overrides = {"provider": "claude", "model": "sonnet"} if switch else {}
    with pytest.raises(ValueError, match="confirmed stopped"):
        tasks.retry(root.operation_id, **overrides)
    assert len(reopened.episode_tasks(episode_id)) == 1
    assert reopened.episode(episode_id).invocations_used == 1


def test_quiescence_settles_only_verified_pass_and_preserves_stage_until_then(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", "/stage", "/stage/one.pid")
    store.fail_agent_task("first", "SSH disconnected")
    assert "/stage" in store.protected_run_stage_roots("remote")
    lifecycle = next(item for item in store.run_stage_lifecycles() if item.stage_root == "/stage")
    assert lifecycle.must_exist
    observed = []

    def confirmed(host, pid_file):
        observed.append((host, pid_file))
        return True

    monkeypatch.setattr(AgentProcessControl, "remote_stopped", confirmed)
    require_remote_provider_quiescence(store, "remote", "/stage")
    assert observed == [("remote", "/stage/one.pid")]
    assert store.unresolved_remote_provider_passes("remote", "/stage") == []
    assert "/stage" not in store.protected_run_stage_roots("remote")
    store.begin_remote_provider_pass("second", "remote", "/stage", "/stage/two.pid")
