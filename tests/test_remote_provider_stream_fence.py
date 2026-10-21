from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

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

    def read_input_text(self, label):
        raise ValueError(label)

    def put_file(self, source, label, *, reuse=False):
        assert reuse is True
        return str(self.root / "inputs" / label)

    def list_workspace_files(self):
        return []

    def write_workspace_text(self, name: str, content: str) -> None:
        self.last_workspace_write = (name, content)


async def _consume(store, launcher, tmp_path, *, stage=None, bind_finalizer=True):
    from rcp.runs.tasks.work import WORK_FINALIZATION_CONTEXT_ROLE

    if bind_finalizer:
        store.record_agent_task_contract("first", WORK_FINALIZATION_CONTEXT_ROLE, "{}", "test")
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
async def test_supervised_stream_records_ownership_and_forwards_pending_state(tmp_path):
    store = _store(tmp_path)

    class Launcher:
        async def stream(self, *_args, **kwargs):
            assert kwargs["supervisor_path"]
            pid = kwargs["remote_pid_file"]
            yield AgentEvent(event="remote_process_start", text=pid)
            yield AgentEvent(
                event="remote_result_pending",
                text="The execution host accepted this turn.",
            )

    _outcome, events = await _consume(
        store,
        Launcher(),
        tmp_path,
    )

    assert "remote_result_pending" in events[0]
    started = next(
        receipt
        for receipt in store.agent_task_receipts("first")
        if receipt.category == "remote_provider_started"
    )
    assert started.payload["supervised"] is True


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
            assert kwargs["supervisor_path"]
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
    assert len(events) == 1


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

    with pytest.raises(AgentTaskAdmissionConflict):
        await _consume(store, Launcher(), tmp_path, stage=CompetingStage())
    assert advanced == [] and closed == [True]
    assert store.unresolved_remote_provider_passes("remote", "/stage") == [
        ("second", "/stage/other.pid")
    ]


@pytest.mark.asyncio
async def test_remote_launch_without_registered_owner_refuses_before_launch(tmp_path):
    store = _store(tmp_path)

    class Launcher:
        async def stream(self, *_args, **_kwargs):
            pytest.fail("An unowned remote turn must never launch")
            yield

    with pytest.raises(ValueError, match="registered finalization contract"):
        await _consume(store, Launcher(), tmp_path, bind_finalizer=False)
    assert store.unresolved_remote_provider_passes("remote", "/stage") == []


def test_every_finalization_contract_in_launch_owners_is_registered():
    from rcp.runs.remote_finalization import RECORDED_FINALIZERS

    # Derive the owner roles from executable launch modules, so adding an owner
    # without routing its retained contract fails even if its launch is mocked.
    roles = set()
    source = Path(__file__).parents[1] / "src" / "rcp" / "runs"
    for path in source.rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if any(
                isinstance(target, ast.Name)
                and "FINALIZATION" in target.id
                and target.id.endswith("ROLE")
                for target in node.targets
            ):
                roles.add(node.value.value)
    assert roles
    assert roles == set(RECORDED_FINALIZERS)

    from rcp.storage.models import is_prompt_contract_role

    assert all(not is_prompt_contract_role(role) for role in roles)
    assert not is_prompt_contract_role("graph_finalization_round_2")
    assert not is_prompt_contract_role("branch_merge_finalization_pass_2")
    assert is_prompt_contract_role("graph_patch_correction_2")
