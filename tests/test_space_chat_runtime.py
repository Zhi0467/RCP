from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.space_chat import (
    SpaceThreadOwner,
    default_space_hidden_read_scope,
    stream_space_chat_run,
)
from rcp.service import read_space_chat_transcript
from tests.helpers import agent_patch_json, create_named_app, seed_patch, store_test_claude_token
from tests.test_dispatch_authority import _authorizer, _record, _work_authority, _work_request


@pytest.mark.parametrize("project_count", [0, 2])
def test_space_runtime_real_storage_and_partial_apply(
    manifest, tmp_path, monkeypatch, project_count
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    human = _authorizer(store)
    if project_count == 0:
        with store.connection() as connection:
            connection.execute("DELETE FROM project_members WHERE user_id=?", (human.user_id,))
    else:
        other = tmp_path / "other"
        for directory in ("repo-a/.research", "repo-b", "claude", "codex"):
            (other / directory).mkdir(parents=True, exist_ok=True)
        locator = other / "repo-a/.research/manifest.toml"
        locator.write_text(manifest.path.read_text().replace(str(tmp_path), str(other)))
        app.state.catalog.register(
            str(locator), seat_member=human.user_id, identity_action="created"
        )
    threads = store.ensure_agent_threads(
        human.user_id, list(store.member_project_ids(human.user_id)), default_names=("Pip",)
    )
    thread = next(item for item in threads if item.kind == "space")
    request = _work_request().model_copy(
        update={"chat_id": thread.thread_id, "provider": "claude", "run_truth_scope": []}
    )
    task = _record(
        store,
        operation_id=str(uuid.uuid4()),
        project_id=store.space_id,
        request=request,
        authorized_by=human,
        dispatch_authority=_work_authority(request),
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='space_chat' WHERE operation_id=?", (task.operation_id,)
        )
    store.record_agent_task_receipt(
        task.operation_id,
        "operation_created",
        {"kind": "space_chat", "attempt": task.attempt, "has_parent": False, "resumed": False},
    )
    store_test_claude_token(store)
    owner = SpaceThreadOwner(store, app.state.catalog, thread.thread_id)
    execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
    installed = []
    monkeypatch.setattr(
        "rcp.runs.space_chat.install_space_hidden_read_scope", lambda scope: installed.append(scope)
    )

    async def prepare_policy(*args, **kwargs):
        return default_space_hidden_read_scope(owner, request, kwargs["workspace"], execution)

    monkeypatch.setattr("rcp.runs.shared._prepare_hidden_read_scope", prepare_policy)

    calls = []

    async def provider(_provider, prompt, **kwargs):
        calls.append(kwargs.get("session_id"))
        stage = kwargs["cwd"]
        scope = kwargs["write_scope"]
        assert len(scope.projects) == project_count
        assert (
            str(tmp_path / "data" / "agent-threads")
            in kwargs["hidden_read_scope"].hidden_directories
        )
        assert len(kwargs["invocation_gate"].children) == project_count
        assert "Earlier conversation" not in prompt
        for index, project in enumerate(scope.projects):
            workspace = Path(project.workspace_root)
            (workspace / "patch.json").write_text(
                agent_patch_json(seed_patch()) if index == 0 else "invalid patch"
            )
        (stage / "patch.json").write_text("stray")
        (stage / "watch.json").write_text("stray")
        artifact_directory = stage / "turns" / kwargs["operation_id"] / "artifacts"
        assert artifact_directory.is_dir()
        (artifact_directory / "note.md").write_text("# Result\nReady.")
        (stage / "outcome.json").write_text('{"status":"done"}')
        yield AgentEvent(event="session", session_id="space-native")
        yield AgentEvent(event="answer", text="Finished the requested work.")
        yield AgentEvent(event="done")

    monkeypatch.setattr(app.state.launcher, "stream", provider)

    async def run():
        return [
            frame
            async for frame in stream_space_chat_run(
                owner, app.state.launcher, request, tmp_path / "data", execution
            )
        ]

    frames = asyncio.run(run())
    assert installed
    assert any('"event":"done"' in frame or "event: done" in frame for frame in frames)
    assert execution.project_results is not None
    assert execution.turn_outcome is not None
    assert execution.stage_root is not None
    assert any("artifact" in frame and "note.md" in frame for frame in frames)
    assert len(execution.project_results) == project_count
    if project_count:
        assert set(execution.project_results.values()) == {"applied", "rejected"}
        from rcp.core.transition_models import GraphTargetRef

        for project_id in execution.project_results:
            assert (
                store.graph_seen_head(
                    project_id, thread.thread_id, "space-native", GraphTargetRef()
                )
                is not None
            )
        assert execution.turn_outcome.status == "partial"
        assert (
            json.loads((Path(execution.stage_root) / "outcome.json").read_text())["status"]
            == "partial"
        )
    else:
        assert execution.write_scope_fingerprint is None
        assert execution.turn_outcome.status == "done"
    transcript = read_space_chat_transcript(tmp_path / "data", human.user_id, thread.thread_id)
    assert transcript is not None
    assert [message.role for message in transcript.messages] == ["user", "assistant"]
    assert transcript.messages[-1].text == "Finished the requested work."
    assert store.chat_session_context("claude", "local", "space-native").kind == "space_chat"

    if project_count == 0:
        from rcp.service import RunRequest

        store.checkpoint_agent_task(task.operation_id, native_session_id="space-native")
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='succeeded' WHERE operation_id=?",
                (task.operation_id,),
            )
        second = store.create_agent_task(
            task.model_copy(
                update={
                    "operation_id": str(uuid.uuid4()),
                    "kind": "space_chat",
                    "status": "queued",
                    "created_at": store.now(),
                    "updated_at": store.now(),
                    "finished_at": None,
                    "request": request.model_copy(update={"message": "Continue."}).model_dump(
                        mode="json"
                    ),
                }
            )
        )
        store.record_agent_task_receipt(
            second.operation_id,
            "operation_created",
            {
                "kind": "space_chat",
                "attempt": second.attempt,
                "has_parent": False,
                "resumed": False,
            },
        )
        second_request = RunRequest.model_validate(second.request)
        assert second_request.session_id == "space-native"
        next_execution = AgentTaskExecution(second.operation_id, store, AgentProcessControl())

        async def continue_run():
            return [
                frame
                async for frame in stream_space_chat_run(
                    owner, app.state.launcher, second_request, tmp_path / "data", next_execution
                )
            ]

        asyncio.run(continue_run())
        assert calls == [None, "space-native"]
        continued = read_space_chat_transcript(tmp_path / "data", human.user_id, thread.thread_id)
        assert continued is not None
        assert len(continued.messages) == 4
