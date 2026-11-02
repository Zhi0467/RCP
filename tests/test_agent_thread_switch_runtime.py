from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.core.models import HiddenReadScope, HiddenReadStatus
from rcp.runs.space_chat import SpaceThreadOwner, stream_space_chat_run
from rcp.runs.tasks.work import stream_work_run
from rcp.service import RunRequest
from tests.helpers import create_named_app, store_test_claude_token, write_turn_outcome
from tests.test_dispatch_authority import _authorizer, _work_authority, _work_request
from tests.test_storage import _task_record


@pytest.mark.parametrize("kind", ["project_chat", "space_chat"])
def test_provider_switch_seeds_actual_first_turn(manifest, tmp_path, monkeypatch, kind):
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    store = app.state.background_tasks.store
    human = _authorizer(store)
    thread = next(
        item
        for item in store.ensure_agent_threads(
            human.user_id, list(store.member_project_ids(human.user_id)), default_names=("Pip",)
        )
        if item.kind == ("space" if kind == "space_chat" else "project")
    )
    if kind == "space_chat":
        with store.connection() as connection:
            connection.execute("DELETE FROM project_members WHERE user_id=?", (human.user_id,))
    owner = SpaceThreadOwner(store, app.state.catalog, thread.thread_id)
    store_test_claude_token(store)
    policy = HiddenReadScope(
        execution_machine="local",
        execution_host="",
        os_account="test",
        hidden_directories=(str(tmp_path / "other-member"),),
        enforcement=HiddenReadStatus(status="enforced"),
    )

    async def prepare_policy(*args, **kwargs):
        return policy

    monkeypatch.setattr("rcp.runs.shared._prepare_hidden_read_scope", prepare_policy)
    monkeypatch.setattr("rcp.runs.tasks.work._prepare_hidden_read_scope", prepare_policy)
    monkeypatch.setattr("rcp.runs.space_chat.default_space_hidden_read_scope", lambda *a: policy)
    monkeypatch.setattr("rcp.runs.space_chat.install_space_hidden_read_scope", lambda scope: None)
    summary = "Retain the chosen experiment and continue the pending analysis."
    calls = []

    async def provider(provider, prompt, **kwargs):
        calls.append((provider, kwargs["capability"], prompt))
        if kwargs["capability"] == "session_summary":
            assert provider == "claude" and kwargs["session_id"] is None
            assert kwargs["fork_session_id"] == "old-native"
            assert kwargs["write_dirs"] == [] and kwargs["write_scope"] is None
            assert kwargs.get("invocation_gate") is None
            yield AgentEvent(event="session", session_id="summary-fork")
            yield AgentEvent(event="answer", text=summary)
        else:
            if provider == "codex":
                # The fresh session's history is a staged file the prompt points to.
                (seed_path,) = re.findall(r"`([^`]*thread-history-[0-9a-f]{16}\.md)`", prompt)
                assert summary in Path(seed_path).read_text() and summary not in prompt
                assert "earlier display answer" not in prompt
                assert kwargs["session_id"] is None
            write_turn_outcome(kwargs["cwd"])
            yield AgentEvent(
                event="session", session_id="old-native" if provider == "claude" else "new-native"
            )
            yield AgentEvent(event="answer", text="earlier display answer")
        yield AgentEvent(event="done")

    monkeypatch.setattr(app.state.launcher, "stream", provider)

    async def run(request):
        task = store.create_agent_task(
            _task_record(
                store.now(),
                operation_id=str(uuid.uuid4()),
                kind=kind,
                status="queued",
                project_id=thread.project_id or store.space_id,
                request=request.model_dump(mode="json"),
                authorized_by=human,
                dispatch_authority=_work_authority(request),
            )
        )
        store.record_agent_task_receipt(
            task.operation_id,
            "operation_created",
            {"kind": kind, "attempt": 1, "has_parent": False, "resumed": False},
        )
        execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
        admitted = RunRequest.model_validate(task.request)
        stream = (
            stream_space_chat_run(owner, app.state.launcher, admitted, data_dir, execution)
            if kind == "space_chat"
            else stream_work_run(
                app.state.service, app.state.launcher, admitted, data_dir, execution=execution
            )
        )
        frames = [frame async for frame in stream]
        assert not any('"event":"error"' in frame for frame in frames), frames
        assert execution.turn_outcome is not None
        store.checkpoint_agent_task(
            task.operation_id,
            native_session_id="old-native" if request.provider == "claude" else "new-native",
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='succeeded' WHERE operation_id=?",
                (task.operation_id,),
            )
        return execution

    request = _work_request().model_copy(
        update={
            "chat_id": thread.thread_id,
            "provider": "claude",
            "browser_requested": False,
            "run_on": "local" if kind == "space_chat" else "laptop",
        }
    )
    asyncio.run(run(request))
    execution = asyncio.run(run(request.model_copy(update={"provider": "codex"})))
    assert [(provider, capability) for provider, capability, _ in calls] == [
        ("claude", "work_auto"),
        ("claude", "session_summary"),
        ("codex", "work_auto"),
    ]
    receipt = store.agent_task_session_resolutions([execution.operation_id])[execution.operation_id]
    assert receipt.payload["reason_code"] == "provider_switch_compacted"
    assert execution.stage_root is not None
    # The summary lives in one staged history file in this thread's private stage.
    holders = [
        path
        for path in Path(execution.stage_root).rglob("*")
        if path.is_file() and summary.encode() in path.read_bytes()
    ]
    assert [path.name.startswith("thread-history-") for path in holders] == [True]
