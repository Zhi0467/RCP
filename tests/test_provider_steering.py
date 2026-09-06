from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher, AgentProcessControl
from rcp.providers import ProviderTurnRequest, profile_for
from tests.helpers import async_wait_until


def _turn(tmp_path: Path, provider: str):
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
    else:
        initial = json.loads(turn.initial_input())
        turn.receive_line(json.dumps({**initial, "isReplay": True}))
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


def test_claude_only_exact_replay_before_first_result_acknowledges(tmp_path: Path):
    turn = _turn(tmp_path, "claude")
    turn_id = turn.steering_state().turn_id
    turn.render_steer(turn_id, "echoed", "One")
    turn.render_steer(turn_id, "racing", "Two")
    for value in (
        {"type": "user", "uuid": "echoed"},
        {"type": "user", "uuid": "foreign", "isReplay": True},
    ):
        assert not turn.receive_line(json.dumps(value)).steer_receipts
    delivered = turn.receive_line(json.dumps({"type": "user", "uuid": "echoed", "isReplay": True}))
    assert delivered.steer_receipts[0][1].status == "delivered"
    result = turn.receive_line(json.dumps({"type": "result", "result": "Final"}))
    assert result.complete and result.stop_process and result.explicit_terminal
    assert [(key, receipt.status) for key, receipt in result.steer_receipts] == [
        ("racing", "refused")
    ]
    assert "completed before delivery" in result.steer_receipts[0][1].reason
    assert not turn.receive_line(
        json.dumps({"type": "user", "uuid": "racing", "isReplay": True})
    ).steer_receipts
    assert not turn.receive_line(
        json.dumps({"type": "result", "result": "Unwanted next turn"})
    ).events
    with pytest.raises(ValueError, match="completed"):
        turn.render_steer(turn_id, "late", "Late")


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
            result = {{'result':{{'turnId':'turn'}}}} if {behavior!r} == 'delivered' else {{'error':{{'message':'no active turn to steer','code':-32600}}}}
            emit({{'id':value['id'],**result}})
            emit({{'method':'item/completed','params':{{'item':{{'type':'agentMessage','text':'Final'}},'threadId':'thread','turnId':'turn'}}}})
            emit({{'method':'turn/completed','params':{{'threadId':'thread','turn':{{'id':'turn','status':'completed'}}}}}})
    else:
        if value['message']['content'] == 'Original prompt':
            emit({{'type':'system','subtype':'init','session_id':'session'}})
            emit({{**value,'isReplay':True}})
        else:
            if {behavior!r} == 'disconnect': raise SystemExit(3)
            if {behavior!r} == 'delivered': emit({{**value,'isReplay':True}})
            emit({{'type':'result','result':'Final'}})
            time.sleep(10)
            Path({str(unwanted)!r}).write_text('A subsequent turn must not run')
            emit({{'type':'result','result':'Unwanted next turn'}})
"""
    )
    executable.chmod(0o755)
    return executable, capture, unwanted


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("behavior", ["delivered", "refused", "disconnect"])
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
        receipt = await asyncio.wait_for(
            asyncio.wrap_future(control.steer(state.turn_id, message_id, "Steer")), 10
        )
        assert receipt.status == ("unknown" if behavior == "disconnect" else behavior)
        await asyncio.wait_for(task, 10)
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
        assert answers == ([] if behavior == "disconnect" else ["Final"])
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
        assert stops == [("fixture-only", "fixture.pid")]
        assert not unwanted.exists()
        assert events[-1].event == ("done" if stop_confirmed else "error")
        if not stop_confirmed:
            assert "could not confirm" in events[-1].text
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
