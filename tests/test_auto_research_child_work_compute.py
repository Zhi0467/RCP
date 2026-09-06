from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.api.app import _generic_watcher_delivery_request
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.auto_research import AutoResearchStartRequest
from rcp.runs.auto_research_admission import start_auto_research, start_auto_research_child_work
from rcp.runs.tasks import auto_research_child_work as child_work
from rcp.runs.tasks import work
from rcp.runs.watcher_admission import start_watcher_notification
from rcp.service import RunRequest
from rcp.watchers import WatcherPoller

from .helpers import (
    append_fixture_patch,
    create_named_app,
    fabricated_authorizer,
    seed_patch,
    wait_for_task,
)
from .test_compute_jobs_commands import commands as commands
from .test_compute_jobs_settlement import _command


@pytest.mark.parametrize("state", ["running", "exited", "cancelled", "nothing", "malformed"])
def test_child_compute_mailbox_and_work_watcher_settlement(
    manifest, tmp_path, monkeypatch, commands, state
):
    data_dir = tmp_path / "child-data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    staged_turns = []
    for owner, name in (
        (child_work, "_start_auto_research_child_validator_mailbox"),
        (work, "_start_work_validator_mailbox"),
    ):
        original = getattr(owner, name)

        def capture(service, staged, _original=original, **kwargs):
            staged_turns.append((staged, kwargs["compute_commands"]))
            return _original(service, staged, **kwargs)

        monkeypatch.setattr(owner, name, capture)

    class Launcher:
        calls = []
        job_id = None
        wake_contract = None

        async def stream(self, provider, prompt, **kwargs):
            staged, handler = staged_turns[-1]
            self.calls.append(kwargs)
            workspace = Path(kwargs["cwd"])
            assert handler.episode_id == "child-compute"
            assert kwargs["invocation_gate"] is staged.invocation_gate
            if len(self.calls) == 1:
                contract = Path(prompt.splitlines()[1]).read_text()
                assert (
                    staged.client_command(
                        "launch",
                        "--key",
                        "<idempotency-key>",
                        "--cwd",
                        "<working-directory>",
                        "--",
                        "<argv...>",
                    )
                    in contract
                )
                boundary = contract.split("## Auto-research child Work boundary", 1)[1]
                assert "`launch`" in boundary.split("- Do not invoke", 1)[0]
                assert "`job-status`" in boundary.split("- Do not invoke", 1)[0]
                assert "`cancel`" in boundary.split("- Do not invoke", 1)[0]
                assert "RCP ignores child watcher output" not in boundary
            if len(self.calls) == 1 and state != "nothing":
                launched = await _command(
                    staged, "launch", "--key", "launch", "--cwd", str(workspace), "--", "true"
                )
                self.job_id = launched["job_id"]
                status = await _command(staged, "job-status", "--key", "status", self.job_id)
                assert status["status"] == "running"
                if state == "cancelled":
                    cancelled = await _command(staged, "cancel", "--key", "cancel", self.job_id)
                    assert cancelled["status"] == "cancelled"
                elif state == "exited":
                    job = store.compute_job(self.job_id)
                    Path(job.exit_path).write_text("0 101")
                    commands.backend.alive_handles.remove(self.job_id)
                elif state == "malformed":
                    (workspace / "watch.json").write_text('{"external": [{"bad": true}]}')
            elif self.job_id and len(self.calls) == 2:
                (workspace / "watch.json").write_text(
                    json.dumps({"external": [{"job_id": self.job_id}], "graph": []})
                )
            if len(self.calls) == 3:
                self.wake_contract = Path(prompt.splitlines()[1]).read_text()
                assert not (workspace / "watch.json").exists()
            yield AgentEvent(event="session", session_id="child-compute-session")
            yield AgentEvent(event="answer", text="Computation handed off.")
            yield AgentEvent(event="done")

    launcher = Launcher()

    async def stream(project_id, kind, request, execution):
        if kind == "auto_research":
            yield 'data: {"event":"session","session_id":"root-session"}\n\n'
            yield 'data: {"event":"done"}\n\n'
            return
        route = store.auto_research_child_work_for_operation(execution.operation_id)
        async for frame in child_work.stream_auto_research_child_work_run(
            service, launcher, request, data_dir, execution, route=route
        ):
            yield frame

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        app.state.default_project_id,
        AutoResearchStartRequest(
            invocation_ceiling=5, provider="codex", run_on="laptop", run_truth_scope=["repo-a"]
        ),
        authorized_by=fabricated_authorizer(),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _: None,
        episode_id="child-compute",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    instruction = "Run bounded compute."
    child = start_auto_research_child_work(
        background,
        episode.episode_id,
        RunRequest(
            provider="codex",
            run_on="laptop",
            run_truth_scope=["repo-a"],
            chat_scope="node",
            node_id="hyp/replanning-restores-plasticity",
            chat_id="00000000-0000-4000-8000-000000000991",
            message=instruction,
            mode="work",
            trigger="orchestrator",
            patch_kind="work",
        ),
        admitted_by_operation_id=root.operation_id,
        worker_id="00000000-0000-4000-8000-000000000991",
        instruction=instruction,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    child = wait_for_task(store, child.operation_id, expect="succeeded")
    needs_correction = state in {"running", "malformed"}
    assert len(launcher.calls) == (2 if needs_correction else 1)
    assert len(commands.backend.starts) == (0 if state == "nothing" else 1)
    watchers = store.watchers(app.state.default_project_id)
    if needs_correction:
        assert len(watchers) == 1
        assert watchers[0].job_id == launcher.job_id
        assert watchers[0].episode_id == episode.episode_id
        assert watchers[0].worker_id == child.request["chat_id"]
        assert watchers[0].status == "active"
        assert Path(launcher.calls[-1]["cwd"], "watch.json").exists()
        assert launcher.calls[-1]["session_id"] == "child-compute-session"
        assert staged_turns[0][1] is staged_turns[1][1]
    else:
        assert watchers == []
    if launcher.job_id:
        job = store.compute_job(launcher.job_id)
        assert job.origin_operation_id == child.operation_id
        assert job.episode_id == episode.episode_id
        assert job.execution_machine == "laptop"
    assert store.episode(episode.episode_id).invocations_used == 2

    if state == "running":
        job = store.compute_job(launcher.job_id)
        Path(job.exit_path).write_text("0 105")
        commands.backend.alive_handles.remove(job.backend_handle)
        groups = WatcherPoller(
            store,
            manifest_for_project=lambda _: service.manifest,
            data_dir=data_dir,
            clock=lambda: watchers[0].next_check_at or store.now(),
        ).poll_once()
        assert len(groups) == 1
        request = _generic_watcher_delivery_request(groups[0], store=store)
        wake = start_watcher_notification(
            background,
            child.project_id,
            "node_chat",
            request,
            [item.watcher_id for item in groups[0]],
            authorized_by=episode.authorized_by,
        )
        assert wake is not None
        wake = wait_for_task(store, wake.operation_id, expect="succeeded")
        assert len(launcher.calls) == 3
        assert launcher.calls[-1]["session_id"] == "child-compute-session"
        assert launcher.calls[-1]["cwd"] == launcher.calls[0]["cwd"]
        assert launcher.job_id in launcher.wake_contract
        assert '"duration_seconds": 5.0' in launcher.wake_contract
        assert "Auto-research child Work boundary" in launcher.wake_contract
        assert wake.native_session_id == child.native_session_id
        assert store.episode(episode.episode_id).invocations_used == 3
