from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.api import create_app
from rcp.background import AgentTaskExecution
from rcp.runs.discuss import stream_discuss_run
from rcp.runs.work import stream_work_run
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, AppStore

from .helpers import agent_patch_json, refresh_patch, seed_patch


class _RecordingLauncher:
    def __init__(self, native_session_id: str) -> None:
        self.native_session_id = native_session_id
        self.prompts: list[str] = []
        self.workspaces: list[Path] = []
        self.sessions: list[str | None] = []

    async def stream(self, _provider, prompt, **kwargs):
        self.prompts.append(prompt)
        self.workspaces.append(Path(kwargs["cwd"]))
        self.sessions.append(kwargs.get("session_id"))
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


def _wait_for_task(
    client: TestClient,
    project_id: str,
    operation_id: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        response = client.get(f"/api/projects/{project_id}/tasks/{operation_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(0.01)
    raise AssertionError("background task did not finish")


@pytest.mark.asyncio
async def test_fresh_discuss_bootstraps_one_master_with_both_mode_contracts(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
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
    prompt = launcher.prompts[0]
    artifact_directory = launcher.workspaces[0] / "turns" / execution.operation_id / "artifacts"
    assert prompt.count("This is a Discuss turn.") == 1
    assert prompt.endswith(
        f"This is a Discuss turn.\nArtifact directory for this turn: {artifact_directory}"
        f"\n\n{request.message}"
    )
    assert "RCP context update" not in prompt
    assert prompt.startswith("Open and retain the RCP chat master context at:\n")
    master_path = Path(prompt.splitlines()[1])
    master = master_path.read_text(encoding="utf-8")
    assert "## Discuss contract" in master
    assert "## Work contract" in master
    assert "Follow only the matching contract below" in master
    assert "This turn has no graph-change channel" in master
    assert "Patch JSON Schema" in master
    assert "named in the envelope" in master
    assert not (launcher.workspaces[0] / "current-turn.json").exists()
    inputs = master_path.parent
    assert len(list(inputs.glob("chat-master-v*.md"))) == 1
    assert len(list(inputs.glob("chat-patch-schema-*.json"))) == 1
    assert len(list(inputs.glob("chat-validator-client-*.py"))) == 1
    assert not list(inputs.glob("*human-request.txt"))
    assert not list(inputs.glob("*discuss*.md"))
    assert not list(inputs.glob("task-*-initial.md"))
    baseline = store.chat_session_context("codex", "laptop", session_id)
    assert baseline is not None
    snapshot = json.loads(baseline.snapshot_json)
    assert set(snapshot["values"]) == {
        "project",
        "settings",
        "current",
        "repositories",
        "skills",
        "patch",
        "workspace",
    }
    # The revision is the one graph fact the session tracks, so a human Sync
    # between turns can reach the conversation as a compact delta.
    assert snapshot["values"]["current"]["graph_revision"] == service.graph_snapshot()["revision"]
    assert "artifacts" not in snapshot["values"]
    launch_receipt = next(
        item
        for item in store.agent_task_receipts(execution.operation_id)
        if item.category == "agent_prompt"
    )
    assert launch_receipt.payload["contract_path"] == str(master_path)


def test_ordinary_resumed_discuss_sends_only_marker_message_without_unchanged_context(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
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
    client = TestClient(app)
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
    assert _wait_for_task(client, project_id, first_operation_id)["status"] == "succeeded"
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
        },
    )
    assert second_response.status_code == 202, second_response.text
    second_operation_id = second_response.json()["operation_id"]
    assert _wait_for_task(client, project_id, second_operation_id)["status"] == "succeeded"

    assert launcher.workspaces[0] == launcher.workspaces[1]
    assert launcher.sessions == [None, session_id]
    prompt = launcher.prompts[1]
    second_artifacts = launcher.workspaces[1] / "turns" / second_operation_id / "artifacts"
    assert prompt == (
        f"This is a Discuss turn.\nArtifact directory for this turn: {second_artifacts}"
        f"\n\n{second_message}"
    )
    assert prompt.count("This is a Discuss turn.") == 1
    assert "Open and retain the RCP chat master context" not in prompt
    assert "# RCP Discuss task contract" not in prompt
    assert "human-request.txt" not in prompt
    assert "RCP context update" not in prompt
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
    assert launch_receipt.payload["contract_path"] == str(master_path)


def test_mode_switch_resumes_same_native_session_and_appends_only_changed_settings(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
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
    client = TestClient(app)
    first = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": "First discuss this.",
            "run_truth_scope": ["repo-a"],
            "mode": "discuss",
        },
    )
    assert first.status_code == 202, first.text
    assert _wait_for_task(client, project_id, first.json()["operation_id"])["status"] == (
        "succeeded"
    )

    work_message = "/graph-audit Keep this slash invocation unchanged."
    second = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": work_message,
            "session_id": session_id,
            "run_truth_scope": ["repo-a"],
            "mode": "work",
            "reasoning": "high",
        },
    )
    assert second.status_code == 202, second.text
    second_id = second.json()["operation_id"]
    assert _wait_for_task(client, project_id, second_id)["status"] == "succeeded"

    assert launcher.sessions == [None, session_id]
    assert launcher.workspaces[0] == launcher.workspaces[1]
    work_artifacts = launcher.workspaces[1] / "turns" / second_id / "artifacts"
    assert launcher.prompts[1].startswith(
        f"This is a Work turn.\nArtifact directory for this turn: {work_artifacts}"
        f"\n\n{work_message}\n\n"
    )
    assert launcher.prompts[1].count("RCP context update") == 1
    assert '"reasoning": "high"' in launcher.prompts[1]
    assert '"repositories"' not in launcher.prompts[1]
    assert '"skills"' not in launcher.prompts[1]
    assert "Open and retain the RCP chat master context" not in launcher.prompts[1]


@pytest.mark.asyncio
async def test_node_chat_master_carries_the_focused_node_and_its_relations(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
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
    revision = service.graph_snapshot()["revision"]
    assert f"## Focused node, as of graph revision {revision}" in master
    # The node's own prose, not a pointer to go read it.
    assert "Search-time replanning restores future learning ability." in master
    assert '"id": "hyp/replanning-restores-plasticity"' in master
    assert "Relations one hop from this node:" in master
    assert '"other_node_id": "rq/learning-after-shift"' in master
    assert "not a live view" in master


def test_a_human_sync_between_turns_announces_only_the_new_revision(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
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
    client = TestClient(app)

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
        assert _wait_for_task(client, project_id, operation_id)["status"] == "succeeded"
        return operation_id

    turn("First question.", resume=False)
    turn("Second question, nothing moved.", resume=True)
    assert "RCP context update" not in launcher.prompts[1]

    # A human Sync between turns is the case this signal exists for.
    service.history.append(refresh_patch())
    turn("Third question, after a Sync.", resume=True)

    update = launcher.prompts[2]
    assert "RCP context update" in update
    assert f'"graph_revision": {service.graph_snapshot()["revision"]}' in update
    assert '"repositories"' not in update
    assert '"settings"' not in update


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
    service.history.append(seed_patch())
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
    client = TestClient(app)

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
        assert _wait_for_task(client, project_id, operation_id)["status"] == "succeeded"

    before = service.graph_snapshot()["revision"]
    turn("Record the transfer question.", resume=False)
    assert service.graph_snapshot()["revision"] > before

    turn("Now just answer something.", resume=True)
    assert "RCP context update" not in launcher.prompts[1]

    # A Sync by someone else still reaches the conversation.
    service.history.append(refresh_patch("rq/a-third-question"))
    turn("And after a human Sync.", resume=True)
    assert f'"graph_revision": {service.graph_snapshot()["revision"]}' in launcher.prompts[2]
