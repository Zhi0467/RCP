from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher, ProviderReadiness
from rcp.agents import launcher as launcher_module
from rcp.runs.provider_process import require_remote_provider_quiescence
from rcp.runs.shared import _ProviderOutcome, _stream_agent_events
from rcp.service import RunRequest
from rcp.storage import AppStore
from rcp.transport import RemoteRunStage, StateMissing, StateUnavailable, StateUnreachable

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
@pytest.mark.parametrize("link_lost", [True, False])
async def test_an_unreachable_readiness_probe_types_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_lost: bool
) -> None:
    """Six real failed turns read `<host> is unreachable` and none carried a
    provider exit, so the classifier saw nothing. The probe already knows whether
    ssh ran and exited 255; that word travels on the error event, never its text,
    and `unreachable` alone is not it: a local ssh that cannot start says so too."""

    launcher = AgentLauncher()
    monkeypatch.setattr(
        launcher,
        "readiness",
        lambda *args, **kwargs: ProviderReadiness(
            provider="codex",
            installed=False,
            authenticated=False,
            path_state="unreachable",
            link_lost=link_lost,
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
    assert error.failure_kind == ("transport_lost" if link_lost else None)


@pytest.mark.parametrize("ssh_ran", [True, False])
def test_a_link_lost_at_a_later_readiness_probe_is_typed_and_never_cached(
    monkeypatch: pytest.MonkeyPatch, ssh_ran: bool
) -> None:
    """Discovery and version answered; the auth probe exited 255. The answer is
    `unreachable` either way and is not cached, so the next launch asks the host
    again. Only a probe whose ssh ran marks the loss a reattempt may fix."""

    launcher = AgentLauncher()
    probes: list[list[str]] = []

    def probe(self, host, command, **_kwargs):
        probes.append(command)
        if command[:2] == ["command", "-v"]:
            return subprocess.CompletedProcess(command, 0, "/opt/codex\n", "")
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex 1.0\n", "")
        if ssh_ran:
            return subprocess.CompletedProcess(command, 255, "", "ssh: connection timed out")
        return launcher_module._ProbeNoVerdict(command, 255, "", "ssh could not start")

    monkeypatch.setattr(AgentLauncher, "_probe", probe)
    first = launcher.readiness("codex", host=HOST)
    assert first.path_state == "unreachable" and not first.authenticated
    assert first.link_lost is ssh_ran
    assert launcher.cached_readiness("codex", host=HOST) is None
    probed = len(probes)
    launcher.readiness("codex", host=HOST)
    assert len(probes) > probed


@pytest.mark.parametrize("exit_code", [255, 1])
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
        with pytest.raises(StateUnreachable, match="unreachable"):
            require_remote_provider_quiescence(store, HOST, "/stage")
    else:
        # Any other answer is the host speaking: a live process is not a lost
        # link, and reattempting would not change it.
        with pytest.raises(ValueError, match="still running"):
            require_remote_provider_quiescence(store, HOST, "/stage")
    assert store.unresolved_remote_provider_passes(HOST, "/stage") == [("first", "/stage/one.pid")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        StateUnreachable("link lost"),
        StateUnavailable("rsync: permission denied"),
        StateMissing("stage is gone"),
        ValueError("bad input"),
    ],
)
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
    # A host that answers, "gone" or "refused", has not lost its link; only silence has.
    assert execution.stage_unreachable is (type(failure) is StateUnreachable)


@pytest.mark.parametrize("spawned", [True, False])
def test_only_a_spawned_rsync_exit_255_names_a_lost_link(
    monkeypatch: pytest.MonkeyPatch, spawned: bool
) -> None:
    """A local rsync that cannot start is given the failed code the commit
    script needs to clean up, but that code is RCP's own, not ssh's."""

    stage = RemoteRunStage(HOST)
    stage.root = PurePosixPath("/tmp/rcp-run.test")
    (stage._pending_input_root() / "notes.md").write_text("inputs")
    monkeypatch.setattr(
        stage, "_ssh", lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", "")
    )

    def rsync(arguments, **_kwargs):
        if not spawned:
            raise FileNotFoundError("rsync")
        return subprocess.CompletedProcess(arguments, 255, "", "ssh: connection closed")

    monkeypatch.setattr(subprocess, "run", rsync)
    with pytest.raises(StateUnavailable) as caught:
        stage.finalize_inputs()
    assert isinstance(caught.value, StateUnreachable) is spawned


@pytest.mark.parametrize("outcome", ["exit_255", "timeout", "not_started"])
def test_only_an_ssh_verdict_of_255_names_a_lost_link(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """ssh connects within its own shorter timeout, so a probe RCP stopped
    waiting for hung on a live link; one that could not start never asked."""

    def run(arguments, **kwargs):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
        if outcome == "not_started":
            raise FileNotFoundError("ssh")
        return subprocess.CompletedProcess(arguments, 255, "", "ssh: connection closed")

    monkeypatch.setattr(subprocess, "run", run)
    probe = AgentLauncher._probe(HOST, ["codex", "--version"])
    assert probe.returncode == 255
    assert launcher_module._link_lost(probe) is (outcome == "exit_255")
    assert bool(probe.stderr)


@pytest.mark.parametrize("outcome", ["exit_255", "exit_1", "no_verdict"])
def test_opening_a_stage_names_a_lost_link_only_from_ssh(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from rcp.transport import run_stage as run_stage_module

    stage = RemoteRunStage(HOST)
    monkeypatch.setattr(stage, "sweep", lambda **_kwargs: None)

    def ssh(arguments):
        if outcome == "no_verdict":
            return run_stage_module._SshNoVerdict([], 255, "", "ssh could not start")
        code = 255 if outcome == "exit_255" else 1
        return subprocess.CompletedProcess(arguments, code, "", "mkdir: refused")

    monkeypatch.setattr(stage, "_ssh", ssh)
    with pytest.raises(StateUnavailable) as caught:
        stage.open("op-open")
    assert isinstance(caught.value, StateUnreachable) is (outcome == "exit_255")
