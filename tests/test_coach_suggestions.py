from __future__ import annotations

import json
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.paper import PaperOp
from rcp.runs.tasks.coach import stream_coach
from rcp.service import CoachRequest
from rcp.storage.models import AgentTaskRecord

from .helpers import create_named_app
from .paper_helpers import apply, human_edit, receipts, saved, wire_saved_paper


@pytest.fixture
def coach(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    live = wire_saved_paper(service)
    return service, live, tmp_path / "data"


async def run_coach(service, launcher, data, *, mode="review", execution=None):
    return [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_coach(
            service,
            launcher,
            service.paper,
            CoachRequest(message="Edit the paper", mode=mode),
            data,
            execution,
        )
    ]


def execution_for(service):
    store = service.paper.store
    task_id = str(uuid4())
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=task_id,
            project_id=service.paper.project_id,
            kind="paper_coach",
            status="running",
            request={"message": "Edit the paper", "provider": "codex"},
            created_at=now,
            updated_at=now,
            status_message="running",
        )
    )
    return AgentTaskExecution(task_id, store, AgentProcessControl())


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_editing, expected_turns", [(False, 2), (True, 3)])
async def test_saved_snapshot_refreshes_same_session_only_affected_files(
    coach, keep_editing, expected_turns
):
    service, live, data = coach
    apply(
        live,
        [PaperOp(kind="create", file_id="other", path="other.md", text="Other\n")],
        service.paper.project_id,
    )
    paths = []
    sessions = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            sessions.append(kwargs["session_id"])
            base = [path for path in kwargs["read_dirs"] if path.name.startswith("paper-base-")][-1]
            paths.append(base)
            assert base.stat().st_mode & 0o222 == 0
            assert (base / "paper.md").stat().st_mode & 0o222 == 0
            assert (base / "other.md").exists() == (len(paths) == 1)
            assert kwargs["capability"] == "paper_suggest"
            assert len(kwargs["write_dirs"]) == 1
            if len(paths) > 1:
                assert str(base) in _prompt
                assert str(kwargs["write_dirs"][0]) in _prompt
            output = kwargs["write_dirs"][0] / "files" / "paper.md"
            output.parent.mkdir()
            output.write_text("Proposed replacement.\n")
            if keep_editing or len(paths) == 1:
                human_edit(
                    live, "paper-file", f"Human version {len(paths)}.\n", service.paper.project_id
                )
            yield AgentEvent(event="session", session_id="same-native-session")
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data, execution=execution_for(service))
    assert not [event for event in events if event.event == "error"]
    assert sessions == [None] + ["same-native-session"] * (expected_turns - 1)
    assert all(not path.exists() for path in paths)
    groups = service.paper_changes.groups(service.paper.project_id)
    assert len(groups) == 1
    assert groups[0].state == ("outdated" if keep_editing else "pending")
    assert receipts(live) == []


@pytest.mark.asyncio
async def test_a_refresh_replaces_every_undecided_card_on_its_file(coach):
    service, live, data = coach
    project = service.paper.project_id
    human_edit(live, "paper-file", "".join(f"line {i}\n" for i in range(30)), project)
    rounds = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            rounds.append(kwargs["session_id"])
            text = saved(live, project).files[0].text or ""
            output = kwargs["write_dirs"][0] / "files" / "paper.md"
            output.parent.mkdir()
            output.write_text(
                text.replace("line 2\n", "coach 2\n")
                .replace("human 2\n", "coach 2\n")
                .replace("line 25\n", "coach 25\n")
            )
            if len(rounds) == 1:
                # Inside the first card only: it goes outdated, the second stays pending.
                human_edit(live, "paper-file", text.replace("line 2\n", "human 2\n"), project)
            yield AgentEvent(event="session", session_id="refresh-native")
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data, execution=execution_for(service))
    assert not [event for event in events if event.event == "error"]
    assert len(rounds) == 2
    undecided = [
        change.after
        for group in service.paper_changes.groups(project)
        if group.state in {"pending", "outdated"}
        for change in group.changes
    ]
    assert sorted(undecided) == ["coach 2\n", "coach 25\n"]


@pytest.mark.asyncio
async def test_a_coach_session_keeps_its_folder_and_sessions_never_share_one(coach):
    service, _live, data = coach
    folders = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            folders.append(kwargs["cwd"])
            yield AgentEvent(event="session", session_id=kwargs["session_id"] or f"s{len(folders)}")
            yield AgentEvent(event="done")

    async def turn(session_id=None):
        execution = execution_for(service)
        events = [
            AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
            async for frame in stream_coach(
                service,
                Launcher(),
                service.paper,
                CoachRequest(message="Edit the paper", session_id=session_id),
                data,
                execution,
            )
        ]
        assert not [event for event in events if event.event == "error"]
        # The task runner records the session the provider reported.
        session = next(event.session_id for event in events if event.event == "session")
        execution.store.checkpoint_agent_task(execution.operation_id, native_session_id=session)
        execution.store.complete_agent_task(
            execution.operation_id, applied_revision=None, result={}
        )

    await turn()
    await turn("s1")
    await turn()
    # Providers look a resumed session up by its folder; each session's mailbox owns it.
    assert folders[0] == folders[1] != folders[2]
    assert folders[0].is_dir()


@pytest.mark.asyncio
async def test_direct_apply_uses_saved_mode_and_admission(coach):
    service, live, data = coach
    execution = execution_for(service)

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            path = kwargs["write_dirs"][0] / "files" / "paper.md"
            path.parent.mkdir()
            path.write_text("Applied text\n")
            yield AgentEvent(event="session", session_id="direct-native")
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data, mode="apply_directly", execution=execution)
    assert not [event for event in events if event.event == "error"]
    assert saved(live, service.paper.project_id).files[0].text == "Applied text\n"
    assert receipts(live)[0]["actor"] == "Coach, applied directly"
    task_receipts = execution.store.agent_task_receipts(execution.operation_id)
    base = next(r.payload for r in task_receipts if r.category == "paper_turn_base")
    assert base["revision"] == 1


@pytest.mark.asyncio
async def test_invalid_output_records_exact_diagnostics_and_cleans_snapshot(coach):
    service, _live, data = coach
    execution = execution_for(service)

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            (kwargs["write_dirs"][0] / "moves.json").write_text(
                json.dumps({"delete": ["../outside"]})
            )
            yield AgentEvent(event="session", session_id="invalid-native")
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data, execution=execution)
    errors = [event.text for event in events if event.event == "error"]
    assert errors
    task_receipts = execution.store.agent_task_receipts(execution.operation_id)
    diagnostic = next(
        r.payload["diagnostics"] for r in task_receipts if r.category == "paper_changes_invalid"
    )
    assert errors == ["; ".join(diagnostic)]
    assert service.paper_changes.groups(service.paper.project_id) == []
    assert service.paper.sessions()[0].native_session_id == "invalid-native"
    assert not list((data / "run-stage").glob("*/paper-base-*"))


@pytest.mark.asyncio
async def test_stop_prevents_refresh_and_direct_application(coach):
    service, live, data = coach
    execution = execution_for(service)
    calls = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            calls.append(kwargs)
            path = kwargs["write_dirs"][0] / "files" / "paper.md"
            path.parent.mkdir()
            path.write_text("Proposed\n")
            execution.control.pause_requested.set()
            yield AgentEvent(event="session", session_id="stopped-native")
            yield AgentEvent(event="done")

    await run_coach(service, Launcher(), data, mode="apply_directly", execution=execution)
    assert len(calls) == 1
    assert receipts(live) == []
    assert not list((data / "run-stage").glob("*/paper-base-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_running", [False, True])
async def test_remote_base_stages_snapshot_and_collects_changes(
    coach, monkeypatch, tmp_path, keep_running
):
    import shutil
    from pathlib import Path

    from rcp.agents.provider_environment import unhidden_read_scope
    from rcp.runs.tasks import coach as coach_module

    from .test_auto_research_stream import _LocalBackedRemoteStage

    service, live, data = coach
    base_machine = service.manifest.repository_map[service.manifest.state.repository].machine
    service.manifest.machine_map[base_machine].host = "base.example"
    state_repository = service.manifest.repository_map[service.manifest.state.repository]
    state_repository.path = str(tmp_path / "base-repository")
    staged_snapshots = []
    collected = []
    stages = []
    provider_folders = []

    class RemoteStage(_LocalBackedRemoteStage):
        base = tmp_path / "remote-stage"
        transport_partition = "test-remote"

        def open(self, operation_id, *, reuse=False, protected_roots=()):
            assert reuse
            result = super().open(operation_id, reuse=reuse, protected_roots=protected_roots)
            stages.append(self)
            return result

        def touch(self):
            assert self.root is not None

        def put_directory(self, source, label, *, reuse=False):
            assert self.root is not None
            target = self.root / "inputs" / label
            shutil.copytree(source, target)
            if "paper-base-" in label:
                staged_snapshots.append(target)
            return str(target)

        def prepare_artifact_directory(self, scope_id, *, reuse):
            target = self.workspace / "turns" / scope_id / "artifacts"
            target.mkdir(parents=True, exist_ok=reuse)
            return target

        def read_workspace_directory(self, name, destination, **_limits):
            source = self.workspace / name
            collected.append(source)
            shutil.copytree(source, destination, dirs_exist_ok=True)

    async def hidden_scope(_service, _request, **kwargs):
        assert kwargs["remote_stage"] is stages[-1]
        assert kwargs["workspace"] == stages[-1].workspace
        return unhidden_read_scope(execution_machine=base_machine, host="base.example")

    monkeypatch.setattr(coach_module, "RemoteRunStage", RemoteStage)
    monkeypatch.setattr(coach_module, "_prepare_hidden_read_scope", hidden_scope)
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", lambda *_args, **_kwargs: False)
    execution = execution_for(service)
    provider_pids = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            stage = stages[-1]
            assert kwargs["supervise_remote"] is True
            pid = kwargs["remote_pid_file"]
            provider_pids.append(pid)
            yield AgentEvent(event="remote_process_start", text=pid)
            assert execution.store.unresolved_remote_provider_passes(
                "base.example", str(stage.root)
            ) == [(execution.operation_id, pid)]
            provider_folders.append(kwargs["cwd"])
            assert kwargs["host"] == "base.example"
            assert kwargs["cwd"] == stage.workspace
            contracts = "\n".join(path.read_text() for path in (stage.root / "inputs").glob("*.md"))
            assert str(Path(state_repository.path) / ".research" / "graph.json") in contracts
            assert str(Path(state_repository.path) / ".research" / "research.md") in contracts
            assert len(kwargs["write_dirs"]) == 1
            output_dir = kwargs["write_dirs"][0]
            assert output_dir.is_relative_to(stage.workspace)
            snapshot = staged_snapshots[-1]
            assert snapshot in kwargs["read_dirs"]
            assert snapshot.stat().st_mode & 0o222 == 0
            assert (snapshot / "paper.md").read_text() == saved(
                live, service.paper.project_id
            ).files[0].text
            assert Path(state_repository.path) in kwargs["read_dirs"]
            output = output_dir / "files" / "paper.md"
            output.parent.mkdir()
            output.write_text("Remote proposed text\n")
            if len(provider_folders) == 1:
                human_edit(live, "paper-file", "New human text\n", service.paper.project_id)
            else:
                assert kwargs["session_id"] == "remote-native"
            yield AgentEvent(event="session", session_id="remote-native")
            if not keep_running:
                yield AgentEvent(event="remote_process_stop", text=pid)
                assert (
                    execution.store.unresolved_remote_provider_passes(
                        "base.example", str(stage.root)
                    )
                    == []
                )
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data, execution=execution)

    task_receipts = execution.store.agent_task_receipts(execution.operation_id)
    starts = [r for r in task_receipts if r.category == "remote_provider_started"]
    stops = [r for r in task_receipts if r.category == "remote_provider_stopped"]
    assert all(r.payload["supervised"] for r in starts)
    if keep_running:
        assert any(event.event == "error" for event in events)
        assert len(provider_folders) == len(starts) == 1
        assert stops == []
        assert execution.store.unresolved_remote_provider_passes(
            "base.example", str(stages[0].root)
        ) == [(execution.operation_id, provider_pids[0])]
        return
    assert len(starts) == len(stops) == len(set(provider_pids)) == 2
    assert not [event for event in events if event.event == "error"]
    assert any(event.event == "done" for event in events)
    assert execution.stage_host == "base.example"
    assert execution.stage_root == str(stages[0].root)
    assert len(collected) == len(staged_snapshots) == 2
    assert provider_folders[0] == provider_folders[1]
    assert (staged_snapshots[0] / "paper.md").read_text() != "New human text\n"
    assert (staged_snapshots[1] / "paper.md").read_text() == "New human text\n"
    groups = service.paper_changes.groups(service.paper.project_id)
    assert len(groups) == 1
    assert groups[0].state == "pending"
    assert groups[0].changes[0].after == "Remote proposed text\n"
    assert service.paper.sessions()[0].execution_machine == base_machine


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["open", "upload"])
@pytest.mark.parametrize("link_lost", [False, True])
async def test_remote_coach_stage_failure_classifies_transport_loss(
    coach, monkeypatch, tmp_path, failure_at, link_lost
):
    from rcp.runs.tasks import coach as coach_module
    from rcp.transport import StateUnavailable, StateUnreachable

    from .test_auto_research_stream import _LocalBackedRemoteStage

    service, _live, data = coach
    machine = service.manifest.repository_map[service.manifest.state.repository].machine
    service.manifest.machine_map[machine].host = "base.example"
    failure = StateUnreachable("connection lost") if link_lost else StateUnavailable("refused")

    class RemoteStage(_LocalBackedRemoteStage):
        base = tmp_path / "remote-stage"

        def open(self, operation_id, **kwargs):
            if failure_at == "open":
                raise failure
            return super().open(operation_id, **kwargs)

        def touch(self):
            pass

        def put_directory(self, *_args, **_kwargs):
            raise failure

    class Launcher:
        def stream(self, *_args, **_kwargs):
            raise AssertionError("provider must not launch after a failed stage transfer")

    monkeypatch.setattr(coach_module, "RemoteRunStage", RemoteStage)
    execution = execution_for(service)
    events = await run_coach(service, Launcher(), data, execution=execution)

    assert any(event.event == "error" for event in events)
    assert execution.stage_unreachable is link_lost
