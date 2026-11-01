from __future__ import annotations

import asyncio

import pytest

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.background import AgentTaskExecution, BackgroundAgentTasks
from rcp.providers import ProviderUsage
from rcp.runs.shared import _sse as encode_sse
from rcp.service import RunRequest
from tests.test_background import _store
from tests.test_storage import _task_record


@pytest.mark.parametrize("first_recorded", [False, True])
def test_background_usage_replay_keeps_one_receipt(tmp_path, first_recorded):
    store = _store(tmp_path)
    request = RunRequest(provider="codex", model="test-model")
    task = store.create_agent_task(
        _task_record(
            store.now(), operation_id="usage-task", request=request.model_dump(mode="json")
        )
    )
    usage = ProviderUsage(
        provider_profile="codex",
        provider_event_type="turn.completed",
        dedupe_key="turn",
        processed_input_tokens=12,
        generated_tokens=7,
    )

    async def stream(*_args):
        yield encode_sse(AgentEvent(event="raw", usage=usage))
        yield encode_sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())

    async def consume():
        for recorded in (first_recorded, True):
            await background._consume(
                task.project_id, task.kind, request, execution, recorded=recorded
            )

    asyncio.run(consume())
    records = store.agent_usage(task.project_id)
    receipts = [
        r for r in store.agent_task_receipts(task.operation_id) if r.category == "provider_usage"
    ]
    assert len(records) == len(receipts) == 1
    assert records[0].provider == "codex" and records[0].model == "test-model"
    assert records[0].processed_input_tokens == 12 and records[0].generated_tokens == 7
    assert receipts[0].payload["usage_id"] == records[0].usage_id
    assert receipts[0].payload["counted"] is True
