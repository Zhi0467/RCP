from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent, AgentLauncher
from rcp.agents.launcher import AgentProcessControl
from rcp.attachments import ChatAttachmentStore, ProjectReferenceSource
from rcp.background import AgentTaskExecution
from rcp.paper import PaperOp
from rcp.project_references import ResolvedProjectReference
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


async def run_coach(
    service,
    launcher,
    data,
    *,
    mode: Literal["review", "apply_directly"] = "review",
    execution=None,
    request=None,
):
    return [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_coach(
            service,
            launcher,
            service.paper,
            request or CoachRequest(message="Edit the paper", mode=mode),
            data,
            execution,
        )
    ]


def attachment_request(service, data):
    store = ChatAttachmentStore(data / "chat-attachments")
    scope, client = str(uuid4()), str(uuid4())
    uploaded = store.add(
        project_id=service.paper.project_id,
        chat_id=scope,
        client_id=client,
        filename="notes.md",
        media_type="text/markdown",
        source=io.BytesIO(b"Uploaded notes"),
    )
    batch = store.claim(
        project_id=service.paper.project_id,
        chat_id=scope,
        client_id=client,
        attachment_set_id=uploaded.attachment_set_id,
        operation_id=str(uuid4()),
        reference_files=[
            ResolvedProjectReference(
                filename="reference.md",
                display_name="Saved reference",
                media_type="text/markdown",
                content=b"Reference bytes",
                reference=ProjectReferenceSource(
                    kind="paper",
                    source_id="paper-file",
                    version="1",
                ),
            )
        ],
    )
    return CoachRequest(
        message="Read these inputs", attachment_scope_id=scope, **batch.model_dump()
    )


def assert_attachment_launch(request, read_dirs, prompt):
    roots = [
        path
        for path in read_dirs
        if path.name == f"chat-attachments-v1-{request.attachment_batch_id}"
    ]
    assert len(roots) == 1
    files = list(roots[0].iterdir())
    assert {path.read_bytes() for path in files} == {b"Uploaded notes", b"Reference bytes"}
    assert all(str(path) in prompt for path in files)
    assert all(item.name in prompt for item in request.attachments)
    assert '"source_id": "paper-file"' in prompt


@pytest.mark.asyncio
async def test_coach_stages_upload_and_reference_on_each_session_turn(coach):
    from .helpers import launch_contract_path

    service, _live, data = coach
    for session in (None, "attachment-session"):
        request = attachment_request(service, data).model_copy(update={"session_id": session})

        class Launcher:
            async def stream(
                self, _provider, prompt, *, session=session, request=request, **kwargs
            ):
                contract = launch_contract_path(prompt).read_text() if session is None else prompt
                assert_attachment_launch(request, kwargs["read_dirs"], contract)
                yield AgentEvent(event="session", session_id="attachment-session")
                yield AgentEvent(event="done")

        events = await run_coach(
            service, Launcher(), data, request=request, execution=execution_for(service)
        )
        assert not [event for event in events if event.event == "error"]


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
                cast(AgentLauncher, Launcher()),
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
    assert errors == ["; ".join(cast(list[str], diagnostic))]
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
    request = attachment_request(service, data)

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
            assert_attachment_launch(request, kwargs["read_dirs"], contracts)
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

    events = await run_coach(service, Launcher(), data, execution=execution, request=request)

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


@pytest.fixture
def detached_coach(coach, monkeypatch, tmp_path):
    import shutil

    from rcp.agents.provider_environment import unhidden_read_scope
    from rcp.runs import recorded_settlement
    from rcp.runs.tasks import coach as coach_module
    from rcp.runs.tasks import work_turn_runtime

    from .test_auto_research_stream import _LocalBackedRemoteStage

    service, _live, data = coach
    base_machine = service.manifest.repository_map[service.manifest.state.repository].machine
    service.manifest.machine_map[base_machine].host = "base.example"

    class RemoteStage(_LocalBackedRemoteStage):
        base = tmp_path / "remote-stage"
        transport_partition = "test-remote"

        def touch(self):
            pass

        @property
        def workspace(self):
            return Path(super().workspace)

        def put_directory(self, source, label, *, reuse=False):
            assert self.root is not None
            target = self.root / "inputs" / label
            shutil.copytree(source, target)
            return str(target)

        def prepare_artifact_directory(self, scope_id, *, reuse):
            target = self.workspace / "turns" / scope_id / "artifacts"
            target.mkdir(parents=True, exist_ok=reuse)
            return target

        def read_workspace_directory(self, name, destination, **_limits):
            shutil.copytree(self.workspace / name, destination, dirs_exist_ok=True)

    async def hidden_scope(_service, _request, **_kwargs):
        return unhidden_read_scope(execution_machine=base_machine, host="base.example")

    async def link_lost(*_args, **kwargs):
        # The provider writes its changes, then the link drops before it exits.
        output = kwargs["write_dirs"][0] / "files" / "paper.md"
        output.parent.mkdir()
        output.write_text("Proposed after the drop\n")
        # Like a controller crash, the live side never learns the native session.
        kwargs["outcome"].remote_result_pending = True
        return
        yield

    monkeypatch.setattr(coach_module, "RemoteRunStage", RemoteStage)
    monkeypatch.setattr(recorded_settlement, "RemoteRunStage", RemoteStage)
    monkeypatch.setattr(work_turn_runtime, "RemoteRunStage", RemoteStage)
    monkeypatch.setattr("rcp.agents.command_mailbox.COMMAND_MAILBOX_REMOTE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(coach_module, "_prepare_hidden_read_scope", hidden_scope)
    monkeypatch.setattr(coach_module, "_stream_agent_events", link_lost)
    execution = execution_for(service)
    request = CoachRequest(message="Edit the paper", provider="codex")

    yield service, data, execution, request
    if execution.command_mailbox is not None:
        execution.command_mailbox.suspend()


@pytest.mark.asyncio
async def test_remote_round_left_by_a_lost_link_settles_from_its_recorded_pass(detached_coach):
    from rcp.runs.remote_finalization import recorded_finalizer
    from rcp.runs.tasks import coach as coach_module

    from .test_work_agent_io import _recorded_pass

    service, data, execution, request = detached_coach
    await run_coach(service, None, data, execution=execution, request=request)
    assert service.paper_changes.groups(service.paper.project_id) == []

    finalizer = recorded_finalizer(execution.store, execution.operation_id)
    assert finalizer is not None
    assert finalizer is coach_module.finalize_recorded_coach_result
    settled = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in finalizer(
            service,
            service.launcher,
            request,
            data,
            execution,
            _recorded_pass("", "Done", thread_id="remote-native"),
        )
    ]
    assert settled[-1].event == "done"
    assert [event.text for event in settled if event.event == "answer"] == ["Done"]
    groups = service.paper_changes.groups(service.paper.project_id)
    assert [group.changes[0].after for group in groups] == ["Proposed after the drop\n"]
    assert [session.native_session_id for session in service.paper.sessions()] == ["remote-native"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [False, True])
async def test_recorded_coach_collection_is_idempotent(detached_coach, tmp_path, refresh):
    from rcp.runs.tasks.coach import finalize_recorded_coach_result

    from .test_work_agent_io import _recorded_pass

    service, data, execution, request = detached_coach
    project = service.paper.project_id
    if refresh:
        prior = tmp_path / "prior"
        (prior / "files").mkdir(parents=True)
        (prior / "files" / "paper.md").write_text("Previous suggestion\n")
        source = execution_for(service)
        source.store.checkpoint_agent_task(source.operation_id, native_session_id="older-session")
        groups = service.paper_changes.collect(
            project, source.operation_id, saved(service.paper_live, project), prior
        )
        request = request.model_copy(
            update={
                "refresh_source_task_id": source.operation_id,
                "refresh_group_ids": [group.group_id for group in groups],
            }
        )
    launched = await run_coach(service, None, data, execution=execution, request=request)
    assert not [event for event in launched if event.event == "error"]
    recorded = _recorded_pass("", "Done", thread_id="remote-native")
    first = None
    for _ in range(2):
        events = [
            AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
            async for frame in finalize_recorded_coach_result(
                service, service.launcher, request, data, execution, recorded
            )
        ]
        assert events[-1].event == "done"
        groups = service.paper_changes.groups(project)
        assert len(groups) == 1
        assert groups[0].changes[0].after == "Proposed after the drop\n"
        if first is None:
            first = groups
        else:
            assert groups == first


@pytest.mark.asyncio
async def test_pending_coach_restores_history_mailbox(detached_coach):
    import asyncio

    from rcp.runs.remote_finalization import resume_recorded_command_mailbox
    from rcp.storage.command_mailbox import CommandMailboxStore

    from .helpers import async_wait_until
    from .test_staged_command_client import _sign

    service, data, execution, request = detached_coach
    await run_coach(service, None, data, execution=execution, request=request)
    owner = execution.command_mailbox
    assert owner is not None and owner.detached and not owner.closed
    credential = owner.staged.credential
    assert not credential.expired
    assert Path(owner.staged.client_path).is_file()
    checkpoints = CommandMailboxStore.for_data_dir(execution.store.path.parent)
    assert checkpoints.load(execution.operation_id) is not None
    await asyncio.to_thread(owner.suspend)
    restored = resume_recorded_command_mailbox(lambda: service, execution)
    assert restored is not None
    assert restored.staged.credential.identity == credential.identity
    assert restored.staged.credential.mailbox_id == credential.mailbox_id
    assert restored.staged.credential.token == credential.token
    try:
        workspace = Path(restored.staged.workspace)
        for arguments, expected in [({}, "ok"), ({"project": "unrelated-project"}, "invalid")]:
            request_id = uuid4().hex
            name = f"rcp-command-{credential.mailbox_id}-{request_id}"
            partial = workspace / f"{name}.partial"
            partial.write_text(
                json.dumps(
                    _sign(
                        restored.staged,
                        {
                            "version": 1,
                            "mailbox_id": credential.mailbox_id,
                            "request_id": request_id,
                            "credential": credential.token,
                            "verb": "graph_log",
                            "arguments": {"id": "hyp/missing", **arguments},
                        },
                    )
                )
            )
            partial.replace(workspace / f"{name}.request.json")
            response_path = workspace / f"{name}.response.json"
            await async_wait_until(response_path.exists)
            response = json.loads(response_path.read_text())
            assert response["status"] == expected
    finally:
        await asyncio.to_thread(restored.settle)
    assert restored.staged.credential.expired
    assert checkpoints.load(execution.operation_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["resume", "retry"])
async def test_stored_coach_on_another_machine_starts_fresh_on_base(
    manifest, tmp_path, monkeypatch, continuation
):
    from .helpers import signed_in_client

    data = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data)
    service = app.state.service
    wire_saved_paper(service)
    previous = execution_for(service)
    request = CoachRequest(
        message="Edit the paper", provider="codex", run_on="old-machine", session_id="old-session"
    )
    with previous.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=?,native_session_id=?,status='paused' "
            "WHERE operation_id=?",
            (request.model_dump_json(), request.session_id, previous.operation_id),
        )
    spawned = []

    def capture(record, request, **kwargs):
        spawned.append((record, request, kwargs))
        return record

    monkeypatch.setattr(app.state.background_tasks, "_spawn_record", capture)
    response = signed_in_client(app).post(
        f"/api/projects/{service.paper.project_id}/tasks/{previous.operation_id}/{continuation}",
        json={},
    )
    assert response.status_code == 202, response.text
    assert len(spawned) == 1
    record, admitted, kwargs = spawned[0]
    base = service.manifest.repository_map[service.manifest.state.repository].machine
    assert record.request["run_on"] == admitted.run_on == base
    assert record.request["session_id"] is admitted.session_id is record.native_session_id is None
    assert record.stage_root is None
    assert kwargs["continuation"] == "handoff"
    execution = AgentTaskExecution(
        record.operation_id, previous.store, AgentProcessControl(), continuation="handoff"
    )
    launches = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            launches.append(kwargs)
            execution.store.checkpoint_agent_task(
                execution.operation_id, native_session_id="new-base-session"
            )
            yield AgentEvent(event="session", session_id="new-base-session")
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data, execution=execution, request=admitted)
    assert not [event for event in events if event.event == "error"]
    assert len(launches) == 1
    assert launches[0]["session_id"] is None
    assert launches[0]["host"] == service.manifest.machine_map[base].host
    assert service.paper.sessions()[0].execution_machine == base
