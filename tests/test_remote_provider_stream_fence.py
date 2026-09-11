from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.shared import _ProviderOutcome, _stream_agent_events
from rcp.service import RunRequest
from rcp.storage import AgentTaskAdmissionConflict

from .test_remote_provider_receipts import _store


class _Stage:
    root = PurePosixPath("/stage")

    def finalize_inputs(self):
        pass

    def list_workspace_files(self):
        return []


async def _consume(store, launcher, tmp_path, *, stage=None):
    outcome = _ProviderOutcome()
    events = [
        event
        async for event in _stream_agent_events(
            launcher,
            RunRequest(provider="codex"),
            "Run the task",
            workspace=tmp_path,
            session_id=None,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            execution_host="remote",
            execution=AgentTaskExecution("first", store, AgentProcessControl()),
            remote_stage=stage or _Stage(),
            capability="work_auto",
            outcome=outcome,
            binary=None,
        )
    ]
    return outcome, events


@pytest.mark.asyncio
async def test_remote_stream_reserves_before_provider_and_settles_pre_prompt_fallback(tmp_path):
    store = _store(tmp_path)
    advanced = []

    class Launcher:
        async def stream(self, *_args, **kwargs):
            first_pid = kwargs["remote_pid_file"]
            yield AgentEvent(event="remote_process_start", text=first_pid)
            assert store.unresolved_remote_provider_passes("remote", "/stage") == [
                ("first", first_pid)
            ]
            advanced.append("first process")
            # The first candidate failed before a prompt, with confirmed process absence.
            yield AgentEvent(event="remote_process_stop", text=first_pid)
            assert store.unresolved_remote_provider_passes("remote", "/stage") == []
            second_pid = "/stage/fallback.pid"
            yield AgentEvent(event="remote_process_start", text=second_pid)
            assert store.unresolved_remote_provider_passes("remote", "/stage") == [
                ("first", second_pid)
            ]
            advanced.append("fallback process")
            yield AgentEvent(event="remote_process_stop", text=second_pid)
            yield AgentEvent(event="done")

    outcome, events = await _consume(store, Launcher(), tmp_path)
    assert advanced == ["first process", "fallback process"]
    assert outcome.completed
    assert events == []  # Internal process evidence is not user-visible provider text.
    assert store.unresolved_remote_provider_passes("remote", "/stage") == []


@pytest.mark.asyncio
async def test_remote_stream_unknown_exit_before_prompt_retains_fence(tmp_path):
    store = _store(tmp_path)
    recorded_pid = []

    class Launcher:
        async def stream(self, *_args, **kwargs):
            pid = kwargs["remote_pid_file"]
            recorded_pid.append(pid)
            yield AgentEvent(event="remote_process_start", text=pid)
            yield AgentEvent(
                event="provider_exit", text='{"return_code": 0, "remote_process_stopped": null}'
            )
            yield AgentEvent(event="error", text="Remote process state is unknown")

    outcome, events = await _consume(store, Launcher(), tmp_path)
    assert outcome.failed and not outcome.completed
    assert store.unresolved_remote_provider_passes("remote", "/stage") == [
        ("first", recorded_pid[0])
    ]
    assert len(events) == 1 and "Remote process state is unknown" in events[0]


@pytest.mark.asyncio
async def test_competing_remote_reservation_closes_launcher_before_provider_advances(tmp_path):
    store = _store(tmp_path)
    advanced = []
    closed = []

    class CompetingStage(_Stage):
        def finalize_inputs(self):
            store.begin_remote_provider_pass("second", "remote", "/stage", "/stage/other.pid")

    class Launcher:
        async def stream(self, *_args, **kwargs):
            try:
                yield AgentEvent(event="remote_process_start", text=kwargs["remote_pid_file"])
                advanced.append("provider spawned")
            finally:
                closed.append(True)

    with pytest.raises(AgentTaskAdmissionConflict, match="live or its exit is unconfirmed"):
        await _consume(store, Launcher(), tmp_path, stage=CompetingStage())
    assert advanced == [] and closed == [True]
    assert store.unresolved_remote_provider_passes("remote", "/stage") == [
        ("second", "/stage/other.pid")
    ]
