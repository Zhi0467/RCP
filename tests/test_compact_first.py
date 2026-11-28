"""Compact first: each runtime compacts the resumed session, then writes the held prompt.

The wire shapes come from real provider runs (Claude Code 2.1.330, Antigravity
1.5.18, codex-cli 0.171.0). The shipped fence must agree with the decoder on
where the turn ends, or a remote host would close the provider after compaction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcp.providers import ProviderTurnRequest, profile_for, runtime_compacts_first
from rcp.providers.turn_fence import COMPACT_COMMAND, turn_fence
from rcp.service import RunRequest

CODEX = "codex.app-server-stdio.v1"


def _turn(tmp_path: Path, provider: str, *, runtime_id: str | None = None):
    profile = profile_for(provider)
    request = ProviderTurnRequest(
        prompt="What word did I ask you to remember?",
        binary=provider,
        cwd=tmp_path,
        model=None,
        reasoning=None,
        session_id="resumed-session",
        read_dirs=[],
        write_dirs=[],
        write_scope=None,
        capability="discuss",
        provider_version={"codex": "0.171.0", "agy": "1.5.18"}.get(provider, "2.1.330"),
        compact_first=True,
    )
    return profile.runtime(runtime_id or profile.legacy_runtime_id).turn(request)


def _status(step):
    return [event.status for event in step.events if event.status is not None]


def _claude_result(message_id: str, **fields) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "user_message_uuid": message_id,
        "user_message_uuids": [message_id],
        **fields,
    }


def test_claude_writes_the_prompt_only_after_its_compaction_result(tmp_path: Path) -> None:
    turn = _turn(tmp_path, "claude")
    fence = turn_fence("claude.stream-json.v1")
    compact = json.loads(turn.initial_input())
    fence.input(compact)
    assert compact["message"]["content"] == COMPACT_COMMAND
    assert not turn.steering_state().can_steer

    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "compact_metadata": {"trigger": "manual", "pre_tokens": 8049, "post_tokens": 2931},
    }
    (status,) = _status(turn.receive_line(json.dumps(boundary)))
    assert status.compaction is not None and status.compaction.trigger == "manual"

    result = _claude_result(compact["uuid"], local_command="compact")
    step = turn.receive_line(json.dumps(result))
    fence.output(result)
    assert not step.complete and not fence.terminal
    (prompt,) = [json.loads(item) for item in step.outgoing]
    assert prompt["message"]["content"] == "What word did I ask you to remember?"

    fence.input(prompt)
    answer = _claude_result(prompt["uuid"], result="pelican", num_turns=1)
    final = turn.receive_line(json.dumps(answer))
    fence.output(answer)
    assert final.complete and fence.terminal
    assert [event.text for event in final.events if event.event == "answer"] == ["pelican"]
    assert fence.compact_first


def test_claude_compaction_failure_never_writes_the_prompt(tmp_path: Path) -> None:
    turn = _turn(tmp_path, "claude")
    compact = json.loads(turn.initial_input())
    failed = _claude_result(
        compact["uuid"], subtype="error_during_execution", is_error=True, result="overloaded"
    )

    step = turn.receive_line(json.dumps(failed))

    assert step.complete and not step.outgoing
    (error,) = [event for event in step.events if event.event == "error"]
    assert error.text.startswith("Compaction failed:")
    assert error.text.endswith("Your message was not sent.")


def _agy_result(response: str = "", status: str = "SUCCESS") -> dict:
    return {"event": "result", "result": {"status": status, "response": response}}


def test_agy_reports_the_compaction_and_answers_only_the_prompt(tmp_path: Path) -> None:
    turn = _turn(tmp_path, "agy")
    fence = turn_fence(profile_for("agy").legacy_runtime_id)
    compact = json.loads(turn.initial_input())
    fence.input(compact)
    assert compact["message"]["content"] == COMPACT_COMMAND
    turn.receive_line(json.dumps({"event": "init", "conversation_id": "resumed-session"}))
    assert not turn.steering_state().can_steer

    step = turn.receive_line(json.dumps(_agy_result()))
    fence.output(_agy_result())
    assert not step.complete and not fence.terminal
    (status,) = _status(step)
    assert status.compaction is not None and status.compaction.trigger == "manual"
    (prompt,) = [json.loads(item) for item in step.outgoing]
    assert prompt["message"]["content"] == "What word did I ask you to remember?"
    assert turn.steering_state().can_steer

    fence.input(prompt)
    final = turn.receive_line(json.dumps(_agy_result("pelican\n")))
    fence.output(_agy_result("pelican\n"))
    assert final.complete and fence.terminal
    assert [event.text for event in final.events if event.event == "answer"] == ["pelican\n"]


def test_agy_compaction_failure_never_writes_the_prompt(tmp_path: Path) -> None:
    turn = _turn(tmp_path, "agy")
    turn.initial_input()

    step = turn.receive_line(json.dumps(_agy_result(status="ERROR")))

    assert step.complete and not step.outgoing
    assert step.events[0].text.startswith("Compaction failed:")


def _codex_resumed(turn, fence):
    for request_id, method, result in (
        (1, "initialize", {}),
        (2, "config/read", {"config": {}}),
        (
            3,
            "thread/resume",
            {
                "thread": {"id": "resumed-session"},
                "approvalPolicy": "never",
                "sandbox": {"type": "dangerFullAccess"},
            },
        ),
    ):
        fence.input({"id": request_id, "method": method})
        step = turn.receive_line(json.dumps({"id": request_id, "result": result}))
        fence.output({"id": request_id, "result": result})
    return step


def _codex_completed(turn_id: str, status: str = "completed") -> dict:
    turn = {"id": turn_id, "status": status}
    return {"method": "turn/completed", "params": {"threadId": "resumed-session", "turn": turn}}


def test_codex_compacts_as_its_own_turn_then_starts_the_prompt(tmp_path: Path) -> None:
    turn = _turn(tmp_path, "codex", runtime_id=CODEX)
    fence = turn_fence(CODEX)
    turn.initial_input()
    step = _codex_resumed(turn, fence)
    (compact,) = [json.loads(item) for item in step.outgoing]
    assert compact["method"] == "thread/compact/start"
    assert step.delivers_prompt
    fence.input(compact)
    assert fence.prompt_started

    turn.receive_line(json.dumps({"id": compact["id"], "result": {}}))
    item = {"type": "contextCompaction", "id": "compaction"}
    params = {"threadId": "resumed-session", "turnId": "compaction-turn", "item": item}
    done = turn.receive_line(json.dumps({"method": "item/completed", "params": params}))
    (status,) = _status(done)
    assert status.compaction is not None and status.compaction.trigger == "manual"

    step = turn.receive_line(json.dumps(_codex_completed("compaction-turn")))
    fence.output(_codex_completed("compaction-turn"))
    assert not step.complete and not fence.terminal
    (start,) = [json.loads(item) for item in step.outgoing]
    assert start["method"] == "turn/start"
    assert not step.delivers_prompt

    fence.input(start)
    started = {"id": start["id"], "result": {"turn": {"id": "prompt-turn"}}}
    turn.receive_line(json.dumps(started))
    fence.output(started)
    final = turn.receive_line(json.dumps(_codex_completed("prompt-turn")))
    fence.output(_codex_completed("prompt-turn"))
    assert final.complete and fence.terminal


@pytest.mark.parametrize(
    "failure",
    [
        {"id": 5, "error": {"message": "thread busy"}},
        _codex_completed("compaction-turn", status="failed"),
    ],
)
def test_codex_compaction_failure_never_starts_the_prompt(tmp_path: Path, failure) -> None:
    turn = _turn(tmp_path, "codex", runtime_id=CODEX)
    turn.initial_input()
    _codex_resumed(turn, turn_fence(CODEX))

    step = turn.receive_line(json.dumps(failure))

    assert step.complete and not step.outgoing
    assert step.events[0].text.startswith("Compaction failed:")


@pytest.mark.parametrize(
    ("provider", "runtime_id", "offered"),
    [
        ("codex", "codex.exec-json.v1", False),
        ("codex", CODEX, True),
        ("claude", "claude.stream-json.v1", True),
        ("opencode", "opencode.run-json.v1", False),
    ],
)
def test_only_a_runtime_that_can_compact_offers_it(provider, runtime_id, offered) -> None:
    assert runtime_compacts_first(provider, runtime_id) is offered


def test_an_unchosen_compact_first_leaves_the_stored_request_unchanged() -> None:
    assert "compact_first" not in RunRequest(message="hi").model_dump(mode="json")
    assert RunRequest(message="hi", compact_first=True).model_dump(mode="json")["compact_first"]


def test_only_a_turns_first_attempt_asks_its_launch_to_compact(manifest, tmp_path, monkeypatch):
    import asyncio
    import uuid

    from rcp.agents import AgentEvent
    from rcp.agents.launcher import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.core.models import HiddenReadScope, HiddenReadStatus
    from rcp.machine_cards import BACKEND_MACHINE_ID
    from rcp.runs.tasks.work import stream_work_run
    from tests.helpers import create_named_app, store_test_claude_token, write_turn_outcome
    from tests.test_dispatch_authority import _authorizer, _work_authority, _work_request
    from tests.test_storage import _task_record

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    human = _authorizer(store)
    thread = next(
        item
        for item in store.ensure_agent_threads(
            human.user_id, list(store.member_project_ids(human.user_id)), default_names=("Pip",)
        )
        if item.kind == "project"
    )
    store_test_claude_token(store)
    policy = HiddenReadScope(
        machine_id=BACKEND_MACHINE_ID,
        os_account="test",
        hidden_directories=(str(tmp_path / "other-member"),),
        enforcement=HiddenReadStatus(status="enforced"),
    )

    async def prepare_policy(*args, **kwargs):
        return policy

    monkeypatch.setattr("rcp.runs.shared._prepare_hidden_read_scope", prepare_policy)
    monkeypatch.setattr("rcp.runs.tasks.work._prepare_hidden_read_scope", prepare_policy)
    asked: list[bool] = []

    async def provider(provider, prompt, **kwargs):
        asked.append(kwargs["compact_first"])
        write_turn_outcome(kwargs["cwd"])
        yield AgentEvent(event="answer", text="done")
        yield AgentEvent(event="done")

    monkeypatch.setattr(app.state.launcher, "stream", provider)
    request = _work_request().model_copy(
        update={
            "chat_id": thread.thread_id,
            "provider": "claude",
            "browser_requested": False,
            "run_on": BACKEND_MACHINE_ID,
            "compact_first": True,
        }
    )

    def run(parent: str | None) -> str:
        task = store.create_agent_task(
            _task_record(
                store.now(),
                operation_id=str(uuid.uuid4()),
                kind="project_chat",
                status="queued",
                project_id=thread.project_id or store.space_id,
                request=request.model_dump(mode="json"),
                authorized_by=human,
                dispatch_authority=_work_authority(request),
                parent_operation_id=parent,
            )
        )
        execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
        stream = stream_work_run(
            app.state.service, app.state.launcher, request, tmp_path / "data", execution=execution
        )

        async def drain() -> None:
            frames = [frame async for frame in stream]
            assert not any('"event":"error"' in frame for frame in frames), frames

        asyncio.run(drain())
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='succeeded' WHERE operation_id=?",
                (task.operation_id,),
            )
        return task.operation_id

    run(run(None))

    assert asked == [True, False]
