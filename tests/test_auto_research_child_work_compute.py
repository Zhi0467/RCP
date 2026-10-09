from __future__ import annotations

import hashlib
import json
import shlex
from functools import partial
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.agents.graph_rules import graph_rules
from rcp.agents.prompts import COMMAND_CLIENT
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
    changed_values,
    create_named_app,
    current_command_client,
    fabricated_authorizer,
    launch_contract_path,
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
                # The session start's master names this turn's client and the launch helper.
                contract = launch_contract_path(prompt).read_text()
                assert current_command_client(prompt) == staged.client_command()
                assert (
                    f"{COMMAND_CLIENT} "
                    + shlex.join(
                        [
                            "launch",
                            "--key",
                            "<idempotency-key>",
                            "--cwd",
                            "<working-directory>",
                            "--",
                            "<argv...>",
                        ]
                    )
                    in contract
                )
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
                # A correction continues the session inline.
                correction = prompt
                assert '"job_id"' not in correction
                if state == "running":
                    assert self.job_id in correction
                (workspace / "watch.json").write_text(
                    json.dumps({"external": [self.watcher], "graph": []})
                )
            if len(self.calls) == 3:
                # A wake continues the session inline and sends only what changed.
                self.wake_contract = prompt
                turn, inputs = child_turns[-1]
                changed = changed_values(self.wake_contract)
                assert changed["patch.command_client"] == turn.patch_inputs.command_client
                assert child_turns[0][0].patch_inputs.command_client not in self.wake_contract
                assert not any(key.startswith(("work.", "skills")) for key in changed)
                assert str(inputs.artifact_directory) in self.wake_contract
                assert '"name": "native-review"' in self.wake_contract
                for extensions in (False, True):
                    rules = graph_rules(edits=True, ontology_extensions=extensions)
                    assert rules not in self.wake_contract
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
            code_worktree=False,
            invocation_ceiling=5,
            provider="codex",
            run_on="laptop",
            run_truth_scope=["repo-a"],
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
    assert child.request["isolation_owner_episode_id"] == episode.episode_id
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
        assert wake.native_session_id == child.native_session_id
        assert store.episode(episode.episode_id).invocations_used == 3


def test_child_session_replaces_its_master_when_turn_owner_changes(manifest, tmp_path):
    from rcp.agents.continuation_prompt import master_key
    from rcp.agents.prompts import WORK_POLICY_VERSION, chat_master_contract_key
    from rcp.runs.auto_research_admission import start_auto_research_child_work_message_wake
    from rcp.runs.auto_research_delivery import record_auto_research_message
    from rcp.runs.session_master import SESSION_MASTER_KEY_ROLE

    data_dir = tmp_path / "owner-data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    session_id = "owner-change-session"
    calls = []
    snapshots = []

    class Launcher:
        async def stream(self, provider, prompt, **kwargs):
            calls.append(kwargs)
            yield AgentEvent(event="session", session_id=session_id)
            yield AgentEvent(event="answer", text="Turn complete.")
            yield AgentEvent(event="done")

    launcher = Launcher()

    async def stream(project_id, kind, request, execution):
        if kind == "auto_research":
            yield 'data: {"event":"session","session_id":"owner-root-session"}\n\n'
            yield 'data: {"event":"done"}\n\n'
            return
        route = store.auto_research_child_work_for_operation(execution.operation_id)
        frames = (
            child_work.stream_auto_research_child_work_run(
                service, launcher, request, data_dir, execution, route=route
            )
            if route is not None
            else work.stream_work_run(service, launcher, request, data_dir, execution)
        )
        async for frame in frames:
            yield frame

    background = BackgroundAgentTasks(store, stream)
    episode, root = start_auto_research(
        background,
        project_id,
        AutoResearchStartRequest(
            code_worktree=False,
            invocation_ceiling=5,
            provider="codex",
            run_on="laptop",
            run_truth_scope=["repo-a"],
        ),
        authorized_by=fabricated_authorizer(),
        graph_base_head=GraphHeadRef(revision=0),
        ensure_graph_target=lambda _: None,
        episode_id="owner-change-episode",
    )
    root = wait_for_task(store, root.operation_id, expect="succeeded")
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_scope="node",
        node_id="hyp/replanning-restores-plasticity",
        chat_id="00000000-0000-4000-8000-000000000992",
        message="Inspect the result.",
        mode="work",
        trigger="orchestrator",
        patch_kind="work",
    )
    child = start_auto_research_child_work(
        background,
        episode.episode_id,
        request,
        admitted_by_operation_id=root.operation_id,
        worker_id=request.chat_id,
        instruction=request.message,
        instruction_sha256=hashlib.sha256(request.message.encode()).hexdigest(),
    )
    child = wait_for_task(store, child.operation_id, expect="succeeded")

    def capture_master(task, owner):
        current = store.chat_session_context("codex", "laptop", session_id)
        assert current is not None
        snapshot = json.loads(current.snapshot_json)
        expected = chat_master_contract_key(ontology_extensions=False, owner=owner)
        assert snapshot["contract_key"] == expected
        assert snapshot["master_operation_id"] == task.operation_id
        assert store.agent_task_contract(task.operation_id, SESSION_MASTER_KEY_ROLE) == expected
        snapshots.append(snapshot)

    capture_master(child, f"episode:{episode.episode_id}")
    # Control only the fixture lifecycle so the same routed session can exercise
    # both master transitions; admission and all three prompt streams stay real.
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status = 'completed' WHERE episode_id = ?", (episode.episode_id,)
        )
    human = background.start(
        project_id,
        "node_chat",
        request.model_copy(
            update={
                "trigger": "human",
                "session_id": session_id,
                "message": "Continue my investigation.",
            }
        ),
        authorized_by=episode.authorized_by,
        graph_target=episode.graph_target,
    )
    human = wait_for_task(store, human.operation_id, expect="succeeded")
    assert human.episode_id is None
    capture_master(human, "human")

    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status = 'running' WHERE episode_id = ?", (episode.episode_id,)
        )
    message = record_auto_research_message(
        store,
        episode_id=episode.episode_id,
        sender_role="orchestrator",
        sender_task_id=root.operation_id,
        authorized_by=None,
        recipient_task_id=child.operation_id,
        body="Check the new evidence.",
    )
    wake = start_auto_research_child_work_message_wake(
        background, episode.episode_id, request.chat_id, [message.message_id]
    )
    assert wake is not None
    wake = wait_for_task(store, wake.operation_id, expect="succeeded")
    assert wake.episode_id == episode.episode_id
    # A routed wake uses the Work continuation protocol; its active master is
    # recorded separately from the ordinary chat baseline retained for humans.
    expected = master_key(
        f"{WORK_POLICY_VERSION}:episode:{episode.episode_id}", ontology_extensions=False
    )
    active_master = store.latest_session_master(project_id, session_id)
    assert active_master is not None
    assert active_master[0] == wake.operation_id
    assert active_master[2] == expected
    assert store.agent_task_contract(wake.operation_id, SESSION_MASTER_KEY_ROLE) == expected
    assert len(calls) == 3
    assert [call["session_id"] for call in calls] == [None, session_id, session_id]
    assert len({str(call["cwd"]) for call in calls}) == 1
    assert snapshots[0]["contract_key"] != snapshots[1]["contract_key"]
