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
from tests.test_dispatch_authority import _authorizer, _work_authority, _work_request
from tests.test_storage import _task_record


@pytest.mark.parametrize(
    "project_count, broken",
    [
        (0, None),
        (2, None),
        (2, "unavailable"),
        (2, "missing"),
        (2, "remote"),
        (2, "collection"),
        (2, "directory"),
    ],
)
def test_space_runtime_real_storage_and_partial_apply(
    manifest, tmp_path, monkeypatch, project_count, broken
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
    if broken in {"unavailable", "missing", "remote"}:
        from rcp.transport import StateUnavailable

        unavailable_id = sorted(store.member_project_ids(human.user_id))[0]
        original_open = app.state.catalog.open_execution

        def open_project(project_id):
            if project_id == unavailable_id:
                if broken == "remote":
                    pytest.fail("remote project was opened")
                raise (
                    StateUnavailable("offline") if broken == "unavailable" else KeyError(project_id)
                )
            return original_open(project_id)

        monkeypatch.setattr(app.state.catalog, "open_execution", open_project)
        if broken == "remote":
            original_local = app.state.catalog.same_machine
            monkeypatch.setattr(
                app.state.catalog,
                "same_machine",
                lambda pid: pid != unavailable_id and original_local(pid),
            )
        project_count -= 1
    threads = store.ensure_agent_threads(
        human.user_id, list(store.member_project_ids(human.user_id)), default_names=("Pip",)
    )
    thread = next(item for item in threads if item.kind == "space")
    request = _work_request().model_copy(
        update={"chat_id": thread.thread_id, "provider": "claude", "run_truth_scope": []}
    )
    task = store.create_agent_task(
        _task_record(
            store.now(),
            kind="space_chat",
            status="failed",
            operation_id=str(uuid.uuid4()),
            project_id=store.space_id,
            request=request.model_dump(mode="json"),
            authorized_by=human,
            dispatch_authority=_work_authority(request),
        )
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
        assert json.loads((stage / "workspace" / "agent-name.json").read_text()) == {
            "name": "Pip",
            "can_self_name": False,
        }
        scope = kwargs["write_scope"]
        assert len(scope.projects) == project_count
        assert stage.parent == tmp_path / "data" / "agent-threads" / human.user_id / "stages"
        assert not any(
            stage.is_relative_to(root) for root in kwargs["hidden_read_scope"].hidden_directories
        )
        assert len(kwargs["invocation_gate"].children) == project_count
        assert "Earlier conversation" not in prompt
        for index, project in enumerate(scope.projects):
            workspace = Path(project.workspace_root)
            if index == 0 and broken == "directory":
                (workspace / "patch.json").mkdir()
                continue
            if index == 0 and broken == "collection":
                from rcp.transport import StateUnavailable

                def unavailable(*args, **kwargs):
                    raise StateUnavailable("state became unavailable")

                monkeypatch.setattr(
                    app.state.catalog.open_execution(project.project_id).history,
                    "append",
                    unavailable,
                )
            (workspace / "patch.json").write_text(
                agent_patch_json(seed_patch())
                if index == 0 or broken in {"collection", "directory"}
                else "invalid patch"
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
        assert set(execution.project_results.values()) == (
            {"applied", "rejected"} if project_count == 2 else {"applied"}
        )
        from rcp.core.transition_models import GraphTargetRef

        for project_id in execution.project_results:
            assert (
                store.graph_seen_head(
                    project_id, thread.thread_id, "space-native", GraphTargetRef()
                )
                is not None
            )
        assert execution.turn_outcome.status == ("partial" if project_count == 2 else "done")
        assert (
            json.loads((Path(execution.stage_root) / "outcome.json").read_text())["status"]
            == execution.turn_outcome.status
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


def test_space_output_clear_removes_agent_directories_without_following_links(tmp_path):
    from rcp.runs.space_projects import clear_space_output

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept").write_text("x")
    stage = tmp_path / "stage"
    made = stage / "projects/one/patch.json"
    made.mkdir(parents=True)
    (made / "inner").write_text("x")
    linked = stage / "projects/two/patch.json"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    for path in (made, linked, stage / "outcome.json"):
        clear_space_output(path)
    # A malformed output never survives into the next fresh turn.
    assert not made.exists() and not linked.is_symlink()
    assert (outside / "kept").exists()
