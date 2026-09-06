from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.runs.tasks import experiment_loop, work
from rcp.service import RunRequest

from .helpers import append_fixture_patch, create_named_app, seed_patch
from .test_api import _chat_task_execution
from .test_compute_jobs_commands import commands as commands
from .test_experiment_loop_agent_io import (
    _EXPERIMENT_ID,
    _events,
    _execution,
    _experiment_patch,
    _loop_request,
)


async def _command(staged, *arguments):
    async with staged.invocation_gate.serve_current_session():
        client = await asyncio.create_subprocess_exec(
            *staged.client_argv(*arguments),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await client.communicate()
    assert client.returncode == 0, (stdout, stderr)
    return json.loads(stdout)["result"]


def _capture_commands(monkeypatch, owner):
    """Keep the real owner/mailbox lifecycle while replacing only the provider."""
    staged_turns = []
    original = owner._start_work_validator_mailbox

    def start(service, staged, **kwargs):
        staged_turns.append((staged, kwargs.get("compute_commands")))
        return original(service, staged, **kwargs)

    monkeypatch.setattr(owner, "_start_work_validator_mailbox", start)
    return staged_turns


class _ComputeTurnLauncher:
    def __init__(self, staged_turns, backend, *, state):
        self.staged_turns = staged_turns
        self.backend = backend
        self.state = state
        self.calls = []
        self.job_id = None
        self.native_session_id = str(uuid.uuid4())

    async def stream(self, _provider, _prompt, **kwargs):
        staged, commands = self.staged_turns[-1]
        assert commands is not None
        assert kwargs["invocation_gate"] is staged.invocation_gate
        assert staged.credential.identity.authority == "broker"
        self.calls.append(kwargs)
        workspace = Path(kwargs["cwd"])
        if len(self.calls) == 1 and self.state != "nothing":
            response = await _command(
                staged, "launch", "--key", "once", "--cwd", str(workspace), "--", "true"
            )
            self.job_id = response["job_id"]
            if self.state == "exited":
                job = commands.execution.store.compute_job(self.job_id)
                Path(job.exit_path).write_text("0 101")
                self.backend.alive_handles.remove(self.job_id)
        elif self.job_id:
            await _command(staged, "job-status", "--key", "inspect-correction", self.job_id)
            (workspace / "watch.json").write_text(
                json.dumps({"external": [{"job_id": self.job_id}], "graph": []})
            )
        yield AgentEvent(event="session", session_id=self.native_session_id)
        yield AgentEvent(event="answer", text="Handed off computation.")
        yield AgentEvent(event="done")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "exited", "nothing"])
async def test_work_settlement_corrects_only_unobserved_running_compute(
    manifest, tmp_path, monkeypatch, commands, state
):
    data_dir = tmp_path / "owner-data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    append_fixture_patch(app.state.service, seed_patch())
    request = RunRequest(
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Run bounded compute.",
        run_truth_scope=["repo-a"],
        mode="work",
    )
    execution = _chat_task_execution(
        app.state.background_tasks.store,
        operation_id="compute-settlement",
        project_id=app.state.default_project_id,
        request=request,
    )
    staged_turns = _capture_commands(monkeypatch, work)
    launcher = _ComputeTurnLauncher(staged_turns, commands.backend, state=state)
    events = await _events(
        work.stream_work_run(app.state.service, launcher, request, data_dir, execution=execution)
    )
    assert not [event.text for event in events if event.event == "error"]
    expected_calls = 2 if state == "running" else 1
    assert len(launcher.calls) == expected_calls
    assert len(commands.backend.starts) == (state != "nothing")
    watchers = execution.store.watchers(app.state.default_project_id, chat_id=request.chat_id)
    receipts = execution.store.agent_task_receipts(execution.operation_id)
    corrections = [item for item in receipts if item.category == "watcher_correction_requested"]
    if state == "running":
        assert len(corrections) == 1
        assert launcher.job_id in corrections[0].payload["problem"]
        assert [watcher.job_id for watcher in watchers] == [launcher.job_id]
        assert watchers[0].status == "active"
        assert launcher.calls[1]["session_id"] == launcher.native_session_id
        assert staged_turns[0][0].invocation_gate != staged_turns[1][0].invocation_gate
        assert staged_turns[0][1] is staged_turns[1][1]
    else:
        assert corrections == []
        assert watchers == []
        if state == "exited":
            assert execution.store.compute_job(launcher.job_id).status == "exited"


@pytest.mark.asyncio
@pytest.mark.parametrize("rewrite_patch", [True, False])
async def test_experiment_patch_correction_launch_revalidates_job_handoff(
    manifest, tmp_path, monkeypatch, commands, rewrite_patch
):
    data_dir = tmp_path / "experiment-data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_patch())
    request = _loop_request(
        str(uuid.uuid4()),
        "compute-loop-correction",
        invocation=1,
        control_revision=service.history.state().revision,
    )
    execution = _execution(
        app.state.background_tasks.store,
        app.state.default_project_id,
        "compute-loop-correction",
        request,
    )
    staged_turns = _capture_commands(monkeypatch, experiment_loop)

    class Launcher(_ComputeTurnLauncher):
        async def stream(self, provider, prompt, **kwargs):
            workspace = Path(kwargs["cwd"])
            index = len(self.calls)
            if index == 0:
                (workspace / "patch.json").write_text(
                    json.dumps({"summary": "Invalid reflection", "ops": [{"op": "unknown"}]})
                )
                (workspace / "watch.json").write_text(
                    json.dumps(
                        {
                            "external": [
                                {
                                    "check_command": "false",
                                    "log_path": str(workspace / "external.log"),
                                    "cwd": str(workspace),
                                }
                            ],
                            "graph": [],
                        }
                    )
                )
            elif index == 1:
                staged, handler = self.staged_turns[-1]
                response = await _command(
                    staged,
                    "launch",
                    "--key",
                    "correction-launch",
                    "--cwd",
                    str(workspace),
                    "--",
                    "true",
                )
                self.job_id = response["job_id"]
                if rewrite_patch:
                    (workspace / "patch.json").write_text(
                        json.dumps({"summary": "Corrected reflection", "ops": []})
                    )
                # Leave the earlier shell handoff unchanged: settlement must
                # detect the newly launched job before accepting this correction.
                assert kwargs["invocation_gate"] is staged.invocation_gate
                self.calls.append(kwargs)
                yield AgentEvent(event="session", session_id=self.native_session_id)
                yield AgentEvent(event="answer", text="Corrected reflection.")
                yield AgentEvent(event="done")
                return
            async for event in super().stream(provider, prompt, **kwargs):
                yield event

    launcher = Launcher(staged_turns, commands.backend, state="nothing")
    events = await _events(
        experiment_loop.stream_experiment_loop_task(
            service, launcher, request, data_dir, execution=execution
        )
    )
    assert not [event.text for event in events if event.event == "error"]
    assert len(launcher.calls) == 3
    assert len(commands.backend.starts) == 1
    watchers = execution.store.watchers(app.state.default_project_id)
    assert [item.job_id for item in watchers] == [launcher.job_id]
    assert watchers[0].status == "active"
    runtime = execution.store.experiment_loop_runtime(app.state.default_project_id, _EXPERIMENT_ID)
    assert runtime.invocations_used == 1
    assert {call["session_id"] for call in launcher.calls[1:]} == {launcher.native_session_id}
