from __future__ import annotations

from contextlib import aclosing
from typing import Literal, cast

import pytest

from rcp.agents import AgentEvent, AgentLauncher
from rcp.runs.tasks.coach import _resolved_coach_request, stream_coach
from rcp.service import CoachRequest

from .test_coach_suggestions import coach as coach
from .test_coach_suggestions import execution_for


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider, model, reasoning, expected_session",
    [
        ("codex", "new-model", "high", "original-session"),
        ("claude", "claude-model", "high", None),
        ("claude", None, None, None),
    ],
)
async def test_coach_profile_changes_follow_chat_session_rules(
    coach, provider, model, reasoning, expected_session
):
    service, _live, data = coach
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
    assert len(sessions) == (1 if expected_session else 2)

    await turn(CoachRequest(message="Continue again", session_id=current.native_session_id))
    third_provider, third_launch = launches[-1]
    assert third_provider == provider
    assert third_launch["session_id"] == current.native_session_id
    assert third_launch["model"] == expected.model
    assert third_launch["reasoning"] == expected.reasoning

    off_base = current.model_copy(
        update={"native_session_id": "off-base-session", "execution_machine": "previous-base"}
    )
    service.paper.record_session(off_base)
    admitted = _resolved_coach_request(
        service,
        CoachRequest(
            message="Continue",
            session_id=off_base.native_session_id,
            run_on=off_base.execution_machine,
        ),
    )
    assert (admitted.provider, admitted.model, admitted.reasoning) == (
        current.provider,
        expected.model or None,
        current.reasoning,
    )
    assert admitted.session_id is None
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
