from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher, AgentProcessControl
from rcp.agents.steering import LiveProviderSteering
from rcp.providers import ProviderTurnRequest, profile_for
from tests.helpers import async_wait_until


def _turn(tmp_path: Path, provider: str, *, ready: bool = True):
    profile = profile_for(provider)
    runtime_id = "codex.app-server-stdio.v1" if provider == "codex" else profile.legacy_runtime_id
    turn = profile.runtime(runtime_id).turn(
        ProviderTurnRequest(
            prompt="Original prompt",
            binary=provider,
            cwd=tmp_path,
            model=None,
            reasoning=None,
            session_id=None,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            capability="discuss",
            provider_version="0.153.2" if provider == "codex" else "2.1.260",
        )
    )
    if provider == "codex":
        for value in (
            {"id": 1, "result": {}},
            {"id": 2, "result": {"config": {}}},
            {
                "id": 3,
                "result": {
                    "thread": {"id": "thread"},
                    "approvalPolicy": "never",
                    "sandbox": {"type": "workspaceWrite"},
                },
            },
            {"id": 4, "result": {"turn": {"id": "turn"}}},
        ):
            turn.receive_line(json.dumps(value))
    elif ready:
        initial = json.loads(turn.initial_input())
        turn.receive_line(
            json.dumps(
                {"type": "command_lifecycle", "state": "started", "command_uuid": initial["uuid"]}
            )
        )
    return turn


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_steer_wire_is_bound_to_exact_turn_and_unique_message(tmp_path: Path, provider: str):
    turn = _turn(tmp_path, provider)
    active = turn.steering_state()
    assert active.can_steer
    with pytest.raises(ValueError, match="no longer active"):
        turn.render_steer("stale", "message", "Steer")
    wire = json.loads(turn.render_steer(active.turn_id, "message", "Steer"))
    if provider == "codex":
        assert wire["method"] == "turn/steer"
        assert wire["params"] == {
            "threadId": "thread",
            "expectedTurnId": "turn",
            "clientUserMessageId": "message",
            "input": [{"type": "text", "text": "Steer"}],
        }
    else:
        assert wire["uuid"] == "message"
        assert wire["message"] == {"role": "user", "content": "Steer"}
        assert "--replay-user-messages" in turn.command
    with pytest.raises(ValueError, match="already sent"):
        turn.render_steer(active.turn_id, "message", "Steer again")


def _lifecycle(turn, state, command_uuid):
    return turn.receive_line(
        json.dumps({"type": "command_lifecycle", "state": state, "command_uuid": command_uuid})
    )


def test_claude_ready_on_initial_started_not_replay(tmp_path: Path):
    turn = _turn(tmp_path, "claude", ready=False)
    initial = json.loads(turn.initial_input())
    assert not turn.steering_state().can_steer
    assert "acknowledge" in turn.steering_state().reason
    turn.receive_line(json.dumps({**initial, "isReplay": True}))
    _lifecycle(turn, "queued", initial["uuid"])
    _lifecycle(turn, "started", "foreign")
    assert not turn.steering_state().can_steer
    _lifecycle(turn, "started", initial["uuid"])
    assert turn.steering_state().can_steer


def test_claude_acknowledges_only_pending_generated_queued_command(tmp_path: Path):
    turn = _turn(tmp_path, "claude")
    turn_id = turn.steering_state().turn_id
    turn.render_steer(turn_id, "follow-up", "One")
    for value in (
        {"type": "user", "uuid": "follow-up", "isReplay": True},
        {"type": "command_lifecycle", "state": "queued", "command_uuid": "foreign"},
        {"type": "command_lifecycle", "state": "started", "command_uuid": "follow-up"},
    ):
        assert not turn.receive_line(json.dumps(value)).steer_receipts
    delivered = _lifecycle(turn, "queued", "follow-up")
    assert [(key, receipt.status) for key, receipt in delivered.steer_receipts] == [
        ("follow-up", "delivered")
    ]
    assert not _lifecycle(turn, "queued", "follow-up").steer_receipts
    with pytest.raises(ValueError, match="already sent"):
        turn.render_steer(turn_id, "follow-up", "Again")


@pytest.mark.parametrize("uuid_field", ["user_message_uuids", "user_message_uuid"])
def test_claude_queued_follow_up_continues_until_final_result(tmp_path: Path, uuid_field):
    turn = _turn(tmp_path, "claude")
    turn_id = turn.steering_state().turn_id
    turn.render_steer(turn_id, "follow-up", "One")
    _lifecycle(turn, "queued", "follow-up")
    _lifecycle(turn, "queued", "foreign")
    _lifecycle(turn, "started", "foreign")
    answers = []
    for message_id, answer in ((turn_id, "First"), ("follow-up", "Second")):
        value = [message_id] if uuid_field == "user_message_uuids" else message_id
        step = turn.receive_line(
            json.dumps(
                {
                    "type": "result",
                    "result": answer,
                    uuid_field: value,
                    "queued_turn_count": 0,
                    "uuid": message_id,
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            )
        )
        answers.extend(event.text for event in step.events if event.event == "answer")
        usage = step.events[0].usage
        assert usage is not None
        assert usage.dedupe_key == message_id
        assert usage.generated_tokens == 2
        final = message_id == "follow-up"
        assert step.complete == step.stop_process == step.explicit_terminal == final
        if not final:
            assert turn.steering_state().can_steer
            _lifecycle(turn, "completed", turn_id)
            _lifecycle(turn, "started", "follow-up")
    assert answers == ["First", "Second"]
    assert not turn.steering_state().can_steer
    with pytest.raises(ValueError, match="completed"):
        turn.render_steer(turn_id, "late", "Late")


def test_claude_follow_up_injected_during_a_tool_call_ends_with_the_first_result(
    tmp_path: Path,
) -> None:
    """Claude 2.1.263 trace: a message sent while Bash runs joins the running turn.

    Its lifecycle completes before the result, which attributes both UUIDs. No
    second turn follows, and nothing is lost: the running turn consumed it.
    Lifecycle alone could not decide this; attribution is what settles the stop.
    """
    turn = _turn(tmp_path, "claude")
    turn_id = turn.steering_state().turn_id
    turn.render_steer(turn_id, "follow-up", "Reply STEERED instead of ORIGINAL.")
    delivered = _lifecycle(turn, "queued", "follow-up")
    assert delivered.steer_receipts[0][1].status == "delivered"
    _lifecycle(turn, "started", "follow-up")
    _lifecycle(turn, "completed", "follow-up")
    step = turn.receive_line(
        json.dumps(
            {"type": "result", "result": "STEERED", "user_message_uuids": [turn_id, "follow-up"]}
        )
    )
    assert [event.text for event in step.events if event.event == "answer"] == ["STEERED"]
    assert step.complete and step.stop_process and step.explicit_terminal
    # The initial command's own `completed` line lands after its result and is inert.
    assert not _lifecycle(turn, "completed", turn_id).events
    assert not turn.steering_state().can_steer


def test_claude_failing_result_ends_the_invocation_despite_an_accepted_follow_up(
    tmp_path: Path,
) -> None:
    """The task engine stops at the first error, so a queued turn cannot help."""
    turn = _turn(tmp_path, "claude")
    turn_id = turn.steering_state().turn_id
    turn.render_steer(turn_id, "follow-up", "One")
    _lifecycle(turn, "queued", "follow-up")
    step = turn.receive_line(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": None,
                "user_message_uuids": [turn_id],
            }
        )
    )
    assert [event.event for event in step.events] == ["error"]
    # The subtype is the only diagnostic this result carries.
    assert step.events[0].text == "error_during_execution"
    assert step.complete and step.stop_process and step.explicit_terminal
    assert not turn.steering_state().can_steer


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"user_message_uuids": []},
        {"user_message_uuids": [None, "", " ", 3]},
        {"user_message_uuid": None},
    ],
)
def test_claude_missing_usable_result_uuid_stops_and_refuses_pending(tmp_path: Path, fields):
    turn = _turn(tmp_path, "claude")
    turn.render_steer(turn.steering_state().turn_id, "queued", "One")
    turn.render_steer(turn.steering_state().turn_id, "racing", "Two")
    _lifecycle(turn, "queued", "queued")
    result = turn.receive_line(json.dumps({"type": "result", "result": "Final", **fields}))
    assert result.complete and result.stop_process and result.explicit_terminal
    assert [(key, receipt.status) for key, receipt in result.steer_receipts] == [
        ("racing", "refused")
    ]
    assert not _lifecycle(turn, "queued", "racing").steer_receipts
    assert not turn.receive_line(
        json.dumps({"type": "result", "result": "Unwanted next turn"})
    ).events


def test_claude_without_follow_up_stops_at_first_result(tmp_path: Path):
    turn = _turn(tmp_path, "claude")
    turn_id = turn.steering_state().turn_id
    _lifecycle(turn, "queued", "foreign")
    step = turn.receive_line(
        json.dumps({"type": "result", "result": "Final", "user_message_uuids": [turn_id]})
    )
    assert step.complete and step.stop_process and step.explicit_terminal
    assert not step.steer_receipts


def test_claude_completed_command_no_longer_holds_process_open(tmp_path: Path):
    turn = _turn(tmp_path, "claude")
    turn_id = turn.steering_state().turn_id
    turn.render_steer(turn_id, "follow-up", "One")
    _lifecycle(turn, "queued", "follow-up")
    _lifecycle(turn, "completed", "follow-up")
    step = turn.receive_line(
        json.dumps({"type": "result", "result": "Final", "user_message_uuids": [turn_id]})
    )
    assert step.complete and step.stop_process


@pytest.mark.parametrize(
    ("response", "status"),
    [
        ({"result": {"turnId": "turn"}}, "delivered"),
        ({"error": {"code": -32600, "message": "no active turn to steer"}}, "refused"),
        ({"result": {"turnId": "another-turn"}}, "unknown"),
        ({"result": None}, "unknown"),
    ],
)
def test_codex_steer_response_never_fails_or_retargets_turn(tmp_path: Path, response, status):
    turn = _turn(tmp_path, "codex")
    outgoing = json.loads(turn.render_steer("turn", "message", "Text"))
    step = turn.receive_line(json.dumps({"id": outgoing["id"], **response}))
    assert step.steer_receipts[0][1].status == status
    assert not step.complete and not step.events
    assert turn.steering_state().can_steer
    turn.receive_line(
        json.dumps(
            {"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "completed"}}}
        )
    )
    with pytest.raises(ValueError, match="not running"):
        turn.render_steer("turn", "late", "Late")


@pytest.mark.asyncio
async def test_cancelling_receipt_during_write_drain_releases_waiter_without_resend(tmp_path):
    draining = asyncio.Event()
    writes = []

    async def drain():
        draining.set()
        await asyncio.Future()

    process = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(is_closing=lambda: False, write=writes.append, drain=drain),
    )
    steering = LiveProviderSteering(process, _turn(tmp_path, "codex"), threading.Event())
    task = asyncio.create_task(steering.send("turn", "message", "Steer"))
    await asyncio.wait_for(draining.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not steering._pending
    assert steering.state().can_steer
    assert (await steering.send("turn", "message", "Steer")).status == "refused"
    assert len(writes) == 1


def _provider(tmp_path: Path, provider: str, behavior: str) -> tuple[Path, Path, Path]:
    executable = tmp_path / "provider"
    capture = tmp_path / "input.jsonl"
    unwanted = tmp_path / "unwanted-next-turn"
    executable.write_text(
        f"""#!{sys.executable}
import json, sys, time
from pathlib import Path
capture = Path({str(capture)!r})
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    with capture.open('a') as file:
        file.write(line)
    value = json.loads(line)
    if {provider!r} == 'codex':
        method = value.get('method')
        if method == 'initialize': emit({{'id':value['id'],'result':{{}}}})
        elif method == 'config/read': emit({{'id':value['id'],'result':{{'config':{{}}}}}})
        elif method == 'thread/start': emit({{'id':value['id'],'result':{{'thread':{{'id':'thread'}},'approvalPolicy':'never','sandbox':{{'type':'workspaceWrite'}}}}}})
        elif method == 'turn/start': emit({{'id':value['id'],'result':{{'turn':{{'id':'turn'}}}}}})
        elif method == 'turn/steer':
            if {behavior!r} == 'disconnect': raise SystemExit(3)
            if {behavior!r} == 'late-ack':
                while not capture.with_suffix('.release').exists(): time.sleep(0.01)
            if {behavior!r} == 'completed-before-refusal':
                emit({{'method':'turn/completed','params':{{'threadId':'thread','turn':{{'id':'turn','status':'completed'}}}}}})
            result = {{'result':{{'turnId':'turn'}}}} if {behavior!r} in ('delivered', 'late-ack') else {{'error':{{'message':'no active turn to steer','code':-32600}}}}
            emit({{'id':value['id'],**result}})
            emit({{'method':'item/completed','params':{{'item':{{'type':'agentMessage','text':'Final'}},'threadId':'thread','turnId':'turn'}}}})
            emit({{'method':'turn/completed','params':{{'threadId':'thread','turn':{{'id':'turn','status':'completed'}}}}}})
    else:
        if value['message']['content'] == 'Original prompt':
            emit({{'type':'system','subtype':'init','session_id':'session'}})
            initial_uuid = value['uuid']
            emit({{'type':'command_lifecycle','state':'started','command_uuid':initial_uuid}})
        else:
            if {behavior!r} == 'disconnect': raise SystemExit(3)
            if {behavior!r} == 'delivered':
                emit({{'type':'command_lifecycle','state':'queued','command_uuid':value['uuid']}})
                emit({{'type':'result','result':'First','user_message_uuids':[initial_uuid]}})
                emit({{'type':'command_lifecycle','state':'completed','command_uuid':initial_uuid}})
                emit({{'type':'command_lifecycle','state':'started','command_uuid':value['uuid']}})
                emit({{**value,'isReplay':True}})
                emit({{'type':'result','result':'Final','user_message_uuids':[value['uuid']]}})
            else:
                emit({{'type':'result','result':'Final'}})
            time.sleep(10)
            Path({str(unwanted)!r}).write_text('A subsequent turn must not run')
            emit({{'type':'result','result':'Unwanted next turn'}})
"""
    )
    executable.chmod(0o755)
    return executable, capture, unwanted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,behavior",
    [
        (provider, behavior)
        for provider in ("codex", "claude")
        for behavior in ("delivered", "refused", "disconnect")
    ]
    + [("codex", "completed-before-refusal"), ("codex", "late-ack")],
)
async def test_live_pipe_receipts_completion_and_disconnect_never_resend(
    tmp_path: Path, provider: str, behavior: str
):
    executable, capture, unwanted = _provider(tmp_path, provider, behavior)
    launcher = AgentLauncher()
    launcher.readiness = lambda *args, **kwargs: type(
        "Ready",
        (),
        {
            "installed": True,
            "authenticated": True,
            "binary_path": str(executable),
            "version": "0.153.2" if provider == "codex" else "2.1.260",
        },
    )()
    control = AgentProcessControl()
    events = []

    async def collect():
        async for event in launcher.stream(
            provider,
            "Original prompt",
            cwd=tmp_path,
            capability="discuss",
            control=control,
            runtime_id="codex.app-server-stdio.v1"
            if provider == "codex"
            else "claude.stream-json.v1",
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    try:
        await async_wait_until(lambda: control.steering_state().can_steer)
        state = control.steering_state()
        rejected = await asyncio.wrap_future(control.steer("stale", str(uuid.uuid4()), "Stale"))
        assert rejected.status == "refused"
        message_id = str(uuid.uuid4())
        future = control.steer(state.turn_id, message_id, "Steer")
        if behavior == "late-ack":
            await async_wait_until(lambda: message_id in capture.read_text())
            # The HTTP deadline cancels this waiter, not the still-live process.
            assert not future.done() and not task.done()
            future.cancel()
            duplicate = await asyncio.wrap_future(control.steer(state.turn_id, message_id, "Steer"))
            assert duplicate.status == "refused"
            assert control.steering_state().can_steer
            capture.with_suffix(".release").touch()
        else:
            receipt = await asyncio.wait_for(asyncio.wrap_future(future), 10)
            expected = {"disconnect": "unknown", "completed-before-refusal": "refused"}.get(
                behavior, behavior
            )
            assert receipt.status == expected
        await asyncio.wait_for(task, 10)
        if behavior == "late-ack":
            assert future.cancelled()
        assert not control.steering_state().can_steer
        late = await asyncio.wrap_future(control.steer(state.turn_id, str(uuid.uuid4()), "Late"))
        assert late.status == "refused"
        lines = [json.loads(line) for line in capture.read_text().splitlines()]
        steers = [
            value
            for value in lines
            if value.get("method") == "turn/steer" or value.get("uuid") == message_id
        ]
        assert len(steers) == 1
        assert "Stale" not in capture.read_text() and "Late" not in capture.read_text()
        assert not unwanted.exists()
        answers = [event.text for event in events if event.event == "answer"]
        expected_answers = [] if behavior == "disconnect" else ["Final"]
        if provider == "claude" and behavior == "delivered":
            expected_answers = ["First", "Final"]
        assert answers == expected_answers
        assert events[-1].event == ("error" if behavior == "disconnect" else "done")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_confirmed", [True, False])
async def test_remote_result_requires_observed_process_stop_without_contacting_ssh(
    tmp_path: Path, monkeypatch, stop_confirmed: bool
):
    import rcp.agents.launcher as launcher_module

    executable, _capture, unwanted = _provider(tmp_path, "claude", "delivered")
    launcher = AgentLauncher()
    launcher.readiness = lambda *args, **kwargs: type(
        "Ready",
        (),
        {
            "installed": True,
            "authenticated": True,
            "binary_path": str(executable),
            "version": "2.1.260",
        },
    )()
    # Exercise the remote dispatch path with a local fixture pipe. No ssh binary
    # or host is contacted; the termination receipt is supplied by the fixture.
    monkeypatch.setattr(launcher, "_remote_login_command", lambda command, **kwargs: command)
    monkeypatch.setattr(launcher_module, "ssh_arguments", lambda host, command: command)
    stops = []

    def stop(host, pid_file):
        stops.append((host, pid_file))
        return stop_confirmed

    monkeypatch.setattr(AgentProcessControl, "_terminate_remote", staticmethod(stop))
    monkeypatch.setattr(
        AgentProcessControl, "remote_stopped", staticmethod(lambda *args: stop_confirmed)
    )
    control = AgentProcessControl()

    async def collect():
        return [
            event
            async for event in launcher.stream(
                "claude",
                "Original prompt",
                cwd=tmp_path,
                capability="discuss",
                control=control,
                host="fixture-only",
                remote_pid_file="fixture.pid",
            )
        ]

    task = asyncio.create_task(collect())
    try:
        await async_wait_until(lambda: control.steering_state().can_steer)
        receipt = await asyncio.wrap_future(
            control.steer(control.steering_state().turn_id, str(uuid.uuid4()), "Steer")
        )
        assert receipt.status == "delivered"
        events = await asyncio.wait_for(task, 10)
        assert stops and all(value == ("fixture-only", "fixture.pid") for value in stops)
        if stop_confirmed:
            assert len(stops) == 1
        assert not unwanted.exists()
        assert events[-1].event == ("done" if stop_confirmed else "error")
        if not stop_confirmed:
            assert "could not confirm" in events[-1].text
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
