from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher, ProviderReadiness
from rcp.agents import launcher as launcher_module
from rcp.runs.provider_process import require_remote_provider_quiescence
from rcp.runs.shared import _ProviderOutcome, _stream_agent_events
from rcp.service import RunRequest
from rcp.storage import AppStore
from rcp.transport import StateUnavailable

from .test_remote_provider_receipts import _store

HOST = "gpu.example.edu"


def _request() -> RunRequest:
    return RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="gpu",
        run_truth_scope=["repo"],
        chat_scope="project",
        chat_id="chat-transport",
        message="Exercise the pre-provider boundary.",
        mode="work",
        patch_kind="work",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path_state", ["unreachable", "missing"])
async def test_an_unreachable_readiness_probe_types_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_state: str
) -> None:
    """Six real failed turns read `<host> is unreachable` and none carried a
    provider exit, so the classifier saw nothing. The probe already knows the
    SSH exit was 255; that word travels on the error event, never its text."""

    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda *args, **kwargs: ProviderReadiness(
            provider="codex",
            installed=False,
            authenticated=False,
            path_state=path_state,  # type: ignore[arg-type]
            reason=f"{HOST} is unreachable, so codex could not be checked.",
        ),
    )
    events = [
        event
        async for event in launcher.stream(
            "codex", "inspect", cwd=tmp_path, host=HOST, capability="scratch_patch"
        )
    ]
    (error,) = [event for event in events if event.event == "error"]
    assert error.text.startswith(f"{HOST} is unreachable")
    assert error.failure_kind == ("transport_lost" if path_state == "unreachable" else None)


@pytest.mark.parametrize("exit_code", [255, 1, 7])
def test_the_previous_pass_check_names_a_host_it_cannot_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    store = _store(tmp_path)
    store.begin_remote_provider_pass("first", HOST, "/stage", "/stage/one.pid")
    store.fail_agent_task("first", "SSH disconnected")
    monkeypatch.setattr(
        launcher_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, exit_code, "", "probe failed"),
    )

    if exit_code == 255:
        with pytest.raises(StateUnavailable, match="unreachable"):
            require_remote_provider_quiescence(store, HOST, "/stage")
    else:
        # Any other answer is the host speaking: a live or unverifiable process
        # is still not a lost link, and reattempting would not change it.
        expected = "still running" if exit_code == 1 else "could not be verified"
        with pytest.raises(ValueError, match=expected):
            require_remote_provider_quiescence(store, HOST, "/stage")
    assert store.unresolved_remote_provider_passes(HOST, "/stage") == [("first", "/stage/one.pid")]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [StateUnavailable("link lost"), ValueError("bad input")])
async def test_a_failed_input_transfer_marks_only_a_lost_link(
    tmp_path: Path, failure: Exception
) -> None:
    def finalize_inputs() -> None:
        raise failure

    execution = SimpleNamespace(
        store=AppStore(tmp_path / "state.sqlite3"),
        operation_id="op-transfer",
        stage_unreachable=False,
    )
    remote_stage = SimpleNamespace(root=Path("/stage"), finalize_inputs=finalize_inputs)
    outcome = _ProviderOutcome()

    def never_launched(*args, **kwargs):
        raise AssertionError("no provider may start when its inputs did not arrive")

    frames = [
        frame
        async for frame in _stream_agent_events(
            SimpleNamespace(stream=never_launched),  # type: ignore[arg-type]
            _request(),
            "prompt",
            workspace=tmp_path,
            session_id=None,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            execution_host=HOST,
            execution=execution,  # type: ignore[arg-type]
            remote_stage=remote_stage,  # type: ignore[arg-type]
            capability="work_auto",
            outcome=outcome,
            binary=None,
        )
    ]

    assert outcome.failed and len(frames) == 1 and str(failure) in frames[0]
    assert execution.stage_unreachable is isinstance(failure, StateUnavailable)
