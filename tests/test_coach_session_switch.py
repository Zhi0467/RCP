from __future__ import annotations

from contextlib import aclosing
from typing import Literal, cast

import pytest

from rcp.agents import AgentEvent, AgentLauncher
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.runs.tasks.coach import _resolved_coach_request, stream_coach
from rcp.service import CoachRequest

from .helpers import store_test_claude_token
from .test_coach_suggestions import coach as coach
from .test_coach_suggestions import execution_for


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider, model, reasoning, expected_session",
    [
        ("claude", "claude-model", "high", None),
        ("claude", None, None, None),
    ],
)
async def test_coach_profile_changes_follow_chat_session_rules(
    coach, provider, model, reasoning, expected_session
):
    service, _live, data = coach
    store_test_claude_token(service.paper.store, BACKEND_MACHINE_ID)
    launches = []

    class Launcher:
        async def stream(self, provider, _prompt, **kwargs):
            launches.append((provider, kwargs))
            yield AgentEvent(
                event="session",
                session_id=kwargs["session_id"]
                or ("original-session" if len(launches) == 1 else "new-provider-session"),
            )
            yield AgentEvent(event="done")

    async def turn(request):
        execution = execution_for(service)
        events = [
            AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
            async for frame in stream_coach(
                service, cast(AgentLauncher, Launcher()), service.paper, request, data, execution
            )
        ]
        assert not [event for event in events if event.event == "error"]
        session_id = next(event.session_id for event in events if event.event == "session")
        execution.store.checkpoint_agent_task(execution.operation_id, native_session_id=session_id)
        execution.store.complete_agent_task(
            execution.operation_id, applied_revision=None, result={}
        )

    await turn(
        CoachRequest(message="Review", provider="codex", model="original-model", reasoning="low")
    )
    await turn(
        CoachRequest(
            message="Continue",
            session_id="original-session",
            provider=provider,
            model=model,
            reasoning=reasoning,
        )
    )
    expected = service.resolve_agent_profile(
        "paper_coach", provider=provider, model=model, reasoning=reasoning
    )
    actual_provider, launch = launches[-1]
    assert actual_provider == provider
    assert launch["session_id"] == expected_session
    assert launch["model"] == expected.model
    assert launch["reasoning"] == expected.reasoning
    sessions = {session.native_session_id: session for session in service.paper.sessions()}
    current = sessions[expected_session or "new-provider-session"]
    assert (current.provider, current.model, current.reasoning) == (
        provider,
        expected.model or "provider-default",
        expected.reasoning,
    )
    assert "original-session" in sessions

    await turn(CoachRequest(message="Continue again", session_id=current.native_session_id))
    third_provider, third_launch = launches[-1]
    assert third_provider == provider
    assert third_launch["session_id"] == current.native_session_id
    assert third_launch["model"] == expected.model
    assert third_launch["reasoning"] == expected.reasoning

    off_base = current.model_copy(
        update={"native_session_id": "off-base-session", "machine_id": "previous-base"}
    )
    service.paper.record_session(off_base)
    admitted = _resolved_coach_request(
        service, CoachRequest(message="Continue", session_id=off_base.native_session_id)
    )
    assert (admitted.provider, admitted.model, admitted.reasoning) == (
        current.provider,
        expected.model or None,
        current.reasoning,
    )
    assert admitted.session_id is None
    assert admitted.previous_session_id == off_base.native_session_id
    assert admitted.run_on == service.manifest.base_machine


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_event", ["error", "paused"])
async def test_coach_records_session_when_consumer_closes_on_failure(
    coach, stop_event: Literal["error", "paused"]
):
    from rcp.providers import configured_runtime_id, legacy_runtime_id

    service, _live, data = coach
    execution = execution_for(service)
    execution.runtime_id = configured_runtime_id("codex", "app-server")
    runtime_id = legacy_runtime_id("codex")

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            yield AgentEvent(event="runtime", text=runtime_id)
            yield AgentEvent(event="session", session_id="interrupted-session")
            yield AgentEvent(event=stop_event, text="Interrupted")

    async with aclosing(
        stream_coach(
            service,
            cast(AgentLauncher, Launcher()),
            service.paper,
            CoachRequest(message="Review", provider="codex"),
            data,
            execution,
        )
    ) as stream:
        async for frame in stream:
            event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
            if event.event in {"error", "paused"}:
                assert event.event == stop_event
                break
        else:
            pytest.fail("The provider did not emit its interruption")
    assert [
        (session.native_session_id, session.runtime_id) for session in service.paper.sessions()
    ] == [("interrupted-session", runtime_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_master", [False, True])
async def test_coach_invocation_is_per_turn_and_outside_retained_master(
    coach, legacy_master, monkeypatch
):
    import json
    from pathlib import Path

    from rcp.agents import PromptFactory
    from rcp.agents.continuation_prompt import SECTIONS
    from rcp.agents.prompts import _invoked_package_section
    from rcp.skill_registry import SkillDefaults
    from rcp.skills.staging import skill_bundle_label

    service, _live, data = coach
    service.manifest.agent.skill_defaults = SkillDefaults(skill_ids=["graph-audit"])
    expected_bundle = skill_bundle_label(service.resolve_skill_selection(CoachRequest(message="")))
    launches = []
    executions = []
    original_contract = PromptFactory.paper_coach_task_contract

    def legacy_contract(**kwargs):
        return original_contract(**kwargs) + _invoked_package_section(kwargs["skill_pointers"])

    class Launcher:
        async def stream(self, _provider, prompt, **kwargs):
            launches.append(prompt)
            yield AgentEvent(event="session", session_id="coach-invocation-session")
            yield AgentEvent(event="done")

    for request in (
        CoachRequest(message="Review", invoked_skill_ids=["graph-audit"]),
        CoachRequest(message="Continue", session_id="coach-invocation-session"),
    ):
        execution = execution_for(service)
        executions.append(execution)
        with monkeypatch.context() as patch:
            if legacy_master and len(executions) == 1:
                patch.setattr(PromptFactory, "PAPER_COACH_POLICY_VERSION", "paper-coach-v10")
                patch.setattr(
                    PromptFactory, "paper_coach_task_contract", staticmethod(legacy_contract)
                )
            events = [
                AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                async for frame in stream_coach(
                    service,
                    cast(AgentLauncher, Launcher()),
                    service.paper,
                    request,
                    data,
                    execution,
                )
            ]
        assert not [event for event in events if event.event == "error"]
        execution.store.checkpoint_agent_task(
            execution.operation_id, native_session_id="coach-invocation-session"
        )
        execution.store.complete_agent_task(
            execution.operation_id, applied_revision=None, result={}
        )

    first = executions[0]
    master = first.store.agent_task_contract(first.operation_id, "session_master")
    assert master is not None
    values = json.loads(
        first.store.agent_task_contract(first.operation_id, "session_master_values")
    )
    assert len(values["skills"]) == 1
    invocation = _invoked_package_section(values["skills"]).strip()
    assert (invocation in master) is legacy_master
    initial_contract = Path(launches[0].splitlines()[1]).read_text()
    assert invocation in initial_contract or invocation in launches[0]
    assert invocation not in launches[1]
    assert values["skills"][0]["description"]
    assert Path(values["skills"][0]["path"]).parent.parent.name == expected_bundle
    if legacy_master:
        second = executions[1]
        current = second.store.agent_task_contract(second.operation_id, "session_master")
        assert current is not None
        second_values = json.loads(
            second.store.agent_task_contract(second.operation_id, "session_master_values")
        )
        assert _invoked_package_section(second_values["skills"]).strip() not in current
        assert SECTIONS["master_rebootstrap"].split("{path}")[0] in launches[1]
        assert first.store.agent_task_contract(first.operation_id, "session_master") == master


@pytest.mark.parametrize("master_opens_now", [False, True])
@pytest.mark.parametrize("retry", [False, True])
def test_coach_recovery_keeps_current_invocation_outside_master(master_opens_now, retry):
    from rcp.agents.prompts import _invoked_package_section
    from rcp.runs.tasks.coach import _coach_continuation_parts

    pointers = [{"kind": "skill", "id": "graph-audit", "path": "/inputs/graph-audit/SKILL.md"}]
    parts = _coach_continuation_parts(
        "recovery",
        refresh=False,
        master_opens_now=master_opens_now,
        human_request_path="/inputs/request.txt",
        retry_diagnostics_path="/inputs/retry.json" if retry else None,
        invoked_skill_pointers=pointers,
    )
    assert _invoked_package_section(pointers).strip() in parts
