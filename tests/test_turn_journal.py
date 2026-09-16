from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from rcp.agents.staged_turn_journal import Lines, WireCompletion
from rcp.agents.turn_journal import STAGED_TURN_JOURNAL_NAME, staged_turn_journal_source


def _start(
    tmp_path,
    code,
    *,
    runtime="codex.exec-json.v1",
    journal_limit=1024 * 1024,
    provider_command=None,
):
    script = tmp_path / STAGED_TURN_JOURNAL_NAME
    script.write_text(staged_turn_journal_source())
    patch = tmp_path / "patch.json"
    patch.write_text('{"operations": []}')
    command = [
        sys.executable,
        str(script),
        "--pid-file",
        str(tmp_path / "provider.pid"),
        "--provider",
        "claude" if runtime.startswith("claude") else "codex",
        "--runtime-id",
        runtime,
        "--patch-path",
        str(patch),
        "--max-journal-bytes",
        str(journal_limit),
        "--max-event-bytes",
        "1048576",
        "--max-stderr-bytes",
        "4096",
        "--max-patch-bytes",
        "1048576",
        "--max-uplink-bytes",
        "16384",
        "--max-control-messages",
        "1024",
        "--stop-hold-seconds",
        "0",
        "--stop-grace-seconds",
        "1",
        "--poll-seconds",
        "0.01",
    ]
    if runtime == "codex.exec-json.v1":
        command.append("--close-input-after-initial")
    command.extend(["--", *(provider_command or [sys.executable, "-u", "-c", code])])
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _finish(process, expected=0):
    try:
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    stderr = process.stderr.read()
    assert process.returncode == expected, stderr.decode()


@pytest.mark.parametrize("broken_reader", [False, True])
@pytest.mark.parametrize(
    "runtime,terminal",
    [
        ("codex.exec-json.v1", {"type": "turn.completed"}),
        (
            "claude.stream-json.v1",
            {"type": "result", "subtype": "success", "result": "final answer"},
        ),
    ],
)
def test_completed_remote_turn_survives_unread_or_broken_uplink(
    tmp_path, broken_reader, runtime, terminal
):
    code = f"""
import json, sys, time
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "final answer"}}}}), flush=True)
for n in range(500):
    print(json.dumps({{"type": "trace", "text": "x" * 1000}}), flush=True)
print({json.dumps(terminal)!r}, flush=True)
time.sleep(30)
"""
    process = _start(tmp_path, code, runtime=runtime)
    process.stdin.write(b'{"mailbox_id":"a","token":"NEVER-PERSIST-THIS"}\n')
    process.stdin.write(b'{"type":"user","uuid":"initial","message":{}}\n')
    process.stdin.close()  # Lost transport, not an authorized stop.
    if broken_reader:
        process.stdout.close()
    # The turn finished, but its completion never reached the controller. The
    # exit status is the only channel left to say so, and 255 is what RCP reads
    # as a lost link. Exiting 0 here would have RCP call this a protocol failure,
    # withhold collection, and offer a Retry that repeats work already done.
    _finish(process, expected=255)
    directory = tmp_path / "provider.pid.turn"
    outcome = json.loads((directory / "outcome.json").read_text())
    events = (directory / "events.jsonl").read_bytes()
    assert outcome["protocol_complete"]
    assert outcome["journal_complete"]
    assert outcome["uplink_detached"]
    assert outcome["events_sha256"] == hashlib.sha256(events).hexdigest()
    assert b"final answer" in events
    assert b"NEVER-PERSIST-THIS" not in b"".join(p.read_bytes() for p in directory.iterdir())
    (tmp_path / "patch.json").write_text("next turn")
    assert (directory / "patch.json").read_text() == '{"operations": []}'


def test_undelivered_terminal_failure_is_also_reported_as_a_lost_link(tmp_path):
    """A failure nobody received is as undelivered as an answer nobody received.

    The provider said why it stopped and the journal holds that, but the drop
    swallowed it. Exiting on the child's own status would have RCP report a
    failure it cannot explain and offer a Retry that repeats the attempt;
    reporting the lost link instead lets collection surface the real reason.
    """

    code = """
import json, sys, time
for n in range(500):
    print(json.dumps({"type": "trace", "text": "x" * 1000}), flush=True)
print(json.dumps({"type": "turn.failed", "error": "the model refused the request"}), flush=True)
time.sleep(30)
"""
    process = _start(tmp_path, code)
    process.stdin.close()
    _finish(process, expected=255)
    outcome = json.loads((tmp_path / "provider.pid.turn/outcome.json").read_text())
    assert outcome["terminal_event"] and not outcome["protocol_complete"]
    assert outcome["uplink_detached"]
    assert (
        "the model refused the request" in (tmp_path / "provider.pid.turn/events.jsonl").read_text()
    )


def test_journal_overflow_stops_run_with_explicit_incomplete_receipt(tmp_path):
    process = _start(
        tmp_path, 'import time; print("x" * 100000, flush=True); time.sleep(30)', journal_limit=4096
    )
    process.stdin.close()
    process.stdin = None
    process.communicate(timeout=10)
    outcome = json.loads((tmp_path / "provider.pid.turn/outcome.json").read_text())
    assert not outcome["journal_complete"]
    assert not outcome["protocol_complete"]
    assert "storage limit" in outcome["error"]
    assert not outcome["patch_present"]
    assert (tmp_path / "provider.pid.turn/events.jsonl").stat().st_size <= 4096


def test_a_journal_that_overflows_says_so_where_a_connected_run_can_hear_it(tmp_path):
    """The one reader of `outcome.json` is collection, and this turn is not collectible.

    Overflow ends the turn with the link still up, so nothing ever opens that
    file: the launcher is left with an exit status and reports whatever generic
    reason it can build from it. The failure has to travel the channel a live
    run already surfaces, or the documented visible failure is invisible in the
    ordinary connected case.
    """

    # Enough to pass the journal ceiling and stay inside the uplink one, so the
    # reason is judged on its own and not on a buffer that overflowed too.
    process = _start(tmp_path, 'print("x" * 8000, flush=True)', journal_limit=4096)
    process.stdin.close()
    process.stdin = None
    _stdout, stderr = process.communicate(timeout=10)

    assert "Provider journal exceeded its storage limit." in stderr.decode()
    # Nonzero so the turn fails, and not the code that means a link ended it.
    assert process.returncode not in {0, 255}
    outcome = json.loads((tmp_path / "provider.pid.turn/outcome.json").read_text())
    assert not outcome["journal_complete"] and not outcome["uplink_detached"]


def test_live_completion_fences_claude_stdin_before_forwarding_result(tmp_path):
    process = _start(
        tmp_path,
        """
import json, sys, time
initial = json.loads(sys.stdin.readline())
print(json.dumps({"type":"result", "subtype":"success", "result":"answer"}), flush=True)
if sys.stdin.readline():
    open("second-turn-ran", "w").write("bad")
time.sleep(30)
""",
        runtime="claude.stream-json.v1",
    )
    process.stdin.write(b'{"type":"user","uuid":"initial","message":{}}\n')
    process.stdin.flush()
    assert json.loads(process.stdout.readline())["type"] == "result"
    try:
        process.stdin.write(b'{"type":"user","uuid":"late","message":{}}\n')
        process.stdin.flush()
    except BrokenPipeError:
        pass
    _finish(process)
    assert not Path("second-turn-ran").exists()
    outcome = json.loads((tmp_path / "provider.pid.turn/outcome.json").read_text())
    assert outcome["input_message_ids"] == ["initial"]


def test_app_server_fence_ignores_subagent_completion_and_preserves_root_receipt(tmp_path):
    code = """
import json, sys, time
for n in range(2):
    request = json.loads(sys.stdin.readline())
    result = {"thread":{"id":"root"}} if request["method"] == "thread/start" else {"turn":{"id":"turn"}}
    print(json.dumps({"id":request["id"], "result":result}), flush=True)
print(json.dumps({"method":"turn/completed", "params":{"threadId":"child", "turn":{"id":"child-turn", "status":"completed"}}}), flush=True)
time.sleep(.1)
print(json.dumps({"method":"item/completed", "params":{"threadId":"root", "turnId":"turn", "item":{"type":"agentMessage", "text":"root final"}}}), flush=True)
print(json.dumps({"method":"turn/completed", "params":{"threadId":"root", "turn":{"id":"turn", "status":"completed"}}}), flush=True)
time.sleep(30)
"""
    process = _start(tmp_path, code, runtime="codex.app-server-stdio.v1")
    process.stdin.write(b'{"id":3,"method":"thread/start"}\n{"id":4,"method":"turn/start"}\n')
    process.stdin.close()
    _finish(process)
    directory = tmp_path / "provider.pid.turn"
    outcome = json.loads((directory / "outcome.json").read_text())
    assert outcome["protocol_complete"]
    assert outcome["root_thread_id"] == "root"
    assert outcome["root_turn_id"] == "turn"
    assert "root final" in (directory / "events.jsonl").read_text()


def test_claude_completion_waits_for_accepted_queued_followup():
    observer = WireCompletion("claude.stream-json.v1")
    for identifier in ("initial", "followup"):
        observer.input({"type": "user", "uuid": identifier})
        observer.output(
            {"type": "command_lifecycle", "command_uuid": identifier, "state": "queued"}
        )
    observer.output({"type": "result", "subtype": "success", "user_message_uuid": "initial"})
    assert not observer.terminal
    observer.output({"type": "result", "subtype": "success", "user_message_uuid": "followup"})
    assert observer.complete


def test_journal_and_live_pipe_end_at_terminal_line(tmp_path):
    code = """
import json, sys, time
lines = [
    {"type":"item.completed", "item":{"type":"agent_message", "text":"real answer"}},
    {"type":"turn.completed"},
    {"type":"item.completed", "item":{"type":"agent_message", "text":"after terminal junk"}},
]
sys.stdout.write("\\n".join(json.dumps(line) for line in lines) + "\\n")
sys.stdout.flush()
time.sleep(30)
"""
    process = _start(tmp_path, code)
    process.stdin.close()
    _finish(process)
    events = (tmp_path / "provider.pid.turn/events.jsonl").read_text()
    live = process.stdout.read().decode()
    assert "real answer" in events and "real answer" in live
    assert "after terminal junk" not in events and "after terminal junk" not in live
    assert json.loads(events.splitlines()[-1])["type"] == "turn.completed"


def test_app_server_disconnect_before_turn_delivery_closes_handshake(tmp_path):
    process = _start(tmp_path, "import sys; sys.stdin.read()", runtime="codex.app-server-stdio.v1")
    process.stdin.write(b'{"id":1,"method":"initialize"}\n')
    process.stdin.close()
    process.wait(timeout=10)
    outcome = json.loads((tmp_path / "provider.pid.turn/outcome.json").read_text())
    assert not outcome["protocol_complete"]
    assert not outcome["patch_present"]


@pytest.mark.parametrize("subtype", ["success", "", "other", "error_during_execution"])
def test_claude_terminal_success_matches_canonical_decoder(subtype):
    from rcp.providers import profile_for

    value = {"type": "result", "subtype": subtype, "result": "labelled answer"}
    observer = WireCompletion("claude.stream-json.v1")
    observer.output(value)
    decoded = profile_for("claude").decode_event(value, json.dumps(value))
    assert observer.terminal
    assert observer.complete is (decoded.event != "error")


def _canonical_app_server_turn(tmp_path, *, handshake: bool):
    from rcp.agents.codex_app_server import CodexAppServerRuntime
    from rcp.providers import ProviderTurnRequest

    turn = CodexAppServerRuntime().turn(
        ProviderTurnRequest(
            prompt="fence",
            binary="codex",
            cwd=tmp_path,
            model=None,
            reasoning=None,
            session_id="fence-thread",
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            capability="paper_readonly",
            provider_version="0.153.4",
        )
    )
    observer = WireCompletion("codex.app-server-stdio.v1")
    if handshake:
        for message in (
            {"id": 1, "result": {}},
            {"id": 2, "result": {"config": {}}},
            {
                "id": 3,
                "result": {
                    "thread": {"id": "fence-thread"},
                    "approvalPolicy": "never",
                    "sandbox": {"type": "readOnly"},
                },
            },
        ):
            turn.receive_line(json.dumps(message))
        observer.input({"id": 3, "method": "thread/resume"})
        observer.output({"id": 3, "result": {"thread": {"id": "fence-thread"}}})
    return turn, observer


@pytest.mark.parametrize("handshake", [False, True])
@pytest.mark.parametrize(
    "request_message",
    [
        {"id": 7, "method": "applyPatchApproval"},
        {"id": 7, "method": "applyPatchApproval", "params": {}},
        {"id": 7, "method": "execCommandApproval", "params": {"threadId": "fence-thread"}},
    ],
)
def test_app_server_request_fence_matches_canonical_decoder(tmp_path, handshake, request_message):
    """An unattended turn cannot answer a request, so both decoders must stop it.

    The wrapper is the only fence once the uplink is gone, so a request it lets
    through leaves stdin open on exactly the link it exists to survive.
    """

    turn, observer = _canonical_app_server_turn(tmp_path, handshake=handshake)
    canonical = turn.receive_line(json.dumps(request_message))
    observer.output(request_message)
    assert canonical.complete and canonical.explicit_terminal
    assert observer.terminal


def test_a_foreign_thread_request_before_the_resume_reply_is_not_a_fence(tmp_path):
    """A resumed server can speak for another thread before it answers this one.

    The canonical decoder knows which thread it asked to resume, so it replies
    to that request and reads on. The wrapper only learned the thread from the
    reply, so before one arrived every foreign request tripped its fence --
    killing a resumed turn that was doing nothing wrong and recording it as
    incomplete for collection to settle.
    """

    turn, observer = _canonical_app_server_turn(tmp_path, handshake=False)
    observer.input({"id": 3, "method": "thread/resume", "params": {"threadId": "fence-thread"}})

    foreign = {"id": 9, "method": "execCommandApproval", "params": {"threadId": "other-thread"}}
    canonical = turn.receive_line(json.dumps(foreign))
    observer.output(foreign)
    assert not canonical.complete
    assert not observer.terminal

    # The fence itself is unchanged for a request this turn's own thread sends.
    own = {"id": 10, "method": "execCommandApproval", "params": {"threadId": "fence-thread"}}
    assert turn.receive_line(json.dumps(own)).complete
    observer.output(own)
    assert observer.terminal


@pytest.mark.parametrize("link_directory", [False, True])
def test_patch_snapshot_refuses_symlink_boundary(tmp_path, link_directory):
    from rcp.agents.staged_turn_journal import _patch_snapshot

    original = tmp_path / "original"
    original.mkdir()
    (original / "patch.json").write_text("not the turn's patch")
    workspace = tmp_path / "workspace"
    if link_directory:
        workspace.symlink_to(original, target_is_directory=True)
    else:
        workspace.mkdir()
        (workspace / "patch.json").symlink_to(original / "patch.json")
    destination = tmp_path / "snapshot.json"
    with pytest.raises(OSError):
        _patch_snapshot(workspace / "patch.json", destination, 1024)
    assert not destination.exists()


def test_journal_wraps_real_broker_without_persisting_bootstrap(tmp_path):
    import secrets
    import uuid

    from rcp.agents.command_protocol import staged_command_broker_source
    from rcp.agents.invocation_broker import ProviderInvocationGate

    broker_path = tmp_path / "broker.py"
    broker_path.write_text(staged_command_broker_source())
    mailbox_id = uuid.uuid4().hex
    socket_path = Path(f"/tmp/rcp-command-{mailbox_id}.sock")
    token = secrets.token_hex(32)
    gate = ProviderInvocationGate(
        mailbox_id=mailbox_id,
        broker_path=str(broker_path),
        socket_path=str(socket_path),
        workspace=str(tmp_path),
        response_timeout_seconds=1,
        _token=token,
    )
    code = """
import json,sys,time
prompt=sys.stdin.read()
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":prompt}}),flush=True)
print(json.dumps({"type":"turn.completed"}),flush=True)
time.sleep(30)
"""
    process = _start(
        tmp_path, code, provider_command=gate.wrap_command([sys.executable, "-u", "-c", code])
    )
    try:
        process.stdin.write(gate.bootstrap(b"exact provider prompt"))
        process.stdin.close()
        _finish(process)
        events = (tmp_path / "provider.pid.turn/events.jsonl").read_text()
        assert gate.ready_line in events
        assert "exact provider prompt" in events
        assert token not in events
        assert not socket_path.exists()
    finally:
        socket_path.unlink(missing_ok=True)


def _canonical_turn(tmp_path, runtime_id: str):
    from rcp.providers import ProviderTurnRequest, profile_for

    provider = "claude" if runtime_id.startswith("claude") else "codex"
    return (
        profile_for(provider)
        .runtime(runtime_id)
        .turn(
            ProviderTurnRequest(
                prompt="corpus",
                binary=provider,
                cwd=tmp_path,
                model=None,
                reasoning=None,
                session_id="corpus-thread",
                read_dirs=[],
                write_dirs=[],
                write_scope=None,
                capability="paper_readonly",
                provider_version="0.153.4",
            )
        )
    )


def _started_decoder_pair(tmp_path, runtime_id: str):
    """Both decoders for one runtime, holding the same turn and ready for its traffic."""

    if runtime_id == "codex.app-server-stdio.v1":
        turn, observer = _canonical_app_server_turn(tmp_path, handshake=True)
        started = {"id": 4, "result": {"turn": {"id": "corpus-turn"}}}
        turn.receive_line(json.dumps(started))
        observer.input({"id": 4, "method": "turn/start"})
        observer.output(started)
        return turn, observer
    turn = _canonical_turn(tmp_path, runtime_id)
    observer = WireCompletion(runtime_id)
    start = (
        {"type": "system", "subtype": "init", "session_id": "corpus-thread"}
        if runtime_id.startswith("claude")
        else {"type": "thread.started", "thread_id": "corpus-thread"}
    )
    turn.receive_line(json.dumps(start))
    observer.output(start)
    return turn, observer


# One protocol message per case: every shape each runtime can end a turn with,
# and the ordinary traffic it must not end one with.
_COMPLETION_CORPUS = tuple(
    (runtime_id, message)
    for runtime_id, messages in (
        (
            "codex.exec-json.v1",
            (
                {"type": "thread.started", "thread_id": "corpus-thread"},
                {"type": "turn.started"},
                {"type": "item.started", "item": {"type": "agent_message"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
                {"type": "item.completed", "item": {"type": "command_execution", "text": "ls"}},
                {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
                {"type": "turn.failed", "error": {"message": "the model refused"}},
                {"type": "error", "message": "codex crashed"},
                {"type": "unrecognized.event"},
                {"item": {"type": "agent_message", "text": "no type at all"}},
            ),
        ),
        (
            "claude.stream-json.v1",
            (
                {"type": "system", "subtype": "init", "session_id": "corpus-thread"},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
                {"type": "result", "subtype": "success", "result": "answer"},
                {"type": "result", "subtype": "error_during_execution", "is_error": True},
                {"type": "result", "subtype": ""},
                {"type": "error", "message": "claude crashed"},
                {"type": "unrecognized.event"},
                {"message": {"content": []}},
            ),
        ),
        (
            "codex.app-server-stdio.v1",
            (
                {"id": 7, "method": "applyPatchApproval"},
                {"id": 7, "method": "applyPatchApproval", "params": {}},
                {"id": 7, "method": "execCommandApproval", "params": {"threadId": "corpus-thread"}},
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "corpus-turn", "status": "completed"}},
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "corpus-turn", "status": "failed"}},
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "child",
                        "turn": {"id": "child-turn", "status": "completed"},
                    },
                },
                {"method": "error", "params": {"willRetry": False, "message": "boom"}},
                {"method": "error", "params": {"willRetry": True, "message": "retrying"}},
                {"method": "item/completed", "params": {"threadId": "corpus-thread", "item": {}}},
                {"method": "item/completed", "params": {"threadId": "child", "item": {}}},
                {"method": "turn/started", "params": {"threadId": "corpus-thread"}},
            ),
        ),
    )
    for message in messages
)


@pytest.mark.parametrize(
    "runtime_id,message", _COMPLETION_CORPUS, ids=lambda value: str(value)[:44]
)
def test_completion_decoders_agree_across_the_protocol_corpus(tmp_path, runtime_id, message):
    """The wrapper's stdlib copy must end a turn exactly where the real decoder does.

    RCP cannot ship its decoder to the execution host, so the rule for "this turn
    is over" is written twice and nothing structural keeps the two together. The
    drift that got through was an ordering difference in one branch, invisible
    until a message carrying no params arrived on a dead link. So compare them on
    the traffic itself, rather than only on the shapes someone thought to worry
    about. Traffic arriving before the handshake is its own test: there the
    canonical decoder is also validating protocol order, which the wrapper is
    deliberately not.
    """

    turn, observer = _started_decoder_pair(tmp_path, runtime_id)
    step = turn.receive_line(json.dumps(message))
    observer.output(message)

    assert step.explicit_terminal == observer.terminal


def test_the_protocol_corpus_covers_both_verdicts(tmp_path):
    """A corpus both decoders agreed to ignore entirely would prove nothing."""

    verdicts: dict[str, set[bool]] = {runtime: set() for runtime, _ in _COMPLETION_CORPUS}
    for runtime_id, message in _COMPLETION_CORPUS:
        _turn, observer = _started_decoder_pair(tmp_path, runtime_id)
        observer.output(message)
        verdicts[runtime_id].add(observer.terminal)
    assert all(seen == {False, True} for seen in verdicts.values()), verdicts


def test_the_wrapper_waits_for_steering_the_live_pipe_tracks_elsewhere(tmp_path):
    """One of two places the decoders part on purpose, recorded so it stays on purpose.

    A Claude result naming some finished inputs while others are still out ends
    the turn for the canonical decoder, whose live pipe owns steering separately.
    The wrapper is the only fence on a dead link, so it holds until every input
    it forwarded has come back.
    """

    turn = _canonical_turn(tmp_path, "claude.stream-json.v1")
    observer = WireCompletion("claude.stream-json.v1")
    for identifier in ("first", "second"):
        observer.input({"type": "user", "uuid": identifier, "message": {}})
        observer.output(
            {"type": "command_lifecycle", "command_uuid": identifier, "state": "started"}
        )
    partial = {"type": "result", "subtype": "success", "user_message_uuids": ["first"]}

    assert turn.receive_line(json.dumps(partial)).explicit_terminal
    observer.output(partial)
    assert not observer.terminal

    observer.output({"type": "result", "subtype": "success", "user_message_uuids": ["second"]})
    assert observer.terminal


def test_the_wrapper_ignores_a_completion_naming_no_thread_and_a_foreign_turn(tmp_path):
    """The other deliberate parting, and the safer side of it to be on.

    A `turn/completed` that names neither this thread nor this turn is a protocol
    violation, and the canonical decoder ends the turn on it. The wrapper holds:
    its fence is keyed to the turn it was launched for, which is what keeps a
    subagent's completion from closing the root turn out from under it.
    """

    foreign = {
        "method": "turn/completed",
        "params": {"turn": {"id": "someone-elses-turn", "status": "completed"}},
    }
    turn, observer = _started_decoder_pair(tmp_path, "codex.app-server-stdio.v1")

    assert turn.receive_line(json.dumps(foreign)).explicit_terminal
    observer.output(foreign)
    assert not observer.terminal


def test_the_wrapper_reads_a_bad_byte_the_way_the_live_pipe_reads_it():
    """One invalid byte must not make the two readers disagree about completion.

    The live pipe decodes each line with `errors="replace"`, so a terminal event
    carrying a stray byte in its text still parses and still ends the turn.
    Reading the same bytes strictly here would record the turn as protocol
    incomplete, and collection would then discard a result live delivery accepted.
    """

    observed = []
    lines = Lines(1 << 20, observed.append)
    observer = WireCompletion("codex.exec-json.v1")
    completion = json.dumps({"type": "turn.completed", "note": "ok"}).encode()

    lines.feed(completion.replace(b"ok", b"o\xffk") + b"\n")

    assert observed and observed[0]["type"] == "turn.completed"
    assert observed[0]["note"] == "o�k"
    observer.output(observed[0])
    assert observer.terminal and observer.complete


def test_a_completion_that_outruns_the_turn_start_reply_still_ends_the_turn(tmp_path):
    """A fast server can finish before RCP has read which turn it started.

    The canonical decoder accepts that completion: with no turn id of its own
    yet, it has nothing to disagree with. The wrapper demanded one, so the turn
    stayed unfenced with stdin open and settled as protocol incomplete -- and
    collection, arriving later, would find nothing finished to adopt.
    """

    turn, observer = _canonical_app_server_turn(tmp_path, handshake=True)
    observer.input({"id": 4, "method": "turn/start"})
    completed = {
        "method": "turn/completed",
        "params": {"turn": {"id": "unread-turn", "status": "completed"}},
    }

    assert turn.receive_line(json.dumps(completed)).complete
    observer.output(completed)

    assert observer.terminal and observer.complete


def test_a_final_event_without_a_newline_is_still_observed(tmp_path):
    """The live reader yields that last partial line at EOF; so must the wrapper.

    A provider that writes its terminal event and closes stdout without a
    trailing newline would otherwise end a finished turn recorded incomplete,
    leaving collection with nothing to adopt for a turn that in fact completed.
    """

    code = """
import json, sys
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}), flush=True)
sys.stdout.write(json.dumps({"type": "turn.completed"}))
sys.stdout.flush()
"""
    process = _start(tmp_path, code)
    process.stdin.write(b'{"type":"user","uuid":"initial","message":{}}\n')
    process.stdin.close()
    # Delivered and complete: the controller flushes the same partial line.
    _finish(process, expected=0)

    outcome = json.loads((tmp_path / "provider.pid.turn" / "outcome.json").read_text())
    assert outcome["protocol_complete"]
