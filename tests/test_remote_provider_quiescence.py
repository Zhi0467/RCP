from __future__ import annotations

import hashlib
import shlex
import sys

import pytest

from rcp.agents import launcher
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
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args, **_kwargs: stopped)
    execution = AgentTaskExecution("second", reopened, AgentProcessControl())

    with pytest.raises(ValueError, match="confirmed stopped"):
        execution.checkpoint_stage("remote", "/stage")

    assert execution.stage_root is None
    assert reopened.agent_task("second").stage_root is None
    assert reopened.unresolved_remote_provider_passes("remote", "/stage") == [
        ("first", "/stage/one.pid")
    ]
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args, **_kwargs: True)
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
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args, **_kwargs: stopped)
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

    def confirmed(host, pid_file, **_kwargs):
        observed.append((host, pid_file))
        return True

    monkeypatch.setattr(AgentProcessControl, "remote_stopped", confirmed)
    require_remote_provider_quiescence(store, "remote", "/stage")
    assert observed == [("remote", "/stage/one.pid")]
    assert store.unresolved_remote_provider_passes("remote", "/stage") == []
    assert "/stage" not in store.protected_run_stage_roots("remote")
    store.begin_remote_provider_pass("second", "remote", "/stage", "/stage/two.pid")


def test_a_pass_that_never_started_stops_fencing_its_stage(tmp_path, monkeypatch):
    """A launch that failed before the host ran anything must not fence forever.

    The fence runs long after that launch ended, so it asks the real probe and
    accepts the one absence the host can prove: a stage that still stands and
    holds no pidfile the wrapper would have written before exec.
    """

    stage = tmp_path / "stage"
    stage.mkdir()
    pid_file = str(stage / "one.pid")
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", "remote", str(stage), pid_file)
    store.fail_agent_task("first", "SSH disconnected")

    monkeypatch.setattr(
        launcher,
        "ssh_arguments",
        lambda _host, remote_command: [sys.executable, *shlex.split(remote_command)[1:]],
    )
    require_remote_provider_quiescence(store, "remote", str(stage))

    assert store.unresolved_remote_provider_passes("remote", str(stage)) == []
