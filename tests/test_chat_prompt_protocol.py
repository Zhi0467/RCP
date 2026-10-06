from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent, AgentProcessControl, continuation_prompt
from rcp.agents.continuation_prompt import SECTIONS
from rcp.agents.prompts import (
    CHAT_MASTER_CONTEXT_VERSION,
    COMMAND_CLIENT,
    chat_master_contract_key,
)
from rcp.api.tasks import _validate_stored_task_request
from rcp.background import AgentTaskExecution
from rcp.config import ComputeConnectionConfig
from rcp.core.models import AuthorizedHuman
from rcp.providers import ProviderSkillReference
from rcp.runs.tasks.discuss import stream_discuss_run
from rcp.runs.tasks.work import stream_work_run
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.skill_registry import SkillDefaults
from rcp.storage import AgentTaskRecord, AppStore
from tests.helpers import signed_in_client

from .helpers import (
    agent_patch_json,
    append_fixture_patch,
    changed_values,
    current_command_client,
    launch_contract_path,
    refresh_patch,
    seed_patch,
    wait_for_task_response,
)
from .helpers import create_named_app as create_app


class _RecordingLauncher:
    def __init__(self, native_session_id: str) -> None:
        self.native_session_id = native_session_id
        self.prompts: list[str] = []
        self.workspaces: list[Path] = []
        self.sessions: list[str | None] = []
        self.launch_kwargs: list[dict[str, object]] = []

    async def stream(self, _provider, prompt, **kwargs):
        if kwargs["capability"] == "discuss" and kwargs.get("invocation_gate") is not None:
            gate = kwargs.get("invocation_gate")
            assert gate is not None
            argv = shlex.split(current_command_client(prompt))
            authority = list(gate.client_arguments())
            prefix = list(gate.client_executable_argv())
            assert argv[: len(prefix)] == prefix
            assert argv[len(prefix) : len(prefix) + len(authority)] == authority
        if kwargs["capability"] == "work_auto":
            # This turn's launches run through its own gate, named by the current client.
            argv = shlex.split(current_command_client(prompt))
            gate = kwargs["invocation_gate"]
            authority = list(gate.client_arguments())
            prefix = list(gate.client_executable_argv())
            assert argv[: len(prefix)] == prefix
            assert argv[len(prefix) : len(prefix) + len(authority)] == authority
            assert argv[argv.index("--workspace") + 1] == str(kwargs["cwd"])
            launch = f"{COMMAND_CLIENT} " + shlex.join(
                ["launch", "--key", "<idempotency-key>", "--cwd", "<working-directory>", "--"]
            )
            master = launch_contract_path(prompt).read_text()
            assert any(launch in text for text in (*self.prompts, prompt, master))
        self.prompts.append(prompt)
        self.workspaces.append(Path(kwargs["cwd"]))
        self.sessions.append(kwargs.get("session_id"))
        self.launch_kwargs.append(kwargs)
        yield AgentEvent(event="session", session_id=self.native_session_id)
        yield AgentEvent(event="answer", text="Discuss answered.")
        yield AgentEvent(event="done")


def _execution(
    store: AppStore,
    *,
    operation_id: str,
    project_id: str,
    request: RunRequest,
    native_session_id: str | None = None,
    human: bool = False,
) -> AgentTaskExecution:
    now = store.now()
    task_kind = "node_chat" if request.chat_scope == "node" else "project_chat"
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind=task_kind,
            status="running",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="running",
            native_session_id=native_session_id,
            dispatch_authority=resolve_dispatch_authority(task_kind, request),
            authorized_by=AuthorizedHuman(
                space_id=str(uuid4()), user_id=str(uuid4()), display_name="Human"
            )
            if human
            else None,
        )
    )
    store.record_agent_task_receipt(
        operation_id,
        "operation_created",
        {
            "kind": task_kind,
            "attempt": 1,
            "has_parent": False,
            "resumed": False,
        },
    )
    return AgentTaskExecution(
        operation_id=operation_id,
        store=store,
        control=AgentProcessControl(),
    )


def _enable_graph_audit(service) -> None:
    surfaces = ("seed", "refresh", "node_chat", "project_chat", "paper_coach")
    service.history.update_agent_settings(
        service.manifest.agent.default_run_truth_scope,
        {surface: service.manifest.agent_profile(surface) for surface in surfaces},
        skill_defaults=SkillDefaults(skill_ids=["graph-audit"]),
    )


def _configure_compute_connections(
    service,
    connections: list[ComputeConnectionConfig],
) -> None:
    surfaces = ("seed", "refresh", "node_chat", "project_chat", "paper_coach")
    service.history.update_agent_settings(
        service.manifest.agent.default_run_truth_scope,
        {surface: service.manifest.agent_profile(surface) for surface in surfaces},
        compute_connections=connections,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["version", "graph_rules"])
async def test_contract_version_change_rebootstraps_an_existing_native_chat(
    manifest, tmp_path, stale
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    chat_id = "contract-version-chat"
    session_id = "contract-version-native-session"
    launcher = _RecordingLauncher(session_id)

    first_request = RunRequest(
        chat_scope="project",
        chat_id=chat_id,
        message="Start the native chat.",
        run_truth_scope=["repo-a"],
        mode="discuss",
    )
    first_execution = _execution(
        store,
        operation_id="contract-version-first",
        project_id=project_id,
        request=first_request,
        native_session_id=session_id,
    )
    async for _frame in stream_discuss_run(
        service,
        launcher,
        first_request,
        tmp_path / "data",
        execution=first_execution,
    ):
        pass
    store.complete_agent_task(first_execution.operation_id, applied_revision=None, result={})

    current = store.chat_session_context("codex", "laptop", session_id)
    assert current is not None
    stale_snapshot = json.loads(current.snapshot_json)
    # A release that only changes graph rules must reach existing chats as surely as a
    # hand-bumped master-context version does.
    stale_version = CHAT_MASTER_CONTEXT_VERSION - (stale == "version")
    stale_snapshot["master_context_version"] = stale_version
    with pytest.MonkeyPatch.context() as older_rules:
        if stale == "graph_rules":
            older_rules.setattr(continuation_prompt, "GRAPH_RULES_VERSION", "0" * 16)
        stale_snapshot["contract_key"] = (
            f"chat-master-v{stale_version}"
            if stale == "version"
            else chat_master_contract_key(ontology_extensions=False)
        )
    stale_snapshot["master_context_path"] = f"/stale/chat-master-v{stale_version}.md"
    stale_json = json.dumps(stale_snapshot, separators=(",", ":"))
    store.commit_chat_session_context(
        provider="codex",
        execution_machine="laptop",
        native_session_id=session_id,
        project_id=project_id,
        kind="project_chat",
        chat_id=chat_id,
        node_id=None,
        protocol_version=stale_version,
        snapshot_json=stale_json,
        snapshot_sha256=hashlib.sha256(stale_json.encode("utf-8")).hexdigest(),
        committed_operation_id=first_execution.operation_id,
        expected_snapshot_sha256=current.snapshot_sha256,
    )

    second_request = first_request.model_copy(
        update={"message": "Use the current contract.", "session_id": session_id}
    )
    second_execution = _execution(
        store,
        operation_id="contract-version-second",
        project_id=project_id,
        request=second_request,
        native_session_id=session_id,
    )
    async for _frame in stream_discuss_run(
        service,
        launcher,
        second_request,
        tmp_path / "data",
        execution=second_execution,
    ):
        pass

    second_prompt = launcher.prompts[1]
    # A continuation names its replacement master last, after the turn's own parts.
    master_line = second_prompt.split("\n\n")[-1].splitlines()[1]
    assert f"chat-master-v{CHAT_MASTER_CONTEXT_VERSION}-" in master_line
    committed = store.chat_session_context("codex", "laptop", session_id)
    assert committed is not None
    assert committed.protocol_version == CHAT_MASTER_CONTEXT_VERSION
    assert json.loads(committed.snapshot_json)["contract_key"] == chat_master_contract_key(
        ontology_extensions=False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["recorded", "legacy_file", "legacy_missing"])
@pytest.mark.parametrize("watcher_wake", [False, True])
async def test_follow_up_reuses_the_session_master_by_its_exact_bytes(
    manifest, tmp_path, state, watcher_wake
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    session_id = "session-master-native"
    launcher = _RecordingLauncher(session_id)
    first_request = RunRequest(
        chat_scope="project",
        chat_id="session-master-chat",
        message="Start the native chat.",
        run_truth_scope=["repo-a"],
        mode="discuss",
    )

    async def turn(operation_id: str, request: RunRequest) -> None:
        execution = _execution(
            store,
            operation_id=operation_id,
            project_id=project_id,
            request=request,
            native_session_id=session_id,
        )
        if watcher_wake and request.session_id is not None:
            execution.continuation = "watcher_wake"
        run = stream_work_run if request.mode == "work" else stream_discuss_run
        async for _frame in run(service, launcher, request, tmp_path / "data", execution=execution):
            assert json.loads(_frame.removeprefix("data: "))["event"] != "error", _frame
        store.complete_agent_task(operation_id, applied_revision=None, result={})

    await turn("session-master-first", first_request)
    master_path = Path(launcher.prompts[0].splitlines()[1])
    recorded = store.agent_task_contract("session-master-first", "session_master")
    assert recorded == master_path.read_text(encoding="utf-8")
    baseline = store.chat_session_context("codex", "laptop", session_id)
    assert baseline is not None
    snapshot = json.loads(baseline.snapshot_json)
    assert snapshot["master_operation_id"] == "session-master-first"
    assert snapshot["master_sha256"] == hashlib.sha256(recorded.encode("utf-8")).hexdigest()

    if state != "recorded":
        # A snapshot committed before master records existed names only a path.
        legacy = {
            key: value
            for key, value in snapshot.items()
            if key not in {"master_operation_id", "master_sha256"}
        }
        legacy_json = json.dumps(legacy, separators=(",", ":"))
        store.commit_chat_session_context(
            provider="codex",
            execution_machine="laptop",
            native_session_id=session_id,
            project_id=project_id,
            kind="project_chat",
            chat_id=first_request.chat_id,
            node_id=None,
            protocol_version=baseline.protocol_version,
            snapshot_json=legacy_json,
            snapshot_sha256=hashlib.sha256(legacy_json.encode("utf-8")).hexdigest(),
            committed_operation_id="session-master-first",
            expected_snapshot_sha256=baseline.snapshot_sha256,
        )
    if state != "legacy_file":
        master_path.unlink()

    await turn(
        "session-master-second",
        first_request.model_copy(
            update={
                "message": "Continue.",
                "session_id": session_id,
                "mode": "work" if watcher_wake else "discuss",
                "trigger": "watcher" if watcher_wake else "human",
            }
        ),
    )
    assert launcher.sessions == [None, session_id]
    # The settled turn becomes the session's committed baseline.
    assert (
        store.chat_session_context("codex", "laptop", session_id).committed_operation_id
        == "session-master-second"
    )
    prompt_receipt = next(
        receipt
        for receipt in store.agent_task_receipts("session-master-second")
        if receipt.category == "chat_master_context"
    )
    assert prompt_receipt.payload["node"] == ("wake" if watcher_wake else "human_turn")
    assert prompt_receipt.payload["bootstrapped"] is (state == "legacy_missing")
    assert store.agent_task_contract("session-master-second", "chat_turn") == launcher.prompts[1]
    last_section = launcher.prompts[1].split("\n\n")[-1]
    captured = store.agent_task_contract("session-master-second", "session_master")
    committed = json.loads(store.chat_session_context("codex", "laptop", session_id).snapshot_json)
    if state == "legacy_missing":
        # Nothing proves what the session held, so a fresh master is sent as a replacement.
        master_path = Path(committed["master_context_path"])
        assert last_section == SECTIONS["master_rebootstrap"].format(path=master_path)
        assert captured == master_path.read_text(encoding="utf-8")
        assert committed["master_operation_id"] == "session-master-second"
        return
    assert last_section == SECTIONS["master_pointer"].format(path=master_path)
    if watcher_wake:
        assert "patch.command_client" in changed_values(launcher.prompts[1])
    assert master_path.read_text(encoding="utf-8") == recorded
    assert recorded not in launcher.prompts[1]
    assert captured == (recorded if state == "legacy_file" else None)
    assert committed["master_operation_id"] == (
        "session-master-second" if state == "legacy_file" else "session-master-first"
    )


@pytest.mark.asyncio
async def test_fresh_discuss_stages_one_master_and_turn_inputs(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    session_id = "native-discuss-session"
    request = RunRequest(
        chat_scope="project",
        chat_id="master-context-chat",
        message="Explain the current project.",
        run_truth_scope=["repo-a"],
        mode="discuss",
    )
    execution = _execution(
        store,
        operation_id="discuss-master-first",
        human=True,
        project_id=project_id,
        request=request,
        native_session_id=session_id,
    )
    launcher = _RecordingLauncher(session_id)

    async for _frame in stream_discuss_run(
        service,
        launcher,
        request,
        tmp_path / "data",
        execution=execution,
    ):
        pass

    assert launcher.sessions == [None]
    assert launcher.launch_kwargs[0]["invocation_gate"] is not None
    prompt = launcher.prompts[0]
    artifact_directory = launcher.workspaces[0] / "turns" / execution.operation_id / "artifacts"
    assert str(artifact_directory) in prompt
    assert prompt.count(request.message) == 1
    master_path = Path(prompt.splitlines()[1])
    assert not (launcher.workspaces[0] / "current-turn.json").exists()
    assert launcher.workspaces[0].parent / "inputs" in launcher.launch_kwargs[0]["read_dirs"]
    inputs = master_path.parent
    assert len(list(inputs.glob("chat-master-v*.md"))) == 1
    assert len(list(inputs.glob("chat-patch-schema-*.json"))) == 1
    assert len(list(inputs.glob("rcp-agent-client-*.py"))) == 1
    assert not list(inputs.glob("*human-request.txt"))
    assert [path.name for path in inputs.glob("task-*.md")] == [
        f"task-{execution.operation_id}-prompt.md"
    ]
    baseline = store.chat_session_context("codex", "laptop", session_id)
    assert baseline is not None
    snapshot = json.loads(baseline.snapshot_json)
    assert set(snapshot["values"]) == {
        "project",
        "settings",
        "current",
        "repositories",
        "compute",
        "skills",
        "patch",
        "workspace",
        "browser",
        "discuss",
    }
    # The revision is the one graph fact the session tracks, so a human Sync
    # between turns can reach the conversation as a compact delta.
    assert snapshot["values"]["current"]["graph_revision"] == service.graph_snapshot()["revision"]
    assert snapshot["values"]["compute"] == {"active": []}
    assert "artifacts" not in snapshot["values"]
    launch_receipt = next(
        item
        for item in store.agent_task_receipts(execution.operation_id)
        if item.category == "agent_prompt"
    )
    assert launch_receipt.payload["contract_path"] == str(
        inputs / f"task-{execution.operation_id}-prompt.md"
    )


@pytest.mark.parametrize("mode", ["discuss", "work"])
def test_fresh_chat_master_contains_only_selected_nonsecret_compute_metadata(
    manifest, tmp_path, mode: str
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    _configure_compute_connections(
        service,
        [
            ComputeConnectionConfig(id="current", name="Current machine", kind="local"),
            ComputeConnectionConfig(
                id="gpu",
                name="GPU VM",
                kind="ssh",
                ssh_target="alice@gpu.example",
                access_hint="Use /scratch/shared for temporary outputs",
            ),
        ],
    )
    project_id = app.state.default_project_id
    chat_id = "3f3eea3f-0e1d-4c92-a1c2-71c877be342d"
    session_id = f"native-compute-{mode}"
    launcher = _RecordingLauncher(session_id)

    async def stream(_project_id, kind, request, execution):
        run = stream_work_run if request.mode == "work" else stream_discuss_run
        async for frame in run(
            service,
            launcher,
            request,
            tmp_path / "data",
            execution=execution,
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": "Use the attached compute resource.",
            "run_truth_scope": ["repo-a"],
            "mode": mode,
            "active_compute_ids": ["gpu"],
            "resolved_compute_context": {
                "active": [{"id": "forged", "name": "Client forged", "kind": "local"}]
            },
        },
    )
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    assert wait_for_task_response(client, project_id, operation_id)["status"] == "succeeded"

    task = app.state.background_tasks.store.agent_task(operation_id)
    assert task is not None
    assert task.request["provider"] == "codex"
    assert task.request["run_on"] == "laptop"
    assert task.request["active_compute_ids"] == ["gpu"]
    assert task.request["resolved_compute_context"] == {
        "active": [
            {
                "id": "gpu",
                "name": "GPU VM",
                "kind": "ssh",
                "ssh_target": "alice@gpu.example",
                "access_hint": "Use /scratch/shared for temporary outputs",
            }
        ]
    }
    assert launcher.sessions == [None]
    assert launcher.launch_kwargs[0].get("host", "") == ""

    master_path = Path(launcher.prompts[0].splitlines()[1])
    master = master_path.read_text(encoding="utf-8")
    assert "alice@gpu.example" in master
    assert "Use /scratch/shared for temporary outputs" in master
    assert '"id": "laptop"' not in master
    assert ".ssh/" not in master
    assert "identity_file" not in master
    assert "private_key" not in master

    baseline = app.state.background_tasks.store.chat_session_context("codex", "laptop", session_id)
    assert baseline is not None
    compute = json.loads(baseline.snapshot_json)["values"]["compute"]
    assert compute == {
        "active": [
            {
                "id": "gpu",
                "name": "GPU VM",
                "kind": "ssh",
                "ssh_target": "alice@gpu.example",
                "access_hint": "Use /scratch/shared for temporary outputs",
            }
        ]
    }


@pytest.mark.parametrize("manifest_change", ["retarget", "delete"])
def test_admitted_compute_snapshot_survives_manifest_change_before_launch(
    manifest,
    tmp_path,
    manifest_change: str,
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    gpu = ComputeConnectionConfig(
        id="gpu",
        name="Admission GPU",
        kind="ssh",
        ssh_target="alice@admitted.example",
        access_hint="Use /scratch/admitted",
    )
    _configure_compute_connections(service, [gpu])
    project_id = app.state.default_project_id
    chat_id = "757241ec-60af-4784-b315-44003a93c1dd"
    launch_gate = threading.Event()
    launcher = _RecordingLauncher("admission-native-session")

    async def stream(_project_id, kind, request, execution):
        await asyncio.to_thread(launch_gate.wait)
        async for frame in stream_discuss_run(
            service,
            launcher,
            request,
            tmp_path / "data",
            execution=execution,
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": "Use the admitted GPU.",
            "mode": "discuss",
            "active_compute_ids": ["gpu"],
        },
    )
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    admitted = app.state.background_tasks.store.agent_task(operation_id)
    assert admitted is not None
    assert admitted.request["resolved_compute_context"]["active"][0]["ssh_target"] == (
        "alice@admitted.example"
    )

    if manifest_change == "retarget":
        _configure_compute_connections(
            service,
            [gpu.model_copy(update={"ssh_target": "alice@changed.example"})],
        )
    else:
        _configure_compute_connections(service, [])
    _validate_stored_task_request(service, "project_chat", admitted.request)
    launch_gate.set()
    assert wait_for_task_response(client, project_id, operation_id)["status"] == "succeeded"

    master = Path(launcher.prompts[0].splitlines()[1]).read_text(encoding="utf-8")
    assert "alice@admitted.example" in master
    assert "Use /scratch/admitted" in master
    assert "alice@changed.example" not in master


def test_resumed_chat_delivers_changed_compute_metadata_in_the_same_session(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    current = ComputeConnectionConfig(id="current", name="Current machine", kind="local")
    gpu = ComputeConnectionConfig(
        id="gpu",
        name="GPU VM",
        kind="ssh",
        ssh_target="alice@gpu.example",
        access_hint="Use /scratch/old",
    )
    _configure_compute_connections(service, [current, gpu])
    project_id = app.state.default_project_id
    chat_id = "943025d2-e23d-4a6c-a48e-6d16ded8d870"
    session_id = "native-compute-delta"
    launcher = _RecordingLauncher(session_id)

    async def stream(_project_id, kind, request, execution):
        async for frame in stream_discuss_run(
            service,
            launcher,
            request,
            tmp_path / "data",
            execution=execution,
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)

    def turn(message: str, active_compute_ids: list[str], *, first: bool = False) -> None:
        body: dict[str, object] = {
            "chat_id": chat_id,
            "message": message,
            "run_truth_scope": ["repo-a"],
            "mode": "discuss",
            "active_compute_ids": active_compute_ids,
        }
        if not first:
            body["session_id"] = session_id
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json=body,
        )
        assert response.status_code == 202, response.text
        operation_id = response.json()["operation_id"]
        assert wait_for_task_response(client, project_id, operation_id)["status"] == "succeeded"

    turn("Start with local compute.", ["current"], first=True)
    turn("Keep the same compute.", ["current"])

    turn("Switch to the GPU.", ["gpu"])
    assert gpu.ssh_target in launcher.prompts[2]
    assert gpu.access_hint in launcher.prompts[2]
    assert ".ssh/" not in launcher.prompts[2]
    assert "private_key" not in launcher.prompts[2]

    renamed_gpu = gpu.model_copy(
        update={
            "name": "GPU Accelerator",
            "ssh_target": "alice@gpu-v2.example",
            "access_hint": "Use /scratch/new",
        }
    )
    _configure_compute_connections(service, [current, renamed_gpu])
    turn("Use the renamed resource.", ["gpu"])
    assert renamed_gpu.ssh_target in launcher.prompts[3]
    assert renamed_gpu.access_hint in launcher.prompts[3]

    turn("Detach compute.", [])
    assert launcher.sessions == [None, session_id, session_id, session_id, session_id]

    transcript = service.chat_transcript(chat_id)
    assert transcript is not None
    assert transcript.messages[-1].active_compute_ids == []


@pytest.mark.asyncio
async def test_fresh_discuss_passes_provider_native_receipt_beside_unchanged_message(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    message = "/native-review  preserve  spacing\nand this line."
    request = RunRequest(
        chat_scope="project",
        chat_id="provider-native-discuss",
        message=message,
        run_truth_scope=["repo-a"],
        mode="discuss",
        resolved_provider_skills=[
            ProviderSkillReference(
                provider="codex",
                machine="laptop",
                provider_version="codex-cli 0.146.1",
                inventory_hash="a" * 64,
                name="native-review",
                label="Native review",
                description="Review with the native checklist.",
                stale=True,
            )
        ],
    )
    execution = _execution(
        store,
        operation_id="provider-native-discuss-first",
        project_id=project_id,
        request=request,
    )
    launcher = _RecordingLauncher("provider-native-session")

    async for _frame in stream_discuss_run(
        service,
        launcher,
        request,
        tmp_path / "data",
        execution=execution,
    ):
        pass

    prompt = launcher.prompts[0]
    assert prompt.count(message) == 1
    assert '"native_token": "$native-review"' in prompt
    assert '"stale": true' in prompt


def test_ordinary_resumed_discuss_repeats_only_master_pointer_with_turn_context(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    _enable_graph_audit(service)
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    chat_id = "74fd1a76-c6ee-4f5e-a0af-6d80f69297b5"
    session_id = "native-resumed-discuss-session"
    launcher = _RecordingLauncher(session_id)
    first_message = "First question."

    async def stream(_project_id, kind, request, execution):
        assert kind == "project_chat"
        async for frame in stream_discuss_run(
            service,
            launcher,
            request,
            tmp_path / "data",
            execution=execution,
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    first_response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": first_message,
            "run_truth_scope": ["repo-a"],
            "mode": "discuss",
        },
    )
    assert first_response.status_code == 202, first_response.text
    first_operation_id = first_response.json()["operation_id"]
    assert wait_for_task_response(client, project_id, first_operation_id)["status"] == "succeeded"
    master_path = Path(launcher.prompts[0].splitlines()[1])

    second_message = "/graph-audit Keep  this spacing.\nAnd this line."
    second_response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": second_message,
            "session_id": session_id,
            "run_truth_scope": ["repo-a"],
            "mode": "discuss",
            "invoked_skill_ids": ["graph-audit"],
        },
    )
    assert second_response.status_code == 202, second_response.text
    second_operation_id = second_response.json()["operation_id"]
    assert wait_for_task_response(client, project_id, second_operation_id)["status"] == "succeeded"

    assert launcher.workspaces[0] == launcher.workspaces[1]
    assert launcher.sessions == [None, session_id]
    prompt = launcher.prompts[1]
    second_artifacts = launcher.workspaces[1] / "turns" / second_operation_id / "artifacts"
    assert str(second_artifacts) in prompt
    assert str(master_path) in prompt
    assert prompt.count(second_message) == 1
    assert "/skill/graph-audit" in prompt
    assert "human-request.txt" not in prompt
    assert not (launcher.workspaces[1] / "current-turn.json").exists()
    inputs = master_path.parent
    assert len(list(inputs.glob("chat-master-v*.md"))) == 1
    assert not list(inputs.glob("*human-request.txt"))
    assert not list(inputs.glob("task-*-initial.md"))
    launch_receipt = next(
        item
        for item in store.agent_task_receipts(second_operation_id)
        if item.category == "agent_prompt"
    )
    contract_path = Path(launch_receipt.payload["contract_path"])
    assert contract_path.read_text(encoding="utf-8") == prompt

    third_message = "No package invocation on this turn."
    third_response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": third_message,
            "session_id": session_id,
            "run_truth_scope": ["repo-a"],
            "mode": "discuss",
        },
    )
    assert third_response.status_code == 202, third_response.text
    third_operation_id = third_response.json()["operation_id"]
    assert wait_for_task_response(client, project_id, third_operation_id)["status"] == "succeeded"
    third_prompt = launcher.prompts[2]
    assert third_prompt.count(third_message) == 1


@pytest.mark.parametrize("legacy_layout", [False, True])
@pytest.mark.parametrize("first_mode", ["discuss", "work"])
def test_mode_switch_resumes_same_native_session_and_appends_only_changed_settings(
    manifest, tmp_path, legacy_layout: bool, first_mode: str
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    _enable_graph_audit(service)
    append_fixture_patch(service, seed_patch())
    project_id = app.state.default_project_id
    chat_id = "65d1ae3c-f234-4abe-96b7-f28c40d85a1b"
    session_id = "native-mode-switch-session"
    launcher = _RecordingLauncher(session_id)

    async def stream(_project_id, kind, request, execution):
        assert kind == "project_chat"
        run = stream_work_run if request.mode == "work" else stream_discuss_run
        async for frame in run(
            service,
            launcher,
            request,
            tmp_path / "data",
            execution=execution,
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    first = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": "First discuss this.",
            "run_truth_scope": ["repo-a"],
            "mode": first_mode,
        },
    )
    assert first.status_code == 202, first.text
    assert wait_for_task_response(client, project_id, first.json()["operation_id"])["status"] == (
        "succeeded"
    )
    if legacy_layout:
        with app.state.background_tasks.store.connection() as connection:
            connection.execute(
                "DELETE FROM graph_run_receipts WHERE category = 'chat_stage_layout'"
            )

    work_message = "/graph-audit Keep this slash invocation unchanged."
    second = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": work_message,
            "session_id": session_id,
            "run_truth_scope": ["repo-a"],
            "mode": "work" if first_mode == "discuss" else "discuss",
            "reasoning": "high",
            "invoked_skill_ids": ["graph-audit"],
        },
    )
    assert second.status_code == 202, second.text
    second_id = second.json()["operation_id"]
    assert wait_for_task_response(client, project_id, second_id)["status"] == "succeeded"

    assert launcher.sessions == [None, session_id]
    if legacy_layout:
        assert launcher.workspaces[1] == launcher.workspaces[0].parent
    else:
        assert launcher.workspaces[0] == launcher.workspaces[1]
    work_artifacts = launcher.workspaces[1] / "turns" / second_id / "artifacts"
    assert str(work_artifacts) in launcher.prompts[1]
    assert launcher.prompts[1].count(work_message) == 1
    delta = changed_values(launcher.prompts[1])
    assert delta["settings.reasoning"] == "high"
    assert not any(key.startswith(("repositories", "skills")) for key in delta)

    if first_mode == "work":
        assert json.loads(delta["discuss.execution_instructions"])
        assert launcher.launch_kwargs[1]["write_scope"] is None


@pytest.mark.asyncio
async def test_node_chat_master_carries_the_focused_node_and_its_relations(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    session_id = "native-node-snapshot-session"
    request = RunRequest(
        chat_scope="node",
        chat_id="node-snapshot-chat",
        node_id="hyp/replanning-restores-plasticity",
        message="What does this claim?",
        run_truth_scope=["repo-a"],
        mode="discuss",
    )
    execution = _execution(
        store,
        operation_id="discuss-node-snapshot",
        project_id=project_id,
        request=request,
        native_session_id=session_id,
    )
    launcher = _RecordingLauncher(session_id)

    async for _frame in stream_discuss_run(
        service, launcher, request, tmp_path / "data", execution=execution
    ):
        pass

    master = Path(launcher.prompts[0].splitlines()[1]).read_text(encoding="utf-8")
    # The node's own prose, not a pointer to go read it.
    assert service.history.state().nodes[request.node_id].statement in master
    assert '"id": "hyp/replanning-restores-plasticity"' in master
    assert '"other_node_id": "rq/learning-after-shift"' in master


def test_a_human_sync_between_turns_announces_only_the_new_revision(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    project_id = app.state.default_project_id
    chat_id = "0b1c2d3e-4f50-4a61-8b72-9c83d4e5f607"
    session_id = "native-sync-delta-session"
    launcher = _RecordingLauncher(session_id)

    async def stream(_project_id, kind, request, execution):
        async for frame in stream_discuss_run(
            service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)

    def turn(message: str, resume: bool) -> str:
        body: dict[str, object] = {
            "chat_id": chat_id,
            "message": message,
            "run_truth_scope": ["repo-a"],
            "mode": "discuss",
        }
        if resume:
            body["session_id"] = session_id
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json=body,
        )
        assert response.status_code == 202, response.text
        operation_id = response.json()["operation_id"]
        assert wait_for_task_response(client, project_id, operation_id)["status"] == "succeeded"
        return operation_id

    turn("First question.", resume=False)
    turn("Second question, nothing moved.", resume=True)

    # A human Sync between turns is the case this signal exists for.
    append_fixture_patch(service, refresh_patch())
    turn("Third question, after a Sync.", resume=True)

    update = changed_values(launcher.prompts[2])
    assert update["current.graph_revision"] == str(service.graph_snapshot()["revision"])
    assert not any(key.startswith(("repositories", "settings")) for key in update)


class _PatchWritingLauncher(_RecordingLauncher):
    """A Work turn that actually moves the graph, so its own revision is its own."""

    def __init__(self, native_session_id: str, patches: list[str | None]) -> None:
        super().__init__(native_session_id)
        self.patches = patches

    async def stream(self, _provider, prompt, **kwargs):
        workspace = Path(kwargs["cwd"])
        index = len(self.prompts)
        if index < len(self.patches) and self.patches[index] is not None:
            (workspace / "patch.json").write_text(self.patches[index], encoding="utf-8")
        async for event in super().stream(_provider, prompt, **kwargs):
            yield event


def test_a_work_turn_does_not_announce_its_own_revision_back_to_itself(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    project_id = app.state.default_project_id
    chat_id = "5f6a7b8c-9d01-4e12-8f23-0a1b2c3d4e5f"
    session_id = "native-own-revision-session"
    launcher = _PatchWritingLauncher(session_id, [agent_patch_json(refresh_patch()), None, None])

    async def stream(_project_id, kind, request, execution):
        async for frame in stream_work_run(
            service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)

    def turn(message: str, resume: bool) -> None:
        body: dict[str, object] = {
            "chat_id": chat_id,
            "message": message,
            "run_truth_scope": ["repo-a"],
            "mode": "work",
        }
        if resume:
            body["session_id"] = session_id
        response = client.post(f"/api/projects/{project_id}/tasks/project_chat", json=body)
        assert response.status_code == 202, response.text
        operation_id = response.json()["operation_id"]
        assert wait_for_task_response(client, project_id, operation_id)["status"] == "succeeded"

    before = service.graph_snapshot()["revision"]
    turn("Record the transfer question.", resume=False)
    assert service.graph_snapshot()["revision"] > before

    turn("Now just answer something.", resume=True)
    second_delta = changed_values(launcher.prompts[1])
    assert {key.split(".")[0] for key in second_delta} == {"patch"}
    assert "rcp-agent-client-" in second_delta["patch.command_client"]

    # A Sync by someone else still reaches the conversation.
    append_fixture_patch(service, refresh_patch("rq/a-third-question"))
    turn("And after a human Sync.", resume=True)
    third_delta = changed_values(launcher.prompts[2])
    assert third_delta["current.graph_revision"] == str(service.graph_snapshot()["revision"])


@pytest.mark.parametrize("mode", ["work", "discuss"])
def test_a_chat_prompt_past_the_receipt_cap_still_resumes_from_its_contract(
    manifest, tmp_path, mode: str
) -> None:
    """The launch receipt drops an oversized prompt whole; the durable contract
    must still let a later continuation find the turn's original prompt."""

    from rcp.limits import AGENT_TASK_RECEIPT_MAX_BYTES
    from rcp.runs.shared import _parent_task_contract_path

    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    launcher = _RecordingLauncher("oversized-session")

    async def stream(_project_id, kind, request, execution):
        run = stream_work_run if request.mode == "work" else stream_discuss_run
        async for frame in run(service, launcher, request, tmp_path / "data", execution=execution):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": "7d1e2f30-4a5b-4c6d-8e7f-90a1b2c3d4e5",
            "message": "Long assignment. " * (AGENT_TASK_RECEIPT_MAX_BYTES // 16),
            "run_truth_scope": ["repo-a"],
            "mode": mode,
        },
    )
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    wait_for_task_response(client, project_id, operation_id)

    receipt = next(
        r for r in store.agent_task_receipts(operation_id) if r.category == "agent_prompt"
    )
    assert receipt.payload["reason"] == "payload_exceeded_limit"
    first = store.agent_task(operation_id)
    assert first is not None and first.stage_root is not None
    now = store.now()
    store.create_agent_task(
        first.model_copy(
            update={
                "operation_id": "wake",
                "status": "running",
                "parent_operation_id": operation_id,
                "attempt": 2,
                "created_at": now,
                "updated_at": now,
            }
        )
    )
    wake = AgentTaskExecution(operation_id="wake", store=store, control=AgentProcessControl())

    path = _parent_task_contract_path(wake, Path(first.stage_root), None)
    assert Path(path).read_text(encoding="utf-8") == launcher.prompts[0]
