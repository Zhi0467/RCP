from __future__ import annotations

import hashlib
import json
from functools import partial
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.api.app import _generic_watcher_delivery_request
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.providers import ProviderSkillReference
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
from .test_compute_jobs_settlement import _command, _job_for_watcher


@pytest.mark.parametrize("state", ["running", "exited", "nothing", "malformed"])
def test_child_compute_mailbox_and_work_watcher_settlement(
    manifest, tmp_path, monkeypatch, commands, state
):
    data_dir = tmp_path / "child-data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    staged_turns = []
    child_turns = []
    original_stage = child_work._stage_auto_research_child_work_turn

    async def capture_turn(*args, **kwargs):
        result = await original_stage(*args, **kwargs)
        child_turns.append(result[:2])
        return result

    monkeypatch.setattr(child_work, "_stage_auto_research_child_work_turn", capture_turn)
    monkeypatch.setattr(
        work, "arm_watchers", partial(work.arm_watchers, check_runner=commands.check_runner)
    )
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
        watcher = None
        wake_contract = None

        async def stream(self, provider, prompt, **kwargs):
            staged, handler = staged_turns[-1]
            self.calls.append(kwargs)
            workspace = Path(kwargs["cwd"])
            assert handler.episode_id == "child-compute"
            assert kwargs["invocation_gate"] is staged.invocation_gate
            if len(self.calls) == 1:
                if "This is a Work turn." in prompt:
                    prefix = "Read current execution instructions relative to this turn's cwd: `"
                    path = next(
                        line.removeprefix(prefix).removesuffix("`")
                        for line in prompt.splitlines()
                        if line.startswith(prefix)
                    )
                    contract = (workspace / path).read_text()
                    master = Path(prompt.splitlines()[1]).read_text()
                    assert "Auto-research child Work boundary" not in master
                else:
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
                assert "`job-status`" not in boundary
                assert "`cancel`" not in boundary
                assert "RCP ignores child watcher output" not in boundary
            if len(self.calls) == 1 and state != "nothing":
                launched = await _command(
                    staged, "launch", "--key", "launch", "--cwd", str(workspace), "--", "true"
                )
                self.watcher = launched["watcher"]
                self.job_id = _job_for_watcher(
                    store, handler.write_scope.project_id, self.watcher
                ).job_id
                if state == "exited":
                    job = store.compute_job(self.job_id)
                    Path(job.exit_path).write_text("0 101")
                    commands.backend.alive_handles.remove(self.job_id)
                elif state == "malformed":
                    (workspace / "watch.json").write_text('{"external": [{"bad": true}]}')
            elif self.job_id and len(self.calls) == 2:
                correction = Path(prompt.splitlines()[1]).read_text()
                assert '"job_id"' not in correction
                assert "`check_command`, `log_path`, and `cwd`" in correction
                if state == "running":
                    assert self.job_id in correction
                    assert "use its launch receipt or authoritative" in correction
                (workspace / "watch.json").write_text(
                    json.dumps({"external": [self.watcher], "graph": []})
                )
            if len(self.calls) == 3:
                self.wake_contract = Path(prompt.splitlines()[1]).read_text()
                turn, inputs = child_turns[-1]
                assert "This is a Work turn." in self.wake_contract
                assert turn.patch_inputs.validator_command in self.wake_contract
                assert child_turns[0][0].patch_inputs.validator_command not in self.wake_contract
                for path in (
                    turn.patch_inputs.patch_path,
                    turn.patch_inputs.watch_path,
                    turn.patch_inputs.schema_path,
                    str(inputs.artifact_directory),
                    *turn.write_scope.writable_roots,
                    *turn.write_scope.protected_write_paths,
                ):
                    assert path in self.wake_contract
                for package in inputs.skill_pointers:
                    assert str(package["path"]) in self.wake_contract
                assert "Invoked for this turn" in self.wake_contract
                assert "skill `graph-audit`" in self.wake_contract
                assert "Invoked provider-native skill this turn" in self.wake_contract
                assert '"name": "native-review"' in self.wake_contract
                assert self.wake_contract.endswith(
                    child_work._auto_research_child_work_contract(
                        turn,
                        inputs,
                        store.auto_research_child_work_for_operation(turn.execution.operation_id),
                    )
                )
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
            skill_ids=["graph-audit"],
            invoked_skill_ids=["graph-audit"],
            resolved_provider_skills=[
                ProviderSkillReference(
                    provider="codex",
                    machine="laptop",
                    provider_version="test-provider",
                    inventory_hash="captured-test-inventory",
                    name="native-review",
                    label="Native review",
                    description="Review the observed result.",
                )
            ],
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
        assert watchers[0].check_command == launcher.watcher["check_command"]
        assert watchers[0].cancel_command == launcher.watcher["cancel_command"]
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
            check_runner=commands.check_runner,
            clock=lambda: watchers[0].next_check_at or store.now(),
        ).poll_once()
        assert len(groups) == 1
        request = _generic_watcher_delivery_request(groups[0])
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
        assert launcher.watcher["log_path"] in launcher.wake_contract
        assert watchers[0].watcher_id in launcher.wake_contract
        assert "Auto-research child Work boundary" in launcher.wake_contract
        assert wake.native_session_id == child.native_session_id
        assert store.episode(episode.episode_id).invocations_used == 3
