from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.background import AgentTaskExecution, TaskFailed
from rcp.core.models import AuthorizedHuman
from rcp.limits import AGENT_STATUS_WRITE_SECONDS
from rcp.providers.base import AgentCompaction, AgentStatusDelta, AgentSubagent
from rcp.providers.live_status import AgentStatus
from rcp.runs.shared import _sse
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from tests.helpers import create_named_app, signed_in_client


def _running_turn(manifest, tmp_path, operation_id: str = "turn"):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    request = RunRequest(chat_scope="project", chat_id="chat", message="Go", provider="codex")
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=app.state.default_project_id,
            kind="project_chat",
            status="running",
            request=request.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Running",
            authorized_by=AuthorizedHuman(
                space_id=str(uuid4()), user_id=str(uuid4()), display_name="Human"
            ),
            dispatch_authority=resolve_dispatch_authority("project_chat", request),
        )
    )
    return app, request


def test_live_status_folds_into_the_task_and_ends_with_the_last_observation(
    manifest, tmp_path
) -> None:
    app, request = _running_turn(manifest, tmp_path)
    tasks = app.state.background_tasks
    store = tasks.store
    project_id = app.state.default_project_id
    assert store.agent_task("turn").agent_status is None

    def task_generation() -> int:
        with store.connection() as connection:
            row = connection.execute(
                "SELECT generation FROM resource_versions WHERE resource='task' AND entity='turn'"
            ).fetchone()
        return row[0] if row else 0

    before = task_generation()
    store.update_agent_task_status("turn", AgentStatus(context_tokens=1))
    # A status-only write refreshes the chat that polls this task.
    assert task_generation() > before

    def frame(**delta: object) -> str:
        return _sse(AgentEvent(event="raw", status=AgentStatusDelta.model_validate(delta)))

    def stored():
        return store.agent_task("turn").agent_status

    async def frames():
        yield frame(context_tokens=1_000, context_window=200_000)
        yield frame(context_tokens=5_000)
        # A held update is written once the throttle lapses, with no further frame.
        await asyncio.sleep(AGENT_STATUS_WRITE_SECONDS + 0.3)
        assert stored().context_tokens == 5_000
        yield frame(compaction=AgentCompaction(trigger="auto", after_tokens=800))
        # A compaction is written at once.
        assert [item.before_tokens for item in stored().compactions] == [5_000]
        yield frame(subagent=AgentSubagent(id="sub", kind="explore"))
        yield frame(subagent=AgentSubagent(id="sub", state="completed", tokens=42))
        assert [(item.id, item.state) for item in stored().subagents] == [("sub", "completed")]
        yield frame(context_tokens=900)

    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    asyncio.run(tasks._consume(project_id, "project_chat", request, execution, source=frames()))

    status = stored()
    assert status.context_tokens == 900
    assert status.context_window == 200_000
    assert [(item.after_tokens, item.trigger) for item in status.compactions] == [(800, "auto")]
    assert [(item.kind, item.tokens) for item in status.subagents] == [("explore", 42)]
    payload = signed_in_client(app).get(f"/api/projects/{project_id}/tasks/turn").json()
    assert payload["agent_status"]["context_tokens"] == 900


def test_a_compaction_without_its_size_takes_the_next_context() -> None:
    from rcp.providers.live_status import fold_agent_status

    status = fold_agent_status(None, AgentStatusDelta(context_tokens=42_000))
    status = fold_agent_status(status, AgentStatusDelta(compaction=AgentCompaction()))
    status = fold_agent_status(status, AgentStatusDelta(context_tokens=25_000))
    status = fold_agent_status(status, AgentStatusDelta(context_tokens=30_000))
    assert [(c.before_tokens, c.after_tokens) for c in status.compactions] == [(42_000, 25_000)]


@pytest.mark.parametrize("fails", [False, True], ids=["ended", "failed"])
def test_an_ended_turn_leaves_nothing_running(manifest, tmp_path, fails: bool) -> None:
    app, request = _running_turn(manifest, tmp_path)
    tasks = app.state.background_tasks

    async def frames():
        live = AgentStatusDelta(compacting=True, subagent=AgentSubagent(id="sub"))
        yield _sse(AgentEvent(event="raw", status=live))
        if fails:
            yield _sse(AgentEvent(event="error", text="stopped"))

    execution = AgentTaskExecution(
        operation_id="turn", store=tasks.store, control=AgentProcessControl()
    )
    run = tasks._consume(
        app.state.default_project_id, "project_chat", request, execution, source=frames()
    )
    if fails:
        with pytest.raises(TaskFailed):
            asyncio.run(run)
    else:
        asyncio.run(run)
    status = tasks.store.agent_task("turn").agent_status
    assert not status.compacting
    assert [item.state for item in status.subagents] == ["failed" if fails else "completed"]
