from __future__ import annotations

import asyncio
from uuid import uuid4

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.core.models import AuthorizedHuman
from rcp.providers.base import AgentCompaction, AgentStatusDelta, AgentSubagent
from rcp.runs.shared import _sse
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from tests.helpers import create_named_app, signed_in_client


def test_live_status_folds_into_the_task_and_ends_with_the_last_observation(
    manifest, tmp_path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    tasks = app.state.background_tasks
    store = tasks.store
    project_id = app.state.default_project_id
    request = RunRequest(chat_scope="project", chat_id="chat", message="Go", provider="codex")
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="turn",
            project_id=project_id,
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
    assert store.agent_task("turn").agent_status is None

    def frame(**delta: object) -> str:
        return _sse(AgentEvent(event="raw", status=AgentStatusDelta.model_validate(delta)))

    def stored():
        return store.agent_task("turn").agent_status

    async def frames():
        yield frame(context_tokens=1_000, context_window=200_000)
        yield frame(context_tokens=5_000)
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
