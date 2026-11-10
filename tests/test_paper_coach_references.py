from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from rcp.agents import AgentEvent, AgentLauncher, PromptFactory
from rcp.agents.continuation_prompt import master_key
from rcp.paper import PaperOp
from rcp.runs.tasks.coach import stream_coach
from rcp.service import CoachRequest
from tests.paper_helpers import apply, coach_execution
from tests.test_coach_suggestions import coach as coach


@pytest.mark.asyncio
async def test_reference_coaching_stages_bibliography_with_updated_policy(coach):
    assert int(PromptFactory.PAPER_COACH_POLICY_VERSION.removeprefix("paper-coach-v")) > 12
    service, live, data = coach
    bibliography = "@book{knuth1984, title={The TeXbook}, year={1984}}\n"
    apply(
        live,
        [PaperOp(kind="create", path="references.bib", text=bibliography)],
        service.paper.project_id,
    )
    execution = coach_execution(service)

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            staged = execution.store.agent_task_contract(
                execution.operation_id, "session_master_values"
            )
            assert staged is not None
            values = json.loads(staged)
            assert execution.store.agent_task_contract(
                execution.operation_id, "session_master_key"
            ) == master_key(PromptFactory.PAPER_COACH_POLICY_VERSION, ontology_extensions=False)
            assert (Path(values["paper"]) / "references.bib").read_text() == bibliography
            assert Path(values["changes"]).is_dir()
            assert Path(values["graph"]).is_file()
            assert Path(values["research_rendering"]).is_file()
            yield AgentEvent(event="session", session_id="references-session")
            yield AgentEvent(event="done")

    events = [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_coach(
            service,
            cast(AgentLauncher, Launcher()),
            service.paper,
            CoachRequest(message="Check the references", provider="codex"),
            data,
            execution,
        )
    ]
    assert not [event for event in events if event.event == "error"]
    assert any(event.event == "done" for event in events)
